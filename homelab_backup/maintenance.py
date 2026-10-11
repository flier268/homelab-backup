import copy
import datetime as dt
import hashlib
import json
import os
import sys

from .backup import backup_one, retention_cmd
from .alerting import (
    alert_state_transaction, load_alert_state, notified_within, notify,
    parse_timestamp,
)
from .backup_state import (
    due_status, load_state, save_state, success_staleness,
)
from .btrfs_snapshot import cleanup_snapshot_state, snapshot_state_services
from .common import (
    RUN_DUE_LOCK_RETRIES, CommandError, FailureSummary, GlobalLock,
    _print_command_failure, clear_stale_restic_locks, die, global_lock_holder,
    restic_env, run,
)
from .manifest import (
    manifest, manifests, service_label, valid_service_name, validate_manifest,
)
from .schedule import local_now, parse_duration


def _manifest_error_recorder(failures):
    def record(path, err):
        failures.append(str(path), f'manifest loading failed: {err}')
        print(f'ERROR: manifest loading failed for {path}: {err}', file=sys.stderr)

    return record


def _service_name(m):
    service = m.get('service')
    if not valid_service_name(service):
        raise ValueError(f'invalid manifest service name: {service!r}')
    return service


def cmd_list(c, args):
    now = local_now()
    failures = FailureSummary()
    for m in manifests(c, on_error=_manifest_error_recorder(failures)):
        service = m.get('service', '<unnamed>')
        try:
            service = _service_name(m)
            due, reason, _ = due_status(c, m, now)
            schedule = m['schedule']
            schedule_text = f"cron {schedule['cron']}"
            print(
                f"{service_label(m)}: {schedule_text}; "
                f"{'DUE' if due else reason}; {m['_path']}"
            )
        except Exception as err:
            failures.record_exception(
                service, err,
                message=f'ERROR: list failed for {service}: {{error}}',
                summary_error=f'listing failed: {err}',
            )
            if os.environ.get('BACKUPCTL_DEBUG') == '1':
                raise
    failures.raise_if_any('LIST FAILURES')


STALE_EXIT_CODE = 2
DEFAULT_MAX_LOCK_AGE = '13h'


def _stalled_lock_alert(c, now):
    """Report the global lock holder.

    Returns ``(alert line, holder)`` when the lock is stalled, otherwise
    ``(None, None)``.
    """
    lock_file = c.get('lock_file')
    if not lock_file:
        return None, None
    try:
        holder = global_lock_holder(lock_file)
    except OSError as err:
        print(
            f'WARNING: could not inspect global lock {lock_file}: {err}',
            file=sys.stderr,
        )
        return None, None
    if holder is None:
        return None, None
    try:
        started = dt.datetime.fromisoformat(holder['started'])
        if started.tzinfo is None:
            raise ValueError('timestamp has no timezone')
    except (TypeError, ValueError):
        print(
            f"WARNING: global lock held by pid={holder['pid']} with unknown "
            f"start time ({holder['started']!r}); cannot tell if it is stalled",
            file=sys.stderr,
        )
        return None, None
    held_seconds = int((now - started).total_seconds())
    print(json.dumps({
        'lock_holder': {
            'pid': holder['pid'], 'started': holder['started'],
            'held_seconds': held_seconds,
        },
    }), file=sys.stderr)
    max_age = (c.get('alert') or {}).get('max_lock_age', DEFAULT_MAX_LOCK_AGE)
    max_age_seconds = parse_duration(max_age, 'alert.max_lock_age')
    if held_seconds <= max_age_seconds:
        return None, None
    return (
        f"global lock held by pid={holder['pid']} since {holder['started']} "
        f"({held_seconds}s > {max_age_seconds}s); backup or check may be stalled"
    ), holder


