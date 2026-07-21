import json
import re
import sys
from pathlib import Path

import yaml

from .common import CommandError, _print_command_failure, run
from .security import (
    lexical_absolute, path_contains, paths_overlap, read_control_text,
    validate_control_directory,
)
from .schedule import validate_schedule
from .types import ServiceManifest


RETENTION_FLAGS = (
    ('keep_last', '--keep-last'),
    ('keep_hourly', '--keep-hourly'),
    ('keep_daily', '--keep-daily'),
    ('keep_weekly', '--keep-weekly'),
    ('keep_monthly', '--keep-monthly'),
    ('keep_yearly', '--keep-yearly'),
)
SERVICE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')
DOCKER_VOLUME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def _load_manifest_yaml(path):
    try:
        return yaml.safe_load(read_control_text(path)) or {}
    except yaml.YAMLError as err:
        raise ValueError(f'{path}: invalid manifest YAML: {err}') from err


def validate_docker_volume_name(value, field='Docker volume name'):
    # Boundary: only Docker-managed named volumes are accepted. Host paths,
    # mount syntax, driver options, and anonymous volumes are out of scope.
    if not isinstance(value, str) or DOCKER_VOLUME_RE.fullmatch(value) is None:
        raise ValueError(f'{field} must be a safe Docker volume name')
    return value


def manifests(c, include_disabled=False, on_error=None) -> list[ServiceManifest]:
    out = []
    services_root = validate_control_directory(c['services_root'])
    for path in sorted(services_root.glob('*/backup.yaml')):
        try:
            m = _load_manifest_yaml(path)
            if not isinstance(m, dict):
                raise ValueError(f'{path}: manifest must be a YAML mapping')
            enabled = m.get('enabled', True)
            if not isinstance(enabled, bool):
                raise ValueError(f'{path}: enabled must be boolean')
        except Exception as err:
            if on_error is None:
                raise
            on_error(path, err)
            continue
        m['_path'] = str(path)
        m['_dir'] = str(path.parent)
        if include_disabled or enabled is not False:
            out.append(m)
    return out


def manifest(c, name) -> ServiceManifest:
    if not valid_service_name(name):
        raise ValueError(f'unknown or disabled service: {name}')
    services_root = lexical_absolute(c['services_root'])
    path = services_root / name / 'backup.yaml'
    if not path_contains(services_root, path):
        raise ValueError(f'unknown or disabled service: {name}')
    try:
        m = _load_manifest_yaml(path)
    except FileNotFoundError as err:
        raise ValueError(f'unknown or disabled service: {name}') from err
    if not isinstance(m, dict):
        raise ValueError(f'{path}: manifest must be a YAML mapping')
    m['_path'] = str(path)
    m['_dir'] = str(path.parent)
    enabled = m.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError(f'{path}: enabled must be boolean')
    if m.get('service') != name or enabled is False:
        raise ValueError(f'unknown or disabled service: {name}')
    return m


def compose_cmd(m):
    cmd = ['docker', 'compose']
    for f in m.get('compose', {}).get('files', ['compose.yaml']):
        cmd += ['-f', f]
    return cmd


def valid_service_name(value):
    return isinstance(value, str) and SERVICE_RE.fullmatch(value) is not None


def validate_retention(m):
    path = m.get('_path', '<manifest>')
    retention = m.get('retention')
    if not isinstance(retention, dict) or not retention:
        raise ValueError(f'{path}: retention must be a non-empty mapping')
    allowed = {key for key, _flag in RETENTION_FLAGS}
    unknown = sorted(set(retention) - allowed)
    if unknown:
        raise ValueError(f'{path}: unsupported retention fields: {unknown}')
    found = False
    for key, _ in RETENTION_FLAGS:
        if key in retention:
            value = retention[key]
            if type(value) is not int or value < 0:
                raise ValueError(f'{path}: retention.{key} must be an integer >= 0')
            found = True
    if not found:
        raise ValueError(f'{path}: retention must define at least one keep_* rule')


