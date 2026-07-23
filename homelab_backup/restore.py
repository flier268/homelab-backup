import curses
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from . import restore_apply
from . import restore_inventory as _restore_inventory
from .common import (
    MIN_FREE_BYTES, CommandError, FailureSummary, GlobalLock,
    _print_command_failure, die, format_bytes, restic_env, run,
)
from .manifest import (
    find_manifest, manifest, service_label, valid_service_name,
    validate_manifest,
)
from .security import (
    clear_control_leaf, ensure_private_directory, lexical_absolute,
    path_contains, read_control_text, validate_control_root,
    validate_trusted_roots,
)
from .storage import (
    validate_docker_bind_probe, validate_docker_environment,
)


MIN_RESTORE_FREE_BYTES = MIN_FREE_BYTES


def _repository_snapshots(c, *filters):
    result = run(
        ['restic', 'snapshots', '--json', '--host', c['host_id'], *filters],
        env=restic_env(c), capture=True,
    )
    try:
        snapshots = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as err:
        raise RuntimeError(f'invalid JSON from restic snapshots: {err}') from err
    if not isinstance(snapshots, list) or any(
        not isinstance(snapshot, dict) for snapshot in snapshots
    ):
        raise RuntimeError('invalid JSON structure from restic snapshots')
    return snapshots


def repository_services(c):
    """Return service names found in Restic snapshot tags for this host."""
    snapshots = _repository_snapshots(c)
    services = set()
    for snapshot in snapshots:
        for tag in snapshot.get('tags') or []:
            if isinstance(tag, str) and tag.startswith('service:') and len(tag) > 8:
                service = tag[8:]
                if not valid_service_name(service):
                    raise RuntimeError(f'invalid service tag in repository: {tag!r}')
                services.add(service)
    return sorted(services)


def resolve_explicit_snapshot(c, service, selector):
    if not re.fullmatch(r'[0-9a-f]{8,64}', selector):
        die('snapshot ID must be an 8-64 character lowercase hexadecimal ID')
    snapshots = _repository_snapshots(c, '--tag', f'service:{service}')
    expected_tag = f'service:{service}'
    matches = []
    for snapshot in snapshots:
        snapshot_id = snapshot.get('id')
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith(selector):
            continue
        if snapshot.get('hostname') != c['host_id']:
            continue
        if expected_tag not in (snapshot.get('tags') or []):
            continue
        matches.append(snapshot_id)
    if not matches:
        die(f'snapshot {selector!r} does not belong to service {service!r} on this host')
    if len(matches) > 1:
        die(f'snapshot prefix {selector!r} is ambiguous for service {service!r}')
    return matches[0]


def cmd_delete_snapshot(c, args):
    if not valid_service_name(args.service):
        die(f'invalid service name: {args.service!r}')
    snapshot = resolve_explicit_snapshot(c, args.service, args.snapshot)
    if not args.yes:
        if not sys.stdin.isatty():
            die('non-interactive snapshot deletion requires --yes')
        if not prompt_yes_no(
                f'Delete snapshot {snapshot} for service {args.service}?',
                default=False,
        ):
            print('Snapshot deletion cancelled.')
            return

    env = restic_env(c)
    run(['restic', 'forget', snapshot], env=env)
    print(f'Deleted snapshot {snapshot} for {args.service}.')
    if not args.prune:
        return

    context = (
        f'Snapshot {snapshot} for {args.service} was deleted, '
        'but repository prune failed'
    )
    try:
        run(['restic', 'prune'], env=env)
    except Exception as err:
        if isinstance(err, CommandError):
            _print_command_failure(err, context=context)
        else:
            print(
                f'ERROR: {context}: {type(err).__name__}: {err}',
                file=sys.stderr,
            )
        raise
    print('Reclaimed unreferenced repository data.')


def estimate_restore_size(c, service, snapshot):
    """Return Restic's estimated expanded size for one service snapshot."""
    result = run([
        'restic', 'stats', '--json', '--mode', 'restore-size',
        '--host', c['host_id'], '--tag', f'service:{service}', snapshot,
    ], env=restic_env(c), capture=True)
    try:
        stats = json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(f'invalid JSON from restic restore size: {err}') from err
    size = stats.get('total_size') if isinstance(stats, dict) else None
    if type(size) is not int or size < 0:
        raise RuntimeError('invalid restore size returned by restic stats')
    return size