def _digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _status_signature(stale, lock_holder, status_errors):
    """Identify a status problem; any change in content re-notifies."""
    return json.dumps({
        'stale': sorted(service for service, _, _ in stale),
        'lock_holder': (
            {'pid': lock_holder.get('pid'), 'started': lock_holder.get('started')}
            if lock_holder else None
        ),
        'status_errors': (
            _digest(sorted([str(op), str(error)] for op, error in status_errors))
            if status_errors else None
        ),
    }, sort_keys=True)


def _status_title(stale, lock_alert, status_errors):
    problems = []
    if stale:
        problems.append('backups are stale')
    if lock_alert:
        problems.append('backup appears stalled')
    if status_errors:
        problems.append('status check failed')
    return '; '.join(problems)


def _record_status_alert(c, fields):
    """Store status alert fields, dropping any older handled-run record."""
    with alert_state_transaction(c) as alert_state:
        entry = alert_state.get('status')
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.pop('handled_at', None)
        entry.pop('invocation_id', None)
        entry.update({key: value for key, value in fields.items() if value})
        if entry or 'status' in alert_state:
            alert_state['status'] = entry


def _notify_status(c, now, stale, lock_alert, lock_holder, status_errors):
    """Notify on change only; repeat an unchanged problem after renotify_after.

    healthchecks OK pings are heartbeats and go out on every OK run; ntfy gets
    a single recovery message when a notified problem clears. A stale/stalled
    problem that was delivered or deliberately suppressed is recorded as
    handled by this run (systemd INVOCATION_ID) so notify-failure does not
    push the same failed run twice.
    """
    host = c.get('host_id', '?')
    previous = load_alert_state(c).get('status')
    previous = previous if isinstance(previous, dict) else {}
    if not (stale or lock_alert or status_errors):
        recovered = bool(previous.get('signature'))
        delivered = notify(
            c, f'homelab-backup on {host}: '
               f"{'recovered' if recovered else 'OK'}",
            'all scheduled backups are fresh', ok=True, recovered=recovered,
        )
        if recovered and delivered:
            with alert_state_transaction(c) as alert_state:
                alert_state.pop('status', None)
        return
    # Status' own errors exit 1 and must always reach the notify-failure
    # push, so such a run is never marked as handled.
    handled = {} if status_errors else {
        'handled_at': now.isoformat(),
        'invocation_id': os.environ.get('INVOCATION_ID'),
    }
    signature = _status_signature(stale, lock_holder, status_errors)
    if previous.get('signature') == signature \
            and notified_within(previous, c, now):
        print(
            f"status alert unchanged since {previous['notified_at']}; "
            'not notifying again before alert.renotify_after',
            file=sys.stderr,
        )
        _record_status_alert(c, handled)
        return
    lines = [
        f"{service}: last success {state.get('last_success_at') or 'never'}"
        f"; last error {_first_line(state.get('last_error') or '-')}"
        for service, state, _ in stale
    ]
    if lock_alert:
        lines.append(lock_alert)
    lines.extend(
        f'status error: {operation}: {_first_line(error)}'
        for operation, error in status_errors
    )
    title = _status_title(stale, lock_alert, status_errors)
    if notify(c, f'homelab-backup on {host}: {title}', '\n'.join(lines)):
        _record_status_alert(c, {
            'signature': signature, 'notified_at': now.isoformat(), **handled,
        })


