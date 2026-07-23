import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import restore as backupctl
from homelab_backup import restore_apply, restore_plan
from tests.helpers import manifest as make_manifest, write_restore_inventory


class RepositoryBoundaryTests(unittest.TestCase):
    def test_invalid_service_tag_is_rejected(self):
        result = mock.Mock(stdout=json.dumps([{'tags': ['service:/etc']}]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'invalid service tag'):
                backupctl.repository_services({'host_id': 'host'})

    def test_explicit_snapshot_is_resolved_for_the_requested_service(self):
        snapshot_id = 'a' * 64
        result = mock.Mock(stdout=json.dumps([{
            'id': snapshot_id,
            'hostname': 'host',
            'tags': ['service:demo'],
        }]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            resolved = backupctl.resolve_explicit_snapshot(
                {'host_id': 'host'}, 'demo', snapshot_id[:8],
            )

        self.assertEqual(resolved, snapshot_id)

    def test_explicit_snapshot_from_another_service_is_rejected(self):
        result = mock.Mock(stdout=json.dumps([{
            'id': 'a' * 64,
            'hostname': 'host',
            'tags': ['service:other'],
        }]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaises(SystemExit):
                backupctl.resolve_explicit_snapshot(
                    {'host_id': 'host'}, 'demo', 'aaaaaaaa',
                )

    def test_invalid_explicit_snapshot_id_is_rejected_before_repository_access(self):
        with mock.patch.object(backupctl, 'run') as run_mock:
            with self.assertRaises(SystemExit):
                backupctl.resolve_explicit_snapshot(
                    {'host_id': 'host'}, 'demo', 'latest:subfolder',
                )
        run_mock.assert_not_called()

    def test_delete_snapshot_resolves_scope_before_forget(self):
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=False, yes=True,
        )
        config_data = {'host_id': 'host'}
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value='a' * 64,
        ) as resolve_mock, mock.patch.object(
            backupctl, 'restic_env', return_value={'RESTIC': 'env'},
        ), mock.patch.object(backupctl, 'run') as run_mock:
            backupctl.cmd_delete_snapshot(config_data, args)

        resolve_mock.assert_called_once_with(config_data, 'demo', 'aaaaaaaa')
        run_mock.assert_called_once_with(
            ['restic', 'forget', 'a' * 64], env={'RESTIC': 'env'},
        )

    def test_delete_snapshot_rejects_invalid_service_before_repository_access(self):
        args = mock.Mock(
            service='../other', snapshot='aaaaaaaa', prune=False, yes=True,
        )
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot',
        ) as resolve_mock, mock.patch.object(backupctl, 'run') as run_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        resolve_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_delete_snapshot_can_prune_immediately(self):
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=True, yes=True,
        )
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value='a' * 64,
        ), mock.patch.object(
            backupctl, 'restic_env', return_value={},
        ), mock.patch.object(backupctl, 'run') as run_mock:
            backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        self.assertEqual(run_mock.call_args_list, [
            mock.call(['restic', 'forget', 'a' * 64], env={}),
            mock.call(['restic', 'prune'], env={}),
        ])

    def test_delete_snapshot_reports_committed_forget_when_prune_fails(self):
        snapshot = 'a' * 64
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=True, yes=True,
        )
        error = StringIO()
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value=snapshot,
        ), mock.patch.object(
            backupctl, 'restic_env', return_value={'RESTIC': 'env'},
        ), mock.patch.object(
            backupctl, 'run', side_effect=[None, OSError('prune unavailable')],
        ) as run_mock, redirect_stderr(error):
            with self.assertRaisesRegex(OSError, 'prune unavailable'):
                backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        self.assertEqual(run_mock.call_args_list, [
            mock.call(
                ['restic', 'forget', snapshot], env={'RESTIC': 'env'},
            ),
            mock.call(['restic', 'prune'], env={'RESTIC': 'env'}),
        ])
        self.assertIn(
            f'Snapshot {snapshot} for demo was deleted, but repository prune failed',
            error.getvalue(),
        )

    def test_delete_snapshot_reports_prune_command_details_once(self):
        snapshot = 'a' * 64
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=True, yes=True,
        )
        failure = backupctl.CommandError(
            ['restic', 'prune'], 1, stderr='backend unavailable',
        )
        error = StringIO()
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value=snapshot,
        ), mock.patch.object(
            backupctl, 'restic_env', return_value={},
        ), mock.patch.object(
            backupctl, 'run', side_effect=[None, failure],
        ), redirect_stderr(error):
            with self.assertRaises(backupctl.CommandError):
                backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        self.assertTrue(failure.reported)
        self.assertIn(
            f'Snapshot {snapshot} for demo was deleted, but repository prune failed',
            error.getvalue(),
        )
        self.assertIn('backend unavailable', error.getvalue())

    def test_noninteractive_delete_snapshot_requires_yes(self):
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=False, yes=False,
        )
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value='a' * 64,
        ), mock.patch.object(
            backupctl.sys.stdin, 'isatty', return_value=False,
        ), mock.patch.object(backupctl, 'run') as run_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        run_mock.assert_not_called()

    def test_interactive_delete_snapshot_can_be_cancelled(self):
        args = mock.Mock(
            service='demo', snapshot='aaaaaaaa', prune=False, yes=False,
        )
        with mock.patch.object(
            backupctl, 'resolve_explicit_snapshot', return_value='a' * 64,
        ), mock.patch.object(
            backupctl.sys.stdin, 'isatty', return_value=True,
        ), mock.patch.object(
            backupctl, 'prompt_yes_no', return_value=False,
        ), mock.patch.object(backupctl, 'run') as run_mock:
            backupctl.cmd_delete_snapshot({'host_id': 'host'}, args)

        run_mock.assert_not_called()

    def test_restore_size_is_scoped_to_service_snapshot_and_host(self):
        result = mock.Mock(stdout=json.dumps({
            'total_size': 123456,
            'total_file_count': 7,
        }))
        config_data = {'host_id': 'host'}
        with mock.patch.object(backupctl, 'run', return_value=result) as run_mock, \
                mock.patch.object(backupctl, 'restic_env', return_value={'RESTIC': 'env'}):
            size = backupctl.estimate_restore_size(
                config_data, 'demo', '01234567',
            )

        self.assertEqual(size, 123456)
        run_mock.assert_called_once_with([
            'restic', 'stats', '--json', '--mode', 'restore-size',
            '--host', 'host', '--tag', 'service:demo', '01234567',
        ], env={'RESTIC': 'env'}, capture=True)

    def test_invalid_restore_size_output_is_rejected(self):
        result = mock.Mock(stdout=json.dumps({'total_size': -1}))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'restore size'):
                backupctl.estimate_restore_size(
                    {'host_id': 'host'}, 'demo', 'latest',
                )