def check_restore_space(c, service, snapshot, *, allow_low_space=False):
    """Require enough free restore space to retain a 1 GiB safety margin."""
    restore_root = ensure_private_directory(c['restore_root'])
    restore_size = estimate_restore_size(c, service, snapshot)
    free = shutil.disk_usage(restore_root).free
    required = restore_size + MIN_RESTORE_FREE_BYTES
    if free >= required:
        return

    shortfall = required - free
    print(
        f'WARNING: restore for {service} is estimated at '
        f'{format_bytes(restore_size)}, but only {format_bytes(free)} is free '
        f'on {restore_root}; the required 1.00 GiB reserve would be short by '
        f'{format_bytes(shortfall)}.',
        file=sys.stderr,
    )
    if allow_low_space:
        print(
            'WARNING: continuing because --allow-low-space was specified.',
            file=sys.stderr,
        )
        return
    if sys.stdin.isatty() and prompt_yes_no(
        'Continue despite insufficient restore space?', default=False,
    ):
        return
    raise RuntimeError(
        'insufficient restore space; free additional space or explicitly '
        'use --allow-low-space'
    )


def _selector_screen(stdscr, title, items):
    curses.curs_set(0)
    stdscr.keypad(True)
    selected = [True] * len(items)
    index = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
        help_text = '↑/↓ 移動  Space 切換  A 全選/取消  Enter 確認  Q 取消'
        stdscr.addnstr(1, 0, help_text, max(1, width - 1), curses.A_DIM)
        max_rows = max(1, height - 4)
        start = min(max(0, index - max_rows + 1), max(0, len(items) - max_rows))
        for row, item_index in enumerate(range(start, min(len(items), start + max_rows)), start=3):
            mark = '■' if selected[item_index] else '□'
            pointer = '◆' if item_index == index else ' '
            text = f'{pointer} {mark} {items[item_index]}'
            attr = curses.A_REVERSE if item_index == index else curses.A_NORMAL
            stdscr.addnstr(row, 0, text, max(1, width - 1), attr)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')):
            index = (index - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord('j')):
            index = (index + 1) % len(items)
        elif key == ord(' '):
            selected[index] = not selected[index]
        elif key in (ord('a'), ord('A')):
            target = not all(selected)
            selected = [target] * len(items)
        elif key in (10, 13, curses.KEY_ENTER):
            return [item for item, enabled in zip(items, selected) if enabled]
        elif key in (ord('q'), ord('Q'), 27):
            return None


def select_services_interactively(services):
    if not services:
        return []
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        die('interactive service selection requires a TTY; specify service names or use --all')
    return curses.wrapper(
        _selector_screen,
        'Which services should be restored? (all selected by default)',
        services,
    )


def prompt_yes_no(question, default=False):
    if not sys.stdin.isatty():
        return default
    suffix = ' [Y/n] ' if default else ' [y/N] '
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print('Please answer y or n.')


def restored_manifest_path(root):
    return Path(root) / '_meta' / 'backup.yaml'


def _snapshot_service_relative_directory(root, service):
    inventory = _restore_inventory.load_restore_inventory(root)
    return _restore_inventory.restore_inventory_service_directory(
        inventory, service,
    )


def prepare_restored_manifest(c, service, root, *, policy='ask'):
    source = restored_manifest_path(root)
    if not valid_service_name(service):
        raise ValueError(f'invalid service name from repository: {service!r}')
    local = find_manifest(c, service, include_disabled=True)
    services_root = lexical_absolute(c['services_root'])
    if local is not None:
        service_dir = lexical_absolute(local['_dir'])
        relative_dir = Path(local['_relative_dir'])
    else:
        relative_dir = _snapshot_service_relative_directory(root, service)
        service_dir = lexical_absolute(services_root / relative_dir)
    if not path_contains(services_root, service_dir):
        raise ValueError(
            f'restored service directory escapes services_root: {service_dir}'
        )
    target = service_dir / 'backup.yaml'
    restore_it = not target.exists()
    if target.exists():
        if policy == 'restore':
            restore_it = True
        elif policy == 'keep':
            restore_it = False
        else:
            restore_it = prompt_yes_no(
                f'{target} already exists. Restore backup.yaml from the snapshot?',
                default=False,
            )
    manifest_source = source if restore_it else target
    try:
        m = yaml.safe_load(
            read_control_text(manifest_source, require_protected=False)
        ) or {}
    except FileNotFoundError as err:
        raise RuntimeError(
            f'restored snapshot does not contain {manifest_source}; '
            'cannot reconstruct backup manifest'
        ) from err
    except yaml.YAMLError as err:
        raise ValueError(f'{manifest_source}: invalid manifest YAML: {err}') from err
    if not isinstance(m, dict):
        raise ValueError(f'{manifest_source}: manifest must be a mapping')
    m['_path'] = str(target)
    m['_dir'] = str(service_dir)
    m['_relative_dir'] = relative_dir.as_posix()
    if m.get('service') != service:
        raise ValueError(
            f'{target}: service is {m.get("service")!r}, expected {service!r}'
        )
    validate_manifest(m)
    m['_snapshot_manifest'] = str(source)
    m['_restore_manifest_requested'] = restore_it
    if not restore_it:
        print(f'Keeping local manifest: {target}')
    return m


