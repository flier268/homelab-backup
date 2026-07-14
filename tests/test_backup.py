import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from homelab_backup import backup, common
from tests.helpers import manifest


class StagingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'staging_root': str(self.root / 'staging')}

    def tearDown(self):
        self.temp.cleanup()

    def test_stale_staging_content_is_removed(self):
        stage = Path(self.config['staging_root']) / 'demo'
        stale = stage / 'paths' / 'removed-source' / 'secret.txt'
        stale.parent.mkdir(parents=True)
        stale.write_text('old data', encoding='utf-8')

        value = manifest(self.root)
        with mock.patch.object(backup, 'sync_paths'), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'hooks'):
            backup.stage_service(self.config, value)

        self.assertFalse(stale.exists())

    def test_staging_overlap_is_rejected_before_deletion(self):
        value = manifest(self.root)
        sentinel = Path(value['_dir']) / 'compose.yaml'
        sentinel.write_text('services: {}\n', encoding='utf-8')
        config = {'staging_root': str(self.root)}

        with mock.patch.object(backup, 'sync_paths') as sync_mock:
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                backup.stage_service(config, value)

        self.assertTrue(sentinel.exists())
        sync_mock.assert_not_called()

    def test_none_mode_does_not_run_hooks(self):
        value = manifest(self.root, consistency={'mode': 'none'})
        with mock.patch.object(backup, 'hooks') as hooks_mock, \
                mock.patch.object(backup, 'sync_paths'), \
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
        value = manifest(self.root, consistency={'mode': 'stop', 'services': ['app']})

        def fake_run(cmd, **kwargs):
            if 'up' in cmd and kwargs.get('check', True):
                raise common.CommandError(cmd, 1)
            return mock.Mock(stdout='')

        with mock.patch.object(backup, 'hooks'), \
                mock.patch.object(backup, 'sync_paths'), \
                mock.patch.object(backup, 'sync_volumes'), \
                mock.patch.object(backup, 'running_services', return_value=['app']), \
                mock.patch.object(backup, 'run', side_effect=fake_run):
            with self.assertRaises(common.CommandError):
                backup.stage_service(self.config, value)


class BackupStateTests(unittest.TestCase):
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