class RestoredManifestTests(unittest.TestCase):
    MANIFEST = (
        'version: 1\n'
        'service: advent-plus\n'
        'name: "Advent Plus"\n'
        'schedule:\n'
        '  cron: "0 0 * * *"\n'
        'retention:\n'
        '  keep_last: 1\n'
        'consistency:\n'
        '  mode: external\n'
        'sources:\n'
        '  paths: []\n'
        '  volumes: []\n'
    )

    def test_apply_rebuilds_into_recorded_nested_service_directory(self):
        snapshot_manifest = (
            'version: 1\n'
            'service: advent-plus\n'
            'name: "Advent Plus"\n'
            'schedule:\n'
            '  cron: "0 0 * * *"\n'
            'retention:\n'
            '  keep_last: 1\n'
            'compose:\n'
            '  files:\n'
            '    - compose.yaml\n'
            'consistency:\n'
            '  mode: external\n'
            'sources:\n'
            '  paths:\n'
            '    - id: compose\n'
            '      path: compose.yaml\n'
            '  volumes: []\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            services.mkdir()
            services.chmod(0o755)
            restored = root / 'restored'
            meta = restored / '_meta'
            staged_compose = restored / 'paths' / 'compose' / 'compose.yaml'
            staged_compose.parent.mkdir(parents=True)
            staged_compose.write_text('services: {}\n', encoding='utf-8')
            meta.mkdir(parents=True, exist_ok=True)
            (meta / 'backup.yaml').write_text(
                snapshot_manifest, encoding='utf-8',
            )
            write_restore_inventory(
                restored,
                service='advent-plus',
                service_relative_directory='Minecraft/Advent Plus',
                paths=[{
                    'id': 'compose',
                    'path': 'compose.yaml',
                    'type': 'file',
                    'present': True,
                }],
            )
            value = backupctl.prepare_restored_manifest(
                {'services_root': str(services)},
                'advent-plus', restored, policy='restore',
            )
            target = services / 'Minecraft' / 'Advent Plus'

            with mock.patch.object(
                    restore_plan, 'docker_project_containers', return_value=(),
            ), mock.patch.object(
                    restore_plan, 'docker_mount_conflicts', return_value=(),
            ), mock.patch.object(
                    restore_apply, 'docker_project_containers', return_value=(),
            ), mock.patch.object(
                    restore_apply, 'docker_mount_conflicts', return_value=(),
            ), mock.patch.object(restore_apply, 'sync_volumes'):
                restore_apply.apply_one(
                    {'trusted_data_roots': [str(services)]},
                    value, restored,
                )

            self.assertEqual(
                (target / 'compose.yaml').read_text(encoding='utf-8'),
                'services: {}\n',
            )
            self.assertEqual(
                (target / 'backup.yaml').read_text(encoding='utf-8'),
                snapshot_manifest,
            )
            self.assertFalse((services / 'advent-plus').exists())

    def test_new_snapshot_restores_manifest_to_recorded_nested_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            services.mkdir()
            services.chmod(0o755)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                self.MANIFEST, encoding='utf-8',
            )
            write_restore_inventory(
                restored, service='advent-plus',
                service_relative_directory='Minecraft/Advent Plus',
            )

            value = backupctl.prepare_restored_manifest(
                {'services_root': str(services)},
                'advent-plus', restored, policy='restore',
            )

        self.assertEqual(
            value['_relative_dir'], 'Minecraft/Advent Plus',
        )
        self.assertEqual(
            Path(value['_path']),
            services / 'Minecraft' / 'Advent Plus' / 'backup.yaml',
        )

    def test_existing_local_manifest_location_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            local_dir = services / 'Current Location' / 'Server'
            local_dir.mkdir(parents=True)
            services.chmod(0o755)
            (services / 'Current Location').chmod(0o755)
            local_dir.chmod(0o755)
            local = local_dir / 'backup.yaml'
            local.write_text(self.MANIFEST, encoding='utf-8')
            local.chmod(0o600)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                self.MANIFEST, encoding='utf-8',
            )
            write_restore_inventory(
                restored, service='advent-plus',
                service_relative_directory='Old Location/Server',
            )

            value = backupctl.prepare_restored_manifest(
                {'services_root': str(services)},
                'advent-plus', restored, policy='keep',
            )

        self.assertEqual(
            value['_relative_dir'], 'Current Location/Server',
        )
        self.assertEqual(Path(value['_path']), local)

    def test_legacy_snapshot_without_relative_directory_uses_service_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            services.mkdir()
            services.chmod(0o755)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                self.MANIFEST, encoding='utf-8',
            )
            write_restore_inventory(restored, service='advent-plus')

            value = backupctl.prepare_restored_manifest(
                {'services_root': str(services)},
                'advent-plus', restored, policy='restore',
            )

        self.assertEqual(value['_relative_dir'], 'advent-plus')

    def test_unsafe_snapshot_relative_directory_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            services.mkdir()
            services.chmod(0o755)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                self.MANIFEST, encoding='utf-8',
            )
            write_restore_inventory(
                restored, service='advent-plus',
                service_relative_directory='../outside',
            )

            with self.assertRaises(ValueError):
                backupctl.prepare_restored_manifest(
                    {'services_root': str(services)},
                    'advent-plus', restored, policy='restore',
                )

            self.assertFalse((root / 'outside').exists())
            self.assertEqual(list(services.iterdir()), [])

    def test_snapshot_directory_uses_shared_inventory_loader_and_validator(self):
        inventory = {
            'version': 1,
            'service': 'demo',
            'service_relative_directory': 'Category/Demo',
        }
        with mock.patch.object(
                backupctl._restore_inventory,
                'load_restore_inventory',
                return_value=inventory,
        ) as load, mock.patch.object(
                backupctl._restore_inventory,
                'restore_inventory_service_directory',
                return_value=Path('Category/Demo'),
        ) as validate:
            value = backupctl._snapshot_service_relative_directory(
                '/restore/root', 'demo',
            )

        self.assertEqual(value, Path('Category/Demo'))
        load.assert_called_once_with('/restore/root')
        validate.assert_called_once_with(inventory, 'demo')

    def test_snapshot_manifest_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            services = root / 'services'
            services.mkdir()
            services.chmod(0o755)
            write_restore_inventory(restored)
            outside = root / 'outside.yaml'
            outside.write_text(
                'version: 1\nservice: demo\n', encoding='utf-8',
            )
            (meta / 'backup.yaml').symlink_to(outside)

            with self.assertRaisesRegex(ValueError, 'regular file'):
                backupctl.prepare_restored_manifest(
                    {'services_root': str(services)},
                    'demo', restored, policy='restore',
                )

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
            target.chmod(0o600)
            (root / 'services').chmod(0o755)
            service_dir.chmod(0o755)
            write_restore_inventory(restored)

            with self.assertRaises(ValueError):
                backupctl.prepare_restored_manifest(
                    {'services_root': str(root / 'services')},
                    'demo', restored, policy='restore',
                )

            self.assertEqual(target.read_text(encoding='utf-8'), original)

