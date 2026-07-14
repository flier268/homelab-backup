import re
from pathlib import Path

from .common import CommandError, die, run
from .config import actual_volume_name, compose_cmd, compose_model, source_path

def docker_volume_exists(name):
    try:
        run(['docker', 'volume', 'inspect', name], capture=True)
        return True
    except CommandError as err:
        output = f'{err.stdout}\n{err.stderr}'
        if re.search(r'\bno such volume\b', output, re.IGNORECASE):
            return False
        raise


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


def validate_runtime_sources(c, m, model):
    for source in (m.get('sources') or {}).get('paths', []):
        path = source_path(m, source)
        if source.get('required', True) and not path.exists():
            raise ValueError(f'missing required source: {path}')
    for source, name in resolved_volume_sources(m, model=model):
        if not docker_volume_exists(name) and source.get('required', True):
            raise RuntimeError(f'Docker volume does not exist or is inaccessible: {name}')
    selected = set((m.get('consistency') or {}).get('services', []))
    compose_services = set((model.get('services') or {}))
    unknown = sorted(selected - compose_services)
    if unknown:
        raise ValueError(f'consistency.services not found in Compose model: {unknown}')


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


def sync_paths(m, stage):
    for source in (m.get('sources') or {}).get('paths', []):
        src = source_path(m, source)
        if not src.exists():
            if source.get('required', True):
                die(f'missing source {src}')
            continue
        dst = stage / 'paths' / source['id']
        if src.is_dir():
            rsync(src, dst, source.get('exclude'))
        else:
            dst.mkdir(parents=True, exist_ok=True)
            run(['rsync', '-aHAX', '--numeric-ids', str(src), str(dst) + '/'])


def sync_volumes(c, m, stage, restore=False, resolved=None):
    volume_sources = resolved_volume_sources(m) if resolved is None else resolved
    for source, name in volume_sources:
        dst = stage / 'volumes' / source['id']
        if restore:
            if not dst.is_dir():
                if source.get('required', True):
                    raise RuntimeError(f'restored volume source is missing: {dst}')
                continue
        else:
            if not docker_volume_exists(name):
                if source.get('required', True):
                    raise RuntimeError(
                        f'Docker volume does not exist or is inaccessible: {name}'
                    )
                print(f'SKIP: optional Docker volume is unavailable: {name}')
                continue
            dst.mkdir(parents=True, exist_ok=True)
            cmd = [
                'docker', 'run', '--rm', '--network', 'none',
                '-v', f'{name}:/src:ro', '-v', f'{dst}:/dst',
                c['volume_helper_image'], 'rsync', '-aHAX', '--numeric-ids', '--delete',
            ]
        if restore:
            cmd = [
                'docker', 'run', '--rm', '--network', 'none',
                '-v', f'{name}:/dst', '-v', f'{dst}:/src:ro',
                c['volume_helper_image'], 'rsync', '-aHAX', '--numeric-ids', '--delete',
            ]
        for item in source.get('exclude', []):
            cmd += ['--exclude', item]
        cmd += ['/src/', '/dst/']
        run(cmd)


def hooks(m, key):
    for hook in (m.get('consistency') or {}).get(key, []) or []:
        run(['/bin/bash', '-euo', 'pipefail', '-c', hook], cwd=m['_dir'])


def running_services(m):
    result = run(
        compose_cmd(m) + ['ps', '--services', '--filter', 'status=running'],
        cwd=m['_dir'], capture=True,
    )
    return [x for x in result.stdout.splitlines() if x.strip()]