def cmd_status(c, args):
    now = local_now()
    failures = FailureSummary()
    max_age = getattr(args, 'max_age', None)
    max_age_seconds = (
        parse_duration(max_age, '--max-age') if isinstance(max_age, str) else None
    )
    stale = []
    for m in manifests(c, on_error=_manifest_error_recorder(failures)):
        service = m.get('service', '<unnamed>')
        try:
            service = _service_name(m)
            due, reason, _ = due_status(c, m, now)
        except Exception as err:
            failures.record_exception(
                service, err,
                message=f'ERROR: status schedule failed for {service}: {{error}}',
                summary_error=f'schedule status failed: {err}',
            )
            if os.environ.get('BACKUPCTL_DEBUG') == '1':
                raise
            continue
        try:
            state = load_state(c, service)
            staleness = success_staleness(
                c, m, state, now, max_age_seconds=max_age_seconds,
            )
        except Exception as err:
            failures.record_exception(
                service, err,
                message=f'ERROR: state loading failed for {service}: {{error}}',
                summary_error=f'state loading failed: {err}',
            )
            if os.environ.get('BACKUPCTL_DEBUG') == '1':
                raise
            continue
        print(json.dumps({
            'service': service, 'due': due, 'schedule_status': reason,
            'last_result': state.get('last_result'),
            'last_success_at': state.get('last_success_at'),
            'last_duration_seconds': state.get('last_duration_seconds'),
            'last_error': state.get('last_error'),
            'last_retention_error': state.get('last_retention_error'),
            **staleness,
        }, ensure_ascii=False))
        if staleness['stale']:
            stale.append((service, state, staleness))
    for service, state, staleness in stale:
        print(
            f"ALERT: {service}: no successful backup within "
            f"{staleness['max_success_age_seconds']}s "
            f"(last success: {state.get('last_success_at') or 'never'}; "
            f"last error: {state.get('last_error') or '-'})",
            file=sys.stderr,
        )
    lock_alert, lock_holder = _stalled_lock_alert(c, now)
    if lock_alert:
        print(f'ALERT: {lock_alert}', file=sys.stderr)
    if getattr(args, 'notify', False) is True:
        _notify_status(c, now, stale, lock_alert, lock_holder, failures.items)
    failures.raise_if_any('STATUS FAILURES')
    if stale or lock_alert:
        raise SystemExit(STALE_EXIT_CODE)
    # A clean status run resets the unit's throttle, so the next status
    # failure is pushed immediately.
    _clear_unit_alert(c, STATUS_UNIT)


BACKUP_UNIT = 'homelab-backup.service'
MAINTENANCE_UNIT = 'homelab-backup-maintenance.service'
CHECK_UNIT = 'homelab-backup-check.service'
STATUS_UNIT = 'homelab-backup-status.service'
STATUS_HANDLED_WINDOW = dt.timedelta(minutes=20)
RUN_DUE_HANDOFF_WINDOW = dt.timedelta(minutes=20)
CRASH_QUIET_PERIOD = dt.timedelta(hours=1)
ERROR_LINE_LIMIT = 200
JOURNAL_TAIL_LIMIT = 3000


def _first_line(error, limit=ERROR_LINE_LIMIT):
    """First non-empty line of an error, truncated for notification bodies."""
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    line = lines[0] if lines else '-'
    return line if len(line) <= limit else line[:limit - 1] + '…'


def print_unit_failure_alert(unit):
    print(f'ALERT: systemd unit failed: {unit}', file=sys.stderr)


def _unit_record(alert_state, unit):
    units = alert_state.get('units')
    record = units.get(unit) if isinstance(units, dict) else None
    return record if isinstance(record, dict) else {}


def _update_unit_record(c, unit, update):
    """Apply ``update(record)`` to ``units[unit]`` under the alert lock.

    A record left empty is removed.
    """
    with alert_state_transaction(c) as alert_state:
        units = alert_state.get('units')
        units = units if isinstance(units, dict) else {}
        record = units.get(unit)
        record = copy.deepcopy(record) if isinstance(record, dict) else {}
        update(record)
        if record:
            units[unit] = record
        else:
            units.pop(unit, None)
        if units or 'units' in alert_state:
            alert_state['units'] = units


