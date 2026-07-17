import copy
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from homelab_backup import backup, backup_state, common
from tests.helpers import manifest


class BackupStateTests(unittest.TestCase):
    def test_save_state_creates_custom_private_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'custom' / 'state'
            backup_state.save_state(
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
        now = backup_state.dt.datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)

        for invalid_timestamp in (123, '2026-07-14T12:00:00'):
            with self.subTest(timestamp=invalid_timestamp), \
                    mock.patch.object(backup_state, 'load_state', return_value={
                        'last_success_at': invalid_timestamp,
                        'last_result': 'success',
                    }):
                due, _reason, occurrence = backup_state.due_status({}, value, now)

            self.assertTrue(due)
            self.assertEqual(
                occurrence,
                backup_state.dt.datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            )

    def test_naive_last_attempt_timestamp_does_not_block_retry_planning(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '0 * * * *', 'retry_after': '30m'},
        }
        now = backup_state.dt.datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)
        with mock.patch.object(backup_state, 'load_state', return_value={
            'last_attempt_at': '2026-07-14T12:00:00',
            'last_result': 'failed',
        }):
            due, _reason, occurrence = backup_state.due_status({}, value, now)

        self.assertTrue(due)
        self.assertEqual(
            occurrence,
            backup_state.dt.datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        )

    def test_max_lateness_uses_elapsed_time_across_fall_back(self):
        timezone_info = ZoneInfo('America/New_York')
        value = {
            'service': 'demo',
            'schedule': {'cron': '30 1 * * *', 'max_lateness': '1h'},
        }
        now = backup_state.dt.datetime(
            2026, 11, 1, 1, 45, tzinfo=timezone_info, fold=1,
        )
        with mock.patch.object(backup_state, 'load_state', return_value={}):
            due, reason, _next_time = backup_state.due_status({}, value, now)

        self.assertFalse(due)
        self.assertIn('missed', reason)

    def test_frequent_schedule_runs_during_second_fall_back_hour(self):
        timezone_info = ZoneInfo('America/New_York')
        value = {
            'service': 'demo',
            'schedule': {'cron': '*/5 * * * *'},
        }
        now = backup_state.dt.datetime(
            2026, 11, 1, 1, 5, tzinfo=timezone_info, fold=1,
        )
        with mock.patch.object(backup_state, 'load_state', return_value={
            'last_success_at': '2026-11-01T01:55:00-04:00',
            'last_result': 'success',
        }):
            due, _reason, occurrence = backup_state.due_status({}, value, now)

        self.assertTrue(due)
        self.assertEqual(occurrence, now)
        self.assertEqual(occurrence.fold, 1)

    def test_long_backup_completion_skips_intervals_reached_while_running(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '*/5 * * * *'},
        }
        now = backup_state.dt.datetime(2026, 7, 15, 12, 18, tzinfo=timezone.utc)
        with mock.patch.object(backup_state, 'load_state', return_value={
            # The 12:00 run finished after the 12:05, 12:10, and 12:15 slots.
            'last_success_at': '2026-07-15T12:17:00+00:00',
            'last_result': 'success',
        }):
            due, reason, next_time = backup_state.due_status({}, value, now)

        self.assertFalse(due)
        self.assertIn('next at', reason)
        self.assertEqual(
            next_time,
            backup_state.dt.datetime(2026, 7, 15, 12, 20, tzinfo=timezone.utc),
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

if __name__ == '__main__':
    unittest.main()
