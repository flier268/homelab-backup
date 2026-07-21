import json
import errno
import os
import secrets
import stat
import subprocess
import shutil
import io
from dataclasses import dataclass
from pathlib import Path


MOUNTINFO_PATH = Path('/proc/self/mountinfo')
ALLOWED_FILESYSTEMS = frozenset({'ext4', 'xfs', 'btrfs'})
_MOUNTINFO_ESCAPES = {
    r'\040': ' ', r'\011': '\t', r'\012': '\n', r'\134': '\\',
}


def _decode_mountinfo(value):
    for escaped, decoded in _MOUNTINFO_ESCAPES.items():
        value = value.replace(escaped, decoded)
    return value


@dataclass(frozen=True)
class MountRecord:
    mount_id: int
    parent_id: int
    device: str
    filesystem_root: Path
    mount_point: Path
    filesystem_type: str
    source: str


def mount_records(path=None):
    path = MOUNTINFO_PATH if path is None else path
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError as err:
        raise RuntimeError(f'cannot inspect mounted filesystems: {err}') from err
    records = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index('-')
            record = MountRecord(
                int(fields[0]), int(fields[1]), fields[2],
                Path(_decode_mountinfo(fields[3])),
                Path(_decode_mountinfo(fields[4])),
                fields[separator + 1], _decode_mountinfo(fields[separator + 2]),
            )
        except (ValueError, IndexError) as err:
            raise RuntimeError(
                f'cannot parse mounted filesystems: malformed {path}'
            ) from err
        records.append(record)
    return tuple(records)


def lexical_absolute(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError(f'path must be absolute: {path}')
    return Path(os.path.normpath(path))


def path_contains(root, path):
    root = lexical_absolute(root)
    path = lexical_absolute(path)
    return path == root or root in path.parents


def paths_overlap(left, right):
    return path_contains(left, right) or path_contains(right, left)


def _components(path):
    path = lexical_absolute(path)
    current = Path('/')
    yield current
    for part in path.parts[1:]:
        current /= part
        yield current


def validate_control_directory(path, *, allow_missing=False, owner_uid=None):
    """Validate every existing component without following symlinks."""
    path = lexical_absolute(path)
    effective_uid = os.geteuid()
    owner_uid = 0 if owner_uid is None else owner_uid
    allowed_owners = {owner_uid}
    if effective_uid != 0:
        # Unit tests call library functions without the root-only CLI boundary.
        # Production always has effective_uid == 0.
        allowed_owners.add(effective_uid)
    missing = False
    for component in _components(path):
        if missing:
            continue
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                missing = True
                continue
            raise ValueError(f'control directory does not exist: {component}')
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f'control path component is not a real directory: {component}')
        if metadata.st_uid not in allowed_owners:
            raise ValueError(f'control directory is not owned by root: {component}')
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            if not (
                effective_uid != 0
                and metadata.st_uid == 0
                and metadata.st_mode & stat.S_ISVTX
            ):
                raise ValueError(f'control directory is group/world writable: {component}')
    return path


