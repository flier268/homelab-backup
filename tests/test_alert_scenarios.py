"""Fake-clock alert scenarios: run-due + notify-failure every 5 minutes.

The real cmd_run_due and cmd_notify_failure run against a temporary
state_root. systemd is imitated: each run-due gets its own INVOCATION_ID,
and notify-failure runs only when run-due exits non-zero, with
MONITOR_INVOCATION_ID / MONITOR_SERVICE_RESULT / MONITOR_EXIT_* set.
"""
import datetime as dt
import os
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import alerting, maintenance

START = dt.datetime(2026, 1, 5, 0, 0, tzinfo=dt.timezone.utc)
STEP_MINUTES = 5
DAY = 24 * 60 // STEP_MINUTES  # 288 rounds

# How systemd describes each simulated end of run-due.
MONITOR = {
    'exit-code': {'MONITOR_SERVICE_RESULT': 'exit-code',
                  'MONITOR_EXIT_CODE': 'exited', 'MONITOR_EXIT_STATUS': '1'},
    'timeout': {'MONITOR_SERVICE_RESULT': 'timeout',
                'MONITOR_EXIT_CODE': 'killed', 'MONITOR_EXIT_STATUS': 'TERM'},
    'oom-kill': {'MONITOR_SERVICE_RESULT': 'oom-kill',
                 'MONITOR_EXIT_CODE': 'killed', 'MONITOR_EXIT_STATUS': 'KILL'},
}
HOUR = 60 // STEP_MINUTES  # 12 rounds


class SimulatedKill(BaseException):
    """systemd killed run-due before it could finish its bookkeeping.

    The argument is the MONITOR_SERVICE_RESULT systemd reports.
    """


class Simulation:
    """Drive run-due every 5 minutes and count notify() calls.

    ``schedules`` maps service -> (period minutes, offset minutes); a
    service is due when the simulated minute matches its schedule, so a
    failed service is retried on its schedule.
    """

    def __init__(self, testcase, schedules, renotify_after='24h'):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.c = {
            'host_id': 'h', 'state_root': str(root / 'state'),
            'lock_file': str(root / 'backupctl.lock'),
            'alert': {'renotify_after': renotify_after},
        }
        self.schedules = schedules
        self.round = 0
        self.failing = set()
        self.crash = None
        self.pushes = []
        stack = ExitStack()
        testcase.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ))
        for name in ('INVOCATION_ID', *MONITOR['exit-code'], 'MONITOR_INVOCATION_ID'):
            os.environ.pop(name, None)
        for name, value in {
            'local_now': self.now,
            'manifests': self.manifests,
            'due_status': self.due_status,
            'backup_one': self.backup_one,
            'clear_stale_restic_locks': lambda _c: None,
            'run': lambda *_a, **_k: SimpleNamespace(stdout='journal tail\n'),
            'notify': self.notify,
        }.items():
            stack.enter_context(mock.patch.object(maintenance, name, side_effect=value))

    def now(self):
        return START + dt.timedelta(minutes=STEP_MINUTES * self.round)

    def manifests(self, _c, on_error=None):
        if self.crash == 'exit-code':
            raise RuntimeError('config error: cannot read manifests')
        return [{'service': name} for name in self.schedules]

    def due_status(self, _c, m, _now):
        period, offset = self.schedules[m['service']]
        minute = STEP_MINUTES * self.round
        due = minute >= offset and (minute - offset) % period == 0
        return due, 'due' if due else 'not due', None

    def backup_one(self, _c, m):
        if self.crash in ('timeout', 'oom-kill'):
            raise SimulatedKill(self.crash)
        if m['service'] in self.failing:
            raise RuntimeError(f"{m['service']} broke\nlong detail")

    def notify(self, _c, title, message, **_kwargs):
        self.pushes.append((self.round, title, message))
        return True

    def step(self, failing=(), crash=None):
        """One timer tick: run-due, then notify-failure if it failed."""
        self.failing, self.crash = set(failing), crash
        run_id = f'run-due-{self.round}'
        result = None
        with mock.patch.dict(os.environ, {'INVOCATION_ID': run_id}), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            try:
                maintenance.cmd_run_due(self.c, SimpleNamespace())
            except SystemExit as exc:
                result = 'exit-code' if exc.code else None
            except SimulatedKill as exc:
                result = exc.args[0]
            except Exception:  # noqa: BLE001 - an uncaught crash exits 1
                result = 'exit-code'
        if result is not None:
            env = {
                'INVOCATION_ID': f'notify-{self.round}',
                'MONITOR_INVOCATION_ID': run_id, **MONITOR[result],
            }
            with mock.patch.dict(os.environ, env), \
                    redirect_stderr(StringIO()):
                maintenance.cmd_notify_failure(self.c, SimpleNamespace(
                    unit=maintenance.BACKUP_UNIT, alert_printed=True,
                ))
        self.round += 1

    def run(self, rounds, **kwargs):
        for _ in range(rounds):
            self.step(**kwargs)

    def run_intermittent(self, rounds, every, crash):
        """Crash every ``every`` rounds; the rounds in between complete."""
        for i in range(rounds):
            self.step(crash=crash if i % every == 0 else None)

    def pushed_services(self):
        """service -> rounds in which it was listed as newly failing."""
        found = {}
        for round_, _, body in self.pushes:
            for name in re.findall(r'^- (\S+):', body, re.MULTILINE):
                found.setdefault(name, []).append(round_)
        return found

    def crash_pushes(self):
        return [
            (round_, match.group(1)) for round_, title, _ in self.pushes
            for match in [re.search(r'failed \(([^)]+)\)$', title)] if match
        ]

    def backup_record(self):
        state = alerting.load_alert_state(self.c)
        return state.get('units', {}).get(maintenance.BACKUP_UNIT, {})


