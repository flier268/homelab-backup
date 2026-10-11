"""Must-fix 1: a stalled backup/check must still alert via `status`."""
import datetime as dt
import fcntl
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import cli, common, maintenance
from tests import test_alerting

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
FRESH = {'last_success_at': (NOW - dt.timedelta(hours=8)).isoformat()}


class LockFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_file = Path(self.tmp.name) / 'backupctl.lock'

    def hold(self, content):
        """Hold the lock through a separate open file description."""
        handle = open(self.lock_file, 'a+', encoding='utf-8')
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(content)
        handle.flush()
        return handle

    def root_check_passes(self):
        """Pass cli.main's root check only; later ownership checks stay real
        so temporary control paths are accepted."""
        real_geteuid = os.geteuid
        answers = [0]
        return mock.patch.object(
            cli.os, 'geteuid',
            side_effect=lambda: answers.pop() if answers else real_geteuid(),
        )


class StatusSkipsGlobalLockTests(LockFixture):
    def test_status_never_enters_global_lock(self):
        # The real cmd_status runs while another process holds the global
        # lock; any attempt to take it (cli, maintenance or common) raises.
        started = (NOW - dt.timedelta(hours=14)).isoformat()
        self.hold(f'pid=4242 started={started}\n')
        config = {
            'host_id': 'h', 'lock_file': str(self.lock_file),
            'state_root': str(Path(self.tmp.name) / 'state'),
        }
        refuse = mock.Mock(side_effect=AssertionError('global lock taken'))
        with self.root_check_passes(), \
                mock.patch.object(cli, 'cfg', return_value=config), \
                mock.patch.object(cli, 'GlobalLock', refuse), \
                mock.patch.object(maintenance, 'GlobalLock', refuse), \
                mock.patch.object(common, 'GlobalLock', refuse), \
                mock.patch.object(
                    maintenance, 'manifests', return_value=[test_alerting.DAILY],
                ), mock.patch.object(maintenance, 'local_now', return_value=NOW), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'next', None),
                ), mock.patch.object(maintenance, 'load_state', return_value=FRESH), \
                mock.patch.object(maintenance, 'notify', return_value=True) as notify, \
                redirect_stdout(StringIO()) as out, \
                redirect_stderr(StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            cli.main(['status', '--notify'])
        self.assertEqual(caught.exception.code, maintenance.STALE_EXIT_CODE)
        refuse.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['stale'])
        self.assertIn('ALERT: global lock held by pid=4242', err.getvalue())
        self.assertIn('stalled', notify.call_args.args[1])
        self.assertNotIn('WARNING', err.getvalue())
        # The holder line written by the other process is untouched, and the
        # alert state used its own lock.
        self.assertIn('pid=4242', self.lock_file.read_text())
        self.assertTrue((Path(config['state_root']) / '_alerts.lock').exists())


class GlobalLockReleaseTests(LockFixture):
    def test_release_clears_holder_line(self):
        with common.GlobalLock(self.lock_file) as acquired:
            self.assertTrue(acquired)
            self.assertIn(f'pid={os.getpid()}', self.lock_file.read_text())
        self.assertEqual(self.lock_file.read_text(), '')

    def test_nested_release_keeps_line_until_outermost_exit(self):
        with common.GlobalLock(self.lock_file):
            with common.GlobalLock(self.lock_file, nonblocking=True):
                pass
            self.assertIn(f'pid={os.getpid()}', self.lock_file.read_text())
        self.assertEqual(self.lock_file.read_text(), '')

    def test_release_after_error_still_clears_and_unlocks(self):
        with self.assertRaises(RuntimeError):
            with common.GlobalLock(self.lock_file):
                raise RuntimeError('boom')
        self.assertEqual(self.lock_file.read_text(), '')
        self.assertIsNone(common.global_lock_holder(self.lock_file))


