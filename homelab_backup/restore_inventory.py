import json
from pathlib import Path

from pydantic import ValidationError

from .inventory_models import RestoreInventoryModel
from .manifest import (
    source_path, valid_service_name, validate_docker_volume_name,
    validate_service_relative_directory,
)
from .security import (
    lexical_absolute, paths_overlap, read_control_text, validate_payload,
)
from .types import ServiceManifest


def _model_error(error):
    first = error.errors()[0]
    location = first.get('loc', ())
    message = first.get('msg', 'invalid value')
    if 'capture_method' in location:
        return RuntimeError('restore inventory capture method is invalid')
    if 'ancestors' in location:
        return RuntimeError('restore inventory ancestor is invalid')
    if location == ('version',):
        return RuntimeError('restore inventory version must be 1')
    if location and location[0] == 'consistency':
        return RuntimeError(
            f'restore inventory consistency metadata is invalid: {message}'
        )
    if location and location[0] in ('paths', 'volumes'):
        return RuntimeError(
            f'restore inventory {location[0]} are invalid: {message}'
        )
    return RuntimeError(f'restore inventory is invalid: {message}')


def _as_inventory_model(inventory):
    if isinstance(inventory, RestoreInventoryModel):
        return inventory
    try:
        return RestoreInventoryModel.from_snapshot_data(inventory)
    except (TypeError, ValueError, ValidationError) as err:
        if isinstance(err, ValidationError):
            raise _model_error(err) from err
        raise RuntimeError(f'restore inventory is invalid: {err}') from err


def load_restore_inventory(root, *, expected_service=None) -> RestoreInventoryModel:
    path = Path(root) / '_meta' / 'inventory.json'
    try:
        payload = read_control_text(path, require_protected=False)
    except FileNotFoundError as err:
        raise RuntimeError(f'restore inventory is missing: {path}') from err
    except (OSError, ValueError) as err:
        raise RuntimeError(f'cannot read restore inventory {path}: {err}') from err
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise RuntimeError(f'restore inventory must be a JSON object: {path}')
        if expected_service is not None and data.get('service') != expected_service:
            raise RuntimeError(
                f"restore inventory belongs to service {data.get('service')!r}, "
                f'expected {expected_service!r}'
            )
        return RestoreInventoryModel.model_validate_json(payload)
    except RuntimeError:
        raise
    except json.JSONDecodeError as err:
        raise RuntimeError(f'cannot read restore inventory {path}: {err}') from err
    except ValidationError as err:
        raise RuntimeError(
            f'cannot read restore inventory {path}: {_model_error(err)}'
        ) from err


def restore_inventory_service_directory(inventory, service):
    if not valid_service_name(service):
        raise ValueError(f'invalid service ID: {service!r}')
    if isinstance(inventory, RestoreInventoryModel):
        version = inventory.version
        inventory_service = inventory.service
        value = inventory.service_relative_directory
        has_relative_directory = (
            'service_relative_directory' in inventory.model_fields_set
        )
    elif isinstance(inventory, dict):
        version = inventory.get('version')
        inventory_service = inventory.get('service')
        value = inventory.get('service_relative_directory')
        has_relative_directory = 'service_relative_directory' in inventory
    else:
        raise RuntimeError('restore inventory must be a JSON object')
    if type(version) is not int or version != 1:
        raise RuntimeError('restore inventory version must be 1')
    if inventory_service != service:
        raise RuntimeError(
            f'restore inventory belongs to service {inventory_service!r}, '
            f'expected {service!r}'
        )
    if not has_relative_directory:
        value = service
    return validate_service_relative_directory(
        value, 'restore inventory service directory',
    )


