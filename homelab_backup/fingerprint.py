import hashlib
import json
import os
import stat
import struct
from pathlib import Path


_CHUNK_SIZE = 1024 * 1024
_FINGERPRINT_VERSION = b'homelab-backup-stage-v1'
_SNAPSHOT_CONTEXT_VERSION = b'homelab-backup-snapshot-context-v1'


def _field(digest, value):
    if not isinstance(value, bytes):
        value = str(value).encode('ascii')
    digest.update(struct.pack('>Q', len(value)))
    digest.update(value)


def _metadata(
        digest, path, relative, metadata, synthetic_directories,
):
    _field(digest, os.fsencode(relative.as_posix()))
    _field(digest, stat.S_IFMT(metadata.st_mode))
    _field(digest, stat.S_IMODE(metadata.st_mode))
    _field(digest, metadata.st_uid)
    _field(digest, metadata.st_gid)

    # Inode and ctime belong to the disposable staging copy. Only the known
    # staging wrapper directory mtimes are excluded; rsync-preserved payload
    # directory mtimes are part of the Restic snapshot and must remain visible.
    synthetic_directory = (
        stat.S_ISDIR(metadata.st_mode)
        and relative.as_posix() in synthetic_directories
    )
    # Generated recovery metadata is compared by content instead of its
    # newly-created mtime.
    generated_metadata = relative.parts[:1] == ('_meta',)
    if not synthetic_directory and not generated_metadata:
        _field(digest, metadata.st_mtime_ns)

    names = sorted(
        os.listxattr(path, follow_symlinks=False),
        key=os.fsencode,
    )
    _field(digest, len(names))
    for name in names:
        _field(digest, os.fsencode(name))
        _field(digest, os.getxattr(path, name, follow_symlinks=False))


def _regular_file(digest, path, relative, metadata, hardlinks):
    if metadata.st_nlink > 1:
        identity = (metadata.st_dev, metadata.st_ino)
        canonical = hardlinks.setdefault(
            identity, os.fsencode(relative.as_posix()),
        )
        _field(digest, b'hardlink')
        _field(digest, canonical)
    else:
        _field(digest, b'file')

    _field(digest, metadata.st_size)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
                or opened.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise RuntimeError(f'staging file changed while hashing: {path}')
        while True:
            chunk = os.read(descriptor, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if (
                finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
        ):
            raise RuntimeError(f'staging file changed while hashing: {path}')
    finally:
        os.close(descriptor)


def stage_fingerprint(root):
    root = Path(root)
    initial = os.lstat(root)
    if not stat.S_ISDIR(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
        raise ValueError(f'staging root must be a real directory: {root}')

    digest = hashlib.sha256()
    _field(digest, _FINGERPRINT_VERSION)
    hardlinks = {}
    synthetic_directories = {'.', 'paths', 'volumes', '_meta'}
    inventory_path = root / '_meta' / 'inventory.json'
    try:
        descriptor = os.open(
            inventory_path, os.O_RDONLY | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        inventory = {}
    else:
        try:
            with os.fdopen(descriptor, encoding='utf-8') as inventory_file:
                descriptor = -1
                inventory = json.load(inventory_file)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    if not isinstance(inventory, dict):
        raise ValueError('staging inventory must be a JSON mapping')
    paths = inventory.get('paths', [])
    if not isinstance(paths, list):
        raise ValueError('staging inventory paths must be a JSON list')
    for item in paths:
        if not isinstance(item, dict):
            raise ValueError('staging inventory path entries must be mappings')
        source_id = item.get('id')
        source_path = Path(source_id) if isinstance(source_id, str) else None
        if (
                item.get('present') is True
                and item.get('type') in {'file', 'symlink'}
                and source_path is not None
                and source_path.parts == (source_id,)
                and source_id not in {'.', '..'}
        ):
            synthetic_directories.add(f'paths/{source_id}')

    def visit(path, relative):
        metadata = os.lstat(path)
        _metadata(
            digest, path, relative, metadata, synthetic_directories,
        )
        if stat.S_ISREG(metadata.st_mode):
            _regular_file(digest, path, relative, metadata, hardlinks)
            return
        if stat.S_ISLNK(metadata.st_mode):
            _field(digest, b'symlink')
            _field(digest, os.fsencode(os.readlink(path)))
            return
        if stat.S_ISDIR(metadata.st_mode):
            _field(digest, b'directory')
            with os.scandir(path) as iterator:
                entries = sorted(
                    iterator, key=lambda item: os.fsencode(item.name),
                )
            for entry in entries:
                visit(Path(entry.path), relative / entry.name)
            return
        raise RuntimeError(f'unsupported staging entry type: {path}')

    visit(root, Path('.'))
    return f'sha256:{digest.hexdigest()}'


def snapshot_context_fingerprint(config, repository_id):
    digest = hashlib.sha256()
    _field(digest, _SNAPSHOT_CONTEXT_VERSION)
    _field(digest, repository_id.encode('ascii'))
    _field(digest, os.fsencode(config.get('host_id', '')))
    return f'sha256:{digest.hexdigest()}'
