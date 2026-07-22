import json
import os
import re
import secrets
import stat
from pathlib import Path

from .common import run
from .security import (
    atomic_write_json, clear_control_leaf, containing_mount,
    ensure_control_directory, lexical_absolute, open_data_path,
    path_contains, read_control_text, select_trusted_root,
    validate_control_directory,
)


SNAPSHOT_DIRECTORY = '.homelab-backup-snapshots'
STATE_VERSION = 1
UUID_RE = re.compile(r'^[0-9a-fA-F-]{16,}$')
STATE_FILE_RE = re.compile(r'^\.([A-Za-z0-9][A-Za-z0-9_.-]*)\.btrfs-snapshots\.json$')
PHASES = {'creating', 'ready', 'deleting'}


def _state_path(c, service):
    root = lexical_absolute(c['state_root'])
    path = root / f'.{service}.btrfs-snapshots.json'
    if not path_contains(root, path):
        raise ValueError(f'Btrfs snapshot state escapes state_root: {path}')
    return path


def _validate_state_entry(entry):
    fields = {
        'phase', 'source_id', 'source_path', 'source_subvolume_id',
        'source_uuid', 'filesystem_uuid', 'trusted_root', 'workspace_path',
        'snapshot_path', 'snapshot_subvolume_id', 'snapshot_uuid',
    }
    if not isinstance(entry, dict) or set(entry) != fields \
            or entry.get('phase') not in PHASES:
        return False
    required_strings = (
        'source_id', 'source_path', 'source_uuid', 'filesystem_uuid',
        'trusted_root', 'workspace_path', 'snapshot_path',
    )
    if any(not isinstance(entry.get(field), str) or not entry[field]
           for field in required_strings):
        return False
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', entry['source_id']) \
            or not UUID_RE.fullmatch(entry['source_uuid']) \
            or not UUID_RE.fullmatch(entry['filesystem_uuid']) \
            or not isinstance(entry.get('source_subvolume_id'), int) \
            or entry['source_subvolume_id'] <= 0:
        return False
    snapshot_id = entry.get('snapshot_subvolume_id')
    snapshot_uuid = entry.get('snapshot_uuid')
    if entry['phase'] == 'creating':
        return snapshot_id is None and snapshot_uuid is None
    return isinstance(snapshot_id, int) and snapshot_id > 0 \
        and isinstance(snapshot_uuid, str) \
        and bool(UUID_RE.fullmatch(snapshot_uuid))


