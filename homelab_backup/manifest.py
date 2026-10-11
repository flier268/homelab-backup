import json
import re
import sys
from pathlib import Path

import yaml

from .common import CommandError, _print_command_failure, run
from .identity import validate_identity
from .security import (
    lexical_absolute, path_contains, paths_overlap, read_control_text,
    validate_control_directory, validate_control_file,
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
DISPLAY_NAME_RE = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9_.-]*(?: [A-Za-z0-9][A-Za-z0-9_.-]*)*'
)
DOCKER_VOLUME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


# Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]*
CONTAINER_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}')

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


def valid_display_name(value):
    return (
        isinstance(value, str)
        and DISPLAY_NAME_RE.fullmatch(value) is not None
    )


def service_label(m):
    service = m.get('service', '<unnamed>')
    name = m.get('name', service)
    return service if name == service else f'{name} [{service}]'


def validate_service_relative_directory(value, field='service directory'):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{field} must be a non-empty relative path')
    path = Path(value)
    if path.is_absolute() or path.as_posix() != value or not path.parts:
        raise ValueError(f'{field} must be a normalized relative path')
    if any(not valid_display_name(part) for part in path.parts):
        raise ValueError(f'{field} contains an unsupported path component')
    return path


def _manifest_paths(services_root):
    # pathlib does not recurse through directory symlinks. Each discovered
    # manifest is validated again through its complete protected parent chain.
    return sorted(services_root.rglob('backup.yaml'))


def _load_discovered_manifest(services_root, path):
    relative_dir = path.parent.relative_to(services_root).as_posix()
    validate_service_relative_directory(
        relative_dir, f'{path} parent directory',
    )
    validate_control_directory(path.parent)
    m = _load_manifest_yaml(path)
    if not isinstance(m, dict):
        raise ValueError(f'{path}: manifest must be a YAML mapping')
    enabled = m.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError(f'{path}: enabled must be boolean')
    m['_path'] = str(path)
    m['_dir'] = str(path.parent)
    m['_relative_dir'] = relative_dir
    return m


def _manifest_scan(c):
    loaded = []
    by_service = {}
    invalid = []
    services_root = validate_control_directory(c['services_root'])
    for path in _manifest_paths(services_root):
        try:
            m = _load_discovered_manifest(services_root, path)
        except Exception as err:
            invalid.append((path, err))
            continue
        loaded.append(m)
        service = m.get('service')
        if valid_service_name(service):
            by_service.setdefault(service, []).append(m)
    return loaded, by_service, invalid


def manifests(c, include_disabled=False, on_error=None) -> list[ServiceManifest]:
    loaded, by_service, invalid = _manifest_scan(c)
    if invalid:
        if on_error is None:
            raise invalid[0][1]
        for path, error in invalid:
            on_error(path, error)
    duplicates = {
        service: values
        for service, values in by_service.items()
        if len(values) > 1
    }
    if duplicates:
        for service, values in sorted(duplicates.items()):
            paths = ', '.join(value['_path'] for value in values)
            error = ValueError(
                f'duplicate service ID {service!r}: {paths}'
            )
            if on_error is None:
                raise error
            for value in values:
                on_error(Path(value['_path']), error)
        duplicate_ids = set(duplicates)
        loaded = [
            m for m in loaded if m.get('service') not in duplicate_ids
        ]
    return [
        m for m in loaded
        if include_disabled or m.get('enabled', True) is not False
    ]


def find_manifest(c, name, *, include_disabled=False):
    if not valid_service_name(name):
        return None
    _, by_service, _ = _manifest_scan(c)
    matches = by_service.get(name, [])
    if len(matches) > 1:
        paths = ', '.join(value['_path'] for value in matches)
        raise ValueError(f'duplicate service ID {name!r}: {paths}')
    if not matches:
        return None
    found = matches[0]
    if found.get('enabled', True) is False and not include_disabled:
        return None
    return found


