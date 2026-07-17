import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homelab_backup import (
    common, restore as backupctl, restore_apply, restore_plan, storage,
)
from tests.helpers import manifest, path_inventory, write_restore_inventory


class RestoreSourceTests(unittest.TestCase):
    def test_missing_required_path_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {'id': 'data', 'path': str(Path(tmp) / 'target')}
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )

    def test_missing_optional_present_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'), 'required': False,
            }
            with self.assertRaisesRegex(RuntimeError, 'artifact is missing'):
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )

    def test_inventory_marks_absent_optional_path_without_guessing_its_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'),
                'required': False,
            }
            inventory = {'paths': [{
                'id': 'data', 'path': source['path'], 'type': 'file',
                'present': False,
            }]}

            with mock.patch.object(restore_apply, 'run') as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, inventory,
                )

            run_mock.assert_not_called()

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

    def test_volume_restore_rejects_staged_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            referent = Path(tmp) / 'host-data'
            referent.mkdir()
            staged = root / 'volumes' / 'db'
            staged.parent.mkdir(parents=True)
            staged.symlink_to(referent, target_is_directory=True)
            value = {'sources': {'volumes': [{'id': 'db', 'name': 'demo-db'}]}}

            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'real directory'):
                    backupctl.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root,
                        restore=True,
                    )

            run_mock.assert_not_called()

    def test_hardlinks_are_rebuilt_only_inside_destination_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'data'
            staged.mkdir(parents=True)
            first = staged / 'first'
            first.write_text('snapshot', encoding='utf-8')
            (staged / 'second').hardlink_to(first)

            outside = base / 'sentinel'
            outside.write_text('outside', encoding='utf-8')
            target = base / 'live'
            target.mkdir()
            (target / 'first').hardlink_to(outside)
            source = {'id': 'data', 'path': str(target)}
            inventory = path_inventory(source)['paths']

            restore_apply.restore_path_source(
                {'_dir': str(base)}, restored, source,
                {'version': 1, 'paths': inventory},
            )

            self.assertEqual(outside.read_text(encoding='utf-8'), 'outside')
            self.assertEqual((target / 'first').stat().st_ino, (target / 'second').stat().st_ino)
            self.assertNotEqual(outside.stat().st_ino, (target / 'first').stat().st_ino)

    def test_path_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'),
                'exclude': ['logs/**'],
            }
            with mock.patch.object(restore_apply, 'rsync') as rsync_mock:
                restore_apply.restore_path_source(
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
            self.assertIn('--mount', command)
            self.assertIn('type=volume,src=demo-db,dst=/dst', command)
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
            with mock.patch.object(restore_apply, 'run') as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, inventory,
                )
            self.assertEqual(run_mock.call_args.args[0][-2], str(old_file))

    def test_file_snapshot_replaces_existing_directory_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.mkdir()
            (target / 'stale.txt').write_text('stale', encoding='utf-8')
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'payload.txt', 'type': 'file',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding='utf-8'), 'snapshot')

    def test_directory_snapshot_replaces_existing_file_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.write_text('stale', encoding='utf-8')
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'data', 'type': 'directory',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_dir())
            self.assertEqual(
                (target / 'payload.txt').read_text(encoding='utf-8'),
                'snapshot',
            )

    def test_type_change_does_not_follow_live_target_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            victim = Path(tmp) / 'victim'
            victim.mkdir()
            sentinel = victim / 'keep-me'
            sentinel.write_text('important', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.symlink_to(victim, target_is_directory=True)
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'payload.txt', 'type': 'file',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding='utf-8'), 'snapshot')
            self.assertTrue(sentinel.exists())

    def test_symlink_snapshot_recreates_link_without_following_referent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            archived = staged / 'old-link'
            archived.symlink_to('../missing-target')
            victim = Path(tmp) / 'victim'
            victim.mkdir()
            sentinel = victim / 'keep-me'
            sentinel.write_text('important', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.symlink_to(victim, target_is_directory=True)
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'old-link', 'type': 'symlink',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), Path('../missing-target'))
            self.assertTrue(sentinel.exists())

