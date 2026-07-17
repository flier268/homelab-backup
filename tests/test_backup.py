import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

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


class BackupStateTests(unittest.TestCase):
    def test_save_state_creates_custom_private_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'custom' / 'state'
            backup.save_state(
                {'state_root': str(state_root)}, 'demo', {'result': 'ok'},
            )
            self.assertEqual(
                json.loads((state_root / 'demo.json').read_text(encoding='utf-8')),
                {'result': 'ok'},
            )
            self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)

    def test_invalid_last_success_timestamp_is_ignored(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '0 * * * *'},
        }
        now = backup.dt.datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)

        for invalid_timestamp in (123, '2026-07-14T12:00:00'):
            with self.subTest(timestamp=invalid_timestamp), \
                    mock.patch.object(backup, 'load_state', return_value={
                        'last_success_at': invalid_timestamp,
                        'last_result': 'success',
                    }):
                due, _reason, occurrence = backup.due_status({}, value, now)

            self.assertTrue(due)
            self.assertEqual(
                occurrence,
                backup.dt.datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            )

    def test_naive_last_attempt_timestamp_does_not_block_retry_planning(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '0 * * * *', 'retry_after': '30m'},
        }
        now = backup.dt.datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)
        with mock.patch.object(backup, 'load_state', return_value={
            'last_attempt_at': '2026-07-14T12:00:00',
            'last_result': 'failed',
        }):
            due, _reason, occurrence = backup.due_status({}, value, now)

        self.assertTrue(due)
        self.assertEqual(
            occurrence,
            backup.dt.datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        )

    def test_max_lateness_uses_elapsed_time_across_fall_back(self):
        timezone_info = ZoneInfo('America/New_York')
        value = {
            'service': 'demo',
            'schedule': {'cron': '30 1 * * *', 'max_lateness': '1h'},
        }
        now = backup.dt.datetime(
            2026, 11, 1, 1, 45, tzinfo=timezone_info, fold=1,
        )
        with mock.patch.object(backup, 'load_state', return_value={}):
            due, reason, _next_time = backup.due_status({}, value, now)

        self.assertFalse(due)
        self.assertIn('missed', reason)

    def test_frequent_schedule_runs_during_second_fall_back_hour(self):
        timezone_info = ZoneInfo('America/New_York')
        value = {
            'service': 'demo',
            'schedule': {'cron': '*/5 * * * *'},
        }
        now = backup.dt.datetime(
            2026, 11, 1, 1, 5, tzinfo=timezone_info, fold=1,
        )
        with mock.patch.object(backup, 'load_state', return_value={
            'last_success_at': '2026-11-01T01:55:00-04:00',
            'last_result': 'success',
        }):
            due, _reason, occurrence = backup.due_status({}, value, now)

        self.assertTrue(due)
        self.assertEqual(occurrence, now)
        self.assertEqual(occurrence.fold, 1)

    def test_long_backup_completion_skips_intervals_reached_while_running(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '*/5 * * * *'},
        }
        now = backup.dt.datetime(2026, 7, 15, 12, 18, tzinfo=timezone.utc)
        with mock.patch.object(backup, 'load_state', return_value={
            # The 12:00 run finished after the 12:05, 12:10, and 12:15 slots.
            'last_success_at': '2026-07-15T12:17:00+00:00',
            'last_result': 'success',
        }):
            due, reason, next_time = backup.due_status({}, value, now)

        self.assertFalse(due)
        self.assertIn('next at', reason)
        self.assertEqual(
            next_time,
            backup.dt.datetime(2026, 7, 15, 12, 20, tzinfo=timezone.utc),
        )

    def test_retention_failure_does_not_turn_snapshot_into_failed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            config = {'host_id': 'host'}
            saved = []

            def fake_run(cmd, **_kwargs):
                if cmd[:2] == ['restic', 'forget']:
                    raise common.CommandError(cmd, 1, stderr='temporary failure')
                return mock.Mock(stdout='')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(backup, 'save_state', side_effect=lambda _c, _s, state: saved.append(copy.deepcopy(state))), \
                    mock.patch.object(backup, 'stage_service', return_value=root), \
                    mock.patch.object(backup, 'restic_env', return_value={}), \
                    mock.patch.object(backup, 'run', side_effect=fake_run):
                self.assertTrue(backup.backup_one(config, value))

            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertIn('temporary failure', saved[-1]['last_retention_error'])


