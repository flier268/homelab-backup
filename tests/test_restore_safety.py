import importlib.machinery
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
backupctl = importlib.machinery.SourceFileLoader(
    'backupctl_restore_test', str(ROOT / 'backupctl')
).load_module()


class RestoreSourceTests(unittest.TestCase):
    def test_missing_required_path_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {'id': 'data', 'path': str(Path(tmp) / 'target')}
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                backupctl.restore_path_source({'_dir': tmp}, root, source, {})

    def test_missing_optional_path_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'), 'required': False,
            }
            backupctl.restore_path_source({'_dir': tmp}, root, source, {})

    def test_missing_volume_source_never_runs_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            value = {'sources': {'volumes': [{'id': 'db', 'name': 'demo-db'}]}}
            with mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'missing'):
                    backupctl.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root, restore=True,
                    )
            run_mock.assert_not_called()

    def test_path_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'),
                'exclude': ['logs/**'],
            }
            with mock.patch.object(backupctl, 'rsync') as rsync_mock:
                backupctl.restore_path_source({'_dir': tmp}, root, source, {})
            self.assertEqual(rsync_mock.call_args.args[2], ['logs/**'])

    def test_volume_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            (root / 'volumes' / 'db').mkdir(parents=True)
            value = {'sources': {'volumes': [{
                'id': 'db', 'name': 'demo-db', 'exclude': ['cache/**'],
            }]}}
            with mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.sync_volumes(
                    {'volume_helper_image': 'helper'}, value, root, restore=True,
                )
            command = run_mock.call_args.args[0]
            self.assertIn('--exclude', command)
            self.assertIn('cache/**', command)

    def test_file_source_uses_snapshot_inventory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'env'
            staged.mkdir(parents=True)
            old_file = staged / 'old.env'
            old_file.write_text('secret', encoding='utf-8')
            target = Path(tmp) / 'new.env'
            source = {'id': 'env', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'env', 'path': 'old.env', 'type': 'file',
            }]}
            with mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.restore_path_source(
                    {'_dir': tmp}, root, source, inventory,
                )
            self.assertEqual(run_mock.call_args.args[0][-2], str(old_file))


class RepositoryBoundaryTests(unittest.TestCase):
    def test_invalid_service_tag_is_rejected(self):
        result = mock.Mock(stdout=json.dumps([{'tags': ['service:/etc']}]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'invalid service tag'):
                backupctl.repository_services({'host_id': 'host'})


class ManifestRestoreTests(unittest.TestCase):
    def test_invalid_snapshot_manifest_does_not_replace_local_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                'version: 1\nservice: wrong\n', encoding='utf-8',
            )
            service_dir = root / 'services' / 'demo'
            service_dir.mkdir(parents=True)
            target = service_dir / 'backup.yaml'
            original = 'original local manifest\n'
            target.write_text(original, encoding='utf-8')

            with self.assertRaises(SystemExit):
                backupctl.prepare_restored_manifest(
                    {'services_root': str(root / 'services')},
                    'demo', restored, policy='restore',
                )

            self.assertEqual(target.read_text(encoding='utf-8'), original)

    def test_missing_compose_file_is_restored_before_service_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restore_root = base / 'restore'
            staged = restore_root / 'paths' / 'compose'
            staged.mkdir(parents=True)
            (staged / 'compose.yaml').write_text('services: {}\n', encoding='utf-8')
            stack = base / 'stack'
            stack.mkdir()
            value = {
                'service': 'demo', '_dir': str(stack),
                'compose': {'files': ['compose.yaml']},
                'consistency': {'services': ['db']},
                'sources': {
                    'paths': [{'id': 'compose', 'path': 'compose.yaml'}],
                    'volumes': [],
                },
            }
            inventory = {
                'schema_version': 1,
                'service': 'demo',
                'paths': [{
                    'id': 'compose', 'path': 'compose.yaml', 'type': 'file',
                }],
                'volumes': [],
            }
            (restore_root / '_meta').mkdir()
            (restore_root / '_meta' / 'inventory.json').write_text(
                json.dumps(inventory), encoding='utf-8',
            )

            def restore_file(_m, _root, _source, _inventory):
                (stack / 'compose.yaml').write_text('services: {}\n', encoding='utf-8')

            with mock.patch.object(
                backupctl, 'restore_path_source', side_effect=restore_file,
            ), mock.patch.object(
                backupctl, 'running_services', return_value=['db'],
            ) as running_mock, mock.patch.object(
                backupctl, 'sync_volumes',
            ), mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.apply_one({}, value, restore_root)

            running_mock.assert_called_once()
            self.assertIn('stop', run_mock.call_args_list[0].args[0])

    def test_apply_stops_all_running_services_when_manifest_selection_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'compose': {'files': ['compose.yaml']},
                'consistency': {'mode': 'stop', 'services': ['old-name']},
                'sources': {'paths': [], 'volumes': []},
            }
            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['new-name']), \
                    mock.patch.object(backupctl, 'sync_volumes'), \
                    mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.apply_one({}, value, root)

            stop_command = next(
                call.args[0] for call in run_mock.call_args_list if 'stop' in call.args[0]
            )
            self.assertIn('new-name', stop_command)
            self.assertNotIn('old-name', stop_command)

    def test_apply_rejects_inventory_from_another_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            meta = restore_root / '_meta'
            meta.mkdir(parents=True)
            (meta / 'inventory.json').write_text(json.dumps({
                'schema_version': 1,
                'service': 'other-service',
                'paths': [],
                'volumes': [],
            }), encoding='utf-8')
            value = {
                'service': 'demo', '_dir': str(root / 'stack'),
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with mock.patch.object(backupctl, 'running_services') as running_mock:
                with self.assertRaisesRegex(RuntimeError, 'other-service'):
                    backupctl.apply_one({}, value, restore_root)

            running_mock.assert_not_called()

    def test_apply_rejects_existing_inventory_without_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            meta = restore_root / '_meta'
            meta.mkdir(parents=True)
            (meta / 'inventory.json').write_text('{}', encoding='utf-8')
            value = {
                'service': 'demo', '_dir': str(root / 'stack'),
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with self.assertRaisesRegex(RuntimeError, 'schema'):
                backupctl.apply_one({}, value, restore_root)