def read_control_text(path, *, encoding='utf-8', require_protected=True):
    """Read a protected regular file without following its leaf symlink."""
    path = lexical_absolute(path)
    if require_protected:
        validate_control_directory(path.parent)
    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    fd = -1
    try:
        try:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as err:
            if err.errno == errno.ELOOP:
                raise ValueError(f'control file must be a regular file: {path}') from err
            raise ValueError(f'control file could not be opened: {path}: {err}') from err
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'control file must be a regular file: {path}')
        if require_protected:
            allowed_owners = {0}
            if os.geteuid() != 0:
                allowed_owners.add(os.geteuid())
            if metadata.st_uid not in allowed_owners:
                raise ValueError(f'control file is not owned by root: {path}')
            if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError(f'control file is group/world writable: {path}')
        with io.TextIOWrapper(os.fdopen(fd, 'rb', closefd=False), encoding=encoding) as source:
            return source.read()
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def validate_control_file(path, *, require_protected=True):
    """Validate a regular control file without following its leaf symlink."""
    path = lexical_absolute(path)
    if require_protected:
        validate_control_directory(path.parent)
    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    fd = -1
    try:
        try:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as err:
            if err.errno == errno.ELOOP:
                raise ValueError(
                    f'control file must be a regular file: {path}'
                ) from err
            raise
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'control file must be a regular file: {path}')
        if require_protected:
            allowed_owners = {0}
            if os.geteuid() != 0:
                allowed_owners.add(os.geteuid())
            if metadata.st_uid not in allowed_owners:
                raise ValueError(f'control file is not owned by root: {path}')
            if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError(
                    f'control file is group/world writable: {path}'
                )
        return path
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def containing_mount(path, records=None):
    path = lexical_absolute(path)
    matches = [
        item for item in (records or mount_records())
        if path == item.mount_point or item.mount_point in path.parents
    ]
    if not matches:
        raise RuntimeError(f'cannot determine filesystem for {path}')
    return max(matches, key=lambda item: len(item.mount_point.parts))


def validate_mount_boundary(root, *, records=None):
    explicit_records = records is not None
    records = tuple(records or mount_records())
    root = lexical_absolute(root)
    anchor = containing_mount(root, records)
    if anchor.filesystem_type not in ALLOWED_FILESYSTEMS \
            and (os.geteuid() == 0 or explicit_records):
        raise ValueError(
            f'unsupported filesystem {anchor.filesystem_type!r} for trusted root {root}'
        )
    nested = sorted(
        item.mount_point for item in records
        if item.mount_point != anchor.mount_point
        and path_contains(root, item.mount_point)
    )
    if nested:
        raise ValueError(f'nested mount is not supported: {nested[0]}')
    return anchor


def validate_control_root(path, *, allow_missing=False, records=None):
    path = lexical_absolute(path)
    if path.exists() or path.is_symlink():
        validate_control_directory(path)
    elif allow_missing:
        validate_control_directory(path.parent)
    else:
        raise ValueError(f'control root does not exist: {path}')
    validate_mount_boundary(path, records=records)
    return path


def select_trusted_root(path, trusted_roots):
    path = lexical_absolute(path)
    matches = [lexical_absolute(root) for root in trusted_roots if path_contains(root, path)]
    if len(matches) != 1:
        raise ValueError(f'path must be contained by exactly one trusted_data_root: {path}')
    return matches[0]


def validate_managed_leaf(path, trusted_roots, *, allow_missing=False, records=None):
    path = lexical_absolute(path)
    trusted_root = select_trusted_root(path, trusted_roots)
    if path == trusted_root:
        raise ValueError(f'managed leaf must be strictly below trusted root: {path}')
    # The payload leaf may be container-owned. Its parent is the final control
    # component and must remain immutable to unprivileged writers.
    validate_control_directory(path.parent, allow_missing=allow_missing)
    validate_mount_boundary(trusted_root, records=records)
    if path.exists() or path.is_symlink():
        metadata = os.lstat(path)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)):
            raise ValueError(f'unsupported managed leaf type: {path}')
    elif not allow_missing:
        raise ValueError(f'managed leaf does not exist: {path}')
    return trusted_root


def validate_trusted_roots(trusted_roots, *, records=None):
    for root in trusted_roots:
        validate_control_root(root, records=records)


def _scan_payload_tree(path):
    with os.scandir(path) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            child = Path(path) / entry.name
            if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                _scan_payload_tree(child)
                continue
            raise ValueError(f'unsupported payload object: {child}')


def _scan_payload_tree_fd(fd, display_path):
    with os.scandir(fd) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            child = Path(display_path) / entry.name
            if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                try:
                    _scan_payload_tree_fd(child_fd, child)
                finally:
                    os.close(child_fd)
                continue
            raise ValueError(f'unsupported payload object: {child}')


