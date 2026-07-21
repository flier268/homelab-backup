import re
import stat
import json
import os
import tempfile
from pathlib import Path

from .common import CommandError, run, run_cleanup
from .manifest import (
    actual_volume_name, compose_model, compose_run, source_path,
    validate_docker_volume_name,
)
from .security import (
    clear_control_leaf, containing_mount, docker_mount_users,
    ensure_private_directory, open_data_path, open_data_path_with_parent_metadata,
    validate_data_path, validate_payload, validate_payload_fd,
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
        on_created=None,
):
    name = validate_docker_volume_name(name)
    command = [
        'docker', 'volume', 'create',
        '--label', f'io.homelab-backup.service={service}',
        '--label', f'io.homelab-backup.source={source["id"]}',
    ]
    if operation_id:
        command += [
            '--label', f'io.homelab-backup.operation={operation_id}',
        ]
    if source.get('compose_volume'):
        command += [
            '--label', f'com.docker.compose.project={project_name}',
            '--label', f'com.docker.compose.volume={source["compose_volume"]}',
        ]
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
    if operation_id and not volume_owned_by_operation(name, operation_id):
        raise RuntimeError(
            f'Docker volume is not owned by this restore operation: {name}'
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


def _source_trusted_roots(c, m):
    return c.get('trusted_data_roots') or [m['_dir']]


def validate_runtime_sources(c, m, model, *, allow_missing_paths=False):
    trusted_roots = _source_trusted_roots(c, m)
    for source in (m.get('sources') or {}).get('paths', []):
        path = source_path(m, source)
        try:
            validate_data_path(path, trusted_roots)
        except FileNotFoundError:
            if source.get('required', True) and not allow_missing_paths:
                raise ValueError(f'missing required source: {path}')
    for source, name in resolved_volume_sources(m, model=model):
        if not docker_volume_exists(name) and source.get('required', True):
            raise RuntimeError(f'Docker volume does not exist or is inaccessible: {name}')


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


def _missing_path_inventory(source):
    return {
        'id': source['id'],
        'path': source['path'],
        'type': None,
        'present': False,
    }


def _same_object(left, right):
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _open_path_source(src, *, trusted_roots=None):
    src = Path(src)
    if trusted_roots is not None:
        return open_data_path(src, trusted_roots)
    initial = src.lstat()
    if stat.S_ISLNK(initial.st_mode):
        flags = os.O_PATH | os.O_NOFOLLOW
    elif stat.S_ISDIR(initial.st_mode):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    elif stat.S_ISREG(initial.st_mode):
        flags = os.O_RDONLY | os.O_NOFOLLOW
    else:
        raise RuntimeError(f'unsupported path source type: {src}')
    fd = os.open(src, flags)
    metadata = os.fstat(fd)
    if not _same_object(initial, metadata):
        os.close(fd)
        raise RuntimeError(f'path source changed while being opened: {src}')
    return fd, metadata


def _verify_path_source(src, metadata, *, trusted_roots=None):
    if trusted_roots is not None:
        try:
            fd, current = open_data_path(src, trusted_roots)
        except FileNotFoundError as err:
            raise RuntimeError(f'path source changed during backup: {src}') from err
        try:
            if not _same_object(metadata, current):
                raise RuntimeError(f'path source changed during backup: {src}')
        finally:
            os.close(fd)
        return
    try:
        current = Path(src).lstat()
    except FileNotFoundError as err:
        raise RuntimeError(f'path source changed during backup: {src}') from err
    if not _same_object(metadata, current):
        raise RuntimeError(f'path source changed during backup: {src}')


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


def estimate_path_source(c, m, source):
    src = source_path(m, source)
    trusted_roots = _source_trusted_roots(c, m)
    try:
        fd, metadata = _open_path_source(
            src, trusted_roots=trusted_roots,
        )
    except FileNotFoundError:
        if source.get('required', True):
            raise ValueError(f'missing source {src}')
        return 0
    filesystem = containing_mount(src).filesystem_type
    try:
        validate_payload_fd(fd, src, filesystem_type=filesystem)
        size = _estimate_open_source(fd, metadata, src)
        _verify_path_source(
            src, metadata, trusted_roots=trusted_roots,
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


def estimate_backup_size(c, m, resolved=None):
    total = sum(
        estimate_path_source(c, m, source)
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
        src, dst, excludes, *, fd=None, metadata=None, trusted_roots=None,
):
    src = Path(src)
    own_fd = fd is None
    if own_fd:
        fd, metadata = _open_path_source(src, trusted_roots=trusted_roots)
    dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    clear_control_leaf(dst)
    dst.mkdir(parents=True, mode=0o700)
    archived = dst / src.name
    try:
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
            cmd = ['rsync', '-aHAX', '--numeric-ids', '--delete']
            for item in excludes or []:
                cmd += ['--exclude', item]
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
        _verify_path_source(src, metadata, trusted_roots=trusted_roots)
        return source_type
    finally:
        if own_fd:
            os.close(fd)


def validate_path_payloads(c, m, *, allow_missing=False):
    trusted_roots = _source_trusted_roots(c, m)
    for source in (m.get('sources') or {}).get('paths', []):
        src = source_path(m, source)
        try:
            fd, _metadata = open_data_path(src, trusted_roots)
        except FileNotFoundError:
            if source.get('required', True) and not allow_missing:
                raise ValueError(f'missing required source: {src}')
            continue
        try:
            filesystem = containing_mount(src).filesystem_type
            validate_payload_fd(fd, src, filesystem_type=filesystem)
        finally:
            os.close(fd)


def sync_paths(c, m=None, stage=None, *, before_copy=None):
    if stage is None:
        # Backward-compatible library call used by focused unit tests. The
        # root-only CLI always supplies the explicit global policy.
        stage = m
        m = c
        roots = sorted(set(
            str(source_path(m, source).parent)
            for source in (m.get('sources') or {}).get('paths', [])
        )) or [m['_dir']]
        c = {'trusted_data_roots': roots}
    trusted_roots = _source_trusted_roots(c, m)
    inventory = []
    for source in (m.get('sources') or {}).get('paths', []):
        src = source_path(m, source)
        dst = stage / 'paths' / source['id']
        try:
            if before_copy is not None:
                before_copy(source)
            filesystem = containing_mount(src).filesystem_type
            fd, metadata, ancestors = open_data_path_with_parent_metadata(
                src, trusted_roots,
            )
            try:
                validate_payload_fd(
                    fd, src, filesystem_type=filesystem,
                )
                source_type = _copy_path_source(
                    src, dst, source.get('exclude'), fd=fd, metadata=metadata,
                    trusted_roots=trusted_roots,
                )
            finally:
                os.close(fd)
        except FileNotFoundError:
            if source.get('required', True):
                raise ValueError(f'missing source {src}')
            dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            clear_control_leaf(dst)
            inventory.append(_missing_path_inventory(source))
            continue
        entry = {
            'id': source['id'],
            'path': source['path'],
            'type': source_type,
            'present': True,
        }
        if ancestors:
            entry['ancestors'] = ancestors
        inventory.append(entry)
    return inventory


def sync_volumes(
        c, m, stage, restore=False, resolved=None, *, before_copy=None,
):
    volume_sources = resolved_volume_sources(m) if resolved is None else resolved
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
                })
                continue
            if before_copy is not None:
                before_copy(source)
            dst.mkdir(parents=True, exist_ok=True)
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
            inventory.append({
                'id': source['id'], 'name': source.get('name'),
                'compose_volume': source.get('compose_volume'),
                'actual_name': name, 'present': True,
            })
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
