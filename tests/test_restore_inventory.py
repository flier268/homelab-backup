import tempfile
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from homelab_backup import restore_inventory
from homelab_backup.inventory_models import RestoreInventoryModel
from tests.helpers import manifest, write_restore_inventory


class RestoreInventoryTests(unittest.TestCase):
    def test_service_directory_accepts_valid_relative_path(self):
        value = restore_inventory.restore_inventory_service_directory({
            'version': 1,
            'service': 'advent-plus',
            'service_relative_directory': 'Minecraft/Advent Plus',
        }, 'advent-plus')

        self.assertEqual(value, Path('Minecraft/Advent Plus'))

    def test_service_directory_falls_back_to_service_id_when_field_is_missing(self):
        value = restore_inventory.restore_inventory_service_directory({
            'version': 1,
            'service': 'advent-plus',
        }, 'advent-plus')

        self.assertEqual(value, Path('advent-plus'))

    def test_service_directory_rejects_non_integer_version_one(self):
        for version in (True, 1.0, '1', 2, None):
            with self.subTest(version=version), self.assertRaisesRegex(
                    RuntimeError, 'version must be 1',
            ):
                restore_inventory.restore_inventory_service_directory({
                    'version': version,
                    'service': 'demo',
                }, 'demo')

    def test_service_directory_rejects_wrong_service(self):
        with self.assertRaisesRegex(RuntimeError, "expected 'demo'"):
            restore_inventory.restore_inventory_service_directory({
                'version': 1,
                'service': 'other',
            }, 'demo')

    def test_service_directory_rejects_invalid_requested_service(self):
        with self.assertRaisesRegex(ValueError, 'invalid service ID'):
            restore_inventory.restore_inventory_service_directory({
                'version': 1,
                'service': '../outside',
            }, '../outside')

    def test_service_directory_rejects_unsafe_or_non_normalized_paths(self):
        for value in (
                None, '', '/outside', '../outside', 'Minecraft//Server',
                'Minecraft/./Server', 'Minecraft/../Server',
                'Minecraft/Server/', 'Minecraft/two  spaces',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                restore_inventory.restore_inventory_service_directory({
                    'version': 1,
                    'service': 'demo',
                    'service_relative_directory': value,
                }, 'demo')

    def test_service_relative_directory_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            inventory = {
                'version': 1, 'service': 'demo',
                'service_relative_directory': 'Minecraft/Advent Plus',
                'paths': [], 'volumes': [],
                'compose': {
                    'project_name': 'demo', 'services': [],
                    'compose_files': ['compose.yaml'], 'volumes': [],
                },
            }
            restore_inventory.validate_restore_inventory(value, inventory)
            for unsafe in (
                None, '/outside', '../outside', 'Minecraft//Server',
                'Minecraft/two  spaces',
            ):
                inventory['service_relative_directory'] = unsafe
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    restore_inventory.validate_restore_inventory(value, inventory)

    def test_capture_and_consistency_metadata_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'snapshot'}, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })
            inventory = {
                'version': 1, 'service': 'demo',
                'paths': [{
                    'id': 'data', 'path': 'data', 'type': 'directory',
                    'present': True, 'capture_method': 'btrfs-snapshot',
                    'writers': ['container'],
                }],
                'volumes': [],
                'compose': {
                    'project_name': 'demo', 'services': [],
                    'compose_files': ['compose.yaml'], 'volumes': [],
                },
                'consistency': {
                    'mode': 'snapshot', 'guarantee': 'btrfs-snapshot',
                    'optional_action_failures': [{
                        'phase': 'finally', 'name': 'resume',
                        'result': 'failed',
                    }],
                    'writers': ['container'],
                },
            }
            restore_inventory.validate_restore_inventory(value, inventory)
            inventory['paths'][0]['capture_method'] = 'unknown'
            with self.assertRaisesRegex(RuntimeError, 'capture method'):
                restore_inventory.validate_restore_inventory(value, inventory)

    def test_ancestor_metadata_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={'paths': [{
                'id': 'saved', 'path': 'palworld/Pal/Saved',
            }], 'volumes': []})
            inventory = {
                'version': 1,
                'service': 'demo',
                'paths': [{
                    'id': 'saved', 'path': 'palworld/Pal/Saved',
                    'type': 'directory', 'present': True,
                    'ancestors': [{
                        'path': '../outside', 'uid': 0, 'gid': 0,
                        'mode': 0o755,
                    }],
                }],
                'volumes': [],
                'compose': {
                    'project_name': 'demo', 'services': [],
                    'compose_files': ['compose.yaml'], 'volumes': [],
                },
            }

            with self.assertRaisesRegex(RuntimeError, 'ancestor is invalid'):
                restore_inventory.validate_restore_inventory(value, inventory)

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

    def test_inventory_model_round_trips_current_snapshot_shape(self):
        data = {
            'version': 1,
            'service': 'demo',
            'service_directory': '/srv/stacks/demo',
            'service_relative_directory': 'demo',
            'paths': [{
                'id': 'data', 'path': 'data', 'type': 'directory',
                'present': True, 'capture_method': 'quiesced-copy',
            }],
            'volumes': [],
            'compose': {
                'project_name': 'demo', 'compose_files': ['compose.yaml'],
                'services': [], 'volumes': [],
            },
            'consistency': {
                'mode': 'external', 'guarantee': 'quiesced-copy',
                'optional_action_failures': [], 'writers': [],
            },
        }

        model = RestoreInventoryModel.from_snapshot_data(data)

        self.assertEqual(model.to_snapshot_dict(), data)
        with self.assertRaises(ValidationError):
            model.service = 'changed'

    def test_inventory_model_rejects_unknown_fields_and_coercion(self):
        base = {
            'version': 1, 'service': 'demo',
            'paths': [], 'volumes': [],
            'compose': {
                'project_name': 'demo', 'compose_files': ['compose.yaml'],
                'services': [], 'volumes': [],
            },
        }
        for update in (
                {'unknown': True},
                {'version': '1'},
                {'version': True},
                {'version': 1.0},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                RestoreInventoryModel.from_snapshot_data(base | update)


if __name__ == '__main__':
    unittest.main()
