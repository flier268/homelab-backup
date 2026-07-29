from dataclasses import dataclass
from pathlib import Path

import yaml

from .inventory_models import ComposeIdentityModel, RestoreInventoryModel
from .manifest import (
    compose_control_paths, compose_file_paths, compose_model, manifest,
    requires_docker, source_path, validate_manifest,
)
from .restore_inventory import (
    load_restore_inventory, restored_path_details, validate_restore_inventory,
    validate_restore_path_separation, validate_restore_sources,
)
from .security import (
    lexical_absolute, read_control_text, validate_data_parent, validate_data_path,
)
from .storage import (
    compose_identity, docker_mount_conflicts, docker_project_containers,
    docker_volume_exists, resolved_volume_sources, running_services,
    validate_path_source_policy, validate_volume_identity,
)
from .types import GlobalConfig, ServiceManifest, VolumeSource


@dataclass(frozen=True)
class RestorePlan:
    root: Path
    inventory: RestoreInventoryModel
    manifest: ServiceManifest
    mode: str
    volumes: tuple[tuple[VolumeSource, str], ...]
    all_volume_names: tuple[str, ...]
    running_services: tuple[str, ...]
    deferred_sources: tuple[str, ...]
    project_name: str


def compose_targets(m):
    return compose_file_paths(m)


def inventory_volumes(m, inventory):
    sources = {item['id']: item for item in (m.get('sources') or {}).get('volumes', [])}
    values = []
    for entry in inventory.volumes:
        source = sources[entry.id]
        values.append((source, entry.actual_name, entry.present))
    return tuple(values)


def deferred_compose_sources(m):
    targets = set(compose_control_paths(m))
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
                for key in ('id', 'path', 'required', 'include', 'exclude')
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


def restore_filter_projection(m):
    return {
        source['id']: {
            'include': list(source.get('include') or []),
            'exclude': list(source.get('exclude') or []),
        }
        for source in (m.get('sources') or {}).get('paths', [])
    }


def _snapshot_manifest(m, root=None):
    snapshot_path = m.get('_snapshot_manifest')
    if not snapshot_path and root is not None:
        snapshot_path = Path(root) / '_meta' / 'backup.yaml'
    if not snapshot_path:
        return m
    try:
        snapshot = yaml.safe_load(
            read_control_text(snapshot_path, require_protected=False)
        ) or {}
    except FileNotFoundError as err:
        raise RuntimeError(
            f'snapshot manifest is missing: {snapshot_path}'
        ) from err
    except yaml.YAMLError as err:
        raise RuntimeError(
            f'snapshot manifest is invalid: {snapshot_path}: {err}'
        ) from err
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f'snapshot manifest must be a mapping: {snapshot_path}'
        )
    snapshot['_path'] = str(snapshot_path)
    snapshot['_dir'] = m['_dir']
    snapshot['_relative_dir'] = m.get('_relative_dir', Path(m['_dir']).name)
    validate_manifest(snapshot)
    if snapshot.get('service') != m.get('service'):
        raise RuntimeError(
            f'snapshot manifest belongs to {snapshot.get("service")!r}, '
            f'expected {m.get("service")!r}'
        )
    return snapshot


def compose_authorization_projection(identity):
    if isinstance(identity, ComposeIdentityModel):
        identity = identity.model_dump(mode='json')
    return {
        'project_name': identity.get('project_name'),
        'compose_files': identity.get('compose_files'),
        'volumes': identity.get('volumes'),
    }


def _load_and_validate_restore_input(m, root):
    validate_manifest(m)
    root = Path(root)
    if not root.is_dir():
        raise RuntimeError(f'restore directory does not exist: {root}')
    inventory = load_restore_inventory(root, expected_service=m['service'])
    validate_restore_inventory(m, inventory)
    validate_restore_sources(m, root, inventory)
    validate_restore_path_separation(m, root, inventory)
    return root, inventory


