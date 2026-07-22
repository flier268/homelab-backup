import json
import os
import sys

from .backup import backup_one, retention_cmd
from .backup_state import due_status, load_state, save_state
from .btrfs_snapshot import cleanup_snapshot_state, snapshot_state_services
from .common import (
    CommandError, FailureSummary, GlobalLock, _print_command_failure, die,
    restic_env, run,
)
from .manifest import manifest, manifests, valid_service_name, validate_manifest
from .schedule import local_now


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
            print(f"{service}: {schedule_text}; {'DUE' if due else reason}; {m['_path']}")
        except Exception as err:
            failures.record_exception(
                service, err,
                message=f'ERROR: list failed for {service}: {{error}}',
                summary_error=f'listing failed: {err}',
            )
            if os.environ.get('BACKUPCTL_DEBUG') == '1':
                raise
    failures.raise_if_any('LIST FAILURES')


def cmd_status(c, args):
    now = local_now()
    failures = FailureSummary()
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
        }, ensure_ascii=False))
    failures.raise_if_any('STATUS FAILURES')


def cmd_run_due(c, args):
    with GlobalLock(c['lock_file'], nonblocking=True) as acquired:
        if not acquired:
            print('SKIP: another backup or maintenance process is still running')
            return
        now = local_now()
        due = []
        failures = FailureSummary()
        for m in manifests(c, on_error=_manifest_error_recorder(failures)):
            service = m.get('service', '<unnamed>')
            try:
                service = _service_name(m)
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
            print(f"{m['service']}: {'DUE' if is_due else 'skip'} ({reason})")
            if is_due:
                due.append(m)
        if not due:
            print('No backups are due.')
        for m in due:
            print(f"\n== Scheduled backup: {m['service']} ==")
            try:
                backup_one(c, m)
            except Exception as err:
                failures.record_exception(
                    m['service'], err,
                    message=f"ERROR: scheduled backup failed for {m['service']}: {{error}}",
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
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
            print(f"\n== Retention: {service} ==")
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
        failures.raise_if_any('MAINTENANCE FAILURES')


def cmd_check(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        cmd = ['restic', 'check']
        subset = (c.get('check') or {}).get('read_data_subset')
        if subset:
            cmd += ['--read-data-subset', str(subset)]
        run(cmd, env=restic_env(c))


def cmd_unlock(c, args):
    with GlobalLock(c['lock_file'], nonblocking=True) as acquired:
        if not acquired:
            die('another backupctl process is running')
        run(['restic', 'unlock'], env=restic_env(c))
