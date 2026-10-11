import os
import sys
from pathlib import Path

import yaml

from .common import die, format_remediable_error, load_yaml
from .schedule import parse_duration
from .security import paths_overlap
from .types import GlobalConfig

CFG = Path('/etc/homelab-backup/config.yaml')
_TRUSTED_ROOTS_WARNING_SHOWN = False


def _load_config():
    try:
        return load_yaml(CFG, protected=True)
    except FileNotFoundError:
        die(f'missing {CFG}')
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as err:
        die(format_remediable_error(f'cannot securely read {CFG}', err))


def _validate_config_header(c):
    if not isinstance(c, dict):
        die(f'{CFG} must contain a YAML mapping')
    allowed = {
        'version', 'host_id', 'services_root', 'repository', 'password_file',
        'rclone_config', 'staging_root', 'restore_root', 'cache_root',
        'volume_helper_image', 'state_root', 'lock_file', 'trusted_data_roots',
        'rclone', 'check', 'alert',
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


def _discard_deprecated_trusted_roots(c):
    global _TRUSTED_ROOTS_WARNING_SHOWN
    if 'trusted_data_roots' not in c:
        return
    c.pop('trusted_data_roots')
    if not _TRUSTED_ROOTS_WARNING_SHOWN:
        print(
            f'WARNING: {CFG}: trusted_data_roots is deprecated and ignored; '
            'remove it from the configuration',
            file=sys.stderr,
        )
        _TRUSTED_ROOTS_WARNING_SHOWN = True


def _validate_optional_sections(c):
    optional_sections = {
        'rclone': {'bwlimit'},
        'check': {'read_data_subset'},
        'alert': {'max_success_age', 'max_lock_age', 'notify_url_file'},
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
    alert = c.get('alert', {})
    for field in ('max_success_age', 'max_lock_age'):
        if field in alert:
            try:
                parse_duration(alert[field], f'alert.{field}')
            except ValueError as err:
                die(f'{CFG}: {err}')
    if 'notify_url_file' in alert:
        value = alert['notify_url_file']
        if not isinstance(value, str) or not value.startswith('/'):
            die(f'{CFG}: alert.notify_url_file must be an absolute path')


def _validate_root_separation(c):
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
def cfg() -> GlobalConfig:
    c = _load_config()
    _validate_config_header(c)
    release_helper_image = os.environ.get(
        'HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE',
    )
    if release_helper_image:
        c['volume_helper_image'] = release_helper_image
    _discard_deprecated_trusted_roots(c)
    _validate_optional_sections(c)
    _validate_root_separation(c)
    return c


def config_lock_file():
    """Read only enough config to select the lock guarding a full reload."""
    candidate = _load_config()
    _validate_config_header(candidate)
    return candidate['lock_file']
