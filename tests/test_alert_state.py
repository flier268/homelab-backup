"""Second review round: alert-state locking, reset on success, status hand-off."""
import datetime as dt
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import alerting, maintenance

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
STALE = {'last_success_at': (NOW - dt.timedelta(days=5)).isoformat()}
CRON = {'cron': '0 4 * * *'}


class AlertFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state_root = self.root / 'state'
        self.c = {
            'host_id': 'h', 'state_root': str(self.state_root),
            'lock_file': str(self.root / 'backupctl.lock'),
        }
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ('INVOCATION_ID', 'MONITOR_INVOCATION_ID'):
            os.environ.pop(name, None)

    def alert_state(self):
        return alerting.load_alert_state(self.c)

    def unit_record(self, unit):
        return self.alert_state().get('units', {}).get(unit, {})

    def notify_failure(self, unit, *, at=NOW, delivered=True, monitor_id=None):
        env = {'MONITOR_INVOCATION_ID': monitor_id} if monitor_id else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            maintenance, 'run', return_value=mock.Mock(stdout='tail'),
        ), mock.patch.object(maintenance, 'local_now', return_value=at), \
                mock.patch.object(
                    maintenance, 'notify', return_value=delivered,
                ) as notify, redirect_stderr(StringIO()):
            maintenance.cmd_notify_failure(self.c, mock.Mock(unit=unit))
        return notify

    def status(self, states, *, at=NOW, invocation_id=None, delivered=True):
        """Run status --notify; a state that is an exception fails loading."""
        manifests = [{'service': name, 'schedule': CRON} for name in states]

        def load_state(_c, service):
            if isinstance(states[service], Exception):
                raise states[service]
            return states[service]

        env = {'INVOCATION_ID': invocation_id} if invocation_id else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            maintenance, 'manifests', return_value=manifests,
        ), mock.patch.object(maintenance, 'local_now', return_value=at), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'next', None),
                ), mock.patch.object(
                    maintenance, 'load_state', side_effect=load_state,
                ), mock.patch.object(
                    maintenance, 'notify', return_value=delivered,
                ) as notify, redirect_stdout(StringIO()), \
                redirect_stderr(StringIO()):
            code = 0
            try:
                maintenance.cmd_status(self.c, mock.Mock(notify=True, max_age=None))
            except SystemExit as exc:
                code = exc.code
        return code, notify