class BackupCommandTests(unittest.TestCase):
    def test_global_lock_creates_custom_private_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'custom' / 'locks' / 'backupctl.lock'
            with common.GlobalLock(lock) as acquired:
                self.assertTrue(acquired)
            self.assertTrue(lock.is_file())
            self.assertEqual(lock.parent.stat().st_mode & 0o777, 0o700)

    def test_run_due_continues_after_another_service_planning_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            good = {'service': 'good'}
            bad = {'service': 'bad'}

            def due_status(_config, value, now):
                if value['service'] == 'bad':
                    raise ValueError('invalid schedule')
                return True, 'due', now

            with mock.patch.object(backup, 'manifests', return_value=[good, bad]), \
                    mock.patch.object(backup, 'due_status', side_effect=due_status), \
                    mock.patch.object(backup, 'backup_one') as backup_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_run_due(config, mock.Mock())

            backup_mock.assert_called_once_with(config, good)

    def test_run_due_continues_after_malformed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_dir = root / 'services' / 'bad'
            good_dir = root / 'services' / 'good'
            bad_dir.mkdir(parents=True)
            good_dir.mkdir(parents=True)
            (bad_dir / 'backup.yaml').write_text('- not\n- a mapping\n', encoding='utf-8')
            (good_dir / 'backup.yaml').write_text('service: good\n', encoding='utf-8')
            config = {
                'services_root': str(root / 'services'),
                'lock_file': str(root / 'backupctl.lock'),
            }

            with mock.patch.object(
                backup, 'due_status', return_value=(True, 'due', None),
            ), mock.patch.object(backup, 'backup_one') as backup_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_run_due(config, mock.Mock())

            self.assertEqual(backup_mock.call_count, 1)
            self.assertEqual(backup_mock.call_args.args[1]['service'], 'good')

    def test_maintenance_continues_after_one_retention_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                'lock_file': str(Path(tmp) / 'backupctl.lock'),
                'host_id': 'host',
            }
            first = {'service': 'first', 'retention': {'keep_last': 1}}
            second = {'service': 'second', 'retention': {'keep_last': 1}}

            def fake_run(command, **_kwargs):
                if 'service:first' in command:
                    raise common.CommandError(command, 1, stderr='forget failed')
                return mock.Mock(stdout='')

            args = mock.Mock(no_wait=False, dry_run=False)
            with mock.patch.object(backup, 'manifests', return_value=[first, second]), \
                    mock.patch.object(backup, 'restic_env', return_value={}), \
                    mock.patch.object(backup, 'run', side_effect=fake_run) as run_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_maintenance(config, args)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('service:second' in command for command in commands))
            self.assertIn(['restic', 'prune'], commands)

    def test_snapshots_always_filters_by_host(self):
        args = mock.Mock(service=None)
        with mock.patch.object(backup, 'restic_env', return_value={}), \
                mock.patch.object(backup, 'run') as run_mock:
            backup.cmd_snapshots({'host_id': 'server-a'}, args)

        self.assertEqual(
            run_mock.call_args.args[0],
            ['restic', 'snapshots', '--host', 'server-a'],
        )

    def test_unlock_removes_only_stale_repository_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            environment = {'RESTIC_REPOSITORY': 'test-repository'}
            with mock.patch.object(
                backup, 'restic_env', return_value=environment,
            ), mock.patch.object(backup, 'run') as run_mock:
                backup.cmd_unlock(config, mock.Mock())

        run_mock.assert_called_once_with(['restic', 'unlock'], env=environment)

    def test_unlock_refuses_while_another_process_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = str(Path(tmp) / 'backupctl.lock')
            with common.GlobalLock(lock_file), \
                    mock.patch.object(backup, 'run') as run_mock:
                with self.assertRaises(SystemExit):
                    backup.cmd_unlock({'lock_file': lock_file}, mock.Mock())

        run_mock.assert_not_called()

    def test_status_exposes_retention_failure(self):
        value = {'service': 'demo', 'schedule': {'cron': '0 0 * * *'}}
        state = {'last_result': 'success', 'last_retention_error': 'forget failed'}
        output = io.StringIO()
        with mock.patch.object(backup, 'manifests', return_value=[value]), \
                mock.patch.object(backup, 'due_status', return_value=(False, 'next', None)), \
                mock.patch.object(backup, 'load_state', return_value=state), \
                redirect_stdout(output):
            backup.cmd_status({}, mock.Mock())

        self.assertEqual(
            json.loads(output.getvalue())['last_retention_error'],
            'forget failed',
        )


if __name__ == '__main__':
    unittest.main()
