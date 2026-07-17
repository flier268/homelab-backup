import curses
import json
import sys
import time
from pathlib import Path

from .common import GlobalLock, die, load_yaml, restic_env, run
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
from .security import ensure_private_directory, validate_trusted_roots
from .storage import (
    compose_model, docker_mount_conflicts, docker_project_containers,
    docker_volume_exists, rsync, running_services, sync_volumes,
    validate_docker_bind_probe, validate_docker_environment,
)


def repository_services(c):
    """Return service names found in Restic snapshot tags for this host."""
    result = run(
        ['restic', 'snapshots', '--json', '--host', c['host_id']],
        env=restic_env(c), capture=True,
    )
    try:
        snapshots = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as err:
        raise RuntimeError(f'invalid JSON from restic snapshots: {err}') from err
    services = set()
    for snapshot in snapshots:
        for tag in snapshot.get('tags') or []:
            if isinstance(tag, str) and tag.startswith('service:') and len(tag) > 8:
                service = tag[8:]
                if not valid_service_name(service):
                    raise RuntimeError(f'invalid service tag in repository: {tag!r}')
                services.add(service)
    return sorted(services)


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
    if not source.is_file():
        die(f'restored snapshot does not contain {source}; cannot reconstruct backup manifest')
    if not valid_service_name(service):
        die(f'invalid service name from repository: {service!r}')
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
    m = load_yaml(manifest_source)
    if not isinstance(m, dict):
        raise ValueError(f'{manifest_source}: manifest must be a mapping')
    m['_path'] = str(target)
    m['_dir'] = str(service_dir)
    if m.get('service') != service:
        die(f'{target}: service is {m.get("service")!r}, expected {service!r}')
    validate_manifest(m)
    m['_snapshot_manifest'] = str(source)
    m['_restore_manifest_requested'] = restore_it
    if not restore_it:
        print(f'Keeping local manifest: {target}')
    return m


def restore_one(c, service, snapshot, manifest_policy):
    if not valid_service_name(service):
        die(f'invalid service name from repository: {service!r}')
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


def cmd_restore(c, args):
    if args.start and not args.apply:
        die('--start requires --apply')
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

    print('Selected services: ' + ', '.join(selected))
    if not args.yes:
        if not sys.stdin.isatty():
            die('non-interactive restore requires --yes')
        if not prompt_yes_no('Continue with restore?', default=False):
            print('Restore cancelled.')
            return
    if args.apply:
        validate_docker_environment()
        validate_docker_bind_probe(c)
        validate_trusted_roots(c['trusted_data_roots'])

    policy = 'restore' if args.restore_manifest else 'keep' if args.keep_manifest else 'ask'
    restored = []
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for service in selected:
            print(f'\n== Restore: {service} ==')
            m, root = restore_one(c, service, args.snapshot, policy)
            if args.apply:
                apply_one(c, m, root, start_services=args.start)
                print(f'Applied restore for {service}')
            restored.append((service, root))

    print('\nRESTORE SUMMARY')
    for service, root in restored:
        print(f'  - {service}: {root}')


def cmd_apply(c, args):
    if not args.yes:
        die('apply requires --yes')
    validate_docker_environment()
    validate_docker_bind_probe(c)
    validate_trusted_roots(c['trusted_data_roots'])
    m = manifest(c, args.service)
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        apply_one(c, m, Path(args.restore_dir), start_services=args.start)