class RunDueAlertResetTests(AlertFixture):
    def run_due(self, results, *, due=True):
        """``results`` maps service -> None (success) or an exception."""
        manifests = [{'service': name, 'schedule': CRON} for name in results]

        def backup_one(_c, m):
            if results[m['service']] is not None:
                raise results[m['service']]

        with mock.patch.object(maintenance, 'manifests', return_value=manifests), \
                mock.patch.object(maintenance, 'local_now', return_value=NOW), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(due, 'r', None),
                ), mock.patch.object(maintenance, 'clear_stale_restic_locks'), \
                mock.patch.object(maintenance, 'backup_one', side_effect=backup_one), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            try:
                maintenance.cmd_run_due(self.c, mock.Mock())
            except SystemExit:
                pass

    def test_failure_records_sorted_failed_services(self):
        self.run_due({'b': RuntimeError('x'), 'a': RuntimeError('y'), 'c': None})
        self.assertEqual(
            self.unit_record(maintenance.BACKUP_UNIT)['failure_signature'],
            ['a', 'b'],
        )

    def test_unchanged_failure_set_is_throttled(self):
        self.run_due({'a': RuntimeError('x')})
        self.notify_failure(maintenance.BACKUP_UNIT).assert_called_once()
        self.run_due({'a': RuntimeError('other text, same service')})
        self.notify_failure(
            maintenance.BACKUP_UNIT, at=NOW + dt.timedelta(minutes=5),
        ).assert_not_called()

    def test_changed_failure_set_renotifies_immediately(self):
        self.run_due({'a': RuntimeError('x'), 'b': None})
        self.notify_failure(maintenance.BACKUP_UNIT).assert_called_once()
        self.run_due({'a': RuntimeError('x'), 'b': RuntimeError('x')})
        notify = self.notify_failure(
            maintenance.BACKUP_UNIT, at=NOW + dt.timedelta(minutes=5),
        )
        notify.assert_called_once()
        self.assertIn('failed: a, b', notify.call_args.args[2])
        self.run_due({'a': RuntimeError('x'), 'b': RuntimeError('x')})
        self.notify_failure(
            maintenance.BACKUP_UNIT, at=NOW + dt.timedelta(minutes=10),
        ).assert_not_called()

    def test_success_clears_record_so_next_failure_pushes(self):
        self.run_due({'a': RuntimeError('x')})
        self.notify_failure(maintenance.BACKUP_UNIT).assert_called_once()
        self.run_due({'a': None})
        self.assertEqual(self.unit_record(maintenance.BACKUP_UNIT), {})
        self.run_due({'a': RuntimeError('x')})
        self.notify_failure(
            maintenance.BACKUP_UNIT, at=NOW + dt.timedelta(minutes=10),
        ).assert_called_once()

    def test_nothing_due_keeps_the_record(self):
        self.run_due({'a': RuntimeError('x')})
        self.notify_failure(maintenance.BACKUP_UNIT)
        before = self.unit_record(maintenance.BACKUP_UNIT)
        self.run_due({'a': None}, due=False)
        self.assertEqual(self.unit_record(maintenance.BACKUP_UNIT), before)
        self.assertIn('notified_at', before)

    def test_success_does_not_touch_other_units(self):
        self.notify_failure(maintenance.CHECK_UNIT)
        self.run_due({'a': RuntimeError('x')})
        self.run_due({'a': None})
        self.assertIn('notified_at', self.unit_record(maintenance.CHECK_UNIT))

    def test_success_without_alert_state_writes_nothing(self):
        self.run_due({'a': None})
        self.assertFalse((self.state_root / alerting.ALERT_STATE_NAME).exists())
        self.assertFalse((self.state_root / alerting.ALERT_LOCK_NAME).exists())


class MaintenanceAndCheckResetTests(AlertFixture):
    def maintenance(self, *, prune_fails=False, dry_run=False):
        def fake_run(command, **_kwargs):
            if prune_fails and command[:2] == ['restic', 'prune']:
                raise RuntimeError('prune failed')
            return mock.Mock(stdout='')

        with mock.patch.object(maintenance, 'clear_stale_restic_locks'), \
                mock.patch.object(maintenance, 'manifests', return_value=[]), \
                mock.patch.object(maintenance, 'snapshot_state_services', return_value=[]), \
                mock.patch.object(maintenance, 'restic_env', return_value={}), \
                mock.patch.object(maintenance, 'run', side_effect=fake_run), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            try:
                maintenance.cmd_maintenance(
                    self.c, mock.Mock(no_wait=False, dry_run=dry_run),
                )
            except SystemExit:
                pass

    def check(self, *, fails=False):
        def fake_run(command, **_kwargs):
            if fails:
                raise RuntimeError('check failed')
            return mock.Mock(stdout='')

        with mock.patch.object(maintenance, 'clear_stale_restic_locks'), \
                mock.patch.object(maintenance, 'restic_env', return_value={}), \
                mock.patch.object(maintenance, 'run', side_effect=fake_run), \
                redirect_stdout(StringIO()):
            try:
                maintenance.cmd_check(self.c, mock.Mock(no_wait=False))
            except RuntimeError:
                pass

    def test_successful_maintenance_clears_only_its_unit(self):
        self.notify_failure(maintenance.MAINTENANCE_UNIT)
        self.notify_failure(maintenance.CHECK_UNIT)
        self.maintenance(prune_fails=True)
        self.assertNotEqual(self.unit_record(maintenance.MAINTENANCE_UNIT), {})
        self.maintenance(dry_run=True)
        self.assertNotEqual(self.unit_record(maintenance.MAINTENANCE_UNIT), {})
        self.maintenance()
        self.assertEqual(self.unit_record(maintenance.MAINTENANCE_UNIT), {})
        self.assertNotEqual(self.unit_record(maintenance.CHECK_UNIT), {})
        self.notify_failure(
            maintenance.MAINTENANCE_UNIT, at=NOW + dt.timedelta(hours=1),
        ).assert_called_once()

    def test_successful_check_clears_its_unit(self):
        self.notify_failure(maintenance.CHECK_UNIT)
        self.check(fails=True)
        self.assertNotEqual(self.unit_record(maintenance.CHECK_UNIT), {})
        self.check()
        self.assertEqual(self.unit_record(maintenance.CHECK_UNIT), {})
        self.notify_failure(
            maintenance.CHECK_UNIT, at=NOW + dt.timedelta(hours=1),
        ).assert_called_once()


