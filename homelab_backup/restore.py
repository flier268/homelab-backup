import curses
import json
import re
import sys
import time
from pathlib import Path

import yaml

from .common import FailureSummary, GlobalLock, die, restic_env, run
from .manifest import manifest, valid_service_name, validate_manifest
from .restore_apply import (
    RestorePlan, apply_one,
    compose_authorization_projection as _compose_authorization_projection,
    compose_files_exist,
    compose_targets as _compose_targets,
    deferred_compose_sources as _deferred_compose_sources,
    inventory_volumes as _inventory_volumes,
    load_restore_inventory, restored_path_details, validate_restore_inventory,
    validate_restore_path_separation, validate_restore_sources,
    restore_authorization_projection as _restore_authorization_projection,
    normalize_restore_target, prepare_restore_plan, restore_path_source,
)
from .security import (
    clear_control_leaf, ensure_private_directory, lexical_absolute,
    read_control_text, validate_control_root, validate_trusted_roots,
)
from .storage import (
    compose_model, docker_mount_conflicts, docker_project_containers,
    docker_volume_exists, rsync, running_services, sync_volumes,
    validate_docker_bind_probe, validate_docker_environment,
)


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


def prepare_restored_manifest(c, service, root, *, policy='ask'):
    source = restored_manifest_path(root)
    if not valid_service_name(service):
        raise ValueError(f'invalid service name from repository: {service!r}')
    service_dir = Path(c['services_root']) / service
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


def restore_one(c, service, snapshot, manifest_policy):
    if not valid_service_name(service):
        raise ValueError(f'invalid service name from repository: {service!r}')
    restore_root = ensure_private_directory(c['restore_root'])
    service_root = ensure_private_directory(restore_root / service)
    root = service_root / (
        time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns() % 1_000_000_000:09d}'
    )
    ensure_private_directory(root)
    run([
        'restic', 'restore', snapshot, '--host', c['host_id'],
        '--tag', f'service:{service}', '--target', str(root),
    ], env=restic_env(c))
    m = prepare_restored_manifest(c, service, root, policy=manifest_policy)
    print(f'Restored snapshot for {service}: {root}')
    return m, root


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
    restored = []
    failures = FailureSummary()
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for service in selected:
            print(f'\n== Restore: {service} ==')
            try:
                m, root = restore_one(c, service, snapshot, policy)
                if args.apply:
                    apply_one(c, m, root, start_services=args.start)
                    try:
                        clear_control_leaf(root)
                    except Exception as cleanup_error:
                        failures.record_exception(
                            f'{service} cleanup', cleanup_error,
                            message=(
                                f'ERROR: {service} was applied, but its temporary '
                                'restore could not be removed: {error}'
                            ),
                        )
                        restored.append((
                            service,
                            f'applied; cleanup failed; retained at {root}',
                        ))
                    else:
                        print(
                            f'Applied restore for {service}; temporary restore removed'
                        )
                        restored.append((
                            service, 'applied; temporary restore removed',
                        ))
                else:
                    restored.append((service, str(root)))
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
        apply_one(c, m, root, start_services=args.start)
