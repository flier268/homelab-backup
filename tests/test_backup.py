import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from homelab_backup import backup, common, storage
from tests.helpers import manifest


class GhostHookExampleTests(unittest.TestCase):
    def test_hooks_restore_only_services_that_were_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'ghost'
            service_dir.mkdir()
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            log = root / 'docker.log'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1 $2" = "compose ps" ]; then\n'
                '  printf "web\\ndb\\n"\n'
                'fi\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            example = common.load_yaml(
                Path(__file__).resolve().parents[1] / 'examples' / 'ghost.backup.yaml'
            )
            state_dir = root / 'hook-state'
            environment = os.environ.copy()
            environment.update({
                'PATH': f'{bin_dir}:{environment["PATH"]}',
                'DOCKER_LOG': str(log),
                'BACKUPCTL_HOOK_STATE_DIR': str(state_dir),
            })

            for hook_name in ('before', 'after'):
                common.run(
                    ['/bin/bash', '-euo', 'pipefail', '-c',
                     example['consistency'][hook_name][0]],
                    cwd=service_dir,
                    env=environment,
                )

            commands = log.read_text(encoding='utf-8').splitlines()
            self.assertIn('compose stop web db', commands)
            self.assertIn('compose start web db', commands)
            self.assertFalse((state_dir / 'ghost.running-services').exists())

    def test_failed_dump_does_not_start_services_that_were_never_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'ghost'
            service_dir.mkdir()
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            log = root / 'docker.log'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
                'if [ "$1 $2" = "compose ps" ]; then\n'
                '  printf "web\\n"\n'
                '  exit 0\n'
                'fi\n'
                'if [ "$1 $2" = "compose exec" ]; then\n'
                '  exit 1\n'
                'fi\n'
                'exit 0\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            example = common.load_yaml(
                Path(__file__).resolve().parents[1] / 'examples' / 'ghost.backup.yaml'
            )
            environment = os.environ.copy()
            environment.update({
                'PATH': f'{bin_dir}:{environment["PATH"]}',
                'DOCKER_LOG': str(log),
                'BACKUPCTL_HOOK_STATE_DIR': str(root / 'hook-state'),
            })

            with self.assertRaises(common.CommandError):
                common.run(
                    ['/bin/bash', '-euo', 'pipefail', '-c',
                     example['consistency']['before'][0]],
                    cwd=service_dir,
                    env=environment,
                )
            common.run(
                ['/bin/bash', '-euo', 'pipefail', '-c',
                 example['consistency']['after'][0]],
                cwd=service_dir,
                env=environment,
            )

            commands = log.read_text(encoding='utf-8').splitlines()
            self.assertFalse(any('compose stop' in item for item in commands))
            self.assertFalse(any('compose start' in item for item in commands))