class StatusFailureHandOffTests(AlertFixture):
    def test_handled_record_stores_invocation_id(self):
        self.status({'a': STALE}, invocation_id='run-1')
        status = self.alert_state()['status']
        self.assertEqual(status['invocation_id'], 'run-1')
        self.assertEqual(status['handled_at'], NOW.isoformat())

    def test_matching_invocation_id_skips_push(self):
        self.status({'a': STALE}, invocation_id='run-1')
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=1),
            monitor_id='run-1',
        ).assert_not_called()

    def test_later_status_crash_is_pushed(self):
        self.status({'a': STALE}, invocation_id='run-1')
        # A later run crashed before it could record anything, within the
        # renotify window that used to swallow it.
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=5),
            monitor_id='run-2',
        ).assert_called_once()

    def test_suppressed_unchanged_problem_is_also_handled(self):
        self.status({'a': STALE}, invocation_id='run-1')
        later = NOW + dt.timedelta(hours=6)
        code, notify = self.status({'a': STALE}, at=later, invocation_id='run-2')
        self.assertEqual(code, maintenance.STALE_EXIT_CODE)
        notify.assert_not_called()
        status = self.alert_state()['status']
        self.assertEqual(status['invocation_id'], 'run-2')
        self.assertEqual(status['notified_at'], NOW.isoformat())
        self.notify_failure(
            maintenance.STATUS_UNIT, at=later, monitor_id='run-2',
        ).assert_not_called()
        self.notify_failure(
            maintenance.STATUS_UNIT, at=later, monitor_id='run-1',
        ).assert_called_once()

    def test_undelivered_problem_is_not_handled(self):
        self.status({'a': STALE}, invocation_id='run-1', delivered=False)
        self.notify_failure(
            maintenance.STATUS_UNIT, monitor_id='run-1',
        ).assert_called_once()

    def test_status_own_error_is_always_pushed(self):
        code, notify = self.status(
            {'a': STALE, 'b': OSError('state unreadable')}, invocation_id='run-1',
        )
        self.assertEqual(code, 1)
        notify.assert_called_once()
        self.assertNotIn('handled_at', self.alert_state()['status'])
        self.notify_failure(
            maintenance.STATUS_UNIT, monitor_id='run-1',
        ).assert_called_once()
        self.notify_failure(maintenance.STATUS_UNIT).assert_not_called()

    def test_own_error_clears_older_handled_record(self):
        self.status({'a': STALE}, invocation_id='run-1')
        self.status(
            {'a': STALE, 'b': OSError('boom')}, invocation_id='run-2',
            at=NOW + dt.timedelta(minutes=1),
        )
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=2),
        ).assert_called_once()

    def test_fallback_without_ids_uses_recent_unconsumed_record(self):
        self.status({'a': STALE})
        self.assertNotIn('invocation_id', self.alert_state()['status'])
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=1),
        ).assert_not_called()
        # The record was consumed by that notify-failure; a second failure
        # in the window is a different run and is pushed.
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=3),
        ).assert_called_once()

    def test_fallback_ignores_old_record(self):
        self.status({'a': STALE})
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=21),
        ).assert_called_once()

    def test_fallback_when_only_one_id_is_known(self):
        self.status({'a': STALE})
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(minutes=1),
            monitor_id='run-9',
        ).assert_not_called()
        self.status({'a': STALE}, invocation_id='run-1',
                    at=NOW + dt.timedelta(hours=2))
        self.notify_failure(
            maintenance.STATUS_UNIT, at=NOW + dt.timedelta(hours=2, minutes=1),
        ).assert_not_called()

    def test_other_units_ignore_status_record(self):
        self.status({'a': STALE}, invocation_id='run-1')
        self.notify_failure(
            maintenance.BACKUP_UNIT, monitor_id='run-1',
        ).assert_called_once()