def manifest(c, name) -> ServiceManifest:
    if not valid_service_name(name):
        raise ValueError(f'unknown or disabled service: {name}')
    m = find_manifest(c, name)
    if m is None:
        raise ValueError(f'unknown or disabled service: {name}')
    return m


def _service_control_path(m, value):
    path = Path(value)
    if not path.is_absolute():
        path = Path(m['_dir']) / path
    return lexical_absolute(path)


def manifest_target(m):
    return lexical_absolute(
        m.get('_path', Path(m['_dir']) / 'backup.yaml')
    )


def compose_file_paths(m):
    return tuple(
        _service_control_path(m, value)
        for value in (m.get('compose') or {}).get('files', ['compose.yaml'])
    )


def compose_env_path(m):
    value = (m.get('compose') or {}).get('env_file')
    return _service_control_path(m, value) if value else None


def compose_control_paths(m):
    paths = list(compose_file_paths(m))
    env_path = compose_env_path(m)
    if env_path is not None:
        paths.append(env_path)
    return tuple(paths)


def path_source_control_overlap(m, source):
    """Return a forbidden protected-control overlap, if any.

    Exact Compose control-file sources are intentionally allowed: restore
    defers those sources and publishes them atomically instead of passing
    them through the ordinary data rsync path.
    """
    target = lexical_absolute(source_path(m, source))
    target_manifest = manifest_target(m)
    if paths_overlap(target, target_manifest):
        return 'manifest', target_manifest
    compose_controls = (
        ()
        if (m.get('consistency') or {}).get('mode') == 'files'
        else compose_control_paths(m)
    )
    for control in compose_controls:
        if target != control and paths_overlap(target, control):
            return 'Compose control', control
    return None


def compose_cmd(m):
    service_dir = lexical_absolute(m['_dir'])
    cmd = [
        'docker', 'compose', '--project-directory', str(service_dir),
    ]
    env_path = compose_env_path(m)
    env_path = (
        validate_control_file(env_path)
        if env_path is not None else Path('/dev/null')
    )
    cmd += ['--env-file', str(env_path)]
    for compose_path in compose_file_paths(m):
        compose_path = validate_control_file(compose_path)
        cmd += ['-f', str(compose_path)]
    return cmd


def compose_env():
    """Return a deterministic environment without Compose self-configuration."""
    env = {
        'HOME': '/root',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    }
    return env


def compose_run(m, args, *, runner=None, **kwargs):
    kwargs.setdefault('cwd', m['_dir'])
    kwargs['env'] = compose_env()
    return (runner or run)(compose_cmd(m) + list(args), **kwargs)


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
    if not any(retention.get(key, 0) > 0 for key, _ in RETENTION_FLAGS):
        raise ValueError(
            f'{path}: retention must keep at least one snapshot'
        )


