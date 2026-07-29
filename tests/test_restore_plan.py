import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from homelab_backup import restore_apply, restore_plan
from tests.helpers import manifest, write_restore_inventory


class RestorePlanningTests(unittest.TestCase):
    def test_existing_restore_rejects_snapshot_filter_mismatch_when_manifest_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local = manifest(base, sources={
                'paths': [{
                    'id': 'data', 'path': 'data', 'include': ['config/**'],
                }],
                'volumes': [],
            })
            snapshot = {
                key: value for key, value in local.items()
                if not key.startswith('_')
            }
            snapshot['sources'] = {
                'paths': [{
                    'id': 'data', 'path': 'data', 'include': ['world/**'],
                }],
                'volumes': [],
            }
            snapshot_path = base / 'snapshot-backup.yaml'
            snapshot_path.write_text(
                yaml.safe_dump(snapshot), encoding='utf-8',
            )
            requested = dict(local)
            requested['_snapshot_manifest'] = str(snapshot_path)
            requested['_restore_manifest_requested'] = False

            with mock.patch.object(
                restore_plan, 'manifest', return_value=local,
            ), mock.patch.object(restore_plan, 'validate_restore_inventory'):
                with self.assertRaisesRegex(RuntimeError, 'filters differ'):
                    restore_plan._authorize_existing_restore(
                        {'services_root': str(base)},
                        requested,
                        {'paths': [], 'volumes': []},
                        (),
                    )

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
            (restored / '_meta' / 'backup.yaml').write_text(
                yaml.safe_dump({
                    key: item for key, item in value.items()
                    if not key.startswith('_')
                }),
                encoding='utf-8',
            )
            model = {'name': 'demo', 'services': {'new-service': {}}, 'volumes': {}}
            with mock.patch.object(restore_plan, 'manifest', return_value=value), \
                    mock.patch.object(restore_plan, 'compose_model', return_value=model), \
                    mock.patch.object(restore_plan, 'running_services', return_value=[]), \
                    mock.patch.object(restore_plan, 'docker_mount_conflicts', return_value=()):
                plan = restore_plan.prepare_restore_plan(
                    {'services_root': str(base)}, value, restored,
                )
            self.assertEqual(plan.mode, 'existing')

    def test_existing_deployment_uses_local_compose_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value = manifest(base)
            restored = base / 'restored'
            write_restore_inventory(restored)
            (restored / '_meta' / 'backup.yaml').write_text(
                yaml.safe_dump({
                    key: item for key, item in value.items()
                    if not key.startswith('_')
                }),
                encoding='utf-8',
            )
            model = {'name': 'demo', 'services': {}, 'volumes': {}}
            with mock.patch.object(restore_plan, 'manifest', return_value=value), \
                    mock.patch.object(restore_plan, 'compose_model', return_value=model), \
                    mock.patch.object(restore_plan, 'running_services', return_value=[]), \
                    mock.patch.object(restore_plan, 'docker_mount_conflicts', return_value=()):
                plan = restore_plan.prepare_restore_plan(
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

    def test_compose_env_source_is_deferred_with_compose_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            value, _restored = self._rebuild_fixture(Path(tmp))
            value['compose']['env_file'] = 'compose.env'
            value['sources']['paths'].append({
                'id': 'env', 'path': 'compose.env', 'required': True,
            })

            self.assertEqual(
                restore_plan.deferred_compose_sources(value),
                ('compose', 'env'),
            )

    def test_rebuild_requires_configured_env_file_snapshot_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            value, _restored = self._rebuild_fixture(Path(tmp))
            value['compose']['env_file'] = 'compose.env'

            with self.assertRaisesRegex(RuntimeError, 'env file'):
                restore_plan._validate_rebuild_compose_sources(
                    value, 'rebuild', restore_plan.compose_targets(value),
                )

    def test_rebuild_requires_every_target_to_be_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            with mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_plan, 'docker_mount_conflicts', return_value=()):
                plan = restore_plan.prepare_restore_plan(
                    {}, value, restored,
                )
                self.assertEqual(plan.mode, 'rebuild')

                target = Path(value['_dir']) / 'compose.yaml'
                target.parent.mkdir(parents=True)
                target.write_text('occupied\n', encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'mixed|already exists'):
                    restore_plan.prepare_restore_plan(
                        {}, value, restored,
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
                restore_plan.prepare_restore_plan(
                    {}, value, restored,
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
                restore_plan, 'docker_volume_exists', return_value=True,
            ), self.assertRaisesRegex(RuntimeError, 'volume already exists'):
                restore_plan.prepare_restore_plan(
                    {}, value, restored,
                )

    def test_rebuild_authorizes_compose_volume_by_protected_logical_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            value['sources']['volumes'] = [{
                'id': 'db', 'compose_volume': 'db',
            }]
            write_restore_inventory(restored, paths=[{
                'id': 'compose', 'path': 'compose.yaml', 'type': 'file',
                'present': True,
            }], volumes=[{
                'id': 'db', 'compose_volume': 'db',
                'actual_name': 'attacker_selected', 'present': True,
            }])
            inventory = restore_plan.load_restore_inventory(restored)
            restored_volumes = restore_plan.inventory_volumes(value, inventory)

            with mock.patch.object(
                restore_plan, 'docker_project_containers', return_value=(),
            ):
                authorized, identity, running = restore_plan._authorize_rebuild_restore(
                    value, inventory, restored_volumes,
                )

            self.assertIs(authorized, value)
            self.assertIs(identity, inventory.compose)
            self.assertEqual(running, ())

    def test_rebuild_accepts_compose_volume_without_rewriting_it_to_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            value, restored = self._rebuild_fixture(base)
            value['sources']['volumes'] = [{
                'id': 'db', 'compose_volume': 'db',
            }]
            write_restore_inventory(restored, paths=[{
                'id': 'compose', 'path': 'compose.yaml', 'type': 'file',
                'present': True,
            }], volumes=[{
                'id': 'db', 'compose_volume': 'db',
                'actual_name': 'reviewed_db', 'present': True,
            }])
            (restored / 'volumes' / 'db').mkdir(parents=True)

            with mock.patch.object(
                restore_plan, 'docker_volume_exists', return_value=False,
            ), mock.patch.object(
                restore_plan, 'docker_project_containers', return_value=(),
            ), mock.patch.object(
                restore_plan, 'docker_mount_conflicts', return_value=(),
            ):
                plan = restore_plan.prepare_restore_plan({}, value, restored)

            self.assertEqual(plan.mode, 'rebuild')
            self.assertEqual(plan.volumes, (
                (value['sources']['volumes'][0], 'reviewed_db'),
            ))

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
            real_copy = restore_apply.atomic_copy_file

            def record_copy(source, target, **kwargs):
                publications.append(Path(target).name)
                return real_copy(source, target, **kwargs)

            with mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_plan, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'sync_volumes'), \
                    mock.patch.object(restore_apply, 'atomic_copy_file', side_effect=record_copy):
                restore_apply.apply_one(
                    {}, value, restored,
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
                restore_plan.prepare_restore_plan(
                    {}, value, restored,
                )

if __name__ == '__main__':
    unittest.main()
