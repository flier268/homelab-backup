import json
import re
import sys
from pathlib import Path

from .common import CommandError, _print_command_failure, die, load_yaml, paths_overlap, resolved_path, run
from .schedule import validate_schedule

CFG = Path('/etc/homelab-backup/config.yaml')
RETENTION_FLAGS = (
    ('keep_last', '--keep-last'),
    ('keep_hourly', '--keep-hourly'),
    ('keep_daily', '--keep-daily'),
    ('keep_weekly', '--keep-weekly'),
    ('keep_monthly', '--keep-monthly'),
    ('keep_yearly', '--keep-yearly'),
)
SERVICE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')

def cfg():
    if not CFG.exists():
        die(f'missing {CFG}')
    c = load_yaml(CFG)
    if not isinstance(c, dict):
        die(f'{CFG} must contain a YAML mapping')
    required = [
        'host_id', 'services_root', 'repository', 'password_file',
        'rclone_config', 'staging_root', 'restore_root', 'cache_root',
        'volume_helper_image', 'state_root', 'lock_file',
    ]
    for key in required:
        if not c.get(key):
            die(f'config missing {key}')
    if c.get('version') != 1:
        die(f'{CFG}: version must be 1')
    roots = {
        'services_root': c['services_root'],
        'staging_root': c['staging_root'],
        'restore_root': c['restore_root'],
    }
    root_names = list(roots)
    for index, left_name in enumerate(root_names):
        for right_name in root_names[index + 1:]:
            if paths_overlap(roots[left_name], roots[right_name]):
                die(f'{left_name} must not overlap {right_name}')
    return c


def manifests(c, include_disabled=False):
    out = []
    for path in sorted(Path(c['services_root']).glob('*/backup.yaml')):
        m = load_yaml(path)
        if not isinstance(m, dict):
            raise ValueError(f'{path}: manifest must be a YAML mapping')
        m['_path'] = str(path)
        m['_dir'] = str(path.parent)
        if include_disabled or m.get('enabled', True):
            out.append(m)
    return out


def manifest(c, name):
    for m in manifests(c):
        if m.get('service') == name:
            return m
    die(f'unknown or disabled service: {name}')


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


def validate_manifest(m):
    if not isinstance(m, dict):
        raise ValueError('manifest must be a mapping')
    path = m.get('_path', '<manifest>')
    allowed_manifest = {
        'version', 'service', 'enabled', 'schedule', 'retention',
        'compose', 'consistency', 'sources', '_path', '_dir',
    }
    unknown_manifest = sorted(set(m) - allowed_manifest)
    if unknown_manifest:
        raise ValueError(f'{path}: unsupported manifest fields: {unknown_manifest}')
    if m.get('version') != 1:
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
    consistency = m.get('consistency')
    if consistency is None:
        consistency = {}
    if not isinstance(consistency, dict):
        raise ValueError(f'{path}: consistency must be a mapping')
    allowed_consistency = {'mode', 'timeout', 'services', 'before', 'after'}
    unknown_consistency = sorted(set(consistency) - allowed_consistency)
    if unknown_consistency:
        raise ValueError(f'{path}: unsupported consistency fields: {unknown_consistency}')
    mode = consistency.get('mode', 'stop')
    if mode not in ('stop', 'hooks', 'none'):
        raise ValueError(f'{path}: invalid consistency.mode {mode!r}; expected stop, hooks, or none')
    timeout = consistency.get('timeout', 120)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError(f'{path}: consistency.timeout must be an integer greater than zero')
    services = consistency.get('services', [])
    if not isinstance(services, list) or not all(
        isinstance(x, str) and x for x in services
    ):
        raise ValueError(f'{path}: consistency.services must be a list of non-empty strings')
    if len(services) != len(set(services)):
        raise ValueError(f'{path}: consistency.services contains duplicates')
    if mode != 'stop' and services:
        raise ValueError(f'{path}: consistency.services is only valid with mode stop')
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
    validate_schedule(m)
    validate_retention(m)
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
            if kind == 'paths':
                if not isinstance(source.get('path'), str) or not source['path']:
                    raise ValueError(f'{path}: sources.paths[{index}].path must be a non-empty string')
                target = resolved_path(source_path(m, source))
                if any(paths_overlap(target, existing) for existing in path_targets):
                    raise ValueError(f'{path}: duplicate or overlapping path target: {target}')
                path_targets.add(target)
            else:
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
                target = (field_name, source[field_name])
                if target in volume_targets:
                    raise ValueError(
                        f'{path}: duplicate Docker volume target declaration: {source[field_name]}'
                    )
                volume_targets.add(target)
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
        return item['name']
    key = item.get('compose_volume')
    if not key:
        raise ValueError(f"volume {item.get('id')} needs name or compose_volume")
    if model is None:
        model = compose_model(m)
    volume = (model.get('volumes') or {}).get(key)
    if not volume:
        raise ValueError(f'compose volume key not found: {key}')
    return volume.get('name') or key


def source_path(m, source):
    path = Path(source['path'])
    return path if path.is_absolute() else Path(m['_dir']) / path
