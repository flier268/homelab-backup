import re
import stat
import json
import os
import tempfile
from pathlib import Path

from .common import CommandError, run, run_cleanup
from .manifest import (
    actual_volume_name, compose_model, compose_run, source_path,
    path_source_control_overlap, validate_docker_volume_name,
)
from .security import (
    clear_control_leaf, containing_mount, docker_mount_users,
    ensure_private_directory, open_data_path, open_data_path_with_parent_metadata,
    lexical_absolute, paths_overlap, validate_data_mount_boundary,
    validate_payload, validate_payload_fd,
)

def docker_volume_exists(name):
    try:
        run(['docker', 'volume', 'inspect', name], capture=True)
        return True
    except CommandError as err:
        output = f'{err.stdout}\n{err.stderr}'
        if re.search(r'\bno such volume\b', output, re.IGNORECASE):
            return False
        raise


def validate_docker_environment():
    overrides = [
        name for name in ('DOCKER_HOST', 'DOCKER_CONTEXT')
        if os.environ.get(name)
    ]
    if overrides:
        raise RuntimeError(
            'Docker endpoint overrides are not supported: ' + ', '.join(overrides)
        )
    endpoint = run(
        ['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'],
        capture=True,
    ).stdout.strip()
    try:
        endpoint = json.loads(endpoint)
    except json.JSONDecodeError as err:
        raise RuntimeError('cannot determine Docker daemon endpoint') from err
    if not isinstance(endpoint, str) or not endpoint.startswith('unix://') \
            or endpoint.startswith('unix:///run/user/'):
        raise RuntimeError(f'only a local rootful Docker Unix socket is supported: {endpoint}')
    security_options = run(
        ['docker', 'info', '--format', '{{json .SecurityOptions}}'], capture=True,
    ).stdout.lower()
    if 'rootless' in security_options:
        raise RuntimeError('rootless Docker is not supported')
    return endpoint


def validate_docker_bind_probe(c):
    """Prove the local daemon resolves the same existing host hierarchy."""
    root = ensure_private_directory(c['staging_root'])
    probe = Path(tempfile.mkdtemp(prefix='.docker-bind-probe-', dir=root))
    probe.chmod(0o700)
    token = f'homelab-backup-{os.getpid()}'
    (probe / 'token').write_text(token, encoding='utf-8')
    try:
        result = run([
            'docker', 'run', '--rm', '--network', 'none',
            '--mount', f'type=bind,src={probe},dst=/probe,readonly',
            c['volume_helper_image'], 'cat', '/probe/token',
        ], capture=True)
        if result.stdout.strip() != token:
            raise RuntimeError('Docker daemon bind probe saw different host data')
    finally:
        run_cleanup(lambda: clear_control_leaf(probe), 'Docker bind probe')


def docker_volume_details(name):
    result = run(['docker', 'volume', 'inspect', name], capture=True)
    values = json.loads(result.stdout)
    if len(values) != 1:
        raise RuntimeError(f'unexpected Docker volume inspection result: {name}')
    return values[0]


def validate_volume_identity(name, *, project_name=None, logical_name=None):
    details = docker_volume_details(name)
    labels = details.get('Labels') or {}
    if logical_name:
        if labels.get('com.docker.compose.project') != project_name or \
                labels.get('com.docker.compose.volume') != logical_name:
            raise RuntimeError(f'Docker volume labels do not match Compose identity: {name}')
    return details


