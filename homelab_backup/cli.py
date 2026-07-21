import argparse
import os
import sys

from . import VERSION
from .backup import (
    cmd_backup,
    cmd_init,
    cmd_validate,
)
from .common import CommandError, GlobalLock, _print_command_failure
from .config import cfg, config_lock_file
from .maintenance import (
    cmd_check, cmd_list, cmd_maintenance, cmd_run_due, cmd_snapshots,
    cmd_status, cmd_unlock,
)
from .restore import cmd_apply, cmd_cleanup_restores, cmd_restore


COMMANDS = {
    'list': cmd_list,
    'status': cmd_status,
    'validate': cmd_validate,
    'init': cmd_init,
    'backup': cmd_backup,
    'run-due': cmd_run_due,
    'snapshots': cmd_snapshots,
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
    sub.add_parser('status')
    sub.add_parser('validate')
    sub.add_parser('init')
    command = sub.add_parser('backup')
    command.add_argument('services', nargs='*')
    sub.add_parser('run-due')
    command = sub.add_parser('snapshots')
    command.add_argument('service', nargs='?')
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if os.geteuid() != 0:
        print('ERROR: backupctl must run as root', file=sys.stderr)
        raise SystemExit(1)
    try:
        while True:
            selected_lock = config_lock_file()
            with GlobalLock(
                selected_lock, nonblocking=_nonblocking_requested(args),
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
        print(f'ERROR: {type(err).__name__}: {err}', file=sys.stderr)
        print('Run with BACKUPCTL_DEBUG=1 to display a Python traceback.', file=sys.stderr)
        if os.environ.get('BACKUPCTL_DEBUG') == '1':
            raise
        raise SystemExit(1)