@dataclass(frozen=True)
class RestoreDownloadDependencies:
    check_space: Callable
    ensure_private_directory: Callable
    restore_id: Callable
    run_command: Callable
    restic_environment: Callable
    prepare_manifest: Callable

    @classmethod
    def production(cls):
        return cls(
            check_space=check_restore_space,
            ensure_private_directory=ensure_private_directory,
            restore_id=lambda: (
                time.strftime('%Y%m%d-%H%M%S')
                + f'-{time.time_ns() % 1_000_000_000:09d}'
            ),
            run_command=run,
            restic_environment=restic_env,
            prepare_manifest=prepare_restored_manifest,
        )


class RestoreDownloadWorkflow:
    def __init__(self, dependencies: RestoreDownloadDependencies):
        self.dependencies = dependencies

    def restore(
            self, c, service, snapshot, manifest_policy, *,
            allow_low_space=False,
    ):
        if not valid_service_name(service):
            raise ValueError(
                f'invalid service name from repository: {service!r}'
            )
        deps = self.dependencies
        deps.check_space(
            c, service, snapshot, allow_low_space=allow_low_space,
        )
        restore_root = deps.ensure_private_directory(c['restore_root'])
        service_root = deps.ensure_private_directory(restore_root / service)
        root = service_root / deps.restore_id()
        deps.ensure_private_directory(root)
        deps.run_command([
            'restic', 'restore', snapshot, '--host', c['host_id'],
            '--tag', f'service:{service}', '--target', str(root),
        ], env=deps.restic_environment(c))
        m = deps.prepare_manifest(
            c, service, root, policy=manifest_policy,
        )
        print(f'Restored snapshot for {service_label(m)}: {root}')
        return m, root


def restore_one(
        c, service, snapshot, manifest_policy, *, allow_low_space=False,
        dependencies=None,
):
    workflow = RestoreDownloadWorkflow(
        dependencies or RestoreDownloadDependencies.production(),
    )
    return workflow.restore(
        c, service, snapshot, manifest_policy,
        allow_low_space=allow_low_space,
    )


RESTORE_ID_RE = re.compile(r'[0-9]{8}-[0-9]{6}-[0-9]{9}')


def _parse_restore_target(value):
    target = Path(value)
    if target.is_absolute() or len(target.parts) != 2:
        raise ValueError(
            f'invalid restore target {value!r}; expected SERVICE/RESTORE_ID'
        )
    service, restore_id = target.parts
    if not valid_service_name(service) or RESTORE_ID_RE.fullmatch(restore_id) is None:
        raise ValueError(
            f'invalid restore target {value!r}; expected SERVICE/RESTORE_ID'
        )
    return service, restore_id


def _all_restore_targets(restore_root):
    restore_root = Path(restore_root)
    if not restore_root.exists() and not restore_root.is_symlink():
        return []
    validate_control_root(restore_root)
    targets = []
    for service_root in sorted(restore_root.iterdir()):
        if not valid_service_name(service_root.name):
            raise ValueError(f'invalid service directory in restore root: {service_root}')
        validate_control_root(service_root)
        for target in sorted(service_root.iterdir()):
            if RESTORE_ID_RE.fullmatch(target.name) is None:
                raise ValueError(f'invalid restore directory: {target}')
            validate_control_root(target)
            targets.append((service_root.name, target.name))
    return targets


def cmd_cleanup_restores(c, args):
    if not args.yes:
        die('cleanup-restores requires --yes')
    if args.all == bool(args.targets):
        die('specify restore targets or --all, but not both')

    restore_root = Path(c['restore_root'])
    try:
        targets = (
            _all_restore_targets(restore_root)
            if args.all else list(dict.fromkeys(
                _parse_restore_target(value) for value in args.targets
            ))
        )
    except (OSError, ValueError, RuntimeError) as err:
        die(str(err))

    failures = FailureSummary()
    removed = []
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for service, restore_id in targets:
            label = f'{service}/{restore_id}'
            target = restore_root / service / restore_id
            try:
                validate_control_root(target)
                clear_control_leaf(target)
                removed.append(label)
                print(f'Removed restore: {label}')
            except Exception as err:
                failures.record_exception(
                    label, err,
                    message=f'ERROR: cannot remove restore {label}: {{error}}',
                )

    print(f'Removed {len(removed)} restore artifact(s).')
    failures.raise_if_any('RESTORE CLEANUP FAILURES')