def _deployment_mode(m, inventory, *, require_local_manifest=True):
    manifest_target = Path(m.get('_path', Path(m['_dir']) / 'backup.yaml'))
    if require_local_manifest and (
        not manifest_target.is_file() or manifest_target.is_symlink()
    ):
        raise RuntimeError(
            'apply requires a protected local manifest; download the restore, '
            'review and install _meta/backup.yaml, run backupctl validate, '
            'then run backupctl apply'
        )
    if not requires_docker(m):
        if not require_local_manifest and not manifest_target.exists() \
                and not manifest_target.is_symlink():
            return 'rebuild', ()
        target_states = []
        sources = {
            item['id']: item
            for item in (m.get('sources') or {}).get('paths', [])
        }
        for entry in inventory.paths:
            if not entry.present:
                continue
            target = source_path(m, sources[entry.id])
            target_states.append(target.exists() or target.is_symlink())
        if not target_states or all(target_states):
            return 'existing', ()
        if not any(target_states):
            return 'rebuild', ()
        raise RuntimeError('mixed existing/missing path target state is not supported')
    compose_file_targets = compose_targets(m)
    compose_controls = compose_control_paths(m)
    manifest_exists = (
        manifest_target.is_file() and not manifest_target.is_symlink()
    )
    compose_exists = [
        path.is_file() and not path.is_symlink()
        for path in compose_controls
    ]
    if not require_local_manifest:
        if manifest_exists and all(compose_exists):
            return 'existing', compose_file_targets
        if not manifest_target.exists() and not manifest_target.is_symlink() \
                and not any(
                    path.exists() or path.is_symlink()
                    for path in compose_controls
                ):
            return 'rebuild', compose_file_targets
        raise RuntimeError('mixed local manifest/Compose state is not supported')
    if all(compose_exists):
        return 'existing', compose_file_targets
    if not any(path.exists() or path.is_symlink() for path in compose_file_targets):
        return 'rebuild', compose_file_targets
    raise RuntimeError('mixed local manifest/Compose state is not supported')


def _validate_rebuild_compose_sources(m, mode, _compose_file_targets):
    deferred = deferred_compose_sources(m)
    source_by_id = {item['id']: item for item in (m.get('sources') or {}).get('paths', [])}
    source_targets = {
        lexical_absolute(source_path(m, source))
        for source in source_by_id.values()
    }
    if mode == 'rebuild' and any(
            target not in source_targets for target in compose_control_paths(m)
    ):
        raise RuntimeError(
            'every Compose file and env file must be a required '
            'snapshot path source'
        )
    if mode == 'rebuild' and any(not source_by_id[item].get('required', True) for item in deferred):
        raise RuntimeError(
            'Compose file and env file path sources must be required for rebuild'
        )
    return deferred


def _validate_path_targets(c, m, inventory, mode):
    targets = [
        validate_path_source_policy(c, m, source)
        for source in (m.get('sources') or {}).get('paths', [])
    ]
    for source in (m.get('sources') or {}).get('paths', []):
        entry = inventory.paths_by_id[source['id']]
        target = validate_path_source_policy(c, m, source)
        if mode == 'rebuild':
            validate_data_parent(target, allow_missing=True)
        else:
            validate_data_path(target)
        exists = target.exists() or target.is_symlink()
        if not entry.present:
            if mode == 'rebuild' and exists:
                raise RuntimeError(
                    f'snapshot-absent path already exists during rebuild: {target}'
                )
            continue
        if mode == 'existing' and not exists:
            raise RuntimeError(f'existing deployment target is missing: {target}')
        if mode == 'rebuild' and exists:
            raise RuntimeError(f'rebuild target already exists: {target}')
    return targets


def _authorize_existing_restore(c, m, inventory, restored_volumes):
    local_m = manifest(c, m['service']) if c.get('services_root') else m
    validate_manifest(local_m)
    validate_restore_inventory(local_m, inventory)
    snapshot_m = _snapshot_manifest(m)
    if restore_filter_projection(snapshot_m) != restore_filter_projection(local_m):
        raise RuntimeError(
            'snapshot path filters differ from local authorization'
        )
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
        identity = inventory.compose
        resolved = tuple(
            (source, name) for source, name, _present in restored_volumes
        )
    if compose_authorization_projection(identity) != \
            compose_authorization_projection(inventory.compose):
        raise RuntimeError('snapshot Compose identity does not match local deployment')
    expected = {source['id']: name for source, name in resolved}
    for source, name, present in restored_volumes:
        if expected.get(source['id']) != name:
            raise RuntimeError(f'inventory volume differs from local deployment: {source["id"]}')
        if present and not docker_volume_exists(name):
            raise RuntimeError(f'existing deployment volume is missing: {name}')
        if present:
            validate_volume_identity(
                name, project_name=_identity_project_name(identity),
                logical_name=source.get('compose_volume'),
            )
    return local_m, identity, tuple(running_services(local_m))


