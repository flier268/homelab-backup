import tempfile
import json
import unittest
from pathlib import Path

from homelab_backup import restore_inventory
from tests.helpers import manifest, write_restore_inventory


class RestoreInventoryTests(unittest.TestCase):
    def test_inventory_symlink_is_rejected_without_reading_referent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restored = root / 'restore'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            outside = root / 'outside.json'
            outside.write_text(json.dumps({'version': 1}), encoding='utf-8')
            (meta / 'inventory.json').symlink_to(outside)

            with self.assertRaisesRegex(RuntimeError, 'regular file'):
                restore_inventory.load_restore_inventory(restored)

    def test_present_optional_path_requires_its_staged_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={'paths': [{
                'id': 'optional', 'path': 'optional', 'required': False,
            }], 'volumes': []})
            inventory = {
                'paths': [{
                    'id': 'optional', 'path': 'optional', 'type': 'file',
                    'present': True,
                }],
                'volumes': [],
            }
            with self.assertRaisesRegex(RuntimeError, 'artifact is missing'):
                restore_inventory.validate_restore_sources(
                    value, root / 'restore', inventory,
                )

    def test_required_volume_cannot_be_absent_in_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={'paths': [], 'volumes': [{
                'id': 'db', 'name': 'demo_db', 'required': True,
            }]})
            restored = root / 'restore'
            write_restore_inventory(restored, volumes=[{
                'id': 'db', 'name': 'demo_db', 'present': False,
            }])
            inventory = restore_inventory.load_restore_inventory(restored)
            with self.assertRaisesRegex(RuntimeError, 'required volume.*absent'):
                restore_inventory.validate_restore_inventory(value, inventory)

if __name__ == '__main__':
    unittest.main()