class RestoreCommandTests(unittest.TestCase):

    def test_low_space_restore_requires_explicit_override_without_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {
                'host_id': 'host',
                'restore_root': str(Path(tmp) / 'restores'),
            }
            error = StringIO()
            with mock.patch.object(
                backupctl, 'estimate_restore_size', return_value=2 * 1024**3,
            ), mock.patch.object(
                backupctl, 'ensure_private_directory',
                return_value=Path(config_data['restore_root']),
            ), mock.patch.object(
                backupctl.shutil, 'disk_usage',
                return_value=mock.Mock(free=3 * 1024**3 - 1),
            ), mock.patch.object(
                backupctl.sys.stdin, 'isatty', return_value=False,
            ), redirect_stderr(error), self.assertRaisesRegex(
                RuntimeError, '--allow-low-space',
            ):
                backupctl.check_restore_space(
                    config_data, 'demo', 'latest', allow_low_space=False,
                )

            self.assertIn('WARNING:', error.getvalue())

    def test_low_space_restore_can_be_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {
                'host_id': 'host',
                'restore_root': str(Path(tmp) / 'restores'),
            }
            error = StringIO()
            with mock.patch.object(
                backupctl, 'estimate_restore_size', return_value=2 * 1024**3,
            ), mock.patch.object(
                backupctl, 'ensure_private_directory',
                return_value=Path(config_data['restore_root']),
            ), mock.patch.object(
                backupctl.shutil, 'disk_usage',
                return_value=mock.Mock(free=2 * 1024**3),
            ), redirect_stderr(error):
                backupctl.check_restore_space(
                    config_data, 'demo', 'latest', allow_low_space=True,
                )

            self.assertIn('WARNING:', error.getvalue())

    def test_restore_with_exactly_one_gib_remaining_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {
                'host_id': 'host',
                'restore_root': str(Path(tmp) / 'restores'),
            }
            error = StringIO()
            with mock.patch.object(
                backupctl, 'estimate_restore_size', return_value=2 * 1024**3,
            ), mock.patch.object(
                backupctl, 'ensure_private_directory',
                return_value=Path(config_data['restore_root']),
            ), mock.patch.object(
                backupctl.shutil, 'disk_usage',
                return_value=mock.Mock(free=3 * 1024**3),
            ), redirect_stderr(error):
                backupctl.check_restore_space(
                    config_data, 'demo', 'latest', allow_low_space=False,
                )

            self.assertEqual(error.getvalue(), '')

    def test_low_space_override_is_forwarded_to_each_restore(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
            allow_low_space=True,
        )
        restored = (mock.Mock(), Path('/restore/demo'))
        with mock.patch.object(
            backupctl, 'repository_services', return_value=['demo'],
        ), mock.patch.object(
            backupctl, 'GlobalLock',
        ) as lock_mock, mock.patch.object(
            backupctl, 'restore_one', return_value=restored,
        ) as restore_mock:
            lock_mock.return_value.__enter__.return_value = True
            backupctl.cmd_restore({'lock_file': '/run/test.lock'}, args)

        restore_mock.assert_called_once_with(
            {'lock_file': '/run/test.lock'}, 'demo', 'latest', 'keep',
            allow_low_space=True,
        )

    def test_positional_services_cannot_be_combined_with_all(self):
        args = mock.Mock(
            services=['demo'], all=True, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'repository_services') as repository_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_restore({}, args)

        repository_mock.assert_not_called()

    def test_explicit_snapshot_rejects_multiple_services_before_download(self):
        args = mock.Mock(
            services=['one', 'two'], all=False, yes=True, apply=False,
            start=False, restore_manifest=False, keep_manifest=True,
            snapshot='a' * 8,
        )
        with mock.patch.object(
            backupctl, 'repository_services', return_value=['one', 'two'],
        ), mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_restore({}, args)

        restore_mock.assert_not_called()

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

    def test_explicit_snapshot_is_resolved_before_download(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='a' * 8,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {'host_id': 'host', 'lock_file': str(Path(tmp) / 'lock')}
            restored = (mock.Mock(), Path(tmp) / 'restored')
            with mock.patch.object(
                backupctl, 'repository_services', return_value=['demo'],
            ), mock.patch.object(
                backupctl, 'resolve_explicit_snapshot', return_value='a' * 64,
            ) as resolve_mock, mock.patch.object(
                backupctl, 'restore_one', return_value=restored,
            ) as restore_mock:
                backupctl.cmd_restore(config_data, args)

        resolve_mock.assert_called_once_with(config_data, 'demo', 'a' * 8)
        self.assertEqual(restore_mock.call_args.args[2], 'a' * 64)

    def test_one_service_failure_does_not_skip_later_services(self):
        args = mock.Mock(
            services=[], all=True, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {'host_id': 'host', 'lock_file': str(Path(tmp) / 'lock')}

            def restore_service(_config, service, _snapshot, _policy):
                if service == 'one':
                    raise RuntimeError('first failed')
                return mock.Mock(), Path(tmp) / service

            with mock.patch.object(
                backupctl, 'repository_services', return_value=['one', 'two'],
            ), mock.patch.object(
                backupctl, 'restore_one', side_effect=restore_service,
            ) as restore_mock:
                with self.assertRaises(SystemExit):
                    backupctl.cmd_restore(config_data, args)

        self.assertEqual(
            [call.args[1] for call in restore_mock.call_args_list],
            ['one', 'two'],
        )

    def test_missing_local_manifest_does_not_skip_later_apply(self):
        args = mock.Mock(
            services=[], all=True, yes=True, apply=True, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services_root = root / 'services'
            second_service = services_root / 'two'
            second_service.mkdir(parents=True)
            services_root.chmod(0o755)
            second_service.chmod(0o755)
            (second_service / 'backup.yaml').write_text(
                'service: two\n', encoding='utf-8',
            )
            (second_service / 'backup.yaml').chmod(0o600)
            restore_roots = {
                service: root / 'restores' / service / (
                    f'20260717-120000-00000000{index}'
                )
                for index, service in enumerate(('one', 'two'), start=1)
            }
            for restore_root in restore_roots.values():
                restore_root.mkdir(parents=True)
            config_data = {
                'host_id': 'host',
                'lock_file': str(root / 'lock'),
                'services_root': str(services_root),
                'trusted_data_roots': [],
            }

            def restore_service(_config, service, _snapshot, _policy):
                return {'service': service}, restore_roots[service]

            def authorize_local_manifest(config, value, _root, **_kwargs):
                backupctl.manifest(config, value['service'])

            with mock.patch.object(
                backupctl, 'repository_services', return_value=['one', 'two'],
            ), mock.patch.object(
                backupctl, 'restore_one', side_effect=restore_service,
            ), mock.patch.object(
                backupctl, 'validate_docker_environment',
            ), mock.patch.object(
                backupctl, 'validate_docker_bind_probe',
            ), mock.patch.object(
                backupctl, 'validate_trusted_roots',
            ), mock.patch.object(
                restore_apply, 'apply_one', side_effect=authorize_local_manifest,
            ) as apply_mock:
                with self.assertRaises(SystemExit):
                    backupctl.cmd_restore(config_data, args)

        self.assertEqual(
            [call.args[1]['service'] for call in apply_mock.call_args_list],
            ['one', 'two'],
        )

    def test_successful_apply_removes_downloaded_restore(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=True, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restored = root / 'restores' / 'demo' / '20260717-120000-000000001'
            restored.mkdir(parents=True)
            (restored / 'payload').write_text('restored', encoding='utf-8')
            config_data = {
                'host_id': 'host',
                'lock_file': str(root / 'lock'),
                'trusted_data_roots': [],
            }
            with mock.patch.object(
                backupctl, 'repository_services', return_value=['demo'],
            ), mock.patch.object(
                backupctl, 'restore_one', return_value=(mock.Mock(), restored),
            ), mock.patch.object(
                backupctl, 'validate_docker_environment',
            ), mock.patch.object(
                backupctl, 'validate_docker_bind_probe',
            ), mock.patch.object(
                backupctl, 'validate_trusted_roots',
            ), mock.patch.object(restore_apply, 'apply_one'):
                backupctl.cmd_restore(config_data, args)

        self.assertFalse(restored.exists())


class RestoreCleanupCommandTests(unittest.TestCase):

    def test_selected_restore_can_be_deleted_manually(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore = root / 'restores' / 'demo' / '20260717-120000-000000001'
            restore.mkdir(parents=True)
            for directory in (root / 'restores', restore.parent, restore):
                directory.chmod(0o700)
            (restore / 'payload').write_text('restored', encoding='utf-8')
            args = mock.Mock(
                targets=['demo/20260717-120000-000000001'],
                all=False,
                yes=True,
            )

            backupctl.cmd_cleanup_restores({
                'restore_root': str(root / 'restores'),
                'lock_file': str(root / 'lock'),
            }, args)

            self.assertFalse(restore.exists())

    def test_duplicate_explicit_restore_target_is_deleted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore = root / 'restores' / 'demo' / '20260717-120000-000000001'
            restore.mkdir(parents=True)
            for directory in (root / 'restores', restore.parent, restore):
                directory.chmod(0o700)
            label = 'demo/20260717-120000-000000001'
            args = mock.Mock(targets=[label, label], all=False, yes=True)

            backupctl.cmd_cleanup_restores({
                'restore_root': str(root / 'restores'),
                'lock_file': str(root / 'lock'),
            }, args)

            self.assertFalse(restore.exists())

    def test_all_restores_can_be_deleted_in_one_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restores = root / 'restores'
            targets = [
                restores / 'one' / '20260717-120000-000000001',
                restores / 'two' / '20260717-120000-000000002',
            ]
            for target in targets:
                target.mkdir(parents=True)
                for directory in (restores, target.parent, target):
                    directory.chmod(0o700)
                (target / 'payload').write_text('restored', encoding='utf-8')
            args = mock.Mock(targets=[], all=True, yes=True)

            backupctl.cmd_cleanup_restores({
                'restore_root': str(restores),
                'lock_file': str(root / 'lock'),
            }, args)

            self.assertTrue(all(not target.exists() for target in targets))

    def test_cleanup_rejects_path_traversal(self):
        args = mock.Mock(targets=['../victim'], all=False, yes=True)
        with self.assertRaises(SystemExit):
            backupctl.cmd_cleanup_restores({
                'restore_root': '/var/lib/homelab-backup/restores',
                'lock_file': '/run/homelab-backup/backupctl.lock',
            }, args)


class ApplyCommandTests(unittest.TestCase):

    def test_apply_rejects_unvalidated_restore_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restores'
            workspace = restore_root / 'demo' / 'latest'
            workspace.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, 'invalid restore directory'):
                backupctl._validated_apply_workspace(
                    {'restore_root': str(restore_root)},
                    'demo', str(workspace),
                )

    def test_apply_rejects_workspace_outside_configured_restore_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restores'
            restore_root.mkdir()
            restore_root.chmod(0o700)
            outside = root / 'outside' / '20260717-120000-000000001'
            outside.mkdir(parents=True)
            args = mock.Mock(
                service='demo', restore_dir=str(outside), start=False, yes=True,
            )
            config_data = {
                'restore_root': str(restore_root),
                'lock_file': str(root / 'lock'),
                'trusted_data_roots': [],
            }

            with mock.patch.object(backupctl, 'validate_docker_environment'), \
                    mock.patch.object(backupctl, 'validate_docker_bind_probe'), \
                    mock.patch.object(backupctl, 'manifest', return_value=mock.Mock()), \
                    mock.patch.object(restore_apply, 'apply_one') as apply_mock:
                with self.assertRaises(ValueError):
                    backupctl.cmd_apply(config_data, args)

            apply_mock.assert_not_called()

    def test_apply_rejects_symlinked_restore_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restores'
            service_root = restore_root / 'demo'
            service_root.mkdir(parents=True)
            for directory in (restore_root, service_root):
                directory.chmod(0o700)
            outside = root / 'outside'
            outside.mkdir()
            workspace = service_root / '20260717-120000-000000001'
            workspace.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, 'not a real directory'):
                backupctl._validated_apply_workspace(
                    {'restore_root': str(restore_root)},
                    'demo', str(workspace),
                )

    def test_apply_validates_trust_and_workspace_while_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restores'
            workspace = (
                restore_root / 'demo' / '20260717-120000-000000001'
            )
            workspace.mkdir(parents=True)
            for directory in (restore_root, workspace.parent, workspace):
                directory.chmod(0o700)
            args = mock.Mock(
                service='demo', restore_dir=str(workspace),
                start=False, yes=True,
            )
            config_data = {
                'restore_root': str(restore_root),
                'lock_file': str(root / 'lock'),
                'trusted_data_roots': [],
            }
            lock_state = {'held': False}

            class TrackingLock:
                def __init__(self, _path):
                    pass

                def __enter__(self):
                    lock_state['held'] = True
                    return True

                def __exit__(self, *_args):
                    lock_state['held'] = False

            def require_lock(*_args, **_kwargs):
                self.assertTrue(lock_state['held'])

            with mock.patch.object(backupctl, 'GlobalLock', TrackingLock), \
                    mock.patch.object(backupctl, 'validate_docker_environment'), \
                    mock.patch.object(backupctl, 'validate_docker_bind_probe'), \
                    mock.patch.object(
                        backupctl, 'validate_trusted_roots',
                        side_effect=require_lock,
                    ) as trust_mock, mock.patch.object(
                        backupctl, 'validate_control_root',
                        side_effect=require_lock,
                    ) as control_mock, mock.patch.object(
                        backupctl, 'manifest', return_value=mock.Mock(),
                    ), mock.patch.object(restore_apply, 'apply_one') as apply_mock:
                backupctl.cmd_apply(config_data, args)

            trust_mock.assert_called_once_with([])
            self.assertGreaterEqual(control_mock.call_count, 3)
            apply_mock.assert_called_once_with(
                config_data, mock.ANY, workspace, start_services=False,
            )


class RestorePlanErrorTests(unittest.TestCase):

    def test_missing_restore_workspace_is_a_normal_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = make_manifest(Path(tmp))
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                restore_plan._load_and_validate_restore_input(
                    value, Path(tmp) / 'missing',
                )


if __name__ == '__main__':
    unittest.main()