class StagingLifecycleTests(unittest.TestCase):
    def test_hook_may_create_a_required_path_before_dynamic_validation(self):
        value = manifest(self.root, consistency={'mode': 'hooks'}, sources={
            'paths': [{'id': 'dump', 'path': 'backup-dumps'}], 'volumes': [],
        })
        dump = Path(value['_dir']) / 'backup-dumps'

        def create_dump(_manifest, name):
            if name == 'before':
                dump.mkdir()
                (dump / 'database.sql').write_text('dump', encoding='utf-8')

        with mock.patch.object(backup, 'hooks', side_effect=create_dump), \
                mock.patch.object(backup, 'sync_volumes'):
            stage = backup.stage_service(self.config, value)

        self.assertEqual(
            (stage / 'paths' / 'dump' / 'database.sql').read_text(encoding='utf-8'),
            'dump',
        )

    def test_hook_cannot_create_a_missing_required_volume(self):
        value = manifest(self.root, consistency={'mode': 'hooks'}, sources={
            'paths': [],
            'volumes': [{'id': 'database', 'name': 'demo_database'}],
        })

        with mock.patch.object(
            storage, 'docker_volume_exists', return_value=False,
        ), mock.patch.object(backup, 'hooks') as hooks_mock:
            with self.assertRaisesRegex(
                RuntimeError, 'Docker volume does not exist',
            ):
                backup.stage_service(self.config, value)

        hooks_mock.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            'staging_root': str(self.root / 'staging'),
            'trusted_data_roots': [str(self.root / 'demo')],
        }
        self.docker_patch = mock.patch.object(
            backup, 'validate_docker_environment', return_value='unix:///var/run/docker.sock',
        )
        self.compose_patch = mock.patch.object(
            backup, 'compose_model', return_value={
                'name': 'demo', 'services': {'app': {}}, 'volumes': {},
            },
        )
        self.writer_patch = mock.patch.object(
            backup, 'validate_no_docker_writers', return_value=None,
        )
        self.bind_probe_patch = mock.patch.object(
            backup, 'validate_docker_bind_probe', return_value=None,
        )
        self.docker_patch.start()
        self.compose_patch.start()
        self.writer_patch.start()
        self.bind_probe_patch.start()

    def tearDown(self):
        self.compose_patch.stop()
        self.docker_patch.stop()
        self.writer_patch.stop()
        self.bind_probe_patch.stop()
        self.temp.cleanup()

    def test_stale_staging_content_is_removed(self):
        stage = Path(self.config['staging_root']) / 'demo'
        stale = stage / 'paths' / 'removed-source' / 'secret.txt'
        stale.parent.mkdir(parents=True)
        Path(self.config['staging_root']).chmod(0o700)
        stage.chmod(0o700)
        stale.write_text('old data', encoding='utf-8')

        value = manifest(self.root)
        with mock.patch.object(backup, 'sync_paths', return_value=[]), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'hooks'):
            backup.stage_service(self.config, value)

        self.assertFalse(stale.exists())

    def test_staging_overlap_is_rejected_before_deletion(self):
        value = manifest(self.root)
        sentinel = Path(value['_dir']) / 'compose.yaml'
        sentinel.write_text('services: {}\n', encoding='utf-8')
        config = {
            'staging_root': str(self.root),
            'trusted_data_roots': [str(self.root / 'demo')],
        }

        with mock.patch.object(backup, 'sync_paths') as sync_mock:
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                backup.stage_service(config, value)

        self.assertTrue(sentinel.exists())
        sync_mock.assert_not_called()

    def test_staging_service_symlink_is_rejected_without_touching_target(self):
        victim = self.root / 'victim'
        victim.mkdir()
        sentinel = victim / 'keep-me'
        sentinel.write_text('important', encoding='utf-8')
        staging = self.root / 'staging'
        staging.mkdir()
        staging.chmod(0o700)
        (staging / 'demo').symlink_to(victim, target_is_directory=True)

        value = manifest(self.root)
        with mock.patch.object(backup, 'sync_paths') as sync_mock:
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                backup.stage_service({
                    'staging_root': str(staging),
                    'trusted_data_roots': [str(self.root / 'demo')],
                }, value)

        self.assertTrue(sentinel.exists())
        sync_mock.assert_not_called()

    def test_path_sources_can_be_staged(self):
        source = Path(manifest(self.root)['_dir']) / 'data'
        source.mkdir(parents=True)
        (source / 'payload.txt').write_text('snapshot', encoding='utf-8')
        value = manifest(self.root, consistency={'mode': 'none'}, sources={
            'paths': [{'id': 'data', 'path': 'data'}],
            'volumes': [],
        })
        inventory = [{
            'id': 'data', 'path': 'data', 'type': 'directory', 'present': True,
        }]

        with mock.patch.object(backup, 'sync_volumes'):
            stage = backup.stage_service(self.config, value)

        self.assertEqual(
            (stage / 'paths' / 'data' / 'payload.txt').read_text(encoding='utf-8'),
            'snapshot',
        )
        saved = json.loads((stage / '_meta' / 'inventory.json').read_text())
        self.assertEqual(saved['paths'], inventory)

    def test_inventory_uses_final_sync_metadata_before_restart(self):
        value = manifest(self.root, consistency={'mode': 'stop'}, sources={
            'paths': [{'id': 'data', 'path': 'data'}],
            'volumes': [],
        })
        live_source = Path(value['_dir']) / 'data'
        live_source.write_text('snapshot-file', encoding='utf-8')
        inventory = [{
            'id': 'data', 'path': 'data', 'type': 'file', 'present': True,
        }]

        def fake_run(command, **_kwargs):
            if 'start' in command:
                live_source.unlink()
                live_source.mkdir()
            return mock.Mock(stdout='')

        with mock.patch.object(backup, 'sync_paths', return_value=inventory), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'running_services', return_value=['app']), \
                mock.patch.object(backup, 'run', side_effect=fake_run):
            stage = backup.stage_service(self.config, value)

        saved = json.loads((stage / '_meta' / 'inventory.json').read_text())
        self.assertEqual(saved['paths'], inventory)

    def test_none_mode_does_not_run_hooks(self):
        value = manifest(self.root, consistency={'mode': 'none'})
        with mock.patch.object(backup, 'hooks') as hooks_mock, \
                mock.patch.object(backup, 'sync_paths', return_value=[]), \
                mock.patch.object(backup, 'sync_volumes'):
            backup.stage_service(self.config, value)

        hooks_mock.assert_not_called()

    def test_after_hook_runs_when_staging_fails(self):
        calls = []
        value = manifest(self.root, consistency={'mode': 'hooks'})

        def record_hook(_manifest, name):
            calls.append(name)

        with mock.patch.object(backup, 'hooks', side_effect=record_hook), \
                mock.patch.object(backup, 'sync_paths', side_effect=RuntimeError('sync failed')):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                backup.stage_service(self.config, value)

        self.assertEqual(calls, ['before', 'after'])

    def test_after_hook_failure_does_not_hide_staging_failure(self):
        value = manifest(self.root, consistency={'mode': 'hooks'})
        error = io.StringIO()

        def fail_after(_manifest, name):
            if name == 'after':
                raise RuntimeError('after failed')

        with mock.patch.object(backup, 'hooks', side_effect=fail_after), \
                mock.patch.object(
                    backup, 'sync_paths', side_effect=RuntimeError('sync failed'),
                ), redirect_stderr(error):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                backup.stage_service(self.config, value)

        self.assertIn('after hook cleanup also failed', error.getvalue())

    def test_after_hook_runs_when_before_hook_fails(self):
        calls = []
        value = manifest(self.root, consistency={'mode': 'hooks'})

        def record_hook(_manifest, name):
            calls.append(name)
            if name == 'before':
                raise RuntimeError('before failed')

        with mock.patch.object(backup, 'hooks', side_effect=record_hook):
            with self.assertRaisesRegex(RuntimeError, 'before failed'):
                backup.stage_service(self.config, value)

        self.assertEqual(calls, ['before', 'after'])

    def test_restart_failure_is_not_suppressed(self):
        value = manifest(self.root, consistency={'mode': 'stop'})

        def fake_run(cmd, **kwargs):
            if 'start' in cmd and kwargs.get('check', True):
                raise common.CommandError(cmd, 1)
            return mock.Mock(stdout='')

        with mock.patch.object(backup, 'hooks'), \
                mock.patch.object(backup, 'sync_paths'), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'running_services', return_value=['app']), \
                mock.patch.object(backup, 'run', side_effect=fake_run):
            with self.assertRaises(common.CommandError):
                backup.stage_service(self.config, value)

    def test_restart_failure_does_not_hide_staging_failure(self):
        value = manifest(self.root, consistency={'mode': 'stop'})
        error = io.StringIO()

        def fake_run(cmd, **_kwargs):
            if 'start' in cmd:
                raise common.CommandError(cmd, 1, stderr='restart failed')
            return mock.Mock(stdout='')

        with mock.patch.object(
            backup, 'sync_paths', side_effect=RuntimeError('sync failed'),
        ), mock.patch.object(
            backup, 'running_services', return_value=['app'],
        ), mock.patch.object(
            backup, 'run', side_effect=fake_run,
        ), redirect_stderr(error):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                backup.stage_service(self.config, value)

        self.assertIn('service restart cleanup also failed', error.getvalue())

    def test_stop_mode_restarts_only_existing_target_containers(self):
        value = manifest(self.root, consistency={'mode': 'stop'})

        with mock.patch.object(backup, 'sync_paths', return_value=[]), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'running_services', return_value=['app']), \
                mock.patch.object(backup, 'run') as run_mock:
            backup.stage_service(self.config, value)

        restart_command = run_mock.call_args_list[-1].args[0]
        self.assertIn('start', restart_command)
        self.assertNotIn('up', restart_command)
        self.assertEqual(restart_command[-1], 'app')