def create_restore_volume(
        name, *, service, source, project_name=None, operation_id=None,
        driver=None, driver_opts=None, labels=None, on_created=None,
):
    name = validate_docker_volume_name(name)
    if driver is not None and (
        not isinstance(driver, str) or not driver
    ):
        raise ValueError('Docker volume driver must be a non-empty string')

    def string_mapping(value, field):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f'{field} must be a mapping')
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f'{field} keys must be non-empty strings')
            if item is None and field == 'Docker volume labels':
                item = ''
            if not isinstance(item, str):
                raise ValueError(f'{field} values must be strings')
            normalized[key] = item
        return normalized

    options = string_mapping(driver_opts, 'Docker volume driver options')
    expected_labels = string_mapping(labels, 'Docker volume labels')
    expected_labels.update({
        'io.homelab-backup.service': service,
        'io.homelab-backup.source': source['id'],
    })
    if operation_id:
        expected_labels['io.homelab-backup.operation'] = operation_id
    if source.get('compose_volume'):
        if not isinstance(project_name, str) or not project_name:
            raise ValueError(
                'Compose-managed restore volume requires a project name'
            )
        expected_labels.update({
            'com.docker.compose.project': project_name,
            'com.docker.compose.volume': source['compose_volume'],
        })
    command = ['docker', 'volume', 'create']
    if driver is not None:
        command += ['--driver', driver]
    for key, value in sorted(options.items()):
        command += ['--opt', f'{key}={value}']
    for key, value in expected_labels.items():
        command += ['--label', f'{key}={value}']
    command.append(name)
    result = run(command, capture=True)
    if on_created is not None:
        on_created(name)
    if result.stdout.strip() != name:
        if not operation_id or volume_owned_by_operation(name, operation_id):
            run_cleanup(
                lambda: run(['docker', 'volume', 'rm', name]),
                f'remove unexpected restore volume {name}',
            )
        raise RuntimeError(
            f'Docker created an unexpected volume: {result.stdout.strip()}'
        )
    if operation_id or driver is not None or driver_opts is not None \
            or labels is not None or source.get('compose_volume'):
        details = docker_volume_details(name)
        if details.get('Name') not in (None, name):
            raise RuntimeError(
                f'Docker volume name does not match restore target: {name}'
            )
        if driver is not None and details.get('Driver') != driver:
            raise RuntimeError(
                f'Docker volume driver does not match Compose model: {name}'
            )
        if driver_opts is not None and (details.get('Options') or {}) != options:
            raise RuntimeError(
                f'Docker volume options do not match Compose model: {name}'
            )
        actual_labels = details.get('Labels') or {}
        mismatched = sorted(
            key for key, value in expected_labels.items()
            if actual_labels.get(key) != value
        )
        if mismatched:
            raise RuntimeError(
                f'Docker volume labels do not match restore specification: '
                f'{name}: {", ".join(mismatched)}'
            )


def volume_owned_by_operation(name, operation_id):
    details = docker_volume_details(name)
    labels = details.get('Labels') or {}
    return labels.get('io.homelab-backup.operation') == operation_id


def docker_mount_conflicts(
    target_paths, volume_names, *, include_stopped=False, writable_only=True,
):
    def runner(command, **_kwargs):
        return run(command, capture=True, check=False)
    return docker_mount_users(
        target_paths, volume_names, include_stopped=include_stopped,
        writable_only=writable_only, run=runner,
    )


def docker_project_containers(project_name, *, include_stopped=False):
    command = ['docker', 'ps']
    if include_stopped:
        command.append('-a')
    command += [
        '-q', '--filter', f'label=com.docker.compose.project={project_name}',
    ]
    return tuple(run(command, capture=True).stdout.split())


def validate_no_docker_writers(m, identity, resolved, *, project_must_be_stopped):
    project_name = identity['project_name']
    if project_must_be_stopped:
        running = docker_project_containers(project_name)
        if running:
            raise RuntimeError(
                'Compose project still has running containers: ' + ', '.join(running)
            )
    conflicts = docker_mount_conflicts(
        [source_path(m, source) for source in (m.get('sources') or {}).get('paths', [])],
        [name for _source, name in resolved],
    )
    if conflicts:
        raise RuntimeError(
            'backup sources are still mounted by containers: ' + ', '.join(conflicts)
        )


def docker_writer_maps(m, resolved):
    path_writers = {}
    volume_writers = {}
    for source in (m.get('sources') or {}).get('paths', []):
        conflicts = docker_mount_conflicts([source_path(m, source)], [])
        if conflicts:
            path_writers[source['id']] = tuple(conflicts)
    for source, name in resolved:
        conflicts = docker_mount_conflicts([], [name])
        if conflicts:
            volume_writers[source['id']] = tuple(conflicts)
    return path_writers, volume_writers


def resolved_volume_sources(m, model=None):
    resolved = []
    seen = {}
    for source in (m.get('sources') or {}).get('volumes', []):
        name = actual_volume_name(m, source, model=model)
        if name in seen:
            raise ValueError(
                f"duplicate Docker volume target {name!r} for sources "
                f"{seen[name]!r} and {source['id']!r}"
            )
        seen[name] = source['id']
        resolved.append((source, name))
    return resolved