class RunDueLockRetryTests(LockFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(common, 'lock_retry_sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_lock_is_retried_three_times(self):
        self.hold('pid=1 started=x\n')
        with common.GlobalLock(
            self.lock_file, nonblocking=True,
            retries=common.RUN_DUE_LOCK_RETRIES,
        ) as acquired:
            self.assertFalse(acquired)
        self.assertEqual(
            self.sleep.call_args_list,
            [mock.call(common.LOCK_RETRY_SECONDS)] * 3,
        )
        self.assertEqual(common.RUN_DUE_LOCK_RETRIES, 3)
        self.assertEqual(common.LOCK_RETRY_SECONDS, 0.1)

    def test_lock_released_during_retry_is_acquired(self):
        handle = self.hold('pid=1 started=x\n')
        self.sleep.side_effect = lambda _s: fcntl.flock(
            handle.fileno(), fcntl.LOCK_UN,
        )
        with common.GlobalLock(
            self.lock_file, nonblocking=True, retries=3,
        ) as acquired:
            self.assertTrue(acquired)
        self.assertEqual(self.sleep.call_count, 1)

    def test_default_has_no_retries(self):
        self.hold('pid=1 started=x\n')
        with common.GlobalLock(self.lock_file, nonblocking=True) as acquired:
            self.assertFalse(acquired)
        self.sleep.assert_not_called()

    def test_cmd_run_due_retries_before_skip(self):
        self.hold('pid=1 started=x\n')
        with mock.patch.object(maintenance, 'manifests') as manifests, \
                redirect_stdout(StringIO()) as out:
            maintenance.cmd_run_due(
                {'lock_file': str(self.lock_file)}, mock.Mock(),
            )
        manifests.assert_not_called()
        self.assertEqual(self.sleep.call_count, 3)
        self.assertIn('SKIP: another backup', out.getvalue())

    def _main(self, argv):
        command = mock.Mock()
        with self.root_check_passes(), \
                mock.patch.object(
                    cli, 'config_lock_file', return_value=str(self.lock_file),
                ), mock.patch.object(cli, 'cfg') as cfg_mock, \
                mock.patch.dict(cli.COMMANDS, {argv[0]: command}), \
                redirect_stdout(StringIO()) as out:
            cli.main(argv)
        cfg_mock.assert_not_called()
        command.assert_not_called()
        return out.getvalue()

    def test_cli_run_due_retries_before_skip(self):
        self.hold('pid=1 started=x\n')
        out = self._main(['run-due'])
        self.assertEqual(self.sleep.call_count, 3)
        self.assertIn('SKIP: another backup', out)

    def test_other_nonblocking_commands_do_not_retry(self):
        self.hold('pid=1 started=x\n')
        for argv in (['check', '--no-wait'], ['maintenance', '--no-wait']):
            with self.subTest(argv=argv):
                self.assertIn('SKIP', self._main(argv))
        self.sleep.assert_not_called()


class GlobalLockHolderTests(LockFixture):
    def test_missing_file_is_not_held_and_not_created(self):
        self.assertIsNone(common.global_lock_holder(self.lock_file))
        self.assertFalse(self.lock_file.exists())

    def test_unheld_file_is_not_held(self):
        self.lock_file.write_text('pid=1 started=2026-10-11T00:00:00+08:00\n')
        self.assertIsNone(common.global_lock_holder(self.lock_file))

    def test_held_file_reports_pid_and_started(self):
        self.hold('pid=4242 started=2026-10-11T00:00:00+08:00\n')
        self.assertEqual(common.global_lock_holder(self.lock_file), {
            'pid': 4242, 'started': '2026-10-11T00:00:00+08:00',
        })

    def test_inspection_does_not_modify_file_or_release_lock(self):
        content = 'pid=4242 started=2026-10-11T00:00:00+08:00\n'
        self.hold(content)
        common.global_lock_holder(self.lock_file)
        self.assertEqual(self.lock_file.read_text(), content)
        with open(self.lock_file, encoding='utf-8') as other:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_global_lock_is_seen_as_held(self):
        with common.GlobalLock(self.lock_file) as acquired:
            self.assertTrue(acquired)
            holder = common.global_lock_holder(self.lock_file)
        self.assertEqual(holder['pid'], os.getpid())
        self.assertIsNotNone(dt.datetime.fromisoformat(holder['started']))
        self.assertIsNone(common.global_lock_holder(self.lock_file))

    def test_symlink_is_not_followed(self):
        target = Path(self.tmp.name) / 'target'
        target.write_text('')
        self.lock_file.symlink_to(target)
        with self.assertRaises(OSError):
            common.global_lock_holder(self.lock_file)


class StatusStalledLockTests(LockFixture):
    def _status(self, *, alert=None, notify_flag=False):
        out, err = StringIO(), StringIO()
        args = mock.Mock(notify=notify_flag, max_age=None)
        c = {'host_id': 'h', 'lock_file': str(self.lock_file)}
        if alert is not None:
            c['alert'] = alert
        with mock.patch.object(
                    maintenance, 'manifests', return_value=[test_alerting.DAILY],
                ), mock.patch.object(maintenance, 'local_now', return_value=NOW), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'next', None),
                ), mock.patch.object(maintenance, 'load_state', return_value=FRESH), \
                mock.patch.object(maintenance, 'notify') as notify, \
                redirect_stdout(out), redirect_stderr(err):
            code = 0
            try:
                maintenance.cmd_status(c, args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), notify

    def _hold_since(self, **ago):
        started = (NOW - dt.timedelta(**ago)).isoformat()
        self.hold(f'pid=4242 started={started}\n')

    def test_no_lock_file_is_ok(self):
        code, _, err, _ = self._status()
        self.assertEqual(code, 0)
        self.assertNotIn('ALERT', err)
        self.assertFalse(self.lock_file.exists())

    def test_lock_held_longer_than_default_alerts(self):
        self._hold_since(hours=14)
        code, out, err, notify = self._status(notify_flag=True)
        self.assertEqual(code, maintenance.STALE_EXIT_CODE)
        self.assertIn('ALERT: global lock held by pid=4242', err)
        self.assertIn('"held_seconds": 50400', err)
        self.assertFalse(json.loads(out)['stale'])
        title, body = notify.call_args.args[1:3]
        self.assertIn('stalled', title)
        self.assertIn('pid=4242', body)

    def test_lock_held_briefly_is_ok(self):
        self._hold_since(minutes=30)
        code, _, err, notify = self._status(notify_flag=True)
        self.assertEqual(code, 0)
        self.assertNotIn('ALERT', err)
        self.assertIn('"held_seconds": 1800', err)
        self.assertTrue(notify.call_args.kwargs.get('ok'))

    def test_configured_max_lock_age_is_used(self):
        self._hold_since(hours=2)
        code, _, err, _ = self._status(alert={'max_lock_age': '1h'})
        self.assertEqual(code, maintenance.STALE_EXIT_CODE)
        self.assertIn('ALERT: global lock', err)

    def test_unparsable_started_only_warns(self):
        self.hold('pid=4242 started=garbage\n')
        code, _, err, _ = self._status()
        self.assertEqual(code, 0)
        self.assertIn('WARNING: global lock held by pid=4242', err)
        self.assertNotIn('ALERT', err)

    def test_unheld_old_lock_file_is_ok(self):
        self.lock_file.write_text('pid=4242 started=2026-01-01T00:00:00+08:00\n')
        code, _, err, _ = self._status()
        self.assertEqual(code, 0)
        self.assertEqual(err, '')


class MaxLockAgeConfigTests(unittest.TestCase):
    def _load(self, alert):
        return test_alerting.AlertConfigTests._load(self, alert)

    def test_max_lock_age_is_accepted(self):
        loaded = self._load({'max_lock_age': '13h'})
        self.assertEqual(loaded['alert']['max_lock_age'], '13h')

    def test_invalid_max_lock_age_is_rejected(self):
        for value in ('soon', '0h', 13, ''):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self._load({'max_lock_age': value})


if __name__ == '__main__':
    unittest.main()
