import argparse
import os
import sys

from . import VERSION
from .backup import (
    cmd_backup,
    cmd_check,
    cmd_init,
    cmd_list,
    cmd_maintenance,
    cmd_run_due,
    cmd_snapshots,
    cmd_status,
    cmd_unlock,
    cmd_validate,
)
from .common import CommandError, _print_command_failure
from .config import cfg
from .restore import cmd_apply, cmd_restore


COMMANDS = {
    'list': cmd_list,
    'status': cmd_status,
    'validate': cmd_validate,
    'init': cmd_init,
    'backup': cmd_backup,
    'run-due': cmd_run_due,
    'snapshots': cmd_snapshots,
    'restore': cmd_restore,
    'apply': cmd_apply,
    'maintenance': cmd_maintenance,
    'check': cmd_check,
    'unlock': cmd_unlock,
}


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
    command.add_argument('--snapshot', default='latest')
    command.add_argument('--apply', action='store_true', help='apply restored data after downloading it')
    command.add_argument('--start', action='store_true', help='start selected Compose stacks after apply')
    command.add_argument(
        '--yes', action='store_true',
        help='confirm restore; required whenever stdin is not a TTY',
    )
    manifest_group = command.add_mutually_exclusive_group()
    manifest_group.add_argument('--restore-manifest', action='store_true', help='replace local backup.yaml from snapshot')
    manifest_group.add_argument('--keep-manifest', action='store_true', help='keep an existing local backup.yaml')
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
    try:
        config = cfg()
        COMMANDS[args.cmd](config, args)
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