class StatusTitleAndSignatureTests(AlertFixture):
    def hold_lock(self, pid, hours):
        started = (NOW - dt.timedelta(hours=hours)).isoformat()
        handle = open(self.c['lock_file'], 'a+', encoding='utf-8')
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(f'pid={pid} started={started}\n')
        handle.flush()
        return handle

    def test_only_status_error_is_not_called_stale(self):
        _, notify = self.status({'a': OSError('state unreadable')})
        title, body = notify.call_args.args[1:3]
        self.assertIn('status check failed', title)
        self.assertNotIn('stale', title)
        self.assertNotIn('stalled', title)
        self.assertIn('a: state loading failed: state unreadable', body)

    def test_title_combines_actual_problems(self):
        self.hold_lock(4242, 14)
        _, notify = self.status({'a': STALE, 'b': OSError('x')})
        title = notify.call_args.args[1]
        for part in ('backups are stale', 'backup appears stalled',
                     'status check failed'):
            self.assertIn(part, title)

    def test_stalled_lock_alone(self):
        self.hold_lock(4242, 14)
        _, notify = self.status({})
        title = notify.call_args.args[1]
        self.assertIn('backup appears stalled', title)
        self.assertNotIn('stale;', title)

    def test_different_status_error_renotifies(self):
        self.status({'a': OSError('first')})
        later = NOW + dt.timedelta(minutes=30)
        self.status({'a': OSError('first')}, at=later)[1].assert_not_called()
        self.status({'a': OSError('second')}, at=later)[1].assert_called_once()

    def test_different_lock_holder_renotifies(self):
        handle = self.hold_lock(4242, 14)
        self.status({})
        later = NOW + dt.timedelta(minutes=30)
        self.status({}, at=later)[1].assert_not_called()
        handle.close()
        self.hold_lock(5151, 14)
        self.status({}, at=later)[1].assert_called_once()

    def test_signature_is_stable_and_contains_no_raw_error(self):
        errors = [('b', 'secret detail'), ('a', 'x')]
        holder = {'pid': 1, 'started': 's'}
        first = maintenance._status_signature([], holder, errors)
        self.assertEqual(
            first, maintenance._status_signature([], holder, errors[::-1]),
        )
        self.assertNotIn('secret detail', first)
        self.assertEqual(json.loads(first)['lock_holder'], holder)


