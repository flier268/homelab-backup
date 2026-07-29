import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import (
    backup, capture, maintenance, restore, restore_apply, restore_plan, staging,
)
from homelab_backup.inventory_models import RestoreInventoryModel
from tests.helpers import manifest


def files_inventory(service='demo', paths=None):
    return {
        'version': 1,
        'service': service,
        'paths': paths or [],
        'volumes': [],
        'consistency': {
            'mode': 'files',
            'guarantee': 'best-effort',
            'optional_action_failures': [],
            'writers': [],
        },
    }


class FilesBackupTests(unittest.TestCase):
    def test_systemd_orders_after_docker_without_requiring_it(self):
        unit = (
            Path(__file__).resolve().parents[1]
            / 'systemd' / 'homelab-backup.service'
        ).read_text(encoding='utf-8')
        self.assertIn(
            'After=docker.service network-online.target',
            unit,
        )
        self.assertNotIn('Requires=docker.service', unit)

    def test_invalid_manifest_does_not_create_docker_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'invalid'})
            config = {
                'staging_root': str(root / 'staging'),
                'restore_root': str(root / 'restore'),
                'password_file': str(root / 'password'),
                'rclone_config': str(root / 'rclone.conf'),
                'services_root': str(root),
            }
            which = mock.Mock(
                side_effect=lambda command: f'/usr/bin/{command}',
            )

            with mock.patch.object(
                    backup, 'manifests', return_value=[value],
            ), mock.patch.object(
                    backup, 'validate_control_root',
            ), mock.patch.object(
                    backup, 'validate_control_file',
            ), mock.patch.object(
                    backup.shutil, 'which', which,
            ), redirect_stdout(StringIO()), redirect_stderr(
                    StringIO(),
            ), self.assertRaises(SystemExit):
                backup.cmd_validate(config, mock.Mock())

            self.assertNotIn(mock.call('docker'), which.call_args_list)

    def test_run_due_reaches_files_stage_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'files'}, sources={
                'paths': [], 'volumes': [],
            })
            config = {
                'lock_file': str(root / 'lock'),
            }

            def scheduled_backup(current_config, current_manifest):
                staging._build_stage_context(
                    current_config, current_manifest,
                    root / 'stage', 'files', False,
                )

            with mock.patch.object(
                    maintenance, 'GlobalLock',
            ) as lock, mock.patch.object(
                    maintenance, 'manifests', return_value=[value],
            ), mock.patch.object(
                    maintenance, 'due_status', return_value=(True, 'due', None),
            ), mock.patch.object(
                    maintenance, 'backup_one', side_effect=scheduled_backup,
            ) as backup_one, mock.patch.object(
                    staging, 'validate_runtime_sources',
            ), mock.patch.object(
                    staging, 'validate_docker_environment',
                    side_effect=AssertionError('Docker validation called'),
            ) as docker_environment, mock.patch.object(
                    staging, 'validate_docker_bind_probe',
                    side_effect=AssertionError('Docker probe called'),
            ) as docker_probe, mock.patch.object(
                    staging, 'compose_model',
                    side_effect=AssertionError('Compose called'),
            ) as compose_model, redirect_stdout(StringIO()):
                lock.return_value.__enter__.return_value = True
                maintenance.cmd_run_due(config, mock.Mock())

            backup_one.assert_called_once_with(config, value)
            docker_environment.assert_not_called()
            docker_probe.assert_not_called()
            compose_model.assert_not_called()

    def test_validate_files_only_runtime_never_calls_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'files'}, sources={
                'paths': [{'id': 'data', 'path': 'data'}],
                'volumes': [],
            })
            config = {
                'staging_root': str(root / 'staging'),
                'restore_root': str(root / 'restore'),
                'password_file': str(root / 'password'),
                'rclone_config': str(root / 'rclone.conf'),
            }

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    backup, 'validate_control_root',
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'validate_control_file',
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'manifests', return_value=[value],
                ))
                stack.enter_context(mock.patch.object(
                    backup.shutil, 'which',
                    side_effect=lambda command: (
                        None if command == 'docker' else f'/usr/bin/{command}'
                    ),
                ))
                docker_environment = stack.enter_context(mock.patch.object(
                    backup, 'validate_docker_environment',
                    side_effect=AssertionError('Docker validation called'),
                ))
                docker_probe = stack.enter_context(mock.patch.object(
                    backup, 'validate_docker_bind_probe',
                    side_effect=AssertionError('Docker probe called'),
                ))
                compose_model = stack.enter_context(mock.patch.object(
                    backup, 'compose_model',
                    side_effect=AssertionError('Compose called'),
                ))
                validate_sources = stack.enter_context(mock.patch.object(
                    backup, 'validate_runtime_sources',
                ))
                with redirect_stdout(StringIO()):
                    backup.cmd_validate(config, mock.Mock())

            docker_environment.assert_not_called()
            docker_probe.assert_not_called()
            compose_model.assert_not_called()
            validate_sources.assert_called_once_with(
                config, value, None, allow_missing_paths=False,
            )

    def test_validate_docker_mode_retains_docker_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            config = {
                'staging_root': str(root / 'staging'),
                'restore_root': str(root / 'restore'),
                'password_file': str(root / 'password'),
                'rclone_config': str(root / 'rclone.conf'),
                'volume_helper_image': 'homelab-backup-volume-helper:local',
            }

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    backup, 'validate_control_root',
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'validate_control_file',
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'manifests', return_value=[value],
                ))
                stack.enter_context(mock.patch.object(
                    backup.shutil, 'which',
                    side_effect=lambda command: f'/usr/bin/{command}',
                ))
                docker_environment = stack.enter_context(mock.patch.object(
                    backup, 'validate_docker_environment',
                ))
                docker_probe = stack.enter_context(mock.patch.object(
                    backup, 'validate_docker_bind_probe',
                ))
                compose_model = stack.enter_context(mock.patch.object(
                    backup, 'compose_model', return_value={},
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'validate_runtime_sources',
                ))
                stack.enter_context(mock.patch.object(
                    backup, 'run', return_value=mock.Mock(stdout=''),
                ))
                with redirect_stdout(StringIO()):
                    backup.cmd_validate(config, mock.Mock())

            docker_environment.assert_called_once_with()
            docker_probe.assert_called_once_with(config)
            compose_model.assert_called_once_with(value)

    def test_files_backup_runs_actions_and_never_calls_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'files'}, sources={
                'paths': [{'id': 'data', 'path': 'data'}],
                'volumes': [],
            })
            config = {
                'staging_root': str(root / 'staging'),
                'state_root': str(root / 'state'),
            }
            saved = {}
            events = []
            entry = {
                'id': 'data', 'path': 'data', 'type': 'directory',
                'present': True, 'capture_method': 'best-effort',
            }

            def ensure(path, replace=False):
                path = Path(path)
                if replace and path.exists():
                    shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)

            def write_json(path, payload):
                if Path(path).name == 'inventory.json':
                    saved.update(payload)

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    staging, 'cleanup_snapshot_state',
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'ensure_private_directory', side_effect=ensure,
                ))
                validate_sources = stack.enter_context(mock.patch.object(
                    staging, 'validate_runtime_sources',
                ))
                docker_environment = stack.enter_context(mock.patch.object(
                    staging, 'validate_docker_environment',
                ))
                docker_probe = stack.enter_context(mock.patch.object(
                    staging, 'validate_docker_bind_probe',
                ))
                compose_model = stack.enter_context(mock.patch.object(
                    staging, 'compose_model',
                ))
                compose_identity = stack.enter_context(mock.patch.object(
                    staging, 'compose_identity',
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'run_before_actions',
                    side_effect=lambda _m: events.append('before') or [],
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'run_finally_actions',
                    side_effect=lambda _m: events.append('finally') or [],
                ))
                stack.enter_context(mock.patch.object(
                    capture, 'validate_path_payloads',
                ))
                stack.enter_context(mock.patch.object(
                    capture, 'estimate_backup_size', return_value=0,
                ))
                stack.enter_context(mock.patch.object(
                    capture, '_check_backup_space',
                ))
                stack.enter_context(mock.patch.object(
                    capture, 'sync_paths', return_value=[entry],
                ))
                sync_volumes = stack.enter_context(mock.patch.object(
                    capture, 'sync_volumes',
                ))
                writer_maps = stack.enter_context(mock.patch.object(
                    capture, 'docker_writer_maps',
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'atomic_copy_file', side_effect=shutil.copy2,
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'atomic_write_json', side_effect=write_json,
                ))
                backup.stage_service(config, value)

            self.assertEqual(events, ['before', 'finally'])
            validate_sources.assert_called_once_with(config, value, None)
            for docker_call in (
                    docker_environment, docker_probe, compose_model,
                    compose_identity, sync_volumes, writer_maps):
                docker_call.assert_not_called()
            self.assertNotIn('compose', saved)
            self.assertEqual(saved['volumes'], [])
            self.assertEqual(saved['consistency']['mode'], 'files')
            self.assertEqual(saved['consistency']['guarantee'], 'best-effort')


