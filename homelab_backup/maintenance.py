import json
import os
import sys

from .backup import backup_one, retention_cmd
from .backup_state import due_status, load_state
from .common import (
    CommandError, GlobalLock, _print_command_failure, die, restic_env, run,
)
from .manifest import manifests
from .schedule import local_now


class FailureSummary:
    def __init__(self):
        self.items = []

    def append(self, operation, error):
        self.items.append((operation, error))

    def record_exception(
            self, operation, error, *, message, command_context=None,
            summary_error=None,
    ):
        if command_context is not None and isinstance(error, CommandError):
            _print_command_failure(error, context=command_context)
        else:
            print(message.format(error=error), file=sys.stderr)
        self.append(operation, summary_error or str(error))

    def raise_if_any(self, title):
        if not self.items:
            return
        print(f'\n{title}', file=sys.stderr)
        for operation, error in self.items:
            print(f'  - {operation}: {error}', file=sys.stderr)
        raise SystemExit(1)


def _manifest_error_recorder(failures):
    def record(path, err):
        failures.append(str(path), f'manifest loading failed: {err}')
        print(f'ERROR: manifest loading failed for {path}: {err}', file=sys.stderr)

    return record


def cmd_list(c, args):
    now = local_now()
    for m in manifests(c):
        due, reason, _ = due_status(c, m, now)
        schedule = m['schedule']
        schedule_text = f"cron {schedule['cron']}"
        print(f"{m['service']}: {schedule_text}; {'DUE' if due else reason}; {m['_path']}")


def cmd_status(c, args):
    now = local_now()
    for m in manifests(c):
        due, reason, _ = due_status(c, m, now)
        state = load_state(c, m['service'])
        print(json.dumps({
            'service': m['service'], 'due': due, 'schedule_status': reason,
            'last_result': state.get('last_result'),
            'last_success_at': state.get('last_success_at'),
            'last_duration_seconds': state.get('last_duration_seconds'),
            'last_error': state.get('last_error'),
            'last_retention_error': state.get('last_retention_error'),
        }, ensure_ascii=False))


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
    cmd = ['restic', 'snapshots', '--host', c['host_id']]
    if args.service:
        cmd += ['--tag', f'service:{args.service}']
    run(cmd, env=restic_env(c))


def cmd_maintenance(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        failures = FailureSummary()
        for m in manifests(c, on_error=_manifest_error_recorder(failures)):
            service = m.get('service', '<unnamed>')
            print(f"\n== Retention: {service} ==")
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