def validate_payload_fd(fd, display_path, *, filesystem_type=None, run=None):
    """Validate a payload through an already pinned, no-follow descriptor."""
    metadata = os.fstat(fd)
    display_path = Path(display_path)
    if stat.S_ISDIR(metadata.st_mode):
        if filesystem_type == 'btrfs' and os.geteuid() == 0:
            runner = run or subprocess.run
            kwargs = {
                'text': True, 'capture_output': True, 'check': False,
                'pass_fds': (fd,),
            }
            result = runner(
                ['btrfs', 'subvolume', 'list', '-o', f'/proc/self/fd/{fd}'],
                **kwargs,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f'cannot inspect Btrfs subvolumes below {display_path}'
                )
            if result.stdout.strip():
                raise ValueError(
                    f'nested Btrfs subvolume is not supported: {display_path}'
                )
        _scan_payload_tree_fd(fd, display_path)
    elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError(f'unsupported payload object: {display_path}')


def validate_payload(path, *, filesystem_type=None, run=None):
    path = Path(path)
    metadata = os.lstat(path)
    if stat.S_ISDIR(metadata.st_mode):
        if filesystem_type == 'btrfs' and os.geteuid() == 0:
            runner = run or subprocess.run
            result = runner(
                ['btrfs', 'subvolume', 'list', '-o', str(path)],
                text=True, capture_output=True, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f'cannot inspect Btrfs subvolumes below {path}')
            if result.stdout.strip():
                raise ValueError(f'nested Btrfs subvolume is not supported: {path}')
        _scan_payload_tree(path)
    elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError(f'unsupported payload object: {path}')


def _remove_entry_at(parent_fd, name, device):
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(metadata.st_mode):
        if metadata.st_dev != device:
            raise ValueError(f'refusing to remove cross-device directory: {name}')
        child_fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd,
        )
        try:
            for entry in os.scandir(child_fd):
                _remove_entry_at(child_fd, entry.name, device)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def clear_control_leaf(path):
    path = lexical_absolute(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            parent_metadata = os.fstat(parent_fd)
            _remove_entry_at(parent_fd, path.name, parent_metadata.st_dev)
        except FileNotFoundError:
            pass
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def ensure_private_directory(path, *, replace=False):
    path = lexical_absolute(path)
    try:
        initial = os.lstat(path)
    except FileNotFoundError:
        initial = None
    if initial is not None and stat.S_ISLNK(initial.st_mode):
        raise ValueError(f'workspace must not be a symbolic link: {path}')
    validate_control_root(path, allow_missing=True)
    if replace:
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError(f'workspace must not be a symbolic link: {path}')
        clear_control_leaf(path)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        metadata = os.lstat(path)
        allowed_owners = {0}
        if os.geteuid() != 0:
            allowed_owners.add(os.geteuid())
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f'workspace must not be a symbolic link: {path}')
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in allowed_owners:
            raise ValueError(f'workspace is not a root-owned real directory: {path}')
        os.chmod(path, 0o700)
    validate_control_root(path)
    return path


