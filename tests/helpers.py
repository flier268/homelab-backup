import json
from pathlib import Path


def manifest(tmp_path, **overrides):
    service_dir = tmp_path / 'demo'
    service_dir.mkdir(exist_ok=True)
    manifest_path = service_dir / 'backup.yaml'
    manifest_path.write_text('version: 1\nservice: demo\n', encoding='utf-8')
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
    return {'paths': [{
        'id': source['id'], 'path': source['path'], 'type': source_type,
    }]}


def write_restore_inventory(root, *, service='demo', paths=None, volumes=None):
    meta = Path(root) / '_meta'
    meta.mkdir(parents=True, exist_ok=True)
    (meta / 'inventory.json').write_text(json.dumps({
        'service': service,
        'paths': paths or [],
        'volumes': volumes or [],
    }), encoding='utf-8')