class FilesRestoreTests(unittest.TestCase):
    def _write_inventory(self, root, payload):
        meta = root / '_meta'
        meta.mkdir(parents=True)
        (meta / 'inventory.json').write_text(
            json.dumps(payload), encoding='utf-8',
        )

    def test_files_inventory_requires_no_compose_or_volumes(self):
        valid = files_inventory()
        model = RestoreInventoryModel.from_snapshot_data(valid)
        self.assertIsNone(model.compose)
        self.assertEqual(model.volumes, ())

        invalid = files_inventory()
        invalid['compose'] = {
            'project_name': 'demo', 'compose_files': ['compose.yaml'],
            'services': [], 'volumes': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), consistency={'mode': 'files'})
            from homelab_backup.restore_inventory import validate_restore_inventory
            with self.assertRaisesRegex(RuntimeError, 'must not contain Compose'):
                validate_restore_inventory(value, invalid)

    def test_existing_and_rebuild_plans_do_not_call_docker(self):
        for deployment in ('existing', 'rebuild'):
            with self.subTest(deployment=deployment), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                service_dir = root / 'service'
                if deployment == 'existing':
                    service_dir.mkdir()
                    manifest_path = service_dir / 'backup.yaml'
                    manifest_path.write_text(
                        'version: 1\nservice: demo\n', encoding='utf-8',
                    )
                    manifest_path.chmod(0o600)
                else:
                    manifest_path = service_dir / 'backup.yaml'
                value = {
                    '_path': str(manifest_path),
                    '_dir': str(service_dir),
                    'version': 1,
                    'service': 'demo',
                    'schedule': {'cron': '0 0 * * *'},
                    'retention': {'keep_last': 1},
                    'consistency': {'mode': 'files'},
                    'sources': {'paths': [], 'volumes': []},
                }
                restored = root / 'restored'
                self._write_inventory(restored, files_inventory())
                docker_functions = (
                    'compose_model', 'compose_identity', 'running_services',
                    'docker_mount_conflicts', 'docker_project_containers',
                    'docker_volume_exists',
                )
                with ExitStack() as stack:
                    calls = [
                        stack.enter_context(mock.patch.object(
                            restore_plan, name,
                            side_effect=AssertionError(f'{name} called'),
                        ))
                        for name in docker_functions
                    ]
                    stack.enter_context(mock.patch.object(
                        restore_plan, '_validate_path_targets', return_value=[],
                    ))
                    plan = restore_plan.prepare_restore_plan(
                        {}, value, restored,
                    )
                self.assertEqual(plan.mode, deployment)
                self.assertEqual(plan.project_name, '')
                self.assertEqual(plan.volumes, ())
                for call in calls:
                    call.assert_not_called()

    def test_start_services_is_rejected_before_restore_mutation(self):
        root = Path('/restore')
        value = {
            'service': 'demo',
            'consistency': {'mode': 'files'},
            'sources': {'paths': [], 'volumes': []},
        }
        plan = restore_plan.RestorePlan(
            root, mock.Mock(), value, 'existing', (), (), (), (), '',
        )
        dependencies = mock.Mock()
        dependencies.prepare_plan.return_value = plan
        workflow = restore_apply.RestoreApplyWorkflow(dependencies)

        with self.assertRaisesRegex(ValueError, '--start is not supported'):
            workflow.apply({}, value, root, start_services=True)
        dependencies.stop_services.assert_not_called()
        dependencies.restore_data.assert_not_called()

    def test_apply_files_restore_never_calls_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'service'
            service_dir.mkdir()
            manifest_path = service_dir / 'backup.yaml'
            manifest_path.write_text(
                'version: 1\nservice: demo\n', encoding='utf-8',
            )
            manifest_path.chmod(0o600)
            value = {
                '_path': str(manifest_path),
                '_dir': str(service_dir),
                'version': 1,
                'service': 'demo',
                'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'consistency': {'mode': 'files'},
                'sources': {'paths': [], 'volumes': []},
            }
            restored = root / 'restored'
            self._write_inventory(restored, files_inventory())

            with ExitStack() as stack:
                plan_calls = [
                    stack.enter_context(mock.patch.object(
                        restore_plan, name,
                        side_effect=AssertionError(f'{name} called'),
                    ))
                    for name in (
                        'compose_model', 'compose_identity', 'running_services',
                        'docker_mount_conflicts', 'docker_project_containers',
                        'docker_volume_exists',
                    )
                ]
                apply_calls = [
                    stack.enter_context(mock.patch.object(
                        restore_apply, name,
                        side_effect=AssertionError(f'{name} called'),
                    ))
                    for name in (
                        'compose_run', 'docker_mount_conflicts',
                        'docker_project_containers', 'docker_volume_exists',
                        'sync_volumes',
                    )
                ]
                restore_apply.apply_one({}, value, restored)

            for call in (*plan_calls, *apply_calls):
                call.assert_not_called()

    def test_restore_command_applies_files_snapshot_without_docker(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=True, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
            allow_low_space=False,
        )
        value = {
            'service': 'demo',
            'consistency': {'mode': 'files'},
            'sources': {'paths': [], 'volumes': []},
        }
        restored = Path('/restore/demo')
        config = {
            'host_id': 'host',
            'lock_file': '/run/test.lock',
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                restore, 'repository_services', return_value=['demo'],
            ))
            lock = stack.enter_context(mock.patch.object(
                restore, 'GlobalLock',
            ))
            lock.return_value.__enter__.return_value = True
            stack.enter_context(mock.patch.object(
                restore, 'restore_one', return_value=(value, restored),
            ))
            docker_environment = stack.enter_context(mock.patch.object(
                restore, 'validate_docker_environment',
                side_effect=AssertionError('Docker validation called'),
            ))
            docker_probe = stack.enter_context(mock.patch.object(
                restore, 'validate_docker_bind_probe',
                side_effect=AssertionError('Docker probe called'),
            ))
            apply_one = stack.enter_context(mock.patch.object(
                restore_apply, 'apply_one',
            ))
            stack.enter_context(mock.patch.object(
                restore, 'clear_control_leaf',
            ))
            with redirect_stdout(StringIO()):
                restore.cmd_restore(config, args)

        docker_environment.assert_not_called()
        docker_probe.assert_not_called()
        apply_one.assert_called_once_with(
            config, value, restored, start_services=False,
        )

    def test_apply_command_uses_downloaded_files_snapshot_without_docker(self):
        args = mock.Mock(
            service='demo',
            restore_dir='/restore/demo/20260717-120000-000000001',
            start=False,
            yes=True,
        )
        value = {
            'service': 'demo',
            'consistency': {'mode': 'files'},
            'sources': {'paths': [], 'volumes': []},
        }
        restored = Path(args.restore_dir)
        config = {
            'lock_file': '/run/test.lock',
        }

        with ExitStack() as stack:
            lock = stack.enter_context(mock.patch.object(
                restore, 'GlobalLock',
            ))
            lock.return_value.__enter__.return_value = True
            stack.enter_context(mock.patch.object(
                restore, '_validated_apply_workspace', return_value=restored,
            ))
            stack.enter_context(mock.patch.object(
                restore, 'manifest', return_value=value,
            ))
            docker_environment = stack.enter_context(mock.patch.object(
                restore, 'validate_docker_environment',
                side_effect=AssertionError('Docker validation called'),
            ))
            docker_probe = stack.enter_context(mock.patch.object(
                restore, 'validate_docker_bind_probe',
                side_effect=AssertionError('Docker probe called'),
            ))
            apply_one = stack.enter_context(mock.patch.object(
                restore_apply, 'apply_one',
            ))
            restore.cmd_apply(config, args)

        docker_environment.assert_not_called()
        docker_probe.assert_not_called()
        apply_one.assert_called_once_with(
            config, value, restored, start_services=False,
        )