def authorize_inventory_against_manifest(
        m: ServiceManifest, inventory: RestoreInventoryModel,
):
    restore_inventory_service_directory(inventory, m['service'])
    declared_paths = {item['id']: item for item in (m.get('sources') or {}).get('paths', [])}
    declared_volumes = {item['id']: item for item in (m.get('sources') or {}).get('volumes', [])}
    inventory_paths = inventory.paths_by_id
    inventory_volumes = inventory.volumes_by_id
    if set(declared_paths) != set(inventory_paths):
        raise RuntimeError('restore inventory path source IDs do not match manifest')
    if set(declared_volumes) != set(inventory_volumes):
        raise RuntimeError('restore inventory volume source IDs do not match manifest')
    for source_id, source in declared_paths.items():
        entry = inventory_paths[source_id]
        if entry.path != source['path']:
            raise RuntimeError(f'restore inventory path differs for source {source_id!r}')
        if source.get('required', True) and not entry.present:
            raise RuntimeError(f'required path is marked absent: {source_id!r}')
    by_identity_id = inventory.compose_volumes_by_id
    if set(by_identity_id) != set(declared_volumes):
        raise RuntimeError('restore inventory Compose volume mapping differs from sources')
    for source_id, source in declared_volumes.items():
        entry = inventory_volumes[source_id]
        if source.get('required', True) and not entry.present:
            raise RuntimeError(f'required volume is marked absent: {source_id!r}')
        actual_name = validate_docker_volume_name(
            entry.actual_name, f'inventory volume {source_id!r} actual_name',
        )
        identity_entry = by_identity_id[source_id]
        if entry.name != source.get('name') or \
                entry.compose_volume != source.get('compose_volume'):
            raise RuntimeError(f'restore inventory volume declaration differs: {source_id!r}')
        if identity_entry.actual_name != actual_name or \
                identity_entry.logical_name != source.get('compose_volume'):
            raise RuntimeError(f'restore inventory volume identity differs: {source_id!r}')
    expected_compose_files = tuple(
        (m.get('compose') or {}).get('files', ['compose.yaml'])
    )
    if inventory.compose.compose_files != expected_compose_files:
        raise RuntimeError('restore inventory Compose file list differs from manifest')
    return inventory


def validate_restore_inventory(m: ServiceManifest, inventory):
    return authorize_inventory_against_manifest(m, _as_inventory_model(inventory))


def _path_entry(inventory, source_id):
    if isinstance(inventory, RestoreInventoryModel):
        return inventory.paths_by_id.get(source_id)
    return next(
        (
            item for item in inventory.get('paths', [])
            if item.get('id') == source_id
        ),
        None,
    )


def restored_path_details(m: ServiceManifest, root, source, inventory):
    staged = Path(root) / 'paths' / source['id']
    target = source_path(m, source)
    entry = _path_entry(inventory, source['id'])
    if entry is None:
        raise RuntimeError(
            f"restore inventory has no path source {source['id']!r}"
        )
    if isinstance(inventory, RestoreInventoryModel):
        present = entry.present
        source_type = entry.type
        archived_path = entry.path
    else:
        present = entry.get('present', True)
        source_type = entry.get('type')
        archived_path = entry.get('path') or source['path']
    if present is False:
        return 'missing', staged, target
    if source_type in ('file', 'symlink'):
        return source_type, staged / Path(archived_path).name, target
    if source_type != 'directory':
        raise RuntimeError(
            f"restore inventory has invalid type for path source {source['id']!r}: "
            f'{source_type!r}'
        )
    return 'directory', staged, target


def validate_restore_sources(m: ServiceManifest, root, inventory):
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
    if isinstance(inventory, RestoreInventoryModel):
        volume_entries = inventory.volumes_by_id
    else:
        volume_entries = {
            item['id']: item for item in inventory.get('volumes', [])
        }
    for source in (m.get('sources') or {}).get('volumes', []):
        entry = volume_entries[source['id']]
        present = (
            entry.present
            if isinstance(inventory, RestoreInventoryModel)
            else entry.get('present', True)
        )
        if not present:
            continue
        restored = Path(root) / 'volumes' / source['id']
        if not restored.is_dir() or restored.is_symlink():
            raise RuntimeError(
                f'restored volume artifact is missing or not a real directory: {restored}'
            )
        if restored.is_dir() and not restored.is_symlink():
            validate_payload(restored)


def validate_restore_path_separation(m: ServiceManifest, root, inventory):
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