def cmd_restore(c, args):
    if args.start and not args.apply:
        die('--start requires --apply')
    if args.services and args.all:
        die('specify services or --all, but not both')
    available = repository_services(c)
    if not available:
        die(f"no service snapshots found for host {c['host_id']!r}")

    if args.services:
        unknown = sorted(set(args.services) - set(available))
        if unknown:
            die(f'services not found in repository: {", ".join(unknown)}')
        selected = list(dict.fromkeys(args.services))
    elif args.all:
        selected = available
    else:
        selected = select_services_interactively(available)
        if selected is None:
            print('Restore cancelled.')
            return
        if not selected:
            die('no services selected')

    if args.snapshot != 'latest' and len(selected) != 1:
        die('an explicit snapshot ID can restore exactly one service')

    print('Selected services: ' + ', '.join(selected))
    if not args.yes:
        if not sys.stdin.isatty():
            die('non-interactive restore requires --yes')
        if not prompt_yes_no('Continue with restore?', default=False):
            print('Restore cancelled.')
            return
    snapshot = args.snapshot
    if snapshot != 'latest':
        snapshot = resolve_explicit_snapshot(c, selected[0], snapshot)
    if args.apply:
        validate_docker_environment()
        validate_docker_bind_probe(c)
        validate_trusted_roots(c['trusted_data_roots'])

    policy = 'restore' if args.restore_manifest else 'keep' if args.keep_manifest else 'ask'
    restore_options = {}
    if vars(args).get('allow_low_space', False):
        restore_options['allow_low_space'] = True
    restored = []
    failures = FailureSummary()
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for service in selected:
            print(f'\n== Restore: {service} ==')
            try:
                m, root = restore_one(
                    c, service, snapshot, policy, **restore_options,
                )
                label = service_label(m) if isinstance(m, dict) else service
                if args.apply:
                    restore_apply.apply_one(c, m, root, start_services=args.start)
                    try:
                        clear_control_leaf(root)
                    except Exception as cleanup_error:
                        failures.record_exception(
                            f'{label} cleanup', cleanup_error,
                            message=(
                                f'ERROR: {label} was applied, but its temporary '
                                'restore could not be removed: {error}'
                            ),
                        )
                        restored.append((
                            label,
                            f'applied; cleanup failed; retained at {root}',
                        ))
                    else:
                        print(
                            f'Applied restore for {label}; temporary restore removed'
                        )
                        restored.append((
                            label, 'applied; temporary restore removed',
                        ))
                else:
                    restored.append((label, str(root)))
            except Exception as err:
                failures.record_exception(
                    service, err,
                    message=f'ERROR: restore failed for {service}: {{error}}',
                    command_context=f'Restore failed for {service}',
                )

    print('\nRESTORE SUMMARY')
    for service, result in restored:
        print(f'  - {service}: {result}')
    failures.raise_if_any('RESTORE FAILURES')


def _validated_apply_workspace(c, service, value):
    if not valid_service_name(service):
        raise ValueError(f'invalid service name: {service!r}')
    restore_root = lexical_absolute(c['restore_root'])
    candidate = lexical_absolute(value)
    restore_id = candidate.name
    if RESTORE_ID_RE.fullmatch(restore_id) is None:
        raise ValueError(
            f'invalid restore directory {value!r}; expected '
            f'{restore_root / service}/RESTORE_ID'
        )
    expected = restore_root / service / restore_id
    if candidate != expected:
        raise ValueError(
            f'restore directory must be under configured restore root: {expected}'
        )
    validate_control_root(restore_root)
    validate_control_root(expected.parent)
    validate_control_root(candidate)
    return candidate


def cmd_apply(c, args):
    if not args.yes:
        die('apply requires --yes')
    validate_docker_environment()
    validate_docker_bind_probe(c)
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        validate_trusted_roots(c['trusted_data_roots'])
        root = _validated_apply_workspace(c, args.service, args.restore_dir)
        m = manifest(c, args.service)
        restore_apply.apply_one(c, m, root, start_services=args.start)
