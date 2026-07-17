from pathlib import Path

from .common import die, load_yaml
from .manifest import (
    DOCKER_VOLUME_RE, RETENTION_FLAGS, SERVICE_RE, actual_volume_name,
    compose_cmd, compose_model, manifest, manifests, source_path,
    valid_service_name, validate_docker_volume_name, validate_manifest,
    validate_retention,
)
from .security import lexical_absolute, paths_overlap
from .types import GlobalConfig

CFG = Path('/etc/homelab-backup/config.yaml')



def _validate_config_header(c):
    if not isinstance(c, dict):
        die(f'{CFG} must contain a YAML mapping')
    allowed = {
        'version', 'host_id', 'services_root', 'repository', 'password_file',
        'rclone_config', 'staging_root', 'restore_root', 'cache_root',
        'volume_helper_image', 'state_root', 'lock_file', 'trusted_data_roots',
        'rclone', 'check',
    }
    unknown = sorted(set(c) - allowed)
    if unknown:
        die(f'{CFG}: unsupported fields: {unknown}')
    required = [
        'host_id', 'services_root', 'repository', 'password_file',
        'rclone_config', 'staging_root', 'restore_root', 'cache_root',
        'volume_helper_image', 'state_root', 'lock_file',
    ]
    for key in required:
        if not isinstance(c.get(key), str) or not c[key]:
            die(f'{CFG}: {key} must be a non-empty string')
    if type(c.get('version')) is not int or c['version'] != 1:
        die(f'{CFG}: version must be 1')


def _normalize_trusted_roots(c):
    trusted_roots = c.get('trusted_data_roots')
    if not isinstance(trusted_roots, list) or not trusted_roots:
        die(f'{CFG}: trusted_data_roots must be a non-empty list')
    try:
        trusted_roots = [lexical_absolute(path) for path in trusted_roots]
    except (TypeError, ValueError) as err:
        die(f'{CFG}: invalid trusted_data_roots: {err}')
    if len(set(trusted_roots)) != len(trusted_roots):
        die(f'{CFG}: trusted_data_roots contains duplicates')
    for index, left in enumerate(trusted_roots):
        if any(paths_overlap(left, right) for right in trusted_roots[index + 1:]):
            die(f'{CFG}: trusted_data_roots must not overlap')
    c['trusted_data_roots'] = [str(path) for path in trusted_roots]
    return trusted_roots


def _validate_optional_sections(c):
    optional_sections = {
        'rclone': {'bwlimit'},
        'check': {'read_data_subset'},
    }
    for section_name, section_fields in optional_sections.items():
        if section_name not in c:
            continue
        section = c[section_name]
        if not isinstance(section, dict):
            die(f'{CFG}: {section_name} must be a mapping')
        unknown_section = sorted(set(section) - section_fields)
        if unknown_section:
            die(f'{CFG}: unsupported {section_name} fields: {unknown_section}')
    if 'bwlimit' in c.get('rclone', {}):
        if not isinstance(c['rclone']['bwlimit'], str):
            die(f'{CFG}: rclone.bwlimit must be a string')
    if 'read_data_subset' in c.get('check', {}):
        subset = c['check']['read_data_subset']
        if not isinstance(subset, str) or not subset:
            die(f'{CFG}: check.read_data_subset must be a non-empty string')


def _validate_root_separation(c, trusted_roots):
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
    protected_paths = {
        'password_file': c['password_file'],
        'rclone_config': c['rclone_config'],
        'cache_root': c['cache_root'],
        'state_root': c['state_root'],
        'lock_file': c['lock_file'],
    }
    for root_name in ('staging_root', 'restore_root'):
        for path_name, path in protected_paths.items():
            if paths_overlap(c[root_name], path):
                die(f'{root_name} must not overlap {path_name}')
    forbidden_data = {
        'staging_root': c['staging_root'], 'restore_root': c['restore_root'],
        **protected_paths,
    }
    for trusted_root in trusted_roots:
        for path_name, path in forbidden_data.items():
            if paths_overlap(trusted_root, path):
                die(f'trusted_data_root {trusted_root} must not overlap {path_name}')


def cfg() -> GlobalConfig:
    if not CFG.exists():
        die(f'missing {CFG}')
    c = load_yaml(CFG)
    _validate_config_header(c)
    trusted_roots = _normalize_trusted_roots(c)
    _validate_optional_sections(c)
    _validate_root_separation(c, trusted_roots)
    return c
