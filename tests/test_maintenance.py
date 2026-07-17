import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from homelab_backup import common, maintenance


class MaintenanceCommandTests(unittest.TestCase):
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

            with mock.patch.object(maintenance, 'manifests', return_value=[good, bad]), \
                    mock.patch.object(maintenance, 'due_status', side_effect=due_status), \
                    mock.patch.object(maintenance, 'backup_one') as backup_mock:
                with self.assertRaises(SystemExit):
                    maintenance.cmd_run_due(config, mock.Mock())

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
                maintenance, 'due_status', return_value=(True, 'due', None),
            ), mock.patch.object(maintenance, 'backup_one') as backup_mock:
                with self.assertRaises(SystemExit):
                    maintenance.cmd_run_due(config, mock.Mock())

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
            with mock.patch.object(maintenance, 'manifests', return_value=[first, second]), \
                    mock.patch.object(maintenance, 'restic_env', return_value={}), \
                    mock.patch.object(maintenance, 'run', side_effect=fake_run) as run_mock:
                with self.assertRaises(SystemExit):
                    maintenance.cmd_maintenance(config, args)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('service:second' in command for command in commands))
            self.assertIn(['restic', 'prune'], commands)

    def test_snapshots_always_filters_by_host(self):
        args = mock.Mock(service=None)
        with mock.patch.object(maintenance, 'restic_env', return_value={}), \
                mock.patch.object(maintenance, 'run') as run_mock:
            maintenance.cmd_snapshots({'host_id': 'server-a'}, args)

        self.assertEqual(
            run_mock.call_args.args[0],
            ['restic', 'snapshots', '--host', 'server-a'],
        )

    def test_unlock_removes_only_stale_repository_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            environment = {'RESTIC_REPOSITORY': 'test-repository'}
            with mock.patch.object(
                maintenance, 'restic_env', return_value=environment,
            ), mock.patch.object(maintenance, 'run') as run_mock:
                maintenance.cmd_unlock(config, mock.Mock())

        run_mock.assert_called_once_with(['restic', 'unlock'], env=environment)

    def test_unlock_refuses_while_another_process_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = str(Path(tmp) / 'backupctl.lock')
            with common.GlobalLock(lock_file), \
                    mock.patch.object(maintenance, 'run') as run_mock:
                with self.assertRaises(SystemExit):
                    maintenance.cmd_unlock({'lock_file': lock_file}, mock.Mock())

        run_mock.assert_not_called()

    def test_status_exposes_retention_failure(self):
        value = {'service': 'demo', 'schedule': {'cron': '0 0 * * *'}}
        state = {'last_result': 'success', 'last_retention_error': 'forget failed'}
        output = io.StringIO()
        with mock.patch.object(maintenance, 'manifests', return_value=[value]), \
                mock.patch.object(maintenance, 'due_status', return_value=(False, 'next', None)), \
                mock.patch.object(maintenance, 'load_state', return_value=state), \
                redirect_stdout(output):
            maintenance.cmd_status({}, mock.Mock())

        self.assertEqual(
            json.loads(output.getvalue())['last_retention_error'],
            'forget failed',
        )

if __name__ == '__main__':
    unittest.main()
