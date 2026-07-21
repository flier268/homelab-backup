import argparse
import configparser
import ctypes
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import tempfile

from . import config as config_module
from .common import load_yaml


CONFIG_MEMBERS = (
    ('configs/restic-password', 'restic-password'),
    ('configs/rclone.conf', 'rclone/rclone.conf'),
    ('configs/config.yaml', 'config.yaml'),
)


def config_lock_path(config_path):
    config = load_yaml(Path(config_path))
    lock_file = config.get('lock_file')
    if not isinstance(lock_file, str) or not Path(lock_file).is_absolute():
        raise SystemExit('ERROR: config lock_file must be an absolute path')
    return lock_file


def validate_lock_path(lock_file):
    path = Path(lock_file)
    parent = path.parent
    metadata = os.lstat(parent)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f'ERROR: lock parent is not a real directory: {parent}')
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise SystemExit(
            f'ERROR: lock parent is not private and root-controlled: {parent}'
        )
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise SystemExit(f'ERROR: lock file is not a root-owned regular file: {path}')


def preflight_bundle(source_root):
    root = Path(source_root) / 'configs'
    password_path = root / 'restic-password'
    rclone_path = root / 'rclone.conf'
    config_path = root / 'config.yaml'
    for path in (password_path, rclone_path, config_path):
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(
                f'ERROR: restored config member is not a regular file: {path}'
            )

    password = password_path.read_bytes()
    if not password.strip() or b'\0' in password:
        raise SystemExit(
            'ERROR: restic password must be non-empty and contain no NUL bytes'
        )

    config_module.CFG = config_path
    config = load_yaml(config_path)
    config_module._validate_config_header(config)
    trusted_roots = config_module._normalize_trusted_roots(config)
    config_module._validate_optional_sections(config)
    config_module._validate_root_separation(config, trusted_roots)
    if config['password_file'] != '/etc/homelab-backup/restic-password':
        raise SystemExit(
            'ERROR: restored config password_file must name the bundled restic password'
        )
    if config['rclone_config'] != '/etc/homelab-backup/rclone/rclone.conf':
        raise SystemExit(
            'ERROR: restored config rclone_config must name the bundled rclone config'
        )

    parser = configparser.RawConfigParser(strict=True)
    try:
        with rclone_path.open(encoding='utf-8') as source:
            parser.read_file(source)
    except (UnicodeError, configparser.Error) as error:
        raise SystemExit(f'ERROR: invalid rclone config: {error}') from error
    if not parser.sections():
        raise SystemExit('ERROR: rclone config must contain at least one remote')
    for section in parser.sections():
        if not parser.get(section, 'type', fallback='').strip():
            raise SystemExit(f'ERROR: rclone remote {section!r} is missing a type')
    repository = config['repository']
    if repository.startswith('rclone:'):
        parts = repository.split(':', 2)
        if len(parts) != 3 or not parts[1] or not parser.has_section(parts[1]):
            raise SystemExit(
                'ERROR: configured repository references a missing rclone remote'
            )


