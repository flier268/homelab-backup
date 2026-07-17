import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from homelab_backup import common, restore as backupctl, storage
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

    def test_missing_optional_present_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'), 'required': False,
            }
            with self.assertRaisesRegex(RuntimeError, 'artifact is missing'):
                backupctl.restore_path_source(
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

            with mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.restore_path_source(
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

            backupctl.restore_path_source(
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
            with mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.restore_path_source(
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

            backupctl.restore_path_source(
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

            backupctl.restore_path_source(
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

            backupctl.restore_path_source(
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

            backupctl.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), Path('../missing-target'))
            self.assertTrue(sentinel.exists())


class RepositoryBoundaryTests(unittest.TestCase):
    def test_invalid_service_tag_is_rejected(self):
        result = mock.Mock(stdout=json.dumps([{'tags': ['service:/etc']}]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'invalid service tag'):
                backupctl.repository_services({'host_id': 'host'})


class ManifestRestoreTests(unittest.TestCase):
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
                backupctl.validate_restore_sources(value, root / 'restore', inventory)

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
            inventory = backupctl.load_restore_inventory(restored)
            with self.assertRaisesRegex(RuntimeError, 'required volume.*absent'):
                backupctl.validate_restore_inventory(value, inventory)

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
            with mock.patch.object(backupctl, 'running_services') as running_mock, \
                    mock.patch.object(backupctl, 'restore_path_source') as path_mock:
                with self.assertRaisesRegex(RuntimeError, 'directory artifact is missing'):
                    backupctl.apply_one({}, value, restore_root)
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
                'service': 'demo', '_dir': str(root / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'none'},
            }

            with self.assertRaisesRegex(RuntimeError, 'inventory is missing'):
                backupctl.apply_one({}, value, restore_root)


class RestorePlanningTests(unittest.TestCase):
    def test_service_list_change_is_diagnostic_not_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value = manifest(base)
            restored = base / 'restored'
            write_restore_inventory(restored)
            inventory_path = restored / '_meta' / 'inventory.json'
            inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
            inventory['compose']['services'] = ['old-service']
            inventory_path.write_text(json.dumps(inventory), encoding='utf-8')
            model = {'name': 'demo', 'services': {'new-service': {}}, 'volumes': {}}
            with mock.patch.object(backupctl, 'manifest', return_value=value), \
                    mock.patch.object(backupctl, 'compose_model', return_value=model), \
                    mock.patch.object(backupctl, 'running_services', return_value=[]), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()):
                plan = backupctl.prepare_restore_plan(
                    {'services_root': str(base)}, value, restored,
                )
            self.assertEqual(plan.mode, 'existing')

    def test_existing_deployment_uses_local_compose_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value = manifest(base)
            restored = base / 'restored'
            write_restore_inventory(restored)
            model = {'name': 'demo', 'services': {}, 'volumes': {}}
            with mock.patch.object(backupctl, 'manifest', return_value=value), \
                    mock.patch.object(backupctl, 'compose_model', return_value=model), \
                    mock.patch.object(backupctl, 'running_services', return_value=[]), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()):
                plan = backupctl.prepare_restore_plan(
                    {'services_root': str(base)}, value, restored,
                )
            self.assertEqual(plan.mode, 'existing')
            self.assertEqual(plan.project_name, 'demo')

    def _rebuild_fixture(self, base):
        service_dir = base / 'services' / 'demo'
        value = {
            '_path': str(service_dir / 'backup.yaml'),
            '_dir': str(service_dir),
            'version': 1,
            'service': 'demo',
            'schedule': {'cron': '0 0 * * *'},
            'retention': {'keep_last': 1},
            'compose': {'files': ['compose.yaml']},
            'consistency': {'mode': 'stop'},
            'sources': {'paths': [
                {'id': 'compose', 'path': 'compose.yaml', 'required': True},
            ], 'volumes': []},
        }
        restored = base / 'restored'
        staged = restored / 'paths' / 'compose'
        staged.mkdir(parents=True)
        (staged / 'compose.yaml').write_text('services: {}\n', encoding='utf-8')
        write_restore_inventory(restored, paths=[{
            'id': 'compose', 'path': 'compose.yaml', 'type': 'file',
            'present': True,
        }])
        return value, restored

    def test_rebuild_requires_every_target_to_be_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            with mock.patch.object(backupctl, 'docker_project_containers', return_value=()), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()):
                plan = backupctl.prepare_restore_plan(
                    {'trusted_data_roots': [str(base / 'services')]}, value, restored,
                )
                self.assertEqual(plan.mode, 'rebuild')

                target = Path(value['_dir']) / 'compose.yaml'
                target.parent.mkdir(parents=True)
                target.write_text('occupied\n', encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'mixed|already exists'):
                    backupctl.prepare_restore_plan(
                        {'trusted_data_roots': [str(base / 'services')]}, value, restored,
                    )

    def test_rebuild_rejects_existing_snapshot_absent_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            target = Path(value['_dir']) / 'optional'
            value['sources']['paths'].append({
                'id': 'optional', 'path': 'optional', 'required': False,
            })
            target.parent.mkdir(parents=True)
            (base / 'services').chmod(0o755)
            target.parent.chmod(0o755)
            target.write_text('stale', encoding='utf-8')
            write_restore_inventory(restored, paths=[
                {'id': 'compose', 'path': 'compose.yaml', 'type': 'file', 'present': True},
                {'id': 'optional', 'path': 'optional', 'type': None, 'present': False},
            ])
            with self.assertRaisesRegex(RuntimeError, 'absent.*already exists'):
                backupctl.prepare_restore_plan(
                    {'trusted_data_roots': [str(base / 'services')]}, value, restored,
                )

    def test_rebuild_rejects_existing_snapshot_absent_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            value['sources']['volumes'] = [{
                'id': 'cache', 'name': 'demo_cache', 'required': False,
            }]
            write_restore_inventory(restored, paths=[{
                'id': 'compose', 'path': 'compose.yaml', 'type': 'file',
                'present': True,
            }], volumes=[{
                'id': 'cache', 'name': 'demo_cache', 'present': False,
            }])
            with mock.patch.object(
                backupctl, 'docker_volume_exists', return_value=True,
            ), self.assertRaisesRegex(RuntimeError, 'volume already exists'):
                backupctl.prepare_restore_plan(
                    {'trusted_data_roots': [str(base / 'services')]}, value, restored,
                )

    def test_rebuild_publishes_compose_and_manifest_after_data_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            trusted = base / 'services'
            trusted.mkdir(mode=0o755)
            snapshot_manifest = restored / '_meta' / 'backup.yaml'
            snapshot_manifest.write_text('version: 1\nservice: demo\n', encoding='utf-8')
            value['_snapshot_manifest'] = str(snapshot_manifest)
            value['_restore_manifest_requested'] = True
            publications = []
            real_copy = backupctl.atomic_copy_file

            def record_copy(source, target, **kwargs):
                publications.append(Path(target).name)
                return real_copy(source, target, **kwargs)

            with mock.patch.object(backupctl, 'docker_project_containers', return_value=()), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(backupctl, 'sync_volumes'), \
                    mock.patch.object(backupctl, 'atomic_copy_file', side_effect=record_copy):
                backupctl.apply_one(
                    {'trusted_data_roots': [str(trusted)]}, value, restored,
                )

            self.assertEqual(publications, ['compose.yaml', 'backup.yaml'])
            self.assertTrue((Path(value['_dir']) / 'compose.yaml').is_file())
            self.assertTrue(Path(value['_path']).is_file())

    def test_partial_manifest_and_compose_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            target = Path(value['_path'])
            target.parent.mkdir(parents=True)
            target.write_text('version: 1\nservice: demo\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'mixed'):
                backupctl.prepare_restore_plan(
                    {'trusted_data_roots': [str(base / 'services')]}, value, restored,
                )


class RestoreCommandTests(unittest.TestCase):
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
            plan = backupctl.RestorePlan(
                root, {'paths': [{**source, 'present': False, 'type': None}]},
                value, 'rebuild', (), (), (), (), 'demo',
            )
            with mock.patch.object(backupctl, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(backupctl, 'docker_project_containers', return_value=()):
                with self.assertRaisesRegex(RuntimeError, 'target appeared'):
                    backupctl.apply_one(
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
            plan = backupctl.RestorePlan(
                root, {'paths': []}, value, 'rebuild', (), ('demo_cache',),
                (), (), 'demo',
            )
            with mock.patch.object(backupctl, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(backupctl, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(backupctl, 'docker_project_containers', return_value=()), \
                    mock.patch.object(backupctl, 'docker_volume_exists', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'volume appeared'):
                    backupctl.apply_one({}, value, root)

    def test_noninteractive_apply_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=True, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'validate_docker_environment'), \
                mock.patch.object(backupctl, 'validate_trusted_roots'), \
                mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaisesRegex(SystemExit, '1'):
                backupctl.cmd_restore({'trusted_data_roots': []}, args)

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
            value = manifest(root, consistency={'mode': 'stop'})

            def fake_run(cmd, **kwargs):
                if 'up' in cmd and kwargs.get('check', True):
                    raise common.CommandError(cmd, 1)
                return mock.Mock(stdout='')

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(backupctl, 'docker_project_containers', return_value=()), \
                    mock.patch.object(backupctl, 'sync_volumes'), \
                    mock.patch.object(backupctl, 'run', side_effect=fake_run):
                with self.assertRaises(common.CommandError):
                    backupctl.apply_one({}, value, root)

    def test_dynamic_preflight_failure_restarts_previously_running_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(
                        backupctl, 'docker_mount_conflicts',
                        side_effect=[(), ('other-container',)],
                    ), mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'used by containers'):
                    backupctl.apply_one({}, value, root)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertTrue(any('up' in command for command in commands))

    def test_apply_failure_does_not_restart_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(backupctl, 'docker_project_containers', return_value=()), \
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
