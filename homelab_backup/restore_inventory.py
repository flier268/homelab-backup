import json
from pathlib import Path

from .manifest import source_path, validate_docker_volume_name
from .security import (
    lexical_absolute, paths_overlap, read_control_text, validate_payload,
)
from .types import RestoreInventory, ServiceManifest


def load_restore_inventory(root) -> RestoreInventory:
    path = Path(root) / '_meta' / 'inventory.json'
    try:
        data = json.loads(read_control_text(path, require_protected=False))
    except FileNotFoundError as err:
        raise RuntimeError(f'restore inventory is missing: {path}') from err
    except (OSError, ValueError, json.JSONDecodeError) as err:
        raise RuntimeError(f'cannot read restore inventory {path}: {err}') from err
    if not isinstance(data, dict):
        raise RuntimeError(f'restore inventory must be a JSON object: {path}')
    return data


def validate_restore_inventory(
        m: ServiceManifest, inventory: RestoreInventory,
):
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


def restored_path_details(
        m: ServiceManifest, root, source, inventory: RestoreInventory,
):
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


def validate_restore_sources(
        m: ServiceManifest, root, inventory: RestoreInventory,
):
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


def validate_restore_path_separation(
        m: ServiceManifest, root, inventory: RestoreInventory,
):
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