def _validate_manifest_header(m):
    if not isinstance(m, dict):
        raise ValueError('manifest must be a mapping')
    path = m.get('_path', '<manifest>')
    allowed_manifest = {
        'version', 'service', 'enabled', 'schedule', 'retention',
        'compose', 'consistency', 'sources', '_path', '_dir',
        '_snapshot_manifest', '_restore_manifest_requested',
    }
    unknown_manifest = sorted(set(m) - allowed_manifest)
    if unknown_manifest:
        raise ValueError(f'{path}: unsupported manifest fields: {unknown_manifest}')
    if type(m.get('version')) is not int or m['version'] != 1:
        raise ValueError(f'{path}: version must be 1')
    if not m.get('service'):
        raise ValueError(f'{path}: missing service')
    if not valid_service_name(m['service']):
        raise ValueError(f'{path}: service contains unsupported characters')
    if m.get('_dir') and Path(m['_dir']).name != m['service']:
        raise ValueError(
            f"{path}: manifest directory must be named {m['service']!r}, "
            f"not {Path(m['_dir']).name!r}"
        )
    if not isinstance(m.get('enabled', True), bool):
        raise ValueError(f'{path}: enabled must be boolean')
    return path


def _validate_consistency(m, path):
    consistency = m.get('consistency')
    if consistency is None:
        consistency = {}
    if not isinstance(consistency, dict):
        raise ValueError(f'{path}: consistency must be a mapping')
    allowed_consistency = {'mode', 'timeout', 'before', 'after'}
    unknown_consistency = sorted(set(consistency) - allowed_consistency)
    if unknown_consistency:
        raise ValueError(f'{path}: unsupported consistency fields: {unknown_consistency}')
    mode = consistency.get('mode', 'stop')
    if mode not in ('stop', 'hooks', 'none'):
        raise ValueError(f'{path}: invalid consistency.mode {mode!r}; expected stop, hooks, or none')
    timeout = consistency.get('timeout', 120)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError(f'{path}: consistency.timeout must be an integer greater than zero')
    for hook_name in ('before', 'after'):
        hook_values = consistency.get(hook_name, [])
        if not isinstance(hook_values, list) or not all(
            isinstance(x, str) and x for x in hook_values
        ):
            raise ValueError(
                f'{path}: consistency.{hook_name} must be a list of non-empty strings'
            )
        if mode != 'hooks' and hook_values:
            raise ValueError(
                f'{path}: consistency.{hook_name} is only valid with mode hooks'
            )


def _validate_compose(m, path):
    compose = m.get('compose')
    if compose is None:
        compose = {}
    if not isinstance(compose, dict):
        raise ValueError(f'{path}: compose must be a mapping')
    unknown_compose = sorted(set(compose) - {'files'})
    if unknown_compose:
        raise ValueError(f'{path}: unsupported compose fields: {unknown_compose}')
    compose_files = compose.get('files', ['compose.yaml'])
    if not isinstance(compose_files, list) or not compose_files or not all(
        isinstance(x, str) and x for x in compose_files
    ):
        raise ValueError(f'{path}: compose.files must be a non-empty list of strings')


def _validate_source_common(path, kind, index, source, ids):
    allowed_source = (
        {'id', 'path', 'required', 'exclude'} if kind == 'paths'
        else {'id', 'name', 'compose_volume', 'required', 'exclude'}
    )
    unknown_source = sorted(set(source) - allowed_source)
    if unknown_source:
        raise ValueError(
            f'{path}: unsupported sources.{kind}[{index}] fields: {unknown_source}'
        )
    source_id = source.get('id')
    if not source_id:
        raise ValueError(f'{path}: sources.{kind}[{index}] is missing id')
    if not isinstance(source_id, str) or not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9_.-]*', source_id
    ):
        raise ValueError(
            f'{path}: sources.{kind}[{index}].id must be a safe single path component'
        )
    if source_id in ids:
        raise ValueError(f'{path}: duplicate source id {source_id!r}')
    ids.add(source_id)
    excludes = source.get('exclude', [])
    if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
        raise ValueError(f'{path}: sources.{kind}[{index}].exclude must be a list of strings')
    if not isinstance(source.get('required', True), bool):
        raise ValueError(f'{path}: sources.{kind}[{index}].required must be boolean')


