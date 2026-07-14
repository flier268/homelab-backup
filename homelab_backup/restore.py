import curses
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .common import CommandError, GlobalLock, _print_command_failure, die, load_yaml, paths_overlap, resolved_path, restic_env, run
from .config import compose_cmd, manifest, source_path, valid_service_name, validate_manifest
from .storage import resolved_volume_sources, rsync, running_services, sync_volumes

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
    if restore_it:
        service_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=service_dir, prefix='.backup.yaml.', suffix='.tmp',
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copy2(source, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        print(f'Restored manifest: {target}')
    else:
        print(f'Keeping local manifest: {target}')
    return m


def restore_one(c, service, snapshot, manifest_policy):
    if not valid_service_name(service):
        die(f'invalid service name from repository: {service!r}')
    root = Path(c['restore_root']) / service / time.strftime('%Y%m%d-%H%M%S')
    root.mkdir(parents=True, exist_ok=False)
    run([
        'restic', 'restore', snapshot, '--host', c['host_id'],
        '--tag', f'service:{service}', '--target', str(root),
    ], env=restic_env(c))
    m = prepare_restored_manifest(c, service, root, policy=manifest_policy)
    print(f'Restored snapshot for {service}: {root}')
    return m, root


def compose_files_exist(m):
    return all((Path(m['_dir']) / f).exists() for f in m.get('compose', {}).get('files', ['compose.yaml']))


def load_restore_inventory(root):
    path = Path(root) / '_meta' / 'inventory.json'
    if not path.is_file():
        raise RuntimeError(f'restore inventory is missing: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as err:
        raise RuntimeError(f'cannot read restore inventory {path}: {err}') from err
    if not isinstance(data, dict):
        raise RuntimeError(f'restore inventory must be a JSON object: {path}')
    return data


def validate_restore_inventory(m, inventory):
    service = inventory.get('service')
    if service != m['service']:
        raise RuntimeError(
            f"restore inventory belongs to service {service!r}, expected {m['service']!r}"
        )


def restored_path_details(m, root, source, inventory):
    staged = Path(root) / 'paths' / source['id']
    target = source_path(m, source)
    entry = next(
        (x for x in inventory.get('paths', []) if x.get('id') == source['id']),
        None,
    )
    if entry is None:
        raise RuntimeError(
            f"restore inventory has no path source {source['id']!r}"
        )
    source_type = entry.get('type')
    if source_type == 'file':
        archived_path = entry.get('path') or source['path']
        return 'file', staged / Path(archived_path).name, target
    if source_type != 'directory':
        raise RuntimeError(
            f"restore inventory has invalid type for path source {source['id']!r}: "
            f'{source_type!r}'
        )
    return 'directory', staged, target


def validate_restore_sources(m, root, inventory):
    for source in (m.get('sources') or {}).get('paths', []):
        source_type, restored, _target = restored_path_details(
            m, root, source, inventory,
        )
        exists = restored.is_file() if source_type == 'file' else restored.is_dir()
        if not exists and source.get('required', True):
            raise RuntimeError(f'restored {source_type} source is missing: {restored}')
    for source in (m.get('sources') or {}).get('volumes', []):
        restored = Path(root) / 'volumes' / source['id']
        if not restored.is_dir() and source.get('required', True):
            raise RuntimeError(f'restored volume source is missing: {restored}')


def restore_path_source(m, root, source, inventory):
    source_type, restored, target = restored_path_details(m, root, source, inventory)
    if source_type == 'file':
        restored_file = restored
        if not restored_file.is_file():
            if source.get('required', True):
                raise RuntimeError(f'restored file source is missing: {restored_file}')
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        run(['rsync', '-aHAX', '--numeric-ids', str(restored_file), str(target)])
        return

    if restored.is_dir():
        rsync(restored, target, source.get('exclude'))
    elif source.get('required', True):
        raise RuntimeError(f'restored directory source is missing: {restored}')


def restore_missing_compose_files(m, root, inventory):
    if compose_files_exist(m):
        return
    sources = (m.get('sources') or {}).get('paths', [])
    by_target = {resolved_path(source_path(m, source)): source for source in sources}
    for compose_file in m.get('compose', {}).get('files', ['compose.yaml']):
        target = Path(compose_file)
        target = target if target.is_absolute() else Path(m['_dir']) / target
        if target.exists():
            continue
        source = by_target.get(resolved_path(target))
        if source is None:
            raise RuntimeError(
                f'Compose file is missing and is not restorable from this snapshot: {target}'
            )
        restore_path_source(m, root, source, inventory)
    if not compose_files_exist(m):
        raise RuntimeError('Compose files are still missing after restore preparation')


def validate_restore_path_separation(m, root, inventory):
    root = resolved_path(root)
    path_sources = (m.get('sources') or {}).get('paths', [])
    staged_sources = [
        resolved_path(restored_path_details(m, root, source, inventory)[1])
        for source in path_sources
    ]
    for source in path_sources:
        target = resolved_path(source_path(m, source))
        if paths_overlap(root, target):
            raise ValueError(
                f'restore directory {root} overlaps live path target {target}'
            )
        for staged in staged_sources:
            if paths_overlap(staged, target):
                raise ValueError(
                    f'restored source {staged} overlaps live path target {target}'
                )


@dataclass(frozen=True)
class RestorePlan:
    root: Path
    inventory: dict
    volumes: tuple
    running_services: tuple


def prepare_restore_plan(m, root):
    root = Path(root)
    if not root.is_dir():
        die(f'restore directory does not exist: {root}')

    inventory = load_restore_inventory(root)
    validate_restore_inventory(m, inventory)
    validate_restore_sources(m, root, inventory)
    validate_restore_path_separation(m, root, inventory)

    # Compose metadata may be required to resolve logical volume aliases. This is
    # the only write permitted before the remaining preflight checks complete.
    restore_missing_compose_files(m, root, inventory)
    volumes = tuple(resolved_volume_sources(m))
    try:
        running = tuple(running_services(m))
    except CommandError as err:
        _print_command_failure(
            err,
            context=f"Unable to inspect running Compose services for {m['service']}",
        )
        raise
    return RestorePlan(root, inventory, volumes, running)


def apply_one(c, m, root, *, start_services=False):
    plan = prepare_restore_plan(m, root)
    # Restore may replace an older manifest whose service selection no longer
    # matches the running Compose project. Stop every running project service
    # before writing shared paths or volumes.
    targets = list(plan.running_services)

    if targets:
        run(
            compose_cmd(m) + ['stop', '-t', str((m.get('consistency') or {}).get('timeout', 120))] + targets,
            cwd=m['_dir'],
        )
    # Paths are restored before volumes so compose.yaml is available to resolve logical volume names.
    for source in (m.get('sources') or {}).get('paths', []):
        restore_path_source(m, plan.root, source, plan.inventory)
    sync_volumes(c, m, plan.root, restore=True, resolved=plan.volumes)

    if start_services:
        if not compose_files_exist(m):
            die(f"cannot start {m['service']}: Compose files were not restored")
        run(compose_cmd(m) + ['up', '-d'], cwd=m['_dir'])
    elif targets:
        run(compose_cmd(m) + ['up', '-d'] + targets, cwd=m['_dir'])


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
    m = manifest(c, args.service)
    if not args.yes:
        die('apply requires --yes')
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        apply_one(c, m, Path(args.restore_dir), start_services=args.start)