def report(name, sim):
    print(f'\n[alert scenario] {name}: {len(sim.pushes)} push(es) '
          f'over {sim.round} rounds', file=sys.stderr)


class PerServiceThrottleScenarios(unittest.TestCase):
    def test_s1b_one_failing_service_next_to_hourly_success(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(DAY, failing={'svc-a'})
        report('S1b A fails, B succeeds hourly, 24h', sim)
        self.assertEqual(len(sim.pushes), 1)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0]})
        self.assertIn('- svc-a: svc-a broke', sim.pushes[0][2])
        self.assertNotIn('long detail', sim.pushes[0][2])

    def test_s6_two_failing_services(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 30)})
        sim.run(DAY, failing={'svc-a', 'svc-b'})
        report('S6 A and B fail, 24h', sim)
        self.assertLessEqual(len(sim.pushes), 2)
        self.assertEqual(len(sim.pushes), 2)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0], 'svc-b': [6]})

    def test_s7_three_failing_services(self):
        sim = Simulation(
            self, {'svc-a': (5, 0), 'svc-b': (60, 30), 'svc-c': (120, 90)},
        )
        sim.run(DAY, failing={'svc-a', 'svc-b', 'svc-c'})
        report('S7 A, B, C fail, 24h', sim)
        self.assertEqual(len(sim.pushes), 3)
        self.assertEqual(
            sim.pushed_services(),
            {'svc-a': [0], 'svc-b': [6], 'svc-c': [18]},
        )

    def test_s7_services_due_together_share_one_push(self):
        sim = Simulation(
            self, {'svc-a': (5, 0), 'svc-b': (60, 0), 'svc-c': (120, 0)},
        )
        sim.run(DAY, failing={'svc-a', 'svc-b', 'svc-c'})
        report('S7 A, B, C fail, due together, 24h', sim)
        self.assertEqual(len(sim.pushes), 1)
        self.assertIn('failed: svc-a, svc-b, svc-c', sim.pushes[0][2])

    def test_failure_after_recovery_is_pushed_immediately(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(12, failing={'svc-a'})
        sim.run(1)
        sim.run(DAY - 13, failing={'svc-a'})
        report('A fails, recovers, fails again, 24h', sim)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0, 13]})
        self.assertEqual(len(sim.pushes), 2)

    def test_hourly_service_recovery_and_relapse(self):
        sim = Simulation(self, {'svc-b': (60, 0)})
        sim.run(36, failing={'svc-b'})   # fails at 00:00, 01:00, 02:00
        sim.run(12)                      # succeeds at 03:00
        sim.run(DAY - 48, failing={'svc-b'})
        report('B (hourly) fails, recovers, fails again, 24h', sim)
        self.assertEqual(sim.pushed_services(), {'svc-b': [0, 48]})

    def test_48h_of_one_failing_service_renotifies_once_per_day(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(2 * DAY, failing={'svc-a'})
        report('A fails for 48h', sim)
        self.assertEqual(len(sim.pushes), 2)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0, DAY]})

    def test_shorter_renotify_after_is_honoured(self):
        sim = Simulation(self, {'svc-a': (5, 0)}, renotify_after='6h')
        sim.run(DAY, failing={'svc-a'})
        report('A fails for 24h, renotify_after 6h', sim)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0, 72, 144, 216]})