def publish_archive(source, archive, mode, *, fail_after_publish=False):
    source = Path(source)
    archive = Path(archive)
    directory = archive.parent
    name = archive.name
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    token = secrets.token_hex(8)
    temporary = f'.{name}.next.{token}'
    rollback = f'.{name}.rollback.{token}'

    def existing_metadata():
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SystemExit(
                    f'ERROR: encrypted archive is not a regular file: {archive}'
                )
            if metadata.st_uid != os.geteuid():
                raise SystemExit(
                    'ERROR: encrypted archive is not owned by the output user: '
                    f'{archive}'
                )
        finally:
            os.close(fd)

    def copy_into_temporary():
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with source.open('rb') as input_file, os.fdopen(
                fd, 'wb', closefd=False,
            ) as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
        finally:
            os.close(fd)

    try:
        if mode not in ('create', 'replace'):
            raise SystemExit(f'ERROR: invalid archive publication mode: {mode}')
        if mode == 'replace':
            existing_metadata()
        else:
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SystemExit(f'ERROR: output already exists: {archive}')

        copy_into_temporary()
        if mode == 'replace':
            os.link(
                name, rollback,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.replace(
                temporary, name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
        else:
            os.link(
                temporary, name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=directory_fd)
        try:
            if fail_after_publish:
                raise OSError('injected late publication failure')
            os.fsync(directory_fd)
        except OSError as publication_error:
            try:
                if mode == 'replace':
                    os.replace(
                        rollback, name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    )
                else:
                    os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError as rollback_error:
                print(
                    'ERROR: archive publication state is uncertain: '
                    f'{rollback_error}',
                    file=sys.stderr,
                )
                raise SystemExit(75) from rollback_error
            raise publication_error
    finally:
        for candidate in (temporary, rollback):
            try:
                os.unlink(candidate, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _validate_bundle_directory(path, owner_uid, owner_gid):
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f'ERROR: config control path is not a real directory: {path}')
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise SystemExit(f'ERROR: config control directory has the wrong owner: {path}')
    if metadata.st_mode & 0o022:
        raise SystemExit(
            f'ERROR: config control directory is writable by other users: {path}'
        )


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_bundle(
        source_root, target_root, *, owner_uid=0, owner_gid=0,
        fail_after=False, fail_after_commit=False,
):
    source_root = Path(source_root)
    target_root = Path(target_root)
    parent = target_root.parent
    _validate_bundle_directory(parent, owner_uid, owner_gid)
    next_prefix = f'.{target_root.name}.restore.next.'
    retired_prefix = f'.{target_root.name}.restore.retired.'
    for stale in parent.iterdir():
        if not (
                stale.name.startswith(next_prefix)
                or stale.name.startswith(retired_prefix)
        ):
            continue
        metadata = os.lstat(stale)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != owner_uid:
            raise SystemExit(f'ERROR: unsafe stale config generation: {stale}')
        shutil.rmtree(stale)
    _fsync_directory(parent)

    generation = Path(tempfile.mkdtemp(prefix=next_prefix, dir=parent))
    committed = False
    try:
        for source_name, relative in CONFIG_MEMBERS:
            source = source_root / source_name
            staged = generation / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged)
            os.chown(staged, owner_uid, owner_gid)
            os.chmod(staged, 0o600)
            _fsync_file(staged)
        for directory in (generation / 'rclone', generation):
            os.chown(directory, owner_uid, owner_gid)
            os.chmod(directory, 0o700)
            _fsync_directory(directory)
        if fail_after:
            raise RuntimeError('injected configuration publication failure')

        if target_root.exists() or target_root.is_symlink():
            _validate_bundle_directory(target_root, owner_uid, owner_gid)
            rclone_root = target_root / 'rclone'
            _validate_bundle_directory(rclone_root, owner_uid, owner_gid)
            for _source_name, relative in CONFIG_MEMBERS:
                target = target_root / relative
                metadata = os.lstat(target)
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError(f'refusing non-regular config target: {target}')
            retired = generation.with_name(
                retired_prefix + generation.name.removeprefix(next_prefix)
            )
            os.replace(generation, retired)
            generation = retired
            _fsync_directory(parent)
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
            renameat2.argtypes = [
                ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            if renameat2(
                -100, os.fsencode(generation), -100, os.fsencode(target_root), 2,
            ) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
        else:
            os.replace(generation, target_root)
        committed = True
        try:
            if fail_after_commit:
                raise OSError('injected post-commit directory sync failure')
            _fsync_directory(parent)
        except OSError as error:
            print(
                'WARNING: config generation was published, but its parent '
                f'directory could not be synced: {error}',
                file=sys.stderr,
            )
    finally:
        if not committed and generation.exists() and not generation.is_symlink():
            shutil.rmtree(generation)
            _fsync_directory(parent)
        elif committed and generation.exists() and not generation.is_symlink():
            try:
                shutil.rmtree(generation)
                _fsync_directory(parent)
            except OSError as error:
                print(
                    'WARNING: could not remove retired plaintext config '
                    f'generation {generation}: {error}',
                    file=sys.stderr,
                )


def build_parser():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('lock-path')
    command.add_argument('config')
    command = commands.add_parser('validate-lock')
    command.add_argument('lock')
    command = commands.add_parser('preflight-bundle')
    command.add_argument('source_root')
    command = commands.add_parser('publish-archive')
    command.add_argument('source')
    command.add_argument('archive')
    command.add_argument('mode', choices=('create', 'replace'))
    command.add_argument('--fail-after-publish', action='store_true', help=argparse.SUPPRESS)
    command = commands.add_parser('publish-bundle')
    command.add_argument('source_root')
    command.add_argument('target_root')
    command.add_argument('--owner-uid', type=int, default=0)
    command.add_argument('--owner-gid', type=int, default=0)
    command.add_argument('--fail-after', action='store_true', help=argparse.SUPPRESS)
    command.add_argument('--fail-after-commit', action='store_true', help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == 'lock-path':
        print(config_lock_path(args.config))
    elif args.command == 'validate-lock':
        validate_lock_path(args.lock)
    elif args.command == 'preflight-bundle':
        preflight_bundle(args.source_root)
    elif args.command == 'publish-archive':
        publish_archive(
            args.source, args.archive, args.mode,
            fail_after_publish=args.fail_after_publish,
        )
    elif args.command == 'publish-bundle':
        publish_bundle(
            args.source_root, args.target_root,
            owner_uid=args.owner_uid, owner_gid=args.owner_gid,
            fail_after=args.fail_after,
            fail_after_commit=args.fail_after_commit,
        )


if __name__ == '__main__':
    main()
