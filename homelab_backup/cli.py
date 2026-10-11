import argparse
import os
import sys

from . import VERSION
from .backup import (
    cmd_backup,
    cmd_init,
    cmd_validate,
)
from .common import (
    RUN_DUE_LOCK_RETRIES, CommandError, GlobalLock, _print_command_failure,
    format_remediable_error,
)
from .config import cfg, config_lock_file
from .docs import DocsError, cmd_docs
from .maintenance import (
    cmd_check, cmd_list, cmd_maintenance, cmd_notify_failure, cmd_run_due,
    cmd_snapshots, cmd_status, cmd_unlock, print_unit_failure_alert,
)
from .restore import (
    cmd_apply, cmd_cleanup_restores, cmd_delete_snapshot, cmd_restore,
)
from .upgrade import UpgradeError, cmd_upgrade


COMMANDS = {
    'list': cmd_list,
    'status': cmd_status,
    'validate': cmd_validate,
    'init': cmd_init,
    'backup': cmd_backup,
    'run-due': cmd_run_due,
    'snapshots': cmd_snapshots,
    'delete-snapshot': cmd_delete_snapshot,
    'restore': cmd_restore,
    'cleanup-restores': cmd_cleanup_restores,
    'apply': cmd_apply,
    'maintenance': cmd_maintenance,
    'check': cmd_check,
    'unlock': cmd_unlock,
}


def _nonblocking_requested(args):
    if args.cmd in ('run-due', 'unlock'):
        return True
    return args.cmd in ('maintenance', 'check') and args.no_wait


def _lock_retries(args):
    # A timer-driven run-due that collides with a just-finishing run would
    # otherwise skip a whole interval; retry briefly first.
    return RUN_DUE_LOCK_RETRIES if args.cmd == 'run-due' else 0