def _load_state(c, service):
    path = _state_path(c, service)
    try:
        data = json.loads(read_control_text(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as err:
        raise RuntimeError(f'cannot load Btrfs snapshot state {path}: {err}') from err
    valid = isinstance(data, dict) \
        and set(data) == {'version', 'service', 'operation_id', 'snapshots'} \
        and data.get('version') == STATE_VERSION \
        and data.get('service') == service \
        and isinstance(data.get('operation_id'), str) \
        and bool(re.fullmatch(r'[0-9a-f]{32}', data['operation_id'])) \
        and isinstance(data.get('snapshots'), list) \
        and all(_validate_state_entry(entry) for entry in data['snapshots'])
    if valid:
        valid = all(
            Path(entry['snapshot_path']).name == (
                f'{service}-{data["operation_id"]}-{entry["source_id"]}'
            )
            for entry in data['snapshots']
        )
    if not valid:
        raise RuntimeError(f'invalid Btrfs snapshot state: {path}')
    return data


def _save_state(c, state):
    ensure_control_directory(c['state_root'])
    atomic_write_json(_state_path(c, state['service']), state)


def _clear_state(c, service):
    path = _state_path(c, service)
    if path.exists() or path.is_symlink():
        clear_control_leaf(path)


def snapshot_state_services(c):
    root = lexical_absolute(c['state_root'])
    if not root.exists() and not root.is_symlink():
        return []
    validate_control_directory(root)
    services = []
    for path in root.iterdir():
        match = STATE_FILE_RE.fullmatch(path.name)
        if match:
            services.append(match.group(1))
    return sorted(services)


def filesystem_uuid(path):
    result = run([
        'findmnt', '--noheadings', '--output', 'UUID', '--target', str(path),
    ], capture=True)
    value = result.stdout.strip()
    if not UUID_RE.fullmatch(value):
        raise RuntimeError(f'cannot determine Btrfs filesystem UUID for {path}')
    return value.lower()


def _normalise_uuid(value):
    value = value.strip().lower()
    return None if value in ('', '-') else value


def _parse_subvolume_show(path, output):
    values = {}
    for line in output.splitlines():
        if ':' not in line:
            continue
        key, value = line.strip().split(':', 1)
        values[key.strip().lower()] = value.strip()
    try:
        subvolume_id = int(values['subvolume id'])
        subvolume_uuid = _normalise_uuid(values['uuid'])
        parent_uuid = _normalise_uuid(values.get('parent uuid', ''))
    except (KeyError, ValueError) as err:
        raise RuntimeError(f'cannot parse Btrfs subvolume identity for {path}') from err
    if subvolume_id <= 0 or subvolume_uuid is None \
            or not UUID_RE.fullmatch(subvolume_uuid) \
            or (parent_uuid is not None and not UUID_RE.fullmatch(parent_uuid)):
        raise RuntimeError(f'invalid Btrfs subvolume identity for {path}')
    return {
        'subvolume_id': subvolume_id,
        'uuid': subvolume_uuid,
        'parent_uuid': parent_uuid,
        'readonly': 'readonly' in values.get('flags', '').lower().split(),
    }


def _command_diagnostic(result):
    return '\n'.join(
        value.strip() for value in (result.stderr, result.stdout)
        if value and value.strip()
    )


def _is_not_subvolume_error(output):
    return re.search(
        r'\bnot (?:a )?(?:btrfs )?subvolume\b', output,
        flags=re.IGNORECASE,
    ) is not None


def subvolume_details(
        path, *, allow_plain=False, filesystem_type=None, pass_fds=(),
        opened_metadata=None,
):
    display_path = str(path)
    if opened_metadata is None:
        path = lexical_absolute(path)
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            if allow_plain:
                return None
            raise
    else:
        metadata = opened_metadata
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return None if allow_plain else _raise_not_subvolume(display_path)
    if filesystem_type is None:
        filesystem_type = containing_mount(path).filesystem_type
    if filesystem_type != 'btrfs':
        return None if allow_plain else _raise_not_subvolume(display_path)
    result = run(
        ['btrfs', 'subvolume', 'show', str(path)], capture=True, check=False,
        pass_fds=pass_fds,
    )
    if result.returncode != 0:
        diagnostic = _command_diagnostic(result)
        if allow_plain and _is_not_subvolume_error(diagnostic):
            return None
        message = f'cannot inspect Btrfs subvolume {display_path}'
        if diagnostic:
            message = f'{message}: {diagnostic}'
        raise RuntimeError(message)
    return _parse_subvolume_show(display_path, result.stdout)


def _raise_not_subvolume(path):
    raise RuntimeError(f'path is not a Btrfs subvolume: {path}')


def snapshot_parent(trusted_root):
    return lexical_absolute(Path(trusted_root) / SNAPSHOT_DIRECTORY)


def _validate_workspace(path):
    path = ensure_control_directory(path, mode=0o700)
    validate_control_directory(path)
    metadata = os.lstat(path)
    allowed_owners = {0}
    allowed_groups = {0}
    if os.geteuid() != 0:
        allowed_owners.add(os.geteuid())
        allowed_groups.add(os.getegid())
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) \
            or metadata.st_uid not in allowed_owners \
            or metadata.st_gid not in allowed_groups \
            or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f'Btrfs snapshot workspace must be root-only mode 0700: {path}')
    return path