def _local_repository_path(repository):
    if not isinstance(repository, str):
        return None
    if repository.startswith('/'):
        return lexical_absolute(repository)
    if repository.startswith('local:/'):
        return lexical_absolute(repository.removeprefix('local:'))
    return None


def validate_path_source_policy(c, m, source):
    path = lexical_absolute(source_path(m, source))
    if path == Path('/'):
        raise ValueError('path source must not be the filesystem root: /')
    forbidden = {
        key: c.get(key)
        for key in (
            'staging_root', 'restore_root', 'cache_root', 'state_root',
            'password_file', 'rclone_config', 'lock_file',
        )
        if c.get(key)
    }
    repository_path = _local_repository_path(c.get('repository'))
    if repository_path is not None:
        forbidden['repository'] = repository_path
    for name, value in forbidden.items():
        if paths_overlap(path, value):
            raise ValueError(
                f'path source {path} must not overlap {name}: {value}'
            )
    overlap = path_source_control_overlap(m, source)
    if overlap is not None:
        name, control = overlap
        raise ValueError(
            f'path source {path} must not overlap protected {name}: {control}'
        )
    return path


def _validate_path_filter_type(source, metadata, path):
    if source.get('include') and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            f'include is only supported for directory path sources: {path}'
        )


def validate_runtime_sources(c, m, model, *, allow_missing_paths=False):
    for source in (m.get('sources') or {}).get('paths', []):
        path = validate_path_source_policy(c, m, source)
        try:
            fd, metadata = open_data_path(path)
            try:
                validate_data_mount_boundary(path)
                _validate_path_filter_type(source, metadata, path)
            finally:
                os.close(fd)
        except FileNotFoundError:
            if source.get('required', True) and not allow_missing_paths:
                raise ValueError(f'missing required source: {path}')
    for source, name in resolved_volume_sources(m, model=model):
        if not docker_volume_exists(name) and source.get('required', True):
            raise RuntimeError(f'Docker volume does not exist or is inaccessible: {name}')


def build_path_filter_args(source, *, protect_destination_dirs=False):
    args = []
    for item in source.get('exclude', []):
        args += ['--exclude', item]
    includes = source.get('include')
    if includes:
        if protect_destination_dirs:
            args += ['--filter', 'P */']
        args += ['--include', '*/']
        for item in includes:
            args += ['--include', item]
        args += ['--exclude', '*', '--prune-empty-dirs']
    return args


def rsync(src, dst, excludes=None, delete=True):
    Path(dst).mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-aHAX', '--numeric-ids']
    if delete:
        cmd.append('--delete')
    for item in excludes or []:
        cmd += ['--exclude', item]
    source = str(Path(src)) + ('/' if Path(src).is_dir() else '')
    target = str(dst) + ('/' if Path(src).is_dir() else '')
    cmd += [source, target]
    run(cmd)


def _missing_path_inventory(source, capture_method='quiesced-copy', writers=()):
    entry = {
        'id': source['id'],
        'path': source['path'],
        'type': None,
        'present': False,
        'capture_method': capture_method,
    }
    if writers:
        entry['writers'] = list(writers)
    return entry


def _same_object(left, right):
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _open_path_source(src):
    return open_data_path(src)


def _verify_path_source(src, metadata):
    try:
        fd, current = open_data_path(src)
    except FileNotFoundError as err:
        raise RuntimeError(f'path source changed during backup: {src}') from err
    try:
        if not _same_object(metadata, current):
            raise RuntimeError(f'path source changed during backup: {src}')
    finally:
        os.close(fd)


def _estimate_open_source(fd, metadata, display_path):
    allocated = metadata.st_blocks * 512
    if stat.S_ISREG(metadata.st_mode):
        return max(metadata.st_size, allocated)
    if stat.S_ISLNK(metadata.st_mode):
        return max(len(os.fsencode(os.readlink('', dir_fd=fd))), allocated)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f'unsupported payload object: {display_path}')
    total = max(4096, allocated)
    with os.scandir(fd) as entries:
        for entry in entries:
            before = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            elif stat.S_ISREG(before.st_mode):
                flags = os.O_RDONLY | os.O_NOFOLLOW
            elif stat.S_ISLNK(before.st_mode):
                flags = os.O_PATH | os.O_NOFOLLOW
            else:
                raise ValueError(
                    f'unsupported payload object: {Path(display_path) / entry.name}'
                )
            child_fd = os.open(entry.name, flags, dir_fd=fd)
            try:
                current = os.fstat(child_fd)
                if not _same_object(before, current):
                    raise RuntimeError(
                        f'path source changed during size estimation: '
                        f'{Path(display_path) / entry.name}'
                    )
                total += _estimate_open_source(
                    child_fd, current, Path(display_path) / entry.name,
                )
            finally:
                os.close(child_fd)
    return total