class CrashScenarios(unittest.TestCase):
    def test_m2_crash_after_normal_failure_is_reported_not_the_stale_set(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(1, failing={'svc-a'})
        sim.run(20 * 12, crash='exit-code')
        report('M2 A fails once, then exit-code crash for 20h', sim)
        self.assertEqual(len(sim.pushes), 2)
        round_, title, body = sim.pushes[1]
        self.assertEqual(round_, 1)
        self.assertTrue(title.endswith('homelab-backup.service failed (exit-code)'))
        self.assertIn('result=exit-code, exit_code=exited, exit_status=1', body)
        self.assertNotIn('svc-a', body.split('\n\n')[0])

        sim.run(12, crash='timeout')
        report('M2 ... then timeout for 1h', sim)
        self.assertEqual(sim.crash_pushes(), [(1, 'exit-code'), (241, 'timeout')])

        # A completed run right after a crash does not clear it, whether a
        # service failed or not. The exit-code entry, quiet since round 240,
        # is cleared by the first completed round, even one where svc-a fails.
        sim.run(1, failing={'svc-a'})
        self.assertEqual(set(sim.backup_record()['crashes']), {'crash:timeout'})
        sim.run(1)
        self.assertEqual(set(sim.backup_record()['crashes']), {'crash:timeout'})
        self.assertEqual(len(sim.pushes), 3)  # svc-a is still within 24h
        sim.run(1, crash='timeout')
        report('M2 ... then completed runs and the same crash at once', sim)
        self.assertEqual(len(sim.pushes), 3)
        # After CRASH_QUIET_PERIOD of successful rounds it is cleared.
        sim.run(HOUR - 1)
        self.assertIn('crashes', sim.backup_record())
        sim.run(1)
        self.assertNotIn('crashes', sim.backup_record())
        sim.run(1, crash='timeout')
        report('M2 ... then 1h of successful runs and the same crash', sim)
        self.assertEqual(sim.crash_pushes()[-1], (268, 'timeout'))
        self.assertEqual(len(sim.pushes), 4)

    def test_hourly_oom_kill_between_successful_rounds(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run_intermittent(DAY, HOUR, 'oom-kill')
        report('run-due OOM-killed hourly, other rounds succeed, 24h', sim)
        self.assertEqual(sim.crash_pushes(), [(0, 'oom-kill')])
        self.assertEqual(len(sim.pushes), 1)
        entry = sim.backup_record()['crashes']['crash:oom-kill']
        self.assertEqual(
            entry['details'], 'result=oom-kill, exit_code=killed, exit_status=KILL',
        )

    def test_half_hourly_crash_between_successful_rounds(self):
        sim = Simulation(self, {'svc-a': (5, 0)})
        sim.run_intermittent(DAY, HOUR // 2, 'timeout')
        report('crash every 30 min, other rounds succeed, 24h', sim)
        self.assertEqual(sim.crash_pushes(), [(0, 'timeout')])
        self.assertEqual(len(sim.pushes), 1)

    def test_same_crash_after_quiet_period_is_pushed_immediately(self):
        sim = Simulation(self, {'svc-a': (5, 0)})
        sim.run(1, crash='oom-kill')
        sim.run(3 * HOUR)
        self.assertNotIn('crashes', sim.backup_record())
        sim.run(1, crash='oom-kill')
        report('crash, 3h of successful rounds, same crash', sim)
        self.assertEqual(
            sim.crash_pushes(), [(0, 'oom-kill'), (3 * HOUR + 1, 'oom-kill')],
        )
        self.assertEqual(len(sim.pushes), 2)

    def test_persistent_crash_renotifies_once_per_renotify_after(self):
        sim = Simulation(self, {'svc-a': (5, 0)})
        sim.run(DAY + 12, crash='exit-code')
        report('exit-code crash for 25h', sim)
        self.assertEqual(sim.crash_pushes(), [(0, 'exit-code'), (DAY, 'exit-code')])

    def test_result_type_change_pushes_again(self):
        sim = Simulation(self, {'svc-a': (5, 0)})
        sim.run(3, crash='exit-code')
        sim.run(3, crash='timeout')
        sim.run(3, crash='exit-code')
        report('exit-code -> timeout -> exit-code crash', sim)
        # Each result type is throttled by its own key.
        self.assertEqual(sim.crash_pushes(), [(0, 'exit-code'), (3, 'timeout')])

    def test_interleaved_crashes_and_failures_do_not_suppress_each_other(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(6, crash='timeout')
        sim.run(94, failing={'svc-a'})
        sim.run(12, crash='exit-code')
        sim.run(88, failing={'svc-a'})
        sim.run(6, crash='exit-code')
        sim.run(DAY - 206, failing={'svc-a'})
        report('interleaved crashes and A failures, 24h', sim)
        self.assertEqual(sim.pushed_services(), {'svc-a': [6]})
        # Rounds where svc-a fails still complete, so 1h after round 111 they
        # clear the exit-code entry and the second episode is pushed.
        self.assertEqual(
            sim.crash_pushes(),
            [(0, 'timeout'), (100, 'exit-code'), (200, 'exit-code')],
        )
        self.assertEqual(len(sim.pushes), 4)

    def test_e2_same_crash_after_an_hour_of_normal_rounds(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(1, crash='oom-kill')
        sim.run(HOUR - 1)
        self.assertIn('crashes', sim.backup_record())
        sim.run(1)
        self.assertNotIn('crashes', sim.backup_record())
        sim.run(1, crash='oom-kill')
        report('E2 crash, 1h of normal rounds, same crash', sim)
        self.assertEqual(
            sim.crash_pushes(), [(0, 'oom-kill'), (HOUR + 1, 'oom-kill')],
        )
        self.assertEqual(len(sim.pushes), 2)

    def test_e3_same_crash_while_a_service_keeps_failing(self):
        sim = Simulation(self, {'svc-a': (5, 0), 'svc-b': (60, 0)})
        sim.run(6, failing={'svc-a'})
        sim.run(1, crash='timeout')
        sim.run(HOUR, failing={'svc-a'})
        self.assertNotIn('crashes', sim.backup_record())
        sim.run(1, crash='timeout')
        sim.run(DAY - HOUR - 8, failing={'svc-a'})
        report('E3 A keeps failing, crash, 1h, same crash, 24h', sim)
        self.assertEqual(sim.pushed_services(), {'svc-a': [0]})
        self.assertEqual(
            sim.crash_pushes(), [(6, 'timeout'), (HOUR + 7, 'timeout')],
        )
        self.assertEqual(len(sim.pushes), 3)

    def test_e2_daily_service_same_crash_after_an_hour_of_nothing_due(self):
        sim = Simulation(self, {'svc-d': (24 * 60, 0)})
        sim.run(1)                       # the only due round today succeeds
        sim.run(1, crash='exit-code')
        sim.run(HOUR - 1)                # "No backups are due"
        self.assertIn('crashes', sim.backup_record())
        sim.run(1)
        self.assertNotIn('crashes', sim.backup_record())
        sim.run(1, crash='exit-code')
        report('E2 daily service, crash, 1h of nothing due, same crash', sim)
        self.assertEqual(
            sim.crash_pushes(), [(1, 'exit-code'), (HOUR + 2, 'exit-code')],
        )
        self.assertEqual(len(sim.pushes), 2)

    def test_daily_service_hourly_crash_between_nothing_due_rounds(self):
        sim = Simulation(self, {'svc-d': (24 * 60, 0)})
        sim.run(1)
        sim.run_intermittent(DAY - 1, HOUR, 'exit-code')
        report('daily service, exit-code crash hourly, nothing due, 24h', sim)
        self.assertEqual(sim.crash_pushes(), [(1, 'exit-code')])
        self.assertEqual(len(sim.pushes), 1)


class StatusScenarios(unittest.TestCase):
    def test_clean_status_resets_status_unit_throttle(self):
        sim = Simulation(self, {'svc-a': (5, 0)})

        def status_failure():
            with mock.patch.dict(os.environ, {
                'MONITOR_INVOCATION_ID': f'status-{sim.round}',
                'MONITOR_SERVICE_RESULT': 'exit-code',
            }), redirect_stderr(StringIO()):
                maintenance.cmd_notify_failure(sim.c, SimpleNamespace(
                    unit=maintenance.STATUS_UNIT, alert_printed=True,
                ))
            sim.round += 1

        def clean_status():
            with mock.patch.object(maintenance, 'load_state', return_value={
                'last_success_at': sim.now().isoformat(),
            }), mock.patch.object(
                maintenance, 'success_staleness', return_value={'stale': False},
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                maintenance.cmd_status(
                    sim.c, SimpleNamespace(notify=True, max_age=None),
                )
            sim.round += 1

        status_failure()
        status_failure()
        self.assertEqual(len(sim.pushes), 1)
        clean_status()
        status_failure()
        report('status fails twice, clean run, fails again', sim)
        problems = [title for _, title, _ in sim.pushes if 'status.service' in title]
        self.assertEqual(len(problems), 2)


if __name__ == '__main__':
    unittest.main()
