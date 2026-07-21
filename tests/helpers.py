import json
from pathlib import Path


def manifest(tmp_path, **overrides):
    service_dir = tmp_path / 'demo'
    service_dir.mkdir(exist_ok=True)
    service_dir.chmod(0o755)
    manifest_path = service_dir / 'backup.yaml'
    manifest_path.write_text('version: 1\nservice: demo\n', encoding='utf-8')
    manifest_path.chmod(0o600)
    compose_path = service_dir / 'compose.yaml'
    if not compose_path.exists():
        compose_path.write_text('services: {}\n', encoding='utf-8')
    compose_path.chmod(0o600)
    value = {
        '_path': str(manifest_path),
        '_dir': str(service_dir),
        'version': 1,
        'service': 'demo',
        'schedule': {'cron': '0 0 * * *'},
        'retention': {'keep_last': 1},
        'consistency': {'mode': 'none'},
        'sources': {'paths': [], 'volumes': []},
    }
    value.update(overrides)
    return value


def path_inventory(source, source_type='directory'):
    return {'version': 1, 'paths': [{
        'id': source['id'], 'path': source['path'], 'type': source_type,
        'present': True,
    }]}


def write_restore_inventory(root, *, service='demo', paths=None, volumes=None):
    paths = [dict(item) for item in (paths or [])]
    for item in paths:
        item.setdefault('present', True)
    volumes = [dict(item) for item in (volumes or [])]
    for item in volumes:
        item.setdefault('present', True)
        item.setdefault(
            'actual_name', item.get('name') or f'{service}_{item.get("compose_volume", item["id"])}',
        )
    meta = Path(root) / '_meta'
    meta.mkdir(parents=True, exist_ok=True)
    (meta / 'inventory.json').write_text(json.dumps({
        'version': 1,
        'service': service,
        'paths': paths,
        'volumes': volumes,
        'compose': {
            'project_name': service, 'services': [],
            'compose_files': ['compose.yaml'], 'volumes': [{
                'id': item['id'], 'logical_name': item.get('compose_volume'),
                'actual_name': item['actual_name'],
            } for item in volumes],
        },
    }), encoding='utf-8')
