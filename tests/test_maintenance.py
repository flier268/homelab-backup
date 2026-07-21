import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from homelab_backup import common, maintenance, security


class MaintenanceCommandTests(unittest.TestCase):
    def test_list_continues_after_manifest_and_schedule_errors(self):
        good = {
            'service': 'good',
            'schedule': {'cron': '0 0 * * *'},
            '_path': '/services/good/backup.yaml',
        }
        bad_schedule = {
            'service': 'bad-schedule',
            'schedule': {'cron': 'invalid'},
            '_path': '/services/bad-schedule/backup.yaml',
        }
        output = io.StringIO()
        error = io.StringIO()

        def fake_manifests(_config, on_error):
            on_error('/services/bad-yaml/backup.yaml', ValueError('bad yaml'))
            return [bad_schedule, good]

        def fake_due_status(_config, value, _now):
            if value['service'] == 'bad-schedule':
                raise ValueError('bad cron')
            return False, 'next', None

        with mock.patch.object(maintenance, 'manifests', side_effect=fake_manifests), \
                mock.patch.object(maintenance, 'due_status', side_effect=fake_due_status), \
                redirect_stdout(output), redirect_stderr(error), \
                self.assertRaises(SystemExit):
            maintenance.cmd_list({}, mock.Mock())

        self.assertIn('good: cron 0 0 * * *; next;', output.getvalue())
        self.assertIn('bad-yaml', error.getvalue())
        self.assertIn('bad-schedule', error.getvalue())
        self.assertIn('LIST FAILURES', error.getvalue())

    def test_status_continues_after_schedule_and_state_errors(self):
        values = [
            {'service': 'bad-schedule'},
            {'service': 'bad-state'},
            {'service': 'good'},
        ]
        output = io.StringIO()
        error = io.StringIO()

        def fake_due_status(_config, value, _now):
            if value['service'] == 'bad-schedule':
                raise ValueError('bad cron')
            return False, 'next', None

        def fake_load_state(_config, service):
            if service == 'bad-state':
                raise ValueError('corrupt state')
            return {'last_result': 'success'}

        with mock.patch.object(maintenance, 'manifests', return_value=values), \
                mock.patch.object(maintenance, 'due_status', side_effect=fake_due_status), \
                mock.patch.object(maintenance, 'load_state', side_effect=fake_load_state), \
                redirect_stdout(output), redirect_stderr(error), \
                self.assertRaises(SystemExit):
            maintenance.cmd_status({}, mock.Mock())

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record['service'] for record in records], ['good'])
        self.assertIn('bad-schedule', error.getvalue())
        self.assertIn('bad-state', error.getvalue())
        self.assertIn('STATUS FAILURES', error.getvalue())

    def test_failure_summary_records_exact_operation_and_error(self):
        summary = maintenance.FailureSummary()
        error = io.StringIO()
        with redirect_stderr(error):
            summary.record_exception(
                'demo', ValueError('invalid schedule'),
                message='ERROR: schedule planning failed for demo: {error}',
                summary_error='schedule planning failed: invalid schedule',
            )
            with self.assertRaises(SystemExit):
                summary.raise_if_any('SCHEDULED BACKUP FAILURES')

        self.assertEqual(
            error.getvalue(),
            'ERROR: schedule planning failed for demo: invalid schedule\n'
            '\nSCHEDULED BACKUP FAILURES\n'
            '  - demo: schedule planning failed: invalid schedule\n',
        )

    def test_run_due_debug_reraises_original_planning_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            failure = ValueError('invalid schedule')
            with mock.patch.object(
                maintenance, 'manifests', return_value=[{'service': 'bad'}],
            ), mock.patch.object(
                maintenance, 'due_status', side_effect=failure,
            ), mock.patch.dict(
                'os.environ', {'BACKUPCTL_DEBUG': '1'},
            ), self.assertRaises(ValueError) as caught:
                maintenance.cmd_run_due(config, mock.Mock())

            self.assertIs(caught.exception, failure)

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
            (root / 'services').chmod(0o755)
            bad_dir.chmod(0o755)
            good_dir.chmod(0o755)
            (bad_dir / 'backup.yaml').write_text('- not\n- a mapping\n', encoding='utf-8')
            (good_dir / 'backup.yaml').write_text('service: good\n', encoding='utf-8')
            (bad_dir / 'backup.yaml').chmod(0o600)
            (good_dir / 'backup.yaml').chmod(0o600)
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

    def test_maintenance_clears_retention_error_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                'lock_file': str(Path(tmp) / 'backupctl.lock'),
                'host_id': 'host',
            }
            value = {'service': 'demo', 'retention': {'keep_last': 1}}
            state = {'last_retention_error': 'forget failed'}
            args = mock.Mock(no_wait=False, dry_run=False)
            with mock.patch.object(
                maintenance, 'manifests', return_value=[value],
            ), mock.patch.object(
                security, 'ensure_control_directory',
            ), mock.patch.object(
                maintenance, 'restic_env', return_value={},
            ), mock.patch.object(
                maintenance, 'load_state', return_value=state,
            ), mock.patch.object(maintenance, 'save_state') as save_mock, \
                    mock.patch.object(maintenance, 'run'):
                maintenance.cmd_maintenance(config, args)

            self.assertIsNone(state['last_retention_error'])
            save_mock.assert_called_once_with(config, 'demo', state)

    def test_maintenance_rejects_unsafe_service_before_state_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                'lock_file': str(Path(tmp) / 'backupctl.lock'),
                'host_id': 'host',
            }
            value = {'service': '../outside', 'retention': {'keep_last': 1}}
            args = mock.Mock(no_wait=False, dry_run=False)
            with mock.patch.object(
                maintenance, 'manifests', return_value=[value],
            ), mock.patch.object(
                security, 'ensure_control_directory',
            ), mock.patch.object(
                maintenance, 'restic_env', return_value={},
            ), mock.patch.object(maintenance, 'load_state') as load_mock, \
                    mock.patch.object(maintenance, 'run') as run_mock, \
                    self.assertRaises(SystemExit):
                maintenance.cmd_maintenance(config, args)

            load_mock.assert_not_called()
            self.assertFalse(any(
                call.args[0][:2] == ['restic', 'forget']
                for call in run_mock.call_args_list
            ))

    def test_maintenance_continues_when_clearing_state_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                'lock_file': str(Path(tmp) / 'backupctl.lock'),
                'host_id': 'host',
            }
            values = [
                {'service': 'first', 'retention': {'keep_last': 1}},
                {'service': 'second', 'retention': {'keep_last': 1}},
            ]
            args = mock.Mock(no_wait=False, dry_run=False)
            with mock.patch.object(
                maintenance, 'manifests', return_value=values,
            ), mock.patch.object(
                security, 'ensure_control_directory',
            ), mock.patch.object(
                maintenance, 'restic_env', return_value={},
            ), mock.patch.object(
                maintenance, 'load_state',
                side_effect=[
                    {'last_retention_error': 'pending'},
                    {'last_retention_error': 'pending'},
                ],
            ), mock.patch.object(
                maintenance, 'save_state',
                side_effect=[OSError('state unavailable'), None],
            ) as save_mock, mock.patch.object(maintenance, 'run'), \
                    self.assertRaises(SystemExit):
                maintenance.cmd_maintenance(config, args)

            self.assertEqual(save_mock.call_count, 2)

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
        class BusyLock:
            def __init__(self, _path, nonblocking=False):
                pass

            def __enter__(self):
                return False

            def __exit__(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(maintenance, 'GlobalLock', BusyLock), \
                mock.patch.object(maintenance, 'run') as run_mock:
            lock_file = str(Path(tmp) / 'backupctl.lock')
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