class RestoreApplyPreflightTests(unittest.TestCase):

    def test_apply_rejects_directory_inventory_backed_by_staged_symlink(self):
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
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
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
            with mock.patch.object(restore_plan, 'running_services') as running_mock, \
                    mock.patch.object(restore_apply, 'restore_path_source') as path_mock:
                with self.assertRaisesRegex(RuntimeError, 'directory artifact is missing'):
                    restore_apply.apply_one({}, value, restore_root)
            running_mock.assert_not_called()
            path_mock.assert_not_called()

    def test_apply_rejects_inventory_from_another_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            meta = restore_root / '_meta'
            meta.mkdir(parents=True)
            (meta / 'inventory.json').write_text(json.dumps({
                'version': 1,
                'service': 'other-service',
                'paths': [],
                'volumes': [],
                'compose': {},
            }), encoding='utf-8')
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with mock.patch.object(restore_plan, 'running_services') as running_mock:
                with self.assertRaisesRegex(RuntimeError, 'other-service'):
                    restore_apply.apply_one({}, value, restore_root)

            running_mock.assert_not_called()

    def test_apply_rejects_missing_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            restore_root.mkdir()
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with self.assertRaisesRegex(RuntimeError, 'inventory is missing'):
                restore_apply.apply_one({}, value, restore_root)

class RestoreApplyLifecycleTests(unittest.TestCase):

    def test_apply_runs_preflight_data_publication_and_restart_in_order(self):
        root = Path('/restore')
        value = {'service': 'demo'}
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        events = []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services',
            side_effect=lambda _plan: events.append('stop') or [],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
            side_effect=lambda _config, _plan: events.append('preflight'),
        ), mock.patch.object(
            restore_apply, '_restore_data',
            side_effect=lambda _config, _plan, _changed: events.append('data'),
        ), mock.patch.object(
            restore_apply, '_publish_controls',
            side_effect=lambda _config, _manifest, _plan, _changed:
            events.append('publish'),
        ), mock.patch.object(
            restore_apply, '_restart_services',
            side_effect=lambda _plan, _targets, _start: events.append('restart'),
        ):
            restore_apply.apply_one({}, value, root)

        self.assertEqual(
            events, ['stop', 'preflight', 'data', 'publish', 'restart'],
        )

    def test_dynamic_rebuild_rejects_snapshot_absent_path_that_appeared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'trusted' / 'demo'
            service_dir.mkdir(parents=True)
            (root / 'trusted').chmod(0o755)
            service_dir.chmod(0o755)
            target = service_dir / 'optional'
            target.write_text('stale', encoding='utf-8')
            source = {'id': 'optional', 'path': 'optional', 'required': False}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [source], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root, {'paths': [{**source, 'present': False, 'type': None}]},
                value, 'rebuild', (), (), (), (), 'demo',
            )
            with mock.patch.object(restore_apply, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(restore_apply, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()):
                with self.assertRaisesRegex(RuntimeError, 'target appeared'):
                    restore_apply.apply_one(
                        {'trusted_data_roots': [str(root / 'trusted')]}, value, root,
                    )

    def test_dynamic_rebuild_rejects_snapshot_absent_volume_that_appeared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {
                '_dir': str(root / 'trusted' / 'demo'),
                '_path': str(root / 'trusted' / 'demo' / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root, {'paths': []}, value, 'rebuild', (), ('demo_cache',),
                (), (), 'demo',
            )
            with mock.patch.object(restore_apply, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(restore_apply, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_volume_exists', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'volume appeared'):
                    restore_apply.apply_one({}, value, root)

    def test_apply_reports_restart_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            def fake_run(cmd, **kwargs):
                if 'up' in cmd and kwargs.get('check', True):
                    raise common.CommandError(cmd, 1)
                return mock.Mock(stdout='')

            with mock.patch.object(restore_apply, 'compose_files_exist', return_value=True), \
                    mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'sync_volumes'), \
                    mock.patch.object(restore_apply, 'run', side_effect=fake_run):
                with self.assertRaises(common.CommandError):
                    restore_apply.apply_one({}, value, root)

    def test_dynamic_preflight_failure_restarts_previously_running_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(
                        restore_apply, 'docker_mount_conflicts',
                        return_value=('other-container',),
                    ), mock.patch.object(
                        restore_plan, 'docker_mount_conflicts', return_value=(),
                    ), mock.patch.object(restore_apply, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'used by containers'):
                    restore_apply.apply_one({}, value, root)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertTrue(any('up' in command for command in commands))

    def test_apply_failure_does_not_restart_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(restore_apply, 'compose_files_exist', return_value=True), \
                    mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(
                        restore_apply, 'sync_volumes', side_effect=RuntimeError('restore failed'),
                    ), mock.patch.object(restore_apply, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                    restore_apply.apply_one({}, value, root, start_services=True)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertFalse(any('up' in command for command in commands))

if __name__ == '__main__':
    unittest.main()