class AlertStateTransactionTests(AlertFixture):
    def test_transaction_saves_changes_and_creates_private_lock(self):
        with alerting.alert_state_transaction(self.c) as state:
            state['x'] = 1
        self.assertEqual(self.alert_state(), {'x': 1})
        lock = self.state_root / alerting.ALERT_LOCK_NAME
        self.assertTrue(stat.S_ISREG(lock.lstat().st_mode))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(lock, Path(self.c['lock_file']))

    def test_unchanged_state_is_not_rewritten(self):
        with mock.patch.object(alerting, 'save_alert_state') as save:
            with alerting.alert_state_transaction(self.c):
                pass
        save.assert_not_called()

    def test_exception_in_block_does_not_save_and_releases_lock(self):
        with self.assertRaises(RuntimeError):
            with alerting.alert_state_transaction(self.c) as state:
                state['x'] = 1
                raise RuntimeError('boom')
        self.assertEqual(self.alert_state(), {})
        with alerting.alert_state_transaction(self.c) as state:
            state['y'] = 2
        self.assertEqual(self.alert_state(), {'y': 2})

    def test_without_state_root_is_a_noop(self):
        with alerting.alert_state_transaction({}) as state:
            state['x'] = 1

    def test_concurrent_threads_do_not_lose_updates(self):
        def worker():
            for _ in range(25):
                with alerting.alert_state_transaction(self.c) as state:
                    state['count'] = state.get('count', 0) + 1

        with redirect_stderr(StringIO()) as err:
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(self.alert_state(), {'count': 100})
        self.assertNotIn('WARNING', err.getvalue())

    def test_concurrent_processes_do_not_lose_updates(self):
        script = (
            'import sys\n'
            'from homelab_backup import alerting\n'
            'c = {"state_root": sys.argv[1]}\n'
            'for _ in range(20):\n'
            '    with alerting.alert_state_transaction(c) as state:\n'
            '        state["count"] = state.get("count", 0) + 1\n'
        )
        processes = [
            subprocess.Popen(
                [sys.executable, '-c', script, str(self.state_root)],
                cwd=ROOT, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(3)
        ]
        for process in processes:
            _, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
            self.assertNotIn('WARNING', err)
        self.assertEqual(self.alert_state(), {'count': 60})

    def test_waits_for_a_briefly_held_lock(self):
        self.state_root.mkdir(mode=0o700)
        holder = open(self.state_root / alerting.ALERT_LOCK_NAME, 'a')
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        sleeps = []

        def release(seconds):
            sleeps.append(seconds)
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

        with mock.patch.object(alerting, 'alert_lock_sleep', side_effect=release), \
                redirect_stderr(StringIO()) as err:
            with alerting.alert_state_transaction(self.c) as state:
                state['x'] = 1
        self.assertEqual(sleeps, [alerting.ALERT_LOCK_RETRY_SECONDS])
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(self.alert_state(), {'x': 1})

    def hold_alert_lock(self):
        self.state_root.mkdir(mode=0o700)
        holder = open(self.state_root / alerting.ALERT_LOCK_NAME, 'a')
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    def test_stuck_lock_fails_open_with_warning(self):
        self.hold_alert_lock()
        with mock.patch.object(alerting, 'ALERT_LOCK_TIMEOUT_SECONDS', 0.05), \
                mock.patch.object(alerting, 'alert_lock_sleep'), \
                redirect_stderr(StringIO()) as err:
            with alerting.alert_state_transaction(self.c) as state:
                state['x'] = 1
        self.assertIn('WARNING: could not lock alert state', err.getvalue())
        self.assertEqual(self.alert_state(), {'x': 1})

    def test_notification_goes_out_while_alert_lock_is_stuck(self):
        self.hold_alert_lock()
        with mock.patch.object(alerting, 'ALERT_LOCK_TIMEOUT_SECONDS', 0), \
                mock.patch.object(alerting, 'alert_lock_sleep'):
            self.notify_failure(maintenance.BACKUP_UNIT).assert_called_once()
            self.assertEqual(self.status({'a': STALE})[1].call_count, 1)

    def test_lock_symlink_is_not_followed(self):
        self.state_root.mkdir(mode=0o700)
        target = self.root / 'elsewhere'
        (self.state_root / alerting.ALERT_LOCK_NAME).symlink_to(target)
        with redirect_stderr(StringIO()) as err:
            with alerting.alert_state_transaction(self.c) as state:
                state['x'] = 1
        self.assertIn('WARNING: could not lock alert state', err.getvalue())
        self.assertFalse(target.exists())
        self.assertEqual(self.alert_state(), {'x': 1})


if __name__ == '__main__':
    unittest.main()