def _authorize_rebuild_restore(m, inventory, restored_volumes):
    identity = inventory.compose
    if not identity.project_name:
        raise RuntimeError('snapshot has no Compose project identity')
    for source, name, present in restored_volumes:
        if source.get('compose_volume'):
            # The protected manifest authorizes the logical Compose key. Its
            # actual name and creation options are resolved from the restored
            # Compose model immediately before Docker creates the volume.
            continue
        if docker_volume_exists(name):
            raise RuntimeError(f'rebuild volume already exists: {name}')
    if docker_project_containers(identity.project_name, include_stopped=True):
        raise RuntimeError('rebuild Compose project already has containers')
    return m, identity, ()


def _authorize_existing_files_restore(c, m, inventory):
    local_m = manifest(c, m['service']) if c.get('services_root') else m
    validate_manifest(local_m)
    validate_restore_inventory(local_m, inventory)
    snapshot_m = _snapshot_manifest(m)
    if restore_filter_projection(snapshot_m) != restore_filter_projection(local_m):
        raise RuntimeError(
            'snapshot path filters differ from local authorization'
        )
    if m.get('_restore_manifest_requested') and \
            restore_authorization_projection(m) != \
            restore_authorization_projection(local_m):
        raise RuntimeError(
            'snapshot manifest security settings differ from local authorization'
        )
    return local_m


def _identity_project_name(identity):
    return (
        identity.project_name
        if isinstance(identity, ComposeIdentityModel)
        else identity['project_name']
    )


def _validate_restore_conflicts(targets, mode, volumes, all_volume_names):
    conflicts = docker_mount_conflicts(
        targets,
        all_volume_names if mode == 'rebuild' else [name for _source, name in volumes],
        include_stopped=mode == 'rebuild',
        writable_only=False,
    )
    if conflicts and mode == 'rebuild':
        raise RuntimeError(f'rebuild targets are referenced by containers: {", ".join(conflicts)}')


def prepare_restore_plan(
        c: GlobalConfig, m: ServiceManifest, root,
) -> RestorePlan:
    root, inventory = _load_and_validate_restore_input(m, root)
    authorized = manifest(c, m['service']) if c.get('services_root') else m
    validate_manifest(authorized)
    validate_restore_inventory(authorized, inventory)
    if c.get('services_root'):
        snapshot_m = _snapshot_manifest(m, root)
        if restore_filter_projection(snapshot_m) != \
                restore_filter_projection(authorized):
            raise RuntimeError(
                'snapshot path filters differ from local authorization'
            )
    files_mode = not requires_docker(authorized)
    mode, compose_file_targets = _deployment_mode(
        authorized, inventory,
        require_local_manifest=bool(c.get('services_root')),
    )
    deferred = (
        ()
        if files_mode
        else _validate_rebuild_compose_sources(
            authorized, mode, compose_file_targets,
        )
    )
    restored_volumes = inventory_volumes(authorized, inventory)
    targets = _validate_path_targets(c, authorized, inventory, mode)
    if files_mode:
        authorized_manifest = (
            _authorize_existing_files_restore(c, m, inventory)
            if mode == 'existing'
            else authorized
        )
        return RestorePlan(
            root, inventory, authorized_manifest, mode, (), (), (), (), '',
        )
    if mode == 'existing':
        authorized_manifest, identity, running = _authorize_existing_restore(
            c, m, inventory, restored_volumes,
        )
    else:
        authorized_manifest, identity, running = _authorize_rebuild_restore(
            authorized, inventory, restored_volumes,
        )
    volumes = tuple((source, name) for source, name, present in restored_volumes if present)
    all_volume_names = tuple(name for _source, name, _present in restored_volumes)
    _validate_restore_conflicts(targets, mode, volumes, all_volume_names)
    return RestorePlan(
        root, inventory, authorized_manifest, mode, volumes, all_volume_names, running,
        deferred, _identity_project_name(identity),
    )
