"""Stop-the-bleeding change 3: clear stale restic locks after our flock."""
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import backup, common, maintenance


class ClearStaleLocksTests(unittest.TestCase):
    def test_runs_plain_unlock_never_remove_all(self):
        with mock.patch.object(common, 'restic_env', return_value={'X': '1'}), \
                mock.patch.object(common, 'run') as run:
            self.assertTrue(common.clear_stale_restic_locks({}))
        run.assert_called_once_with(['restic', 'unlock'], env={'X': '1'})
        self.assertNotIn('--remove-all', run.call_args.args[0])

    def test_unlock_failure_is_only_a_warning(self):
        with mock.patch.object(common, 'restic_env', return_value={}), \
                mock.patch.object(
                    common, 'run',
                    side_effect=common.CommandError(
                        ['restic', 'unlock'], 1, stderr='repo offline',
                    ),
                ), redirect_stderr(StringIO()) as err:
            self.assertFalse(common.clear_stale_restic_locks({}))
        self.assertIn('WARNING: could not clear stale restic locks', err.getvalue())


class CallersUnlockAfterFlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = {
            'lock_file': str(root / 'backupctl.lock'),
            'host_id': 'host',
            'state_root': str(root / 'state'),
        }
        self.events = []

    def _lock_held(self):
        key = (os.getpid(), os.path.abspath(self.config['lock_file']))
        return common.GlobalLock._held.get(key) is not None

    def _record_unlock(self, _c):
        self.events.append(('unlock', self._lock_held()))
        return True

    def test_maintenance_unlocks_before_forget_and_prune(self):
        def fake_run(command, **_kwargs):
            self.events.append(tuple(command[:2]))
            return mock.Mock(stdout='')

        m = {'service': 'demo', 'retention': {'keep_last': 1}}
        with mock.patch.object(
            maintenance, 'clear_stale_restic_locks',
            side_effect=self._record_unlock,
        ), mock.patch.object(maintenance, 'manifests', return_value=[m]), \
                mock.patch.object(maintenance, 'restic_env', return_value={}), \
                mock.patch.object(maintenance, 'load_state', return_value={}), \
                mock.patch.object(maintenance, 'snapshot_state_services', return_value=[]), \
                mock.patch.object(maintenance, 'run', side_effect=fake_run), \
                redirect_stdout(StringIO()):
            maintenance.cmd_maintenance(
                self.config, mock.Mock(no_wait=False, dry_run=False),
            )
        self.assertEqual(self.events[0], ('unlock', True))
        self.assertIn(('restic', 'forget'), self.events[1:])
        self.assertIn(('restic', 'prune'), self.events[1:])

    def test_check_unlocks_before_restic_check(self):
        def fake_run(command, **_kwargs):
            self.events.append(tuple(command[:2]))
            return mock.Mock(stdout='')

        with mock.patch.object(
            maintenance, 'clear_stale_restic_locks',
            side_effect=self._record_unlock,
        ), mock.patch.object(maintenance, 'restic_env', return_value={}), \
                mock.patch.object(maintenance, 'run', side_effect=fake_run):
            maintenance.cmd_check(self.config, mock.Mock(no_wait=False))
        self.assertEqual(self.events, [('unlock', True), ('restic', 'check')])

    def test_run_due_unlocks_only_when_a_backup_is_due(self):
        m = {'service': 'demo', 'schedule': {'cron': '0 3 * * *'}}
        for due, expected in ((False, []), (True, [('unlock', True), 'backup'])):
            self.events.clear()
            with self.subTest(due=due), mock.patch.object(
                maintenance, 'clear_stale_restic_locks',
                side_effect=self._record_unlock,
            ), mock.patch.object(maintenance, 'manifests', return_value=[m]), \
                    mock.patch.object(
                        maintenance, 'due_status',
                        return_value=(due, 'reason', None),
                    ), mock.patch.object(
                        maintenance, 'backup_one',
                        side_effect=lambda *_a, **_k: self.events.append('backup'),
                    ), redirect_stdout(StringIO()):
                maintenance.cmd_run_due(self.config, mock.Mock())
            self.assertEqual(self.events, expected)

    def test_manual_backup_unlocks_before_first_service(self):
        m = {'service': 'demo'}
        with mock.patch.object(
            backup, 'clear_stale_restic_locks', side_effect=self._record_unlock,
        ), mock.patch.object(backup, 'manifests', return_value=[m]), \
                mock.patch.object(
                    backup, 'backup_one',
                    side_effect=lambda *_a, **_k: self.events.append('backup'),
                ):
            backup.cmd_backup(
                self.config, mock.Mock(services=[], allow_low_space=False),
            )
        self.assertEqual(self.events, [('unlock', True), 'backup'])


if __name__ == '__main__':
    unittest.main()
