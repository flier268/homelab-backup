import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from homelab_backup import common, config, restore as backupctl, storage
from tests.helpers import manifest, path_inventory, write_restore_inventory


class RestoreSourceTests(unittest.TestCase):
    def test_missing_required_path_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {'id': 'data', 'path': str(Path(tmp) / 'target')}
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                backupctl.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )

    def test_missing_optional_path_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'), 'required': False,
            }
            backupctl.restore_path_source(
                {'_dir': tmp}, root, source, path_inventory(source),
            )

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
                backupctl.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )
            self.assertEqual(rsync_mock.call_args.args[2], ['logs/**'])

    def test_volume_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            (root / 'volumes' / 'db').mkdir(parents=True)
            value = {'sources': {'volumes': [{
                'id': 'db', 'name': 'demo-db', 'exclude': ['cache/**'],
            }]}}
            with mock.patch.object(storage, 'run') as run_mock:
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
            write_restore_inventory(root)
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

    def test_apply_rejects_restore_root_overlapping_live_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'demo' / 'data'
            cases = [
                target,
                target / 'restore',
                base / 'demo',
            ]
            symlink = base / 'data-link'
            target.mkdir(parents=True)
            symlink.symlink_to(target, target_is_directory=True)
            cases.append(symlink / 'restore')
            for restore_root in cases:
                with self.subTest(restore_root=restore_root):
                    staged = restore_root / 'paths' / 'data'
                    staged.mkdir(parents=True, exist_ok=True)
                    value = {
                        'service': 'demo', '_dir': str(base / 'demo'),
                        'compose': {'files': ['compose.yaml']},
                        'consistency': {'mode': 'none'},
                        'sources': {
                            'paths': [{'id': 'data', 'path': str(target)}],
                            'volumes': [],
                        },
                    }
                    with mock.patch.object(backupctl, 'running_services') as running_mock, \
                            mock.patch.object(backupctl, 'restore_path_source') as path_mock:
                        write_restore_inventory(restore_root, paths=[{
                            'id': 'data', 'path': str(target), 'type': 'directory',
                        }])
                        with self.assertRaisesRegex(ValueError, 'overlap'):
                            backupctl.apply_one({}, value, restore_root)
                    running_mock.assert_not_called()
                    path_mock.assert_not_called()

    def test_apply_rejects_staged_symlink_overlapping_live_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'demo' / 'data'
            target.mkdir(parents=True)
            restore_root = base / 'restore'
            staged_parent = restore_root / 'paths'
            staged_parent.mkdir(parents=True)
            (staged_parent / 'data').symlink_to(target, target_is_directory=True)
            value = {
                'service': 'demo', '_dir': str(base / 'demo'),
                'compose': {'files': ['compose.yaml']},
                'consistency': {'mode': 'none'},
                'sources': {
                    'paths': [{'id': 'data', 'path': str(target)}],
                    'volumes': [],
                },
            }
            write_restore_inventory(restore_root, paths=[{
                'id': 'data', 'path': str(target), 'type': 'directory',
            }])
            with mock.patch.object(backupctl, 'running_services') as running_mock, \
                    mock.patch.object(backupctl, 'restore_path_source') as path_mock:
                with self.assertRaisesRegex(ValueError, 'overlap'):
                    backupctl.apply_one({}, value, restore_root)
            running_mock.assert_not_called()
            path_mock.assert_not_called()

    def test_duplicate_resolved_volume_aborts_before_stop_or_path_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            (restore_root / 'paths' / 'data').mkdir(parents=True)
            (restore_root / 'volumes' / 'direct').mkdir(parents=True)
            (restore_root / 'volumes' / 'logical').mkdir(parents=True)
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'compose': {'files': ['compose.yaml']},
                'consistency': {'mode': 'stop'},
                'sources': {
                    'paths': [{'id': 'data', 'path': 'data'}],
                    'volumes': [
                        {'id': 'direct', 'name': 'project_db'},
                        {'id': 'logical', 'compose_volume': 'db'},
                    ],
                },
            }
            model = {'volumes': {'db': {'name': 'project_db'}}}
            write_restore_inventory(
                restore_root,
                paths=[{'id': 'data', 'path': 'data', 'type': 'directory'}],
                volumes=[
                    {'id': 'direct', 'name': 'project_db'},
                    {'id': 'logical', 'compose_volume': 'db'},
                ],
            )
            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(config, 'compose_model', return_value=model), \
                    mock.patch.object(backupctl, 'running_services') as running_mock, \
                    mock.patch.object(backupctl, 'restore_path_source') as path_mock, \
                    mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(ValueError, 'duplicate Docker volume target'):
                    backupctl.apply_one({'volume_helper_image': 'helper'}, value, restore_root)
            running_mock.assert_not_called()
            path_mock.assert_not_called()
            run_mock.assert_not_called()

    def test_missing_compose_matches_canonical_equivalent_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            restore_root = root / 'restore'
            restore_root.mkdir()
            value = {
                '_dir': str(stack),
                'compose': {'files': ['compose.yaml']},
                'sources': {'paths': [{
                    'id': 'compose', 'path': 'nested/../compose.yaml',
                }]},
            }

            def restore_file(_m, _root, _source, _inventory):
                (stack / 'compose.yaml').write_text('services: {}\n', encoding='utf-8')

            with mock.patch.object(backupctl, 'restore_path_source', side_effect=restore_file):
                backupctl.restore_missing_compose_files(value, restore_root, {})

    def test_apply_rejects_inventory_from_another_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            meta = restore_root / '_meta'
            meta.mkdir(parents=True)
            (meta / 'inventory.json').write_text(json.dumps({
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

    def test_apply_rejects_missing_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            restore_root.mkdir()
            value = {
                'service': 'demo', '_dir': str(root / 'stack'),
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with self.assertRaisesRegex(RuntimeError, 'inventory is missing'):
                backupctl.apply_one({}, value, restore_root)


class RestoreCommandTests(unittest.TestCase):
    def test_noninteractive_apply_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=True, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaisesRegex(SystemExit, '1'):
                backupctl.cmd_restore({}, args)

        restore_mock.assert_not_called()

    def test_noninteractive_download_only_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=False, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_restore({}, args)
        restore_mock.assert_not_called()

    def test_noninteractive_download_only_runs_with_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {'host_id': 'host', 'lock_file': str(Path(tmp) / 'lock')}
            restored = (mock.Mock(), Path(tmp) / 'restored')
            with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                    mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                    mock.patch.object(backupctl, 'restore_one', return_value=restored) as restore_mock:
                backupctl.cmd_restore(config_data, args)
        restore_mock.assert_called_once()

    def test_apply_reports_restart_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop', 'services': ['app']})

            def fake_run(cmd, **kwargs):
                if 'up' in cmd and kwargs.get('check', True):
                    raise common.CommandError(cmd, 1)
                return mock.Mock(stdout='')

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(backupctl, 'sync_volumes'), \
                    mock.patch.object(backupctl, 'run', side_effect=fake_run):
                with self.assertRaises(common.CommandError):
                    backupctl.apply_one({}, value, root)

    def test_apply_failure_does_not_restart_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop', 'services': ['app']})

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(
                        backupctl, 'sync_volumes', side_effect=RuntimeError('restore failed'),
                    ), mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                    backupctl.apply_one({}, value, root, start_services=True)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertFalse(any('up' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