def _validate_path_source(m, path, index, source, path_targets):
    if not isinstance(source.get('path'), str) or not source['path']:
        raise ValueError(f'{path}: sources.paths[{index}].path must be a non-empty string')
    target = lexical_absolute(source_path(m, source))
    if any(paths_overlap(target, existing) for existing in path_targets):
        raise ValueError(f'{path}: duplicate or overlapping path target: {target}')
    path_targets.add(target)


def _validate_volume_source(path, index, source, volume_targets):
    volume_fields = [key for key in ('name', 'compose_volume') if source.get(key)]
    if len(volume_fields) != 1:
        raise ValueError(
            f'{path}: sources.volumes[{index}] must define exactly one of name or compose_volume'
        )
    field_name = volume_fields[0]
    if not isinstance(source[field_name], str):
        raise ValueError(
            f'{path}: sources.volumes[{index}].{field_name} must be a non-empty string'
        )
    if field_name == 'name':
        validate_docker_volume_name(
            source[field_name],
            f'{path}: sources.volumes[{index}].name',
        )
    target = (field_name, source[field_name])
    if target in volume_targets:
        raise ValueError(
            f'{path}: duplicate Docker volume target declaration: {source[field_name]}'
        )
    volume_targets.add(target)


def _validate_sources(m, path):
    sources = m.get('sources')
    if sources is None:
        sources = {}
    if not isinstance(sources, dict):
        raise ValueError(f'{path}: sources must be a mapping')
    unknown_sources = sorted(set(sources) - {'paths', 'volumes'})
    if unknown_sources:
        raise ValueError(f'{path}: unsupported sources fields: {unknown_sources}')
    ids = set()
    path_targets = set()
    volume_targets = set()
    for kind in ('paths', 'volumes'):
        entries = sources.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f'{path}: sources.{kind} must be a list')
        for index, source in enumerate(entries):
            if not isinstance(source, dict):
                raise ValueError(f'{path}: sources.{kind}[{index}] must be a mapping')
            _validate_source_common(path, kind, index, source, ids)
            if kind == 'paths':
                _validate_path_source(m, path, index, source, path_targets)
            else:
                _validate_volume_source(path, index, source, volume_targets)


def validate_manifest(m: ServiceManifest):
    path = _validate_manifest_header(m)
    _validate_consistency(m, path)
    _validate_compose(m, path)
    validate_schedule(m)
    validate_retention(m)
    _validate_sources(m, path)
    return True


def compose_model(m):
    cmd = compose_cmd(m) + ['config', '--format', 'json']
    try:
        result = run(cmd, cwd=m['_dir'], capture=True)
    except CommandError as err:
        _print_command_failure(err, context=(
            f"Compose configuration validation failed for service "
            f"'{m.get('service', '<unknown>')}' ({m['_path']})"
        ))
        print('  diagnostic hints:', file=sys.stderr)
        print('    - Check that every compose.files entry exists.', file=sys.stderr)
        print('    - Check required variables in .env and ${VAR:?message} expressions.', file=sys.stderr)
        print('    - Run the printed docker compose command from the shown directory.', file=sys.stderr)
        raise
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(f'invalid JSON from docker compose config: {err}') from err


def actual_volume_name(m, item, model=None):
    if item.get('name'):
        return validate_docker_volume_name(item['name'])
    key = item.get('compose_volume')
    if not key:
        raise ValueError(f"volume {item.get('id')} needs name or compose_volume")
    if model is None:
        model = compose_model(m)
    volume = (model.get('volumes') or {}).get(key)
    if not volume:
        raise ValueError(f'compose volume key not found: {key}')
    return validate_docker_volume_name(volume.get('name') or key)


def source_path(m, source):
    path = Path(source['path'])
    return path if path.is_absolute() else Path(m['_dir']) / path