def _report_unavailable_lock(command):
    if command == 'run-due':
        print('SKIP: another backup or maintenance process is still running')
    elif command in ('maintenance', 'check'):
        print('SKIP: backup or maintenance is still running')
    else:
        print('ERROR: another backupctl process is running', file=sys.stderr)
        raise SystemExit(1)


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=f'backupctl {VERSION}')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    command = sub.add_parser('status')
    command.add_argument(
        '--max-age', metavar='DURATION',
        help='alert when the last success is older than this (e.g. 36h); '
             'default: alert.max_success_age or two schedule periods + 1h',
    )
    command.add_argument(
        '--notify', action='store_true',
        help='send the result to alert.notify_url_file (ntfy/healthchecks)',
    )
    sub.add_parser('validate')
    sub.add_parser('init')
    command = sub.add_parser('backup')
    command.add_argument('services', nargs='*')
    command.add_argument(
        '--allow-low-space', action='store_true',
        help='continue even if staging would leave less than 1 GiB free',
    )
    sub.add_parser('run-due')
    command = sub.add_parser('snapshots')
    command.add_argument('service', nargs='?')
    command = sub.add_parser('delete-snapshot')
    command.add_argument('service')
    command.add_argument('snapshot', help='8-64 character snapshot ID or prefix')
    command.add_argument(
        '--prune', action='store_true',
        help='immediately reclaim repository space after deleting the snapshot',
    )
    command.add_argument(
        '--yes', action='store_true',
        help='confirm deletion; required whenever stdin is not a TTY',
    )
    command = sub.add_parser('restore')
    command.add_argument('services', nargs='*')
    command.add_argument('--all', action='store_true', help='restore all services without the selector')
    command.add_argument(
        '--snapshot', default='latest',
        help='latest, or one snapshot ID when restoring exactly one service',
    )
    command.add_argument('--apply', action='store_true', help='apply restored data after downloading it')
    command.add_argument('--start', action='store_true', help='start selected Compose stacks after apply')
    command.add_argument(
        '--yes', action='store_true',
        help='confirm restore; required whenever stdin is not a TTY',
    )
    command.add_argument(
        '--allow-low-space', action='store_true',
        help='continue even if the restore would leave less than 1 GiB free',
    )
    manifest_group = command.add_mutually_exclusive_group()
    manifest_group.add_argument('--restore-manifest', action='store_true', help='replace local backup.yaml from snapshot')
    manifest_group.add_argument('--keep-manifest', action='store_true', help='keep an existing local backup.yaml')
    command = sub.add_parser('cleanup-restores')
    command.add_argument('targets', nargs='*', help='SERVICE/RESTORE_ID')
    command.add_argument('--all', action='store_true', help='remove every downloaded restore')
    command.add_argument('--yes', action='store_true', help='confirm deletion')
    command = sub.add_parser('apply')
    command.add_argument('service')
    command.add_argument('restore_dir')
    command.add_argument('--start', action='store_true')
    command.add_argument('--yes', action='store_true')
    command = sub.add_parser('maintenance')
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--no-wait', action='store_true')
    command = sub.add_parser('check')
    command.add_argument('--no-wait', action='store_true')
    sub.add_parser('unlock')
    command = sub.add_parser(
        'notify-failure',
        help='send a failure notification for a systemd unit (OnFailure=); '
             'the push includes the last 30 journal lines of the unit and '
             'is repeated at most once per alert.renotify_after while the '
             'failure is unchanged',
        description='Log an ALERT line for UNIT and push a notification '
                    'including the last 30 journal lines of the unit to '
                    'alert.notify_url_file. run-due failures are throttled '
                    'per service: a service is pushed when it newly fails or '
                    'its last push is older than alert.renotify_after, and its '
                    'successful backup resets it; a run-due that ended '
                    'without recording its failures (crash, timeout, kill) is '
                    'pushed per result type. Other units are pushed at most '
                    'once per alert.renotify_after; a successful run of the '
                    'unit resets this.',
    )
    command.add_argument('unit')
    sub.add_parser('docs')
    sub.add_parser('upgrade')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == 'docs':
            cmd_docs(args)
            return
        if os.geteuid() != 0:
            print('ERROR: backupctl must run as root', file=sys.stderr)
            raise SystemExit(1)
        if args.cmd == 'upgrade':
            cmd_upgrade(args)
            return
        if args.cmd == 'notify-failure':
            # Log first so the journal shows the failure even when the
            # config cannot be loaded. Must not wait behind the global lock.
            print_unit_failure_alert(args.unit)
            args.alert_printed = True
            cmd_notify_failure(cfg(), args)
            return
        if args.cmd == 'status':
            # Reads only state files; must still alert while a backup or
            # check holds (or is stuck holding) the global lock.
            cmd_status(cfg(), args)
            return
        while True:
            selected_lock = config_lock_file()
            with GlobalLock(
                selected_lock, nonblocking=_nonblocking_requested(args),
                retries=_lock_retries(args),
            ) as acquired:
                if not acquired:
                    _report_unavailable_lock(args.cmd)
                    return
                config = cfg()
                if os.path.abspath(config['lock_file']) != \
                        os.path.abspath(selected_lock):
                    continue
                COMMANDS[args.cmd](config, args)
                break
    except (DocsError, UpgradeError) as err:
        print(f'ERROR: {err}', file=sys.stderr)
        raise SystemExit(1)
    except CommandError as err:
        if not err.reported:
            _print_command_failure(err)
        raise SystemExit(err.returncode or 1)
    except KeyboardInterrupt:
        print('\nERROR: interrupted by user', file=sys.stderr)
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as err:
        print(
            f'ERROR: {format_remediable_error(type(err).__name__, err)}',
            file=sys.stderr,
        )
        print('Run with BACKUPCTL_DEBUG=1 to display a Python traceback.', file=sys.stderr)
        if os.environ.get('BACKUPCTL_DEBUG') == '1':
            raise
        raise SystemExit(1)