def _parse_filtered_size_stats(output, source):
    number = r'([0-9][0-9,]*)'
    file_match = re.search(
        rf'^Number of created files:\s+{number}(?:\s|\(|$)',
        output,
        re.MULTILINE,
    )
    size_match = re.search(
        rf'^Total file size:\s+{number}\s+bytes$', output, re.MULTILINE,
    )
    if file_match is None or size_match is None:
        raise RuntimeError(
            f'invalid filtered size estimate for path source {source["id"]!r}'
        )
    return (
        int(file_match.group(1).replace(',', '')),
        int(size_match.group(1).replace(',', '')),
    )


def _estimate_filtered_directory(fd, source, allocation_unit):
    with tempfile.TemporaryDirectory(prefix='homelab-backup-estimate-') as destination:
        command = [
            'rsync', '-aHAX', '--numeric-ids', '--dry-run', '--stats',
            '--one-file-system',
            *build_path_filter_args(source),
            f'/proc/self/fd/{fd}/', f'{destination}/',
        ]
        environment = os.environ.copy()
        environment['LC_ALL'] = 'C'
        result = run(
            command, capture=True, pass_fds=(fd,), env=environment,
        )
    item_count, logical_size = _parse_filtered_size_stats(
        result.stdout, source,
    )
    return logical_size + item_count * allocation_unit


def estimate_path_source(
        c, m, source, *, physical_source=None, allocation_unit=4096,
):
    src = Path(physical_source) if physical_source is not None else source_path(m, source)
    if physical_source is None:
        validate_path_source_policy(c, m, source)
    try:
        fd, metadata = _open_path_source(src)
    except FileNotFoundError:
        if source.get('required', True):
            raise ValueError(f'missing source {src}')
        return 0
    filesystem = containing_mount(src).filesystem_type
    mount_state = validate_data_mount_boundary(src)
    try:
        validate_payload_fd(fd, src, filesystem_type=filesystem)
        _validate_path_filter_type(source, metadata, src)
        if stat.S_ISDIR(metadata.st_mode) and build_path_filter_args(source):
            size = _estimate_filtered_directory(
                fd, source, allocation_unit,
            )
        else:
            size = _estimate_open_source(fd, metadata, src)
        _verify_path_source(src, metadata)
        if validate_data_mount_boundary(src) != mount_state:
            raise RuntimeError(
                f'mount topology changed during size estimation: {src}'
            )
        return size
    finally:
        os.close(fd)


def _parse_du_size(result, name):
    fields = result.stdout.split()
    if not fields or not fields[0].isdigit():
        raise RuntimeError(f'invalid size estimate for Docker volume: {name}')
    return int(fields[0])


def estimate_volume_source(c, source, name):
    name = validate_docker_volume_name(name)
    if not docker_volume_exists(name):
        if source.get('required', True):
            raise RuntimeError(
                f'Docker volume does not exist or is inaccessible: {name}'
            )
        return 0
    base = [
        'docker', 'run', '--rm', '--network', 'none',
        '--mount', f'type=volume,src={name},dst=/src,readonly',
        c['volume_helper_image'], 'du', '-s', '-B1', '-x',
    ]
    allocated = _parse_du_size(run(base + ['/src'], capture=True), name)
    apparent = _parse_du_size(
        run(base + ['--apparent-size', '/src'], capture=True), name,
    )
    return max(allocated, apparent)


def estimate_backup_size(
        c, m, resolved=None, *, source_overrides=None, staging_path=None,
):
    source_overrides = source_overrides or {}
    allocation_path = Path(staging_path or c['staging_root'])
    allocation_unit = os.statvfs(allocation_path).f_frsize
    total = sum(
        estimate_path_source(
            c, m, source,
            physical_source=source_overrides.get(source['id']),
            allocation_unit=allocation_unit,
        )
        for source in (m.get('sources') or {}).get('paths', [])
    )
    volume_sources = (
        resolved_volume_sources(m) if resolved is None else resolved
    )
    total += sum(
        estimate_volume_source(c, source, name)
        for source, name in volume_sources
    )
    return total


