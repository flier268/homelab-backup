import curses
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common import GlobalLock, die, load_yaml, restic_env, run
from .config import (
    compose_cmd, compose_model, manifest, source_path, valid_service_name,
    validate_docker_volume_name, validate_manifest,
)
from .security import (
    atomic_copy_file, clear_control_leaf, ensure_control_parent,
    ensure_private_directory,
    lexical_absolute, paths_overlap, validate_control_directory,
    validate_managed_leaf, validate_payload, validate_trusted_roots,
)
from .storage import (
    compose_identity, create_restore_volume, docker_mount_conflicts,
    docker_project_containers, docker_volume_exists, resolved_volume_sources,
    rsync, running_services, sync_volumes, validate_volume_identity,
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
    if inventory.get('version') != 1:
        raise RuntimeError('restore inventory version must be 1')
    service = inventory.get('service')
    if service != m['service']:
        raise RuntimeError(
            f"restore inventory belongs to service {service!r}, expected {m['service']!r}"
        )
    path_entries = inventory.get('paths')
    volume_entries = inventory.get('volumes')
    identity = inventory.get('compose')
    if not isinstance(path_entries, list) or not isinstance(volume_entries, list):
        raise RuntimeError('restore inventory paths and volumes must be lists')
    if not isinstance(identity, dict) or not isinstance(identity.get('project_name'), str):
        raise RuntimeError('restore inventory has no valid Compose identity')
    identity_services = identity.get('services')
    if not isinstance(identity_services, list) or \
            not all(isinstance(item, str) and item for item in identity_services) or \
            identity_services != sorted(set(identity_services)):
        raise RuntimeError('restore inventory Compose service list is invalid')
    declared_paths = {item['id']: item for item in (m.get('sources') or {}).get('paths', [])}
    inventory_paths = {item.get('id'): item for item in path_entries}
    declared_volumes = {item['id']: item for item in (m.get('sources') or {}).get('volumes', [])}
    inventory_volumes = {item.get('id'): item for item in volume_entries}
    if len(inventory_paths) != len(path_entries) or len(inventory_volumes) != len(volume_entries):
        raise RuntimeError('restore inventory contains duplicate source IDs')
    if set(declared_paths) != set(inventory_paths):
        raise RuntimeError('restore inventory path source IDs do not match manifest')
    if set(declared_volumes) != set(inventory_volumes):
        raise RuntimeError('restore inventory volume source IDs do not match manifest')
    for source_id, source in declared_paths.items():
        entry = inventory_paths[source_id]
        if entry.get('path') != source['path']:
            raise RuntimeError(f'restore inventory path differs for source {source_id!r}')
        if type(entry.get('present')) is not bool:
            raise RuntimeError(f'restore inventory path present flag is invalid: {source_id!r}')
        if entry['present'] and entry.get('type') not in ('file', 'directory', 'symlink'):
            raise RuntimeError(f'restore inventory path type is invalid: {source_id!r}')
        if not entry['present'] and entry.get('type') is not None:
            raise RuntimeError(f'absent restore inventory path has a type: {source_id!r}')
        if source.get('required', True) and not entry['present']:
            raise RuntimeError(f'required path is marked absent: {source_id!r}')
    identity_volumes = identity.get('volumes')
    if not isinstance(identity_volumes, list):
        raise RuntimeError('restore inventory Compose volume mapping must be a list')
    by_identity_id = {entry.get('id'): entry for entry in identity_volumes}
    if len(by_identity_id) != len(identity_volumes) or set(by_identity_id) != set(declared_volumes):
        raise RuntimeError('restore inventory Compose volume mapping differs from sources')
    for source_id, source in declared_volumes.items():
        entry = inventory_volumes[source_id]
        if type(entry.get('present')) is not bool:
            raise RuntimeError(f'restore inventory volume present flag is invalid: {source_id!r}')
        if source.get('required', True) and not entry['present']:
            raise RuntimeError(f'required volume is marked absent: {source_id!r}')
        actual_name = validate_docker_volume_name(
            entry.get('actual_name'), f'inventory volume {source_id!r} actual_name',
        )
        identity_entry = by_identity_id[source_id]
        if entry.get('name') != source.get('name') or \
                entry.get('compose_volume') != source.get('compose_volume'):
            raise RuntimeError(f'restore inventory volume declaration differs: {source_id!r}')
        if identity_entry.get('actual_name') != actual_name or \
                identity_entry.get('logical_name') != source.get('compose_volume'):
            raise RuntimeError(f'restore inventory volume identity differs: {source_id!r}')
    expected_compose_files = list((m.get('compose') or {}).get('files', ['compose.yaml']))
    if identity.get('compose_files') != expected_compose_files:
        raise RuntimeError('restore inventory Compose file list differs from manifest')


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
    if entry.get('present', True) is False:
        return 'missing', staged, target
    source_type = entry.get('type')
    if source_type in ('file', 'symlink'):
        archived_path = entry.get('path') or source['path']
        return source_type, staged / Path(archived_path).name, target
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
        if source_type == 'missing':
            continue
        exists = (
            restored.is_file() and not restored.is_symlink()
            if source_type == 'file'
            else restored.is_dir() and not restored.is_symlink()
            if source_type == 'directory'
            else restored.is_symlink()
            if source_type == 'symlink'
            else False
        )
        if not exists:
            raise RuntimeError(f'restored {source_type} artifact is missing: {restored}')
        if exists:
            validate_payload(restored)
    volume_entries = {item['id']: item for item in inventory.get('volumes', [])}
    for source in (m.get('sources') or {}).get('volumes', []):
        if not volume_entries[source['id']].get('present', True):
            continue
        restored = Path(root) / 'volumes' / source['id']
        if not restored.is_dir() or restored.is_symlink():
            raise RuntimeError(
                f'restored volume artifact is missing or not a real directory: {restored}'
            )
        if restored.is_dir() and not restored.is_symlink():
            validate_payload(restored)


def normalize_restore_target(target, source_type):
    target = Path(target)
    if target.is_symlink():
        clear_control_leaf(target)
        return
    if not target.exists():
        return
    if source_type in ('file', 'symlink') and target.is_dir():
        clear_control_leaf(target)
    elif source_type == 'directory' and target.is_file():
        clear_control_leaf(target)
    elif not (target.is_file() or target.is_dir()):
        raise RuntimeError(f'unsupported live target type: {target}')


def restore_path_source(m, root, source, inventory, *, c=None, rebuild=False):
    source_type, restored, target = restored_path_details(m, root, source, inventory)
    if source_type == 'missing':
        if source.get('required', True):
            raise RuntimeError(f'restored path source is missing: {restored}')
        return
    if source_type == 'file' and (not restored.is_file() or restored.is_symlink()):
        raise RuntimeError(f'restored file artifact is missing: {restored}')
    if source_type == 'symlink' and not restored.is_symlink():
        raise RuntimeError(f'restored symlink artifact is missing: {restored}')
    if source_type == 'directory' and (not restored.is_dir() or restored.is_symlink()):
        raise RuntimeError(f'restored directory artifact is missing: {restored}')
    direct_library_call = c is None
    c = c or {'trusted_data_roots': [str(Path(target).parent)]}
    validate_managed_leaf(
        target, c['trusted_data_roots'],
        allow_missing=rebuild or direct_library_call,
    )
    validate_payload(restored)
    if rebuild:
        ensure_control_parent(target.parent, c['trusted_data_roots'])
        validate_control_directory(target.parent)
    if source_type == 'file':
        restored_file = restored
        if not restored_file.is_file() or restored_file.is_symlink():
            raise RuntimeError(f'restored file artifact is missing: {restored_file}')
        normalize_restore_target(target, source_type)
        run(['rsync', '-aHAX', '--numeric-ids', str(restored_file), str(target)])
        return

    if source_type == 'symlink':
        if not restored.is_symlink():
            raise RuntimeError(f'restored symlink artifact is missing: {restored}')
        normalize_restore_target(target, source_type)
        run(['rsync', '-aHAX', '--numeric-ids', str(restored), str(target)])
        return

    if restored.is_dir() and not restored.is_symlink():
        normalize_restore_target(target, source_type)
        rsync(restored, target, source.get('exclude'))
    else:
        raise RuntimeError(f'restored directory artifact is missing: {restored}')


def validate_restore_path_separation(m, root, inventory):
    root = lexical_absolute(root)
    path_sources = (m.get('sources') or {}).get('paths', [])
    staged_sources = [
        lexical_absolute(restored_path_details(m, root, source, inventory)[1])
        for source in path_sources
    ]
    for source in path_sources:
        target = lexical_absolute(source_path(m, source))
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
    manifest: dict
    mode: str
    volumes: tuple
    all_volume_names: tuple
    running_services: tuple
    deferred_sources: tuple
    project_name: str


def _compose_targets(m):
    return tuple(
        lexical_absolute(Path(m['_dir']) / item)
        for item in m.get('compose', {}).get('files', ['compose.yaml'])
    )


def _inventory_volumes(m, inventory):
    sources = {item['id']: item for item in (m.get('sources') or {}).get('volumes', [])}
    values = []
    for entry in inventory.get('volumes', []):
        source = sources[entry['id']]
        name = entry.get('actual_name')
        if not isinstance(name, str):
            raise RuntimeError(f'inventory volume has no actual_name: {entry["id"]}')
        values.append((source, name, bool(entry.get('present', True))))
    return tuple(values)


def _deferred_compose_sources(m):
    targets = set(_compose_targets(m))
    sources = []
    for source in (m.get('sources') or {}).get('paths', []):
        if lexical_absolute(source_path(m, source)) in targets:
            sources.append(source['id'])
    return tuple(sources)


def _restore_authorization_projection(m):
    sources = m.get('sources') or {}
    consistency = m.get('consistency') or {}
    return {
        'service': m.get('service'),
        'compose_files': list((m.get('compose') or {}).get('files', ['compose.yaml'])),
        'consistency': {
            'mode': consistency.get('mode', 'stop'),
        },
        'paths': [
            {
                key: source.get(key)
                for key in ('id', 'path', 'required', 'exclude')
            }
            for source in sources.get('paths', [])
        ],
        'volumes': [
            {
                key: source.get(key)
                for key in ('id', 'name', 'compose_volume', 'required', 'exclude')
            }
            for source in sources.get('volumes', [])
        ],
    }


def _compose_authorization_projection(identity):
    return {
        'project_name': identity.get('project_name'),
        'compose_files': identity.get('compose_files'),
        'volumes': identity.get('volumes'),
    }


def prepare_restore_plan(c, m, root):
    validate_manifest(m)
    root = Path(root)
    if not root.is_dir():
        die(f'restore directory does not exist: {root}')

    inventory = load_restore_inventory(root)
    validate_restore_inventory(m, inventory)
    validate_restore_sources(m, root, inventory)
    validate_restore_path_separation(m, root, inventory)

    manifest_target = Path(m.get('_path', Path(m['_dir']) / 'backup.yaml'))
    compose_targets = _compose_targets(m)
    manifest_exists = manifest_target.is_file() and not manifest_target.is_symlink()
    compose_exists = [path.is_file() and not path.is_symlink() for path in compose_targets]
    if manifest_exists and all(compose_exists):
        mode = 'existing'
    elif not manifest_target.exists() and not manifest_target.is_symlink() \
            and not any(path.exists() or path.is_symlink() for path in compose_targets):
        mode = 'rebuild'
    else:
        raise RuntimeError('mixed local manifest/Compose state is not supported')

    deferred = _deferred_compose_sources(m)
    if mode == 'rebuild' and len(deferred) != len(compose_targets):
        raise RuntimeError('every Compose file must be a required snapshot path source')
    source_by_id = {item['id']: item for item in (m.get('sources') or {}).get('paths', [])}
    if mode == 'rebuild' and any(not source_by_id[item].get('required', True) for item in deferred):
        raise RuntimeError('Compose file path sources must be required for rebuild')

    inventory_volumes = _inventory_volumes(m, inventory)
    targets = [source_path(m, source) for source in (m.get('sources') or {}).get('paths', [])]
    for source in (m.get('sources') or {}).get('paths', []):
        entry = next(item for item in inventory['paths'] if item['id'] == source['id'])
        target = source_path(m, source)
        trusted_roots = c.get('trusted_data_roots') or [str(Path(target).parent)]
        validate_managed_leaf(target, trusted_roots, allow_missing=mode == 'rebuild')
        exists = target.exists() or target.is_symlink()
        if not entry['present']:
            if mode == 'rebuild' and exists:
                raise RuntimeError(
                    f'snapshot-absent path already exists during rebuild: {target}'
                )
            continue
        if mode == 'existing' and not exists:
            raise RuntimeError(f'existing deployment target is missing: {target}')
        if mode == 'rebuild' and exists:
            raise RuntimeError(f'rebuild target already exists: {target}')

    if mode == 'existing':
        local_m = manifest(c, m['service']) if c.get('services_root') else m
        validate_manifest(local_m)
        validate_restore_inventory(local_m, inventory)
        if m.get('_restore_manifest_requested') and \
                _restore_authorization_projection(m) != \
                _restore_authorization_projection(local_m):
            raise RuntimeError(
                'snapshot manifest security settings differ from local authorization'
            )
        if c.get('services_root'):
            model = compose_model(local_m)
            resolved = tuple(resolved_volume_sources(local_m, model=model))
            identity = compose_identity(local_m, model=model, resolved=resolved)
        else:
            identity = inventory.get('compose')
            resolved = tuple(
                (source, name) for source, name, _present in inventory_volumes
            )
        if _compose_authorization_projection(identity) != \
                _compose_authorization_projection(inventory.get('compose')):
            raise RuntimeError('snapshot Compose identity does not match local deployment')
        expected = {source['id']: name for source, name in resolved}
        for source, name, present in inventory_volumes:
            if expected.get(source['id']) != name:
                raise RuntimeError(f'inventory volume differs from local deployment: {source["id"]}')
            if present and not docker_volume_exists(name):
                raise RuntimeError(f'existing deployment volume is missing: {name}')
            if present:
                validate_volume_identity(
                    name, project_name=identity['project_name'],
                    logical_name=source.get('compose_volume'),
                )
        running = tuple(running_services(local_m))
        authorized_manifest = local_m
    else:
        identity = inventory.get('compose')
        if not isinstance(identity, dict) or not identity.get('project_name'):
            raise RuntimeError('snapshot has no Compose project identity')
        for _source, name, _present in inventory_volumes:
            if docker_volume_exists(name):
                raise RuntimeError(f'rebuild volume already exists: {name}')
        if docker_project_containers(identity['project_name'], include_stopped=True):
            raise RuntimeError('rebuild Compose project already has containers')
        running = ()
        authorized_manifest = m

    volumes = tuple((source, name) for source, name, present in inventory_volumes if present)
    all_volume_names = tuple(name for _source, name, _present in inventory_volumes)
    conflicts = docker_mount_conflicts(
        targets,
        all_volume_names if mode == 'rebuild' else [name for _source, name in volumes],
        include_stopped=mode == 'rebuild',
        writable_only=False,
    )
    if conflicts and mode == 'rebuild':
        raise RuntimeError(f'rebuild targets are referenced by containers: {", ".join(conflicts)}')
    return RestorePlan(
        root, inventory, authorized_manifest, mode, volumes, all_volume_names, running,
        deferred, identity['project_name'],
    )


def apply_one(c, m, root, *, start_services=False):
    plan = prepare_restore_plan(c, m, root)
    targets = list(plan.running_services)
    if targets:
        run(
            compose_cmd(plan.manifest) + ['stop', '-t', str((plan.manifest.get('consistency') or {}).get('timeout', 120))] + targets,
            cwd=plan.manifest['_dir'],
        )
    mutation_started = False
    changed_targets = []
    try:
        path_targets = [source_path(plan.manifest, source) for source in (plan.manifest.get('sources') or {}).get('paths', [])]
        conflicts = docker_mount_conflicts(
            path_targets,
            plan.all_volume_names if plan.mode == 'rebuild'
            else [name for _source, name in plan.volumes],
            include_stopped=plan.mode == 'rebuild',
            writable_only=False,
        )
        if conflicts:
            raise RuntimeError(f'restore targets are used by containers: {", ".join(conflicts)}')
        remaining = docker_project_containers(
            plan.project_name, include_stopped=plan.mode == 'rebuild',
        )
        if remaining:
            raise RuntimeError(
                'Compose project still has running containers after stop: '
                + ', '.join(remaining)
            )
        for source in (plan.manifest.get('sources') or {}).get('paths', []):
            target = source_path(plan.manifest, source)
            validate_managed_leaf(
                target, c.get('trusted_data_roots') or [str(Path(target).parent)],
                allow_missing=plan.mode == 'rebuild',
            )
            entry = next(
                item for item in plan.inventory['paths'] if item['id'] == source['id']
            )
            exists = target.exists() or target.is_symlink()
            if plan.mode == 'rebuild':
                if exists:
                    raise RuntimeError(f'rebuild target appeared during preflight: {target}')
            elif entry['present'] and not exists:
                raise RuntimeError(f'existing target disappeared during preflight: {target}')
        if plan.mode == 'rebuild':
            for name in plan.all_volume_names:
                if docker_volume_exists(name):
                    raise RuntimeError(f'rebuild volume appeared during preflight: {name}')
        for source, name in plan.volumes:
            exists = docker_volume_exists(name)
            if plan.mode == 'existing':
                if not exists:
                    raise RuntimeError(f'existing volume disappeared during preflight: {name}')
                validate_volume_identity(
                    name, project_name=plan.project_name,
                    logical_name=source.get('compose_volume'),
                )
        if plan.mode == 'rebuild':
            manifest_target = Path(plan.manifest.get(
                '_path', Path(plan.manifest['_dir']) / 'backup.yaml',
            ))
            controls = (manifest_target, *_compose_targets(plan.manifest))
            if any(path.exists() or path.is_symlink() for path in controls):
                raise RuntimeError('rebuild control target appeared during preflight')
        mutation_started = True
        if plan.mode == 'rebuild':
            for source, name in plan.volumes:
                changed_targets.append(f'volume:{name}')
                create_restore_volume(
                    name, service=plan.manifest['service'], source=source,
                    project_name=plan.project_name,
                )
        for source in (plan.manifest.get('sources') or {}).get('paths', []):
            if source['id'] not in plan.deferred_sources:
                changed_targets.append(str(source_path(plan.manifest, source)))
                restore_path_source(
                    plan.manifest, plan.root, source, plan.inventory,
                    c=c, rebuild=plan.mode == 'rebuild',
                )
        changed_targets.extend(f'volume:{name}' for _source, name in plan.volumes)
        sync_volumes(c, plan.manifest, plan.root, restore=True, resolved=plan.volumes)
        for source in (plan.manifest.get('sources') or {}).get('paths', []):
            if source['id'] in plan.deferred_sources:
                source_type, restored, target = restored_path_details(
                    plan.manifest, plan.root, source, plan.inventory,
                )
                if source_type != 'file':
                    raise RuntimeError(f'Compose source must be a regular file: {target}')
                if plan.mode == 'rebuild':
                    ensure_control_parent(target.parent, c['trusted_data_roots'])
                changed_targets.append(str(target))
                atomic_copy_file(restored, target, require_absent=plan.mode == 'rebuild')
        if m.get('_restore_manifest_requested'):
            target = Path(m['_path'])
            if plan.mode == 'rebuild':
                ensure_control_parent(target.parent, c['trusted_data_roots'])
            changed_targets.append(str(target))
            atomic_copy_file(
                m['_snapshot_manifest'], target,
                require_absent=plan.mode == 'rebuild',
            )
    except Exception:
        if not mutation_started and targets:
            run(
                compose_cmd(plan.manifest) + ['up', '-d', '--no-deps'] + targets,
                cwd=plan.manifest['_dir'],
            )
        elif mutation_started:
            print(
                'ERROR: restore failed after live mutation; services remain stopped',
                file=sys.stderr,
            )
            for target in dict.fromkeys(changed_targets):
                print(f'  - possibly modified: {target}', file=sys.stderr)
        raise
    if start_services:
        if not compose_files_exist(plan.manifest):
            die(f"cannot start {plan.manifest['service']}: Compose files were not restored")
        run(compose_cmd(plan.manifest) + ['up', '-d'], cwd=plan.manifest['_dir'])
    elif targets:
        run(compose_cmd(plan.manifest) + ['up', '-d', '--no-deps'] + targets, cwd=plan.manifest['_dir'])


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