def _identity_error(entry, reason):
    return RuntimeError(
        f'Btrfs snapshot identity does not fully match state ({reason}); '
        f'refusing automatic deletion of {entry.get("snapshot_path")!r}. '
        'Inspect the snapshot and state file manually.'
    )


def _entry_paths(entry):
    trusted_root = lexical_absolute(entry['trusted_root'])
    workspace = lexical_absolute(entry['workspace_path'])
    snapshot = lexical_absolute(entry['snapshot_path'])
    expected_workspace = snapshot_parent(trusted_root)
    if workspace != expected_workspace or snapshot.parent != workspace \
            or not path_contains(workspace, snapshot):
        raise _identity_error(entry, 'unexpected snapshot path')
    return trusted_root, workspace, snapshot


def _snapshot_exists(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _validate_cleanup_identity(entry, *, creating=False):
    trusted_root, workspace, snapshot = _entry_paths(entry)
    try:
        validate_control_directory(workspace)
        workspace_metadata = os.lstat(workspace)
        snapshot_metadata = os.lstat(snapshot)
    except (FileNotFoundError, ValueError) as err:
        raise _identity_error(entry, f'workspace or snapshot path is invalid: {err}') from err
    allowed_owners = {0}
    allowed_groups = {0}
    if os.geteuid() != 0:
        allowed_owners.add(os.geteuid())
        allowed_groups.add(os.getegid())
    if workspace_metadata.st_uid not in allowed_owners \
            or workspace_metadata.st_gid not in allowed_groups \
            or stat.S_IMODE(workspace_metadata.st_mode) != 0o700 \
            or not stat.S_ISDIR(snapshot_metadata.st_mode) \
            or stat.S_ISLNK(snapshot_metadata.st_mode):
        raise _identity_error(entry, 'workspace or snapshot path is invalid')
    if filesystem_uuid(trusted_root) != entry['filesystem_uuid'] \
            or filesystem_uuid(workspace) != entry['filesystem_uuid'] \
            or filesystem_uuid(snapshot) != entry['filesystem_uuid']:
        raise _identity_error(entry, 'filesystem UUID')
    details = subvolume_details(snapshot)
    if not details['readonly']:
        raise _identity_error(entry, 'snapshot is not read-only')
    if details.get('parent_uuid') != entry['source_uuid']:
        raise _identity_error(entry, 'parent UUID')
    if not creating:
        if details['subvolume_id'] != entry['snapshot_subvolume_id']:
            raise _identity_error(entry, 'snapshot subvolume ID')
        if details['uuid'] != entry['snapshot_uuid']:
            raise _identity_error(entry, 'snapshot UUID')
    return snapshot, details


def _remove_empty_workspace(path):
    try:
        os.rmdir(path)
    except OSError:
        pass


def _commit_entry_removal(c, state, workspace):
    state['snapshots'].pop(0)
    if state['snapshots']:
        _save_state(c, state)
    else:
        _clear_state(c, state['service'])
    _remove_empty_workspace(workspace)


def cleanup_snapshot_state(c, service):
    state = _load_state(c, service)
    if state is None:
        return
    while state['snapshots']:
        entry = state['snapshots'][0]
        _trusted_root, workspace, snapshot = _entry_paths(entry)
        exists = _snapshot_exists(snapshot)
        if entry['phase'] == 'creating':
            if not exists:
                _commit_entry_removal(c, state, workspace)
                continue
            _snapshot, details = _validate_cleanup_identity(entry, creating=True)
            entry['snapshot_subvolume_id'] = details['subvolume_id']
            entry['snapshot_uuid'] = details['uuid']
            entry['phase'] = 'deleting'
            _save_state(c, state)
        elif entry['phase'] == 'ready':
            if not exists:
                raise _identity_error(entry, 'recorded snapshot is missing')
            _validate_cleanup_identity(entry)
            entry['phase'] = 'deleting'
            _save_state(c, state)

        if not _snapshot_exists(snapshot):
            _commit_entry_removal(c, state, workspace)
            continue
        snapshot, _details = _validate_cleanup_identity(entry)
        run(['btrfs', 'subvolume', 'delete', str(snapshot)])
        run(['btrfs', 'subvolume', 'sync', str(workspace)])
        if _snapshot_exists(snapshot):
            raise _identity_error(entry, 'snapshot still exists after deletion')
        _commit_entry_removal(c, state, workspace)


class SnapshotTransaction:
    def __init__(self, c, m, conflict_checker):
        self.c = c
        self.m = m
        self.conflict_checker = conflict_checker
        self.overrides = {}

    def create(self):
        from .manifest import source_path

        state = {
            'version': STATE_VERSION,
            'service': self.m['service'],
            'operation_id': secrets.token_hex(16),
            'snapshots': [],
        }
        for source in (self.m.get('sources') or {}).get('paths', []):
            source_value = lexical_absolute(source_path(self.m, source))
            try:
                source_fd, source_metadata = open_data_path(
                    source_value, self.c['trusted_data_roots'],
                )
            except FileNotFoundError:
                if source.get('required', True):
                    raise
                continue
            try:
                trusted_root = select_trusted_root(
                    source_value, self.c['trusted_data_roots'],
                )
                mount = containing_mount(trusted_root)
                source_proc_path = f'/proc/self/fd/{source_fd}'
                source_details = subvolume_details(
                    source_proc_path, allow_plain=True,
                    filesystem_type=mount.filesystem_type,
                    pass_fds=(source_fd,), opened_metadata=source_metadata,
                )
                if source_details is None:
                    continue
                workspace = _validate_workspace(snapshot_parent(trusted_root))
                # Btrfs may expose each subvolume with a distinct st_dev even
                # though they belong to the same filesystem.  The filesystem
                # UUID is the authoritative cross-subvolume identity here.
                source_filesystem_uuid = filesystem_uuid(trusted_root)
                if filesystem_uuid(workspace) != source_filesystem_uuid:
                    raise RuntimeError(
                        'Btrfs snapshot workspace is not on the source filesystem: '
                        f'{workspace}'
                    )
                conflicts = self.conflict_checker(workspace)
                if conflicts:
                    raise RuntimeError(
                        'Btrfs snapshot workspace is mounted by containers: '
                        + ', '.join(conflicts)
                    )
                name = (
                    f'{self.m["service"]}-{state["operation_id"]}-'
                    f'{source["id"]}'
                )
                snapshot = workspace / name
                entry = {
                    'phase': 'creating',
                    'source_id': source['id'],
                    'source_path': str(source_value),
                    'source_subvolume_id': source_details['subvolume_id'],
                    'source_uuid': source_details['uuid'],
                    'filesystem_uuid': source_filesystem_uuid,
                    'trusted_root': str(trusted_root),
                    'workspace_path': str(workspace),
                    'snapshot_path': str(snapshot),
                    'snapshot_subvolume_id': None,
                    'snapshot_uuid': None,
                }
                state['snapshots'].append(entry)
                _save_state(self.c, state)
                run([
                    'btrfs', 'subvolume', 'snapshot', '-r',
                    source_proc_path, str(snapshot),
                ], pass_fds=(source_fd,))
                snapshot_details = subvolume_details(snapshot)
                if not snapshot_details['readonly']:
                    raise RuntimeError(f'Btrfs created a writable snapshot: {snapshot}')
                if snapshot_details.get('parent_uuid') != source_details['uuid']:
                    raise RuntimeError(
                        f'Btrfs snapshot parent UUID does not match source: {snapshot}'
                    )
                entry['snapshot_subvolume_id'] = snapshot_details['subvolume_id']
                entry['snapshot_uuid'] = snapshot_details['uuid']
                entry['phase'] = 'ready'
                _save_state(self.c, state)
                self.overrides[source['id']] = snapshot
            finally:
                os.close(source_fd)
        return dict(self.overrides)

    def cleanup(self):
        cleanup_snapshot_state(self.c, self.m['service'])