def ensure_control_directory(path, *, mode=0o700):
    """Create a private control-directory chain without following symlinks."""
    path = lexical_absolute(path)
    validate_control_directory(path, allow_missing=True)

    existing = path
    missing = []
    while True:
        try:
            metadata = os.lstat(existing)
        except FileNotFoundError:
            missing.append(existing.name)
            existing = existing.parent
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f'control path component is not a real directory: {existing}')
        break

    current_fd = os.open(
        existing, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for component in reversed(missing):
            try:
                os.mkdir(component, mode, dir_fd=current_fd)
                os.fsync(current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            metadata = os.fstat(next_fd)
            allowed_owners = {0}
            if os.geteuid() != 0:
                allowed_owners.add(os.geteuid())
            if metadata.st_uid not in allowed_owners or \
                    metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                os.close(next_fd)
                raise ValueError(
                    f'created control directory is not protected: {path}'
                )
            os.close(current_fd)
            current_fd = next_fd
        os.fsync(current_fd)
    finally:
        os.close(current_fd)

    validate_control_directory(path)
    return path


def ensure_control_parent(path, trusted_roots):
    """Create a missing control directory chain relative to a trusted root fd."""
    path = lexical_absolute(path)
    trusted_root = select_trusted_root(path, trusted_roots)
    validate_control_directory(trusted_root)
    current_fd = os.open(
        trusted_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for component in path.relative_to(trusted_root).parts:
            try:
                os.mkdir(component, 0o755, dir_fd=current_fd)
                os.fsync(current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            metadata = os.fstat(next_fd)
            allowed_owners = {0}
            if os.geteuid() != 0:
                allowed_owners.add(os.geteuid())
            if metadata.st_uid not in allowed_owners or \
                    metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                os.close(next_fd)
                raise ValueError(f'created control directory is not protected: {path}')
            os.close(current_fd)
            current_fd = next_fd
        os.fsync(current_fd)
    finally:
        os.close(current_fd)
    return path


def atomic_copy_file(
        source, target, *, mode=0o600, require_absent=False,
        on_publish=None,
):
    source = Path(source)
    target = lexical_absolute(target)
    source_metadata = os.lstat(source)
    if not stat.S_ISREG(source_metadata.st_mode):
        raise ValueError(f'atomic source is not a regular file: {source}')
    validate_control_directory(target.parent)
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp_name = f'.{target.name}.{secrets.token_hex(8)}.tmp'
    fd = None
    published_identity = None
    try:
        fd = os.open(
            tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode, dir_fd=parent_fd,
        )
        with source.open('rb') as input_handle, os.fdopen(fd, 'wb', closefd=False) as output:
            shutil.copyfileobj(input_handle, output)
            output.flush()
            os.fsync(output.fileno())
            metadata = os.fstat(output.fileno())
            published_identity = (metadata.st_dev, metadata.st_ino)
        os.close(fd)
        fd = None
        if require_absent:
            os.link(
                tmp_name, target.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if on_publish is not None:
                on_publish(published_identity)
            os.unlink(tmp_name, dir_fd=parent_fd)
        else:
            os.replace(
                tmp_name, target.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            )
            if on_publish is not None:
                on_publish(published_identity)
        os.fsync(parent_fd)
        return published_identity
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def atomic_write_json(path, data):
    path = lexical_absolute(path)
    validate_control_directory(path.parent)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp_name = f'.{path.name}.{secrets.token_hex(8)}.tmp'
    fd = None
    try:
        fd = os.open(
            tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent_fd,
        )
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
        with os.fdopen(fd, 'wb', closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = None
        os.replace(tmp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def docker_mount_users(
    target_paths, volume_names, *, include_stopped=False, writable_only=True,
    run=None,
):
    """Return containers whose mounts intersect protected paths or volumes."""
    if not target_paths and not volume_names:
        return ()
    runner = run or subprocess.run
    command = ['docker', 'ps', '-aq'] if include_stopped else ['docker', 'ps', '-q']
    listed = runner(command, text=True, capture_output=True, check=False)
    if listed.returncode != 0:
        raise RuntimeError('cannot enumerate Docker containers')
    ids = listed.stdout.split()
    if not ids:
        return ()
    inspected = runner(
        ['docker', 'inspect', *ids], text=True, capture_output=True, check=False,
    )
    if inspected.returncode != 0:
        raise RuntimeError('cannot inspect Docker containers')
    targets = tuple(lexical_absolute(path) for path in target_paths)
    volumes = set(volume_names)
    users = []
    for container in json.loads(inspected.stdout):
        for mount in container.get('Mounts') or []:
            if writable_only and mount.get('RW') is False:
                continue
            if mount.get('Type') == 'volume' and mount.get('Name') in volumes:
                users.append(container.get('Id') or container.get('Name'))
                break
            if mount.get('Type') == 'bind' and mount.get('Source'):
                source = lexical_absolute(mount['Source'])
                if any(paths_overlap(source, target) for target in targets):
                    users.append(container.get('Id') or container.get('Name'))
                    break
    return tuple(users)