class BackupCommandTests(unittest.TestCase):

    def test_backup_space_requires_reserve_unless_explicitly_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {'service': 'demo'}
            disk = mock.Mock(free=common.MIN_FREE_BYTES + 99)
            with mock.patch.object(backup.shutil, 'disk_usage', return_value=disk), \
                    self.assertRaisesRegex(RuntimeError, 'insufficient backup'):
                backup._check_backup_space({}, value, root, 100)

            with mock.patch.object(backup.shutil, 'disk_usage', return_value=disk):
                backup._check_backup_space(
                    {}, value, root, 100, allow_low_space=True,
                )

    def test_backup_space_accepts_exact_one_gib_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disk = mock.Mock(free=common.MIN_FREE_BYTES + 100)
            with mock.patch.object(backup.shutil, 'disk_usage', return_value=disk):
                backup._check_backup_space({}, {'service': 'demo'}, root, 100)

    def test_unknown_service_does_not_skip_later_explicit_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            args = mock.Mock(services=['missing', 'two'])
            with mock.patch.object(
                backup, 'manifest',
                side_effect=[ValueError('unknown service'), {'service': 'two'}],
            ), mock.patch.object(backup, 'backup_one') as backup_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_backup(config, args)

        self.assertEqual(backup_mock.call_args.args[1]['service'], 'two')

    def test_one_service_failure_does_not_skip_later_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            args = mock.Mock(services=['one', 'two'])
            services = {
                'one': {'service': 'one'},
                'two': {'service': 'two'},
            }

            def fail_first(_config, service):
                if service['service'] == 'one':
                    raise RuntimeError('first failed')

            with mock.patch.object(
                backup, 'manifest', side_effect=lambda _config, name: services[name],
            ), mock.patch.object(
                backup, 'backup_one', side_effect=fail_first,
            ) as backup_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_backup(config, args)

        self.assertEqual(
            [call.args[1]['service'] for call in backup_mock.call_args_list],
            ['one', 'two'],
        )


if __name__ == '__main__':
    unittest.main()
