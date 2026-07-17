from dataclasses import dataclass
from pathlib import Path

from .common import die
from .manifest import (
    compose_model, manifest, source_path, validate_manifest,
)
from .restore_inventory import (
    load_restore_inventory, restored_path_details, validate_restore_inventory,
    validate_restore_path_separation, validate_restore_sources,
)
from .security import lexical_absolute, validate_managed_leaf
from .storage import (
    compose_identity, docker_mount_conflicts, docker_project_containers,
    docker_volume_exists, resolved_volume_sources, running_services,
    validate_volume_identity,
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


def compose_targets(m):
    return tuple(
        lexical_absolute(Path(m['_dir']) / item)
        for item in m.get('compose', {}).get('files', ['compose.yaml'])
    )


def inventory_volumes(m, inventory):
    sources = {item['id']: item for item in (m.get('sources') or {}).get('volumes', [])}
    values = []
    for entry in inventory.get('volumes', []):
        source = sources[entry['id']]
        name = entry.get('actual_name')
        if not isinstance(name, str):
            raise RuntimeError(f'inventory volume has no actual_name: {entry["id"]}')
        values.append((source, name, bool(entry.get('present', True))))
    return tuple(values)


def deferred_compose_sources(m):
    targets = set(compose_targets(m))
    sources = []
    for source in (m.get('sources') or {}).get('paths', []):
        if lexical_absolute(source_path(m, source)) in targets:
            sources.append(source['id'])
    return tuple(sources)


def restore_authorization_projection(m):
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


def compose_authorization_projection(identity):
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
    compose_file_targets = compose_targets(m)
    manifest_exists = manifest_target.is_file() and not manifest_target.is_symlink()
    compose_exists = [path.is_file() and not path.is_symlink() for path in compose_file_targets]
    if manifest_exists and all(compose_exists):
        mode = 'existing'
    elif not manifest_target.exists() and not manifest_target.is_symlink() \
            and not any(path.exists() or path.is_symlink() for path in compose_file_targets):
        mode = 'rebuild'
    else:
        raise RuntimeError('mixed local manifest/Compose state is not supported')

    deferred = deferred_compose_sources(m)
    if mode == 'rebuild' and len(deferred) != len(compose_file_targets):
        raise RuntimeError('every Compose file must be a required snapshot path source')
    source_by_id = {item['id']: item for item in (m.get('sources') or {}).get('paths', [])}
    if mode == 'rebuild' and any(not source_by_id[item].get('required', True) for item in deferred):
        raise RuntimeError('Compose file path sources must be required for rebuild')

    restored_volumes = inventory_volumes(m, inventory)
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
                restore_authorization_projection(m) != \
                restore_authorization_projection(local_m):
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
                (source, name) for source, name, _present in restored_volumes
            )
        if compose_authorization_projection(identity) != \
                compose_authorization_projection(inventory.get('compose')):
            raise RuntimeError('snapshot Compose identity does not match local deployment')
        expected = {source['id']: name for source, name in resolved}
        for source, name, present in restored_volumes:
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
        for _source, name, _present in restored_volumes:
            if docker_volume_exists(name):
                raise RuntimeError(f'rebuild volume already exists: {name}')
        if docker_project_containers(identity['project_name'], include_stopped=True):
            raise RuntimeError('rebuild Compose project already has containers')
        running = ()
        authorized_manifest = m

    volumes = tuple((source, name) for source, name, present in restored_volumes if present)
    all_volume_names = tuple(name for _source, name, _present in restored_volumes)
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