def _record_dict(record, key):
    value = record.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _record_run_due(c, now, attempted, failures, known_services):
    """Per-service alert bookkeeping at the end of a completed run-due.

    A successful backup removes only that service's throttle entry; services
    not attempted this round keep theirs. ``known_services`` (None when some
    manifest failed to load) lets entries of removed services be dropped.
    The failed set is stored with this run's INVOCATION_ID so notify-failure
    can tell it apart from a stale record of an older run. A crash entry is
    cleared only by a round that attempted a backup with no failures, once
    the crash has not been seen for CRASH_QUIET_PERIOD, so an intermittent
    crash between good rounds stays throttled; "No backups are due" proves
    nothing.
    """
    failed = {}
    for operation, error in failures.items:
        failed.setdefault(str(operation), _first_line(error))
    succeeded = set(attempted) - set(failed)
    if not failed and not _unit_record(load_alert_state(c), BACKUP_UNIT):
        return

    def update(record):
        # Whole-unit throttle fields from older releases.
        for legacy in ('failure_signature', 'notified_signature', 'notified_at'):
            record.pop(legacy, None)
        services = _record_dict(record, 'services')
        for name in list(services):
            if name in succeeded or (
                known_services is not None
                and name not in known_services and name not in failed
            ):
                services.pop(name)
        if services:
            record['services'] = services
        else:
            record.pop('services', None)
        crashes = _record_dict(record, 'crashes')
        if attempted and not failed:
            for key in [k for k, v in crashes.items() if _crash_quiet(v, now)]:
                crashes.pop(key)
        if crashes:
            record['crashes'] = crashes
        else:
            record.pop('crashes', None)
        if failed:
            record['run'] = {
                'invocation_id': os.environ.get('INVOCATION_ID'),
                'completed_at': now.isoformat(),
                'failures': failed,
            }
        else:
            record.pop('run', None)
            record.pop('last_failure_at', None)

    _update_unit_record(c, BACKUP_UNIT, update)


def _crash_quiet(entry, now):
    """True when a crash entry was last seen CRASH_QUIET_PERIOD ago or more."""
    if not isinstance(entry, dict):
        return True
    # Entries from older releases only carry notified_at.
    last_seen = parse_timestamp(entry.get('last_seen_at') or entry.get('notified_at'))
    if last_seen is None:
        return True
    return last_seen <= now and now - last_seen >= CRASH_QUIET_PERIOD


def _clear_unit_alert(c, unit):
    """A successful run resets throttling, so the next failure pushes."""
    if not _unit_record(load_alert_state(c), unit):
        return
    with alert_state_transaction(c) as alert_state:
        units = alert_state.get('units')
        if isinstance(units, dict):
            units.pop(unit, None)


def _status_handled_failed_run(alert_state, record, now):
    """True when ``status --notify`` already handled the run that failed."""
    status = alert_state.get('status')
    if not isinstance(status, dict):
        return False
    handled_at = parse_timestamp(status.get('handled_at'))
    if handled_at is None or handled_at > now:
        return False
    # systemd >= 251 passes the failed run's id to OnFailure= units; a
    # record without an id then cannot be proven to belong to this run.
    failed_id = os.environ.get('MONITOR_INVOCATION_ID')
    if failed_id:
        return status.get('invocation_id') == failed_id
    # Without the id, only a recent record that no earlier notify-failure
    # has already consumed can belong to this failure.
    if now - handled_at > STATUS_HANDLED_WINDOW:
        return False
    last_failure = parse_timestamp(record.get('last_failure_at'))
    return last_failure is None or handled_at > last_failure