def _validate_manifest_header(m):
    if not isinstance(m, dict):
        raise ValueError('manifest must be a mapping')
    path = m.get('_path', '<manifest>')
    allowed_manifest = {
        'version', 'service', 'name', 'enabled', 'schedule', 'retention',
        'create_snapshot_if_unchanged', 'compose', 'consistency', 'actions',
        'sources', '_path', '_dir',
        '_relative_dir', '_snapshot_manifest', '_restore_manifest_requested',
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
    if 'name' in m and not valid_display_name(m['name']):
        raise ValueError(f'{path}: name contains unsupported characters')
    if '_relative_dir' in m:
        validate_service_relative_directory(
            m['_relative_dir'], f'{path}: service directory',
        )
    if not isinstance(m.get('enabled', True), bool):
        raise ValueError(f'{path}: enabled must be boolean')
    if not isinstance(m.get('create_snapshot_if_unchanged', False), bool):
        raise ValueError(
            f'{path}: create_snapshot_if_unchanged must be boolean'
        )
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
    modes = ('stop', 'hooks', 'external', 'live', 'snapshot', 'files')
    if mode not in modes:
        raise ValueError(
            f'{path}: invalid consistency.mode {mode!r}; expected '
            + ', '.join(modes)
        )
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


def _validate_action_definition(action, field, seen):
    if not isinstance(action, dict):
        raise ValueError(f'{field} must be a mapping')
    unknown_action = sorted(
        set(action) - {
            'name', 'command', 'script', 'timeout', 'required', 'run_as',
            'skip_if_not_running',
        }
    )
    if unknown_action:
        raise ValueError(f'{field} has unsupported fields: {unknown_action}')
    name = action.get('name')
    if not isinstance(name, str) or not name:
        raise ValueError(f'{field}.name must be a non-empty string')
    if name in seen:
        raise ValueError(f'duplicate action name {name!r}')
    seen.add(name)
    validate_identity(action.get('run_as'), f'{field}.run_as')
    command = action.get('command')
    script = action.get('script')
    if (command is None) == (script is None):
        raise ValueError(
            f'{field} must define exactly one of command or script'
        )
    if command is not None and (
            not isinstance(command, list) or not command or not all(
                isinstance(item, str) and item for item in command
            )
    ):
        raise ValueError(
            f'{field}.command must be a non-empty list of non-empty strings'
        )
    if script is not None and (not isinstance(script, str) or not script):
        raise ValueError(f'{field}.script must be a non-empty string')
    timeout = action.get('timeout', 30)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError(
            f'{field}.timeout must be an integer greater than zero'
        )
    if not isinstance(action.get('required', True), bool):
        raise ValueError(f'{field}.required must be boolean')
    if 'skip_if_not_running' in action:
        containers = action['skip_if_not_running']
        if isinstance(containers, str):
            containers = [containers]
        if not isinstance(containers, list) or not containers or not all(
                isinstance(item, str) and CONTAINER_NAME_RE.fullmatch(item)
                for item in containers
        ):
            raise ValueError(
                f'{field}.skip_if_not_running must be a container name or a '
                'non-empty list of container names'
            )


def _validate_actions(m, path):
    actions = m.get('actions')
    if actions is None:
        return
    if not isinstance(actions, dict):
        raise ValueError(f'{path}: actions must be a mapping')
    operations = ('backup', 'restore')
    phases = ('before', 'after', 'finally', 'on_success', 'on_failure')
    legacy_phases = ('before', 'finally', 'on_success', 'on_failure')
    nested = any(key in actions for key in operations)
    legacy = any(key in actions for key in legacy_phases)
    if nested and legacy:
        raise ValueError(
            f'{path}: nested backup/restore actions cannot be mixed with '
            'legacy flat actions'
        )
    allowed = operations if nested else legacy_phases
    unknown = sorted(set(actions) - set(allowed))
    if unknown:
        raise ValueError(f'{path}: unsupported actions fields: {unknown}')
    consistency = m.get('consistency') or {}
    if nested and ('before' in consistency or 'after' in consistency):
        raise ValueError(
            f'{path}: nested actions cannot be mixed with '
            'consistency.before/after hooks'
        )
    seen = set()
    sections = (
        (
            (
                operation,
                actions[operation] if operation in actions else {},
            )
            for operation in operations
        )
        if nested else
        (('', actions),)
    )
    for operation, section in sections:
        if not isinstance(section, dict):
            raise ValueError(f'{path}: actions.{operation} must be a mapping')
        operation_phases = phases if nested else legacy_phases
        unknown_phases = sorted(set(section) - set(operation_phases))
        if unknown_phases:
            prefix = f'actions.{operation}' if operation else 'actions'
            raise ValueError(
                f'{path}: unsupported {prefix} fields: {unknown_phases}'
            )
        for phase in operation_phases:
            entries = section.get(phase, [])
            prefix = f'{operation}.' if operation else ''
            if not isinstance(entries, list):
                raise ValueError(
                    f'{path}: actions.{prefix}{phase} must be a list'
                )
            for index, action in enumerate(entries):
                field = f'{path}: actions.{prefix}{phase}[{index}]'
                if operation == 'restore' and isinstance(action, dict) \
                        and 'skip_if_not_running' in action:
                    # A restore must never silently skip a step.
                    raise ValueError(
                        f'{field}.skip_if_not_running is only supported for '
                        'backup actions'
                    )
                try:
                    _validate_action_definition(action, field, seen)
                except ValueError as err:
                    if str(err).startswith('duplicate action name'):
                        raise ValueError(f'{path}: {err}') from err
                    raise


def _validate_compose(m, path):
    compose = m.get('compose')
    if not requires_docker(m) and 'compose' in m:
        raise ValueError(f'{path}: compose is not supported with consistency.mode files')
    if compose is None:
        compose = {}
    if not isinstance(compose, dict):
        raise ValueError(f'{path}: compose must be a mapping')
    unknown_compose = sorted(set(compose) - {'files', 'env_file'})
    if unknown_compose:
        raise ValueError(f'{path}: unsupported compose fields: {unknown_compose}')
    compose_files = compose.get('files', ['compose.yaml'])
    if not isinstance(compose_files, list) or not compose_files or not all(
        isinstance(x, str) and x for x in compose_files
    ):
        raise ValueError(f'{path}: compose.files must be a non-empty list of strings')
    env_file = compose.get('env_file')
    if env_file is not None and (
        not isinstance(env_file, str) or not env_file
    ):
        raise ValueError(f'{path}: compose.env_file must be a non-empty string')


def _validate_source_common(path, kind, index, source, ids):
    allowed_source = (
        {'id', 'path', 'required', 'include', 'exclude'} if kind == 'paths'
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
    if kind == 'paths' and 'include' in source:
        includes = source['include']
        if not isinstance(includes, list) or not includes or not all(
            isinstance(x, str) and x for x in includes
        ):
            raise ValueError(
                f'{path}: sources.paths[{index}].include must be '
                'a non-empty list of non-empty strings'
            )
    if not isinstance(source.get('required', True), bool):
        raise ValueError(f'{path}: sources.{kind}[{index}].required must be boolean')


def _validate_path_source(m, path, index, source, path_targets):
    if not isinstance(source.get('path'), str) or not source['path']:
        raise ValueError(f'{path}: sources.paths[{index}].path must be a non-empty string')
    target = lexical_absolute(source_path(m, source))
    if any(paths_overlap(target, existing) for existing in path_targets):
        raise ValueError(f'{path}: duplicate or overlapping path target: {target}')
    overlap = path_source_control_overlap(m, source)
    if overlap is not None:
        name, control = overlap
        raise ValueError(
            f'{path}: path source {target} must not overlap '
            f'protected {name}: {control}'
        )
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
        if kind == 'volumes' and entries and not requires_docker(m):
            raise ValueError(
                f'{path}: sources.volumes is not supported with consistency.mode files'
            )
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
    _validate_actions(m, path)
    _validate_compose(m, path)
    validate_schedule(m)
    validate_retention(m)
    _validate_sources(m, path)
    return True


def requires_docker(m: ServiceManifest):
    consistency = m.get('consistency')
    if not isinstance(consistency, dict):
        return True
    return consistency.get('mode', 'stop') != 'files'


def compose_model(m):
    try:
        result = compose_run(m, ['config', '--format', 'json'], capture=True)
    except CommandError as err:
        _print_command_failure(err, context=(
            f"Compose configuration validation failed for service "
            f"'{service_label(m)}' ({m['_path']})"
        ))
        print('  diagnostic hints:', file=sys.stderr)
        print('    - Check that every compose.files entry exists.', file=sys.stderr)
        print(
            '    - Check compose.env_file and ${VAR:?message} expressions.',
            file=sys.stderr,
        )
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