def _copy_path_source(
        src, dst, source, *, fd=None, metadata=None,
):
    src = Path(src)
    own_fd = fd is None
    if own_fd:
        fd, metadata = _open_path_source(src)
    mount_state = validate_data_mount_boundary(src)
    dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    clear_control_leaf(dst)
    dst.mkdir(parents=True, mode=0o700)
    archived = dst / src.name
    try:
        _validate_path_filter_type(source, metadata, src)
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink('', dir_fd=fd)
            os.symlink(target, archived)
            try:
                os.chown(
                    archived, metadata.st_uid, metadata.st_gid,
                    follow_symlinks=False,
                )
            except PermissionError:
                if os.geteuid() == 0:
                    raise
            os.utime(
                archived,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                follow_symlinks=False,
            )
            for name in os.listxattr(src, follow_symlinks=False):
                value = os.getxattr(src, name, follow_symlinks=False)
                os.setxattr(
                    archived, name, value, follow_symlinks=False,
                )
            source_type = 'symlink'
        elif stat.S_ISDIR(metadata.st_mode):
            cmd = [
                'rsync', '-aHAX', '--numeric-ids', '--delete',
                '--one-file-system',
            ]
            cmd += build_path_filter_args(source)
            cmd += [f'/proc/self/fd/{fd}/', f'{dst}/']
            run(cmd, pass_fds=(fd,))
            source_type = 'directory'
        elif stat.S_ISREG(metadata.st_mode):
            run([
                'rsync', '-aHAX', '--numeric-ids', '--copy-unsafe-links',
                f'/proc/self/fd/{fd}', str(archived),
            ], pass_fds=(fd,))
            source_type = 'file'
        else:
            raise RuntimeError(f'unsupported path source type: {src}')
        _verify_path_source(src, metadata)
        if validate_data_mount_boundary(src) != mount_state:
            raise RuntimeError(f'mount topology changed during backup: {src}')
        return source_type
    finally:
        if own_fd:
            os.close(fd)


def validate_path_payloads(c, m, *, allow_missing=False):
    for source in (m.get('sources') or {}).get('paths', []):
        src = validate_path_source_policy(c, m, source)
        try:
            fd, _metadata = open_data_path(src)
        except FileNotFoundError:
            if source.get('required', True) and not allow_missing:
                raise ValueError(f'missing required source: {src}')
            continue
        try:
            validate_data_mount_boundary(src)
            filesystem = containing_mount(src).filesystem_type
            validate_payload_fd(fd, src, filesystem_type=filesystem)
        finally:
            os.close(fd)


def sync_paths(
        c, m=None, stage=None, *, before_copy=None, source_overrides=None,
        capture_methods=None, writer_map=None,
):
    if stage is None:
        # Backward-compatible library call used by focused unit tests. The
        # root-only CLI always supplies the explicit global policy.
        stage = m
        m = c
        c = {}
    source_overrides = source_overrides or {}
    capture_methods = capture_methods or {}
    writer_map = writer_map or {}
    inventory = []
    for source in (m.get('sources') or {}).get('paths', []):
        src = validate_path_source_policy(c, m, source)
        physical = source_overrides.get(source['id'])
        copy_source = Path(physical) if physical is not None else src
        capture_method = capture_methods.get(source['id'], 'quiesced-copy')
        writers = tuple(writer_map.get(source['id'], ()))
        dst = stage / 'paths' / source['id']
        try:
            if before_copy is not None:
                before_copy(source)
            logical_fd, logical_metadata, ancestors = \
                open_data_path_with_parent_metadata(src)
            fd = logical_fd
            try:
                filesystem = containing_mount(copy_source).filesystem_type
                if physical is None:
                    metadata = logical_metadata
                else:
                    os.close(fd)
                    fd = None
                    fd, metadata = _open_path_source(copy_source)
                validate_payload_fd(
                    fd, copy_source, filesystem_type=filesystem,
                )
                source_type = _copy_path_source(
                    copy_source, dst, source, fd=fd, metadata=metadata,
                )
                _verify_path_source(src, logical_metadata)
            finally:
                if fd is not None:
                    os.close(fd)
        except FileNotFoundError:
            if source.get('required', True):
                raise ValueError(f'missing source {src}')
            dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            clear_control_leaf(dst)
            inventory.append(_missing_path_inventory(
                source, capture_method=capture_method, writers=writers,
            ))
            continue
        entry = {
            'id': source['id'],
            'path': source['path'],
            'type': source_type,
            'present': True,
            'capture_method': capture_method,
        }
        if writers:
            entry['writers'] = list(writers)
        if ancestors:
            entry['ancestors'] = ancestors
        inventory.append(entry)
    return inventory