def cmd_notify_failure(c, args):
    """Entry point for systemd OnFailure= (homelab-backup-notify@.service).

    The ALERT line always reaches the journal. run-due failures are throttled
    per service (see ``_notify_run_due_failure``); other units by
    alert.renotify_after per unit, and their successful run clears the
    record. A failed status run whose problem ``status --notify`` already
    handled is not pushed again; status' own errors always are.
    """
    unit = args.unit
    if getattr(args, 'alert_printed', False) is not True:
        print_unit_failure_alert(unit)
    now = local_now()
    if unit == BACKUP_UNIT:
        _notify_run_due_failure(c, now)
        return
    alert_state = load_alert_state(c)
    record = _unit_record(alert_state, unit)
    delivered = False
    if unit == STATUS_UNIT and _status_handled_failed_run(alert_state, record, now):
        print(
            'status already handled this problem; not pushing it again',
            file=sys.stderr,
        )
    elif notified_within(record, c, now):
        print(
            f"{unit} failure already notified at {record['notified_at']}; "
            'not pushing again before alert.renotify_after',
            file=sys.stderr,
        )
    else:
        delivered = _push_unit_failure(c, unit, f'{unit} failed', '')

    def update(current):
        current['last_failure_at'] = now.isoformat()
        if delivered:
            current['notified_at'] = now.isoformat()

    _update_unit_record(c, unit, update)


def _monitor_result():
    """The failed run's result as passed by systemd >= 251 to OnFailure=."""
    result = os.environ.get('MONITOR_SERVICE_RESULT') or 'unknown'
    details = [
        f'{name}={value}' for name, value in (
            ('result', result),
            ('exit_code', os.environ.get('MONITOR_EXIT_CODE')),
            ('exit_status', os.environ.get('MONITOR_EXIT_STATUS')),
        ) if value
    ]
    return result, ', '.join(details)


def _trusted_run_due_record(record, now):
    """The run-due failure record written by the run that failed, or None.

    With MONITOR_INVOCATION_ID (systemd >= 251) the ids must match. Without
    it, only a recent record that no earlier notify-failure consumed counts.
    """
    run_record = record.get('run')
    if not isinstance(run_record, dict) \
            or not isinstance(run_record.get('failures'), dict) \
            or not run_record['failures']:
        return None
    failed_id = os.environ.get('MONITOR_INVOCATION_ID')
    if failed_id:
        return run_record if run_record.get('invocation_id') == failed_id else None
    completed = parse_timestamp(run_record.get('completed_at'))
    if completed is None or completed > now \
            or now - completed > RUN_DUE_HANDOFF_WINDOW:
        return None
    last_failure = parse_timestamp(record.get('last_failure_at'))
    if last_failure is not None and last_failure >= completed:
        return None
    return run_record


def _notify_run_due_failure(c, now):
    """Push run-due failures per service, or a crash without bookkeeping.

    A failed service is pushed when it newly enters failure or its last push
    is older than alert.renotify_after; one push lists every such service.
    When run-due did not finish its bookkeeping (crash, timeout, OOM, kill,
    config error) the failure is pushed as ``crash:<result>``, throttled per
    result type by its last push. Every crash, pushed or not, refreshes the
    entry's last_seen_at, which ``_record_run_due`` uses to clear it.
    """
    record = _unit_record(load_alert_state(c), BACKUP_UNIT)
    run_record = _trusted_run_due_record(record, now)
    delivered = False
    if run_record is not None:
        failed = {
            str(name): str(error) for name, error in run_record['failures'].items()
        }
        services = _record_dict(record, 'services')
        pending = sorted(
            name for name in failed
            if not notified_within(services.get(name), c, now)
        )
        known = sorted(set(failed) - set(pending))
        if pending:
            lines = ['failed: ' + ', '.join(pending)]
            lines += [f'- {name}: {failed[name]}' for name in pending]
            if known:
                lines.append(
                    'still failing (already notified): ' + ', '.join(known),
                )
            delivered = _push_unit_failure(
                c, BACKUP_UNIT, f'{BACKUP_UNIT} failed', '\n'.join(lines),
            )
        else:
            print(
                f"failed services already notified ({', '.join(known)}); "
                'not pushing again before alert.renotify_after',
                file=sys.stderr,
            )

        def update(current):
            current['last_failure_at'] = now.isoformat()
            if delivered:
                stored = _record_dict(current, 'services')
                for name in pending:
                    stored[name] = {
                        'notified_at': now.isoformat(), 'error': failed[name],
                    }
                current['services'] = stored

        _update_unit_record(c, BACKUP_UNIT, update)
        return
    result, details = _monitor_result()
    key = f'crash:{result}'
    crashes = _record_dict(record, 'crashes')
    if notified_within(crashes.get(key), c, now):
        print(
            f"{BACKUP_UNIT} {result} failure already notified at "
            f"{crashes[key]['notified_at']}; not pushing again before "
            'alert.renotify_after',
            file=sys.stderr,
        )
    else:
        delivered = _push_unit_failure(
            c, BACKUP_UNIT, f'{BACKUP_UNIT} failed ({result})',
            f'run-due ended without recording which services failed '
            f'({details}); it crashed, timed out, was killed or could not '
            'start. Check the journal.',
        )

    def update(current):
        current['last_failure_at'] = now.isoformat()
        stored = _record_dict(current, 'crashes')
        entry = _record_dict(stored, key)
        entry.update(last_seen_at=now.isoformat(), details=details)
        if delivered:
            entry['notified_at'] = now.isoformat()
        stored[key] = entry
        current['crashes'] = stored

    _update_unit_record(c, BACKUP_UNIT, update)


