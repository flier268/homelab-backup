import re
import stat
import json
import os
import tempfile
from pathlib import Path

from .common import CommandError, die, run
from .manifest import (
    actual_volume_name, compose_cmd, compose_model, source_path,
    validate_docker_volume_name,
)
from .security import (
    clear_control_leaf, containing_mount, docker_mount_users,
    ensure_private_directory,
    validate_managed_leaf, validate_payload,
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
        clear_control_leaf(probe)


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


def create_restore_volume(name, *, service, source, project_name=None):
    name = validate_docker_volume_name(name)
    command = [
        'docker', 'volume', 'create',
        '--label', f'io.homelab-backup.service={service}',
        '--label', f'io.homelab-backup.source={source["id"]}',
    ]
    if source.get('compose_volume'):
        command += [
            '--label', f'com.docker.compose.project={project_name}',
            '--label', f'com.docker.compose.volume={source["compose_volume"]}',
        ]
    command.append(name)
    result = run(command, capture=True)
    if result.stdout.strip() != name:
        raise RuntimeError(f'Docker created an unexpected volume: {result.stdout.strip()}')


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


def validate_runtime_sources(c, m, model, *, allow_missing_paths=False):
    for source in (m.get('sources') or {}).get('paths', []):
        path = source_path(m, source)
        if source.get('required', True) and not allow_missing_paths \
                and not (path.exists() or path.is_symlink()):
            raise ValueError(f'missing required source: {path}')
        if path.exists() or path.is_symlink():
            validate_managed_leaf(path, c['trusted_data_roots'])
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


def _copy_path_source(src, dst, excludes):
    src = Path(src)
    metadata = src.lstat()
    dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    clear_control_leaf(dst)
    dst.mkdir(parents=True, mode=0o700)
    archived = dst / src.name
    if stat.S_ISLNK(metadata.st_mode):
        run(['rsync', '-aHAX', '--numeric-ids', str(src), str(archived)])
        return 'symlink'
    if stat.S_ISDIR(metadata.st_mode):
        rsync(src, dst, excludes)
        return 'directory'
    if stat.S_ISREG(metadata.st_mode):
        run(['rsync', '-aHAX', '--numeric-ids', str(src), str(archived)])
        return 'file'
    raise RuntimeError(f'unsupported path source type: {src}')


def validate_path_payloads(c, m, *, allow_missing=False):
    for source in (m.get('sources') or {}).get('paths', []):
        src = source_path(m, source)
        present = src.exists() or src.is_symlink()
        if not present:
            if source.get('required', True) and not allow_missing:
                raise ValueError(f'missing required source: {src}')
            continue
        validate_managed_leaf(src, c['trusted_data_roots'])
        filesystem = containing_mount(src).filesystem_type
        validate_payload(src, filesystem_type=filesystem)


def sync_paths(c, m=None, stage=None):
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
    inventory = []
    for source in (m.get('sources') or {}).get('paths', []):
        src = source_path(m, source)
        dst = stage / 'paths' / source['id']
        if not (src.exists() or src.is_symlink()):
            if source.get('required', True):
                die(f'missing source {src}')
            dst.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            clear_control_leaf(dst)
            inventory.append(_missing_path_inventory(source))
            continue
        try:
            validate_managed_leaf(src, c['trusted_data_roots'])
            filesystem = containing_mount(src).filesystem_type
            validate_payload(src, filesystem_type=filesystem)
            source_type = _copy_path_source(src, dst, source.get('exclude'))
        except FileNotFoundError:
            if source.get('required', True):
                die(f'missing source {src}')
            clear_control_leaf(dst)
            inventory.append(_missing_path_inventory(source))
            continue
        inventory.append({
            'id': source['id'],
            'path': source['path'],
            'type': source_type,
            'present': True,
        })
    return inventory


def sync_volumes(c, m, stage, restore=False, resolved=None):
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
    result = run(
        compose_cmd(m) + ['ps', '--services', '--filter', 'status=running'],
        cwd=m['_dir'], capture=True,
    )
    return [x for x in result.stdout.splitlines() if x.strip()]