def sync_volumes(
        c, m, stage, restore=False, resolved=None, *, before_copy=None,
        capture_methods=None, writer_map=None,
):
    volume_sources = resolved_volume_sources(m) if resolved is None else resolved
    capture_methods = capture_methods or {}
    writer_map = writer_map or {}
    inventory = []
    for source, name in volume_sources:
        name = validate_docker_volume_name(name)
        dst = stage / 'volumes' / source['id']
        if restore:
            volume_root = Path(stage) / 'volumes'
            valid_root = volume_root.is_dir() and not volume_root.is_symlink()
            valid_source = dst.is_dir() and not dst.is_symlink()
            if not valid_root or not valid_source:
                raise RuntimeError(
                    f'restored volume artifact is missing or not a real directory: {dst}'
                )
        else:
            if not docker_volume_exists(name):
                if source.get('required', True):
                    raise RuntimeError(
                        f'Docker volume does not exist or is inaccessible: {name}'
                    )
                print(f'SKIP: optional Docker volume is unavailable: {name}')
                inventory.append({
                    'id': source['id'], 'name': source.get('name'),
                    'compose_volume': source.get('compose_volume'),
                    'actual_name': name, 'present': False,
                    'capture_method': capture_methods.get(
                        source['id'], 'quiesced-copy',
                    ),
                })
                continue
            if before_copy is not None:
                before_copy(source)
            dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                clear_control_leaf(dst)
            dst.mkdir(mode=0o700)
            cmd = [
                'docker', 'run', '--rm', '--network', 'none',
                '--mount', f'type=volume,src={name},dst=/src,readonly',
                '--mount', f'type=bind,src={dst},dst=/dst',
                c['volume_helper_image'], 'rsync', '-aHAX', '--numeric-ids', '--delete',
            ]
        if restore:
            cmd = [
                'docker', 'run', '--rm', '--network', 'none',
                '--mount', f'type=volume,src={name},dst=/dst',
                '--mount', f'type=bind,src={dst},dst=/src,readonly',
                c['volume_helper_image'], 'rsync', '-aHAX', '--numeric-ids', '--delete',
            ]
        for item in source.get('exclude', []):
            cmd += ['--exclude', item]
        cmd += ['/src/', '/dst/']
        run(cmd)
        if not restore:
            validate_payload(dst)
            entry = {
                'id': source['id'], 'name': source.get('name'),
                'compose_volume': source.get('compose_volume'),
                'actual_name': name, 'present': True,
                'capture_method': capture_methods.get(
                    source['id'], 'quiesced-copy',
                ),
            }
            writers = tuple(writer_map.get(source['id'], ()))
            if writers:
                entry['writers'] = list(writers)
            inventory.append(entry)
    return inventory


def compose_identity(m, model=None, resolved=None):
    model = compose_model(m) if model is None else model
    resolved = resolved_volume_sources(m, model=model) if resolved is None else resolved
    return {
        'project_name': model.get('name') or Path(m['_dir']).name,
        'services': sorted((model.get('services') or {}).keys()),
        'compose_files': list(m.get('compose', {}).get('files', ['compose.yaml'])),
        'volumes': [
            {
                'id': source['id'],
                'logical_name': source.get('compose_volume'),
                'actual_name': name,
            }
            for source, name in resolved
        ],
    }


def hooks(m, key):
    for hook in (m.get('consistency') or {}).get(key, []) or []:
        run(['/bin/bash', '-euo', 'pipefail', '-c', hook], cwd=m['_dir'])


def running_services(m):
    result = compose_run(
        m, ['ps', '--services', '--filter', 'status=running'], capture=True,
        runner=run,
    )
    return [x for x in result.stdout.splitlines() if x.strip()]