def _push_unit_failure(c, unit, title, summary):
    try:
        journal = run(
            ['journalctl', '--unit', unit, '--lines', '30', '--no-pager',
             '--output', 'cat'],
            capture=True, check=False,
        ).stdout or ''
    except Exception as err:  # noqa: BLE001 - journal is optional context
        journal = f'(journal unavailable: {err})'
    result, details = _monitor_result()
    if unit != BACKUP_UNIT and os.environ.get('MONITOR_SERVICE_RESULT') \
            and result != 'exit-code':
        title += f' ({result})'
    if not summary and os.environ.get('MONITOR_SERVICE_RESULT'):
        summary = details
    journal = journal[-JOURNAL_TAIL_LIMIT:]
    detail = '\n\n'.join(part for part in (summary, journal) if part)
    delivered = notify(
        c, f"homelab-backup on {c.get('host_id', '?')}: {title}",
        detail or 'see journalctl for details',
    )
    if not delivered:
        print(
            'WARNING: failure notification was not delivered '
            '(alert.notify_url_file not configured or unreachable)',
            file=sys.stderr,
        )
    return delivered


def cmd_run_due(c, args):
    with GlobalLock(
        c['lock_file'], nonblocking=True, retries=RUN_DUE_LOCK_RETRIES,
    ) as acquired:
        if not acquired:
            print('SKIP: another backup or maintenance process is still running')
            return
        now = local_now()
        due = []
        failures = FailureSummary()
        known_services = set()
        record_manifest_error = _manifest_error_recorder(failures)

        def on_manifest_error(path, err):
            nonlocal known_services
            known_services = None
            record_manifest_error(path, err)

        for m in manifests(c, on_error=on_manifest_error):
            service = m.get('service', '<unnamed>')
            try:
                service = _service_name(m)
                if known_services is not None:
                    known_services.add(service)
                is_due, reason, _ = due_status(c, m, now)
            except Exception as err:
                failures.record_exception(
                    service, err,
                    message=f'ERROR: schedule planning failed for {service}: {{error}}',
                    summary_error=f'schedule planning failed: {err}',
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
                continue
            print(
                f"{service_label(m)}: "
                f"{'DUE' if is_due else 'skip'} ({reason})"
            )
            if is_due:
                due.append(m)
        if not due:
            print('No backups are due.')
        else:
            clear_stale_restic_locks(c)
        for m in due:
            print(f"\n== Scheduled backup: {service_label(m)} ==")
            try:
                backup_one(c, m)
            except Exception as err:
                failures.record_exception(
                    m['service'], err,
                    message=(
                        f"ERROR: scheduled backup failed for "
                        f"{service_label(m)}: {{error}}"
                    ),
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
        _record_run_due(
            c, local_now(), [m['service'] for m in due], failures,
            known_services,
        )
        failures.raise_if_any('SCHEDULED BACKUP FAILURES')


def cmd_snapshots(c, args):
    env = restic_env(c)
    cmd = ['restic', 'snapshots', '--host', c['host_id']]
    if args.service:
        cmd += ['--tag', f'service:{args.service}']
    run(cmd, env=env)
    if not args.service:
        print('\nSpecify a service to include retention reasons: '
              'backupctl snapshots SERVICE')
        return

    try:
        m = manifest(c, args.service)
        validate_manifest(m)
    except Exception as err:
        print(
            f'WARNING: retention preview unavailable for {args.service}: {err}',
            file=sys.stderr,
        )
        return

    print(f'\n== Retention preview: {args.service} ==')
    run(retention_cmd(c, m, dry_run=True), env=env)


def cmd_maintenance(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        clear_stale_restic_locks(c)
        failures = FailureSummary()
        if not args.dry_run:
            try:
                snapshot_services = snapshot_state_services(c)
            except Exception as err:
                failures.record_exception(
                    'Btrfs snapshot recovery', err,
                    message='ERROR: Btrfs snapshot journal scan failed: {error}',
                )
                snapshot_services = []
            for service in snapshot_services:
                try:
                    cleanup_snapshot_state(c, service)
                except Exception as err:
                    failures.record_exception(
                        service, err,
                        message=(
                            'ERROR: Btrfs snapshot recovery failed for '
                            f'{service}: {{error}}'
                        ),
                        summary_error=f'Btrfs snapshot recovery failed: {err}',
                    )
                    if os.environ.get('BACKUPCTL_DEBUG') == '1':
                        raise
        for m in manifests(c, on_error=_manifest_error_recorder(failures)):
            service = m.get('service', '<unnamed>')
            print(f"\n== Retention: {service_label(m)} ==")
            try:
                service = _service_name(m)
            except Exception as err:
                failures.record_exception(
                    service, err,
                    command_context=f'Retention failed for {service}',
                    message=f'ERROR: retention failed for {service}: {{error}}',
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
                continue
            try:
                run(retention_cmd(c, m, dry_run=args.dry_run), env=restic_env(c))
            except Exception as err:
                failures.record_exception(
                    service, err,
                    command_context=f'Retention failed for {service}',
                    message=f'ERROR: retention failed for {service}: {{error}}',
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
                continue
            if not args.dry_run:
                try:
                    state = load_state(c, service)
                    if state.get('last_retention_error') is not None:
                        state['last_retention_error'] = None
                        save_state(c, service, state)
                except Exception as err:
                    failures.record_exception(
                        service, err,
                        message=(
                            'ERROR: retention state update failed for '
                            f'{service}: {{error}}'
                        ),
                        summary_error=f'retention state update failed: {err}',
                    )
                    if os.environ.get('BACKUPCTL_DEBUG') == '1':
                        raise
        if not args.dry_run:
            try:
                run(['restic', 'prune'], env=restic_env(c))
            except Exception as err:
                failures.record_exception(
                    'repository prune', err,
                    command_context='Repository prune failed',
                    message='ERROR: repository prune failed: {error}',
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
        if not args.dry_run and not failures.has_failures():
            _clear_unit_alert(c, MAINTENANCE_UNIT)
        failures.raise_if_any('MAINTENANCE FAILURES')


def cmd_check(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        clear_stale_restic_locks(c)
        cmd = ['restic', 'check']
        subset = (c.get('check') or {}).get('read_data_subset')
        if subset:
            cmd += ['--read-data-subset', str(subset)]
        run(cmd, env=restic_env(c))
        _clear_unit_alert(c, CHECK_UNIT)


def cmd_unlock(c, args):
    with GlobalLock(c['lock_file'], nonblocking=True) as acquired:
        if not acquired:
            die('another backupctl process is running')
        run(['restic', 'unlock'], env=restic_env(c))
