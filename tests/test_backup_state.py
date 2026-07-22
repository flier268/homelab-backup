import copy
import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from homelab_backup import backup, backup_state, common, storage
from tests.helpers import manifest


class BackupStateTests(unittest.TestCase):
    def test_state_path_rejects_unsafe_service_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'invalid service name'):
                backup_state.state_path(
                    {'state_root': str(root / 'state')}, '../outside',
                )
            with self.assertRaisesRegex(ValueError, 'invalid service name'):
                backup_state.save_state(
                    {'state_root': str(root / 'state')}, '../outside', {},
                )

            self.assertFalse((root / 'outside.json').exists())
            self.assertFalse((root / 'state').exists())

    def test_load_state_returns_empty_only_when_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'state'
            state_root.mkdir()
            config = {'state_root': str(state_root)}
            self.assertEqual(backup_state.load_state(config, 'missing'), {})

            for payload in ('not json', '[]'):
                with self.subTest(payload=payload):
                    (state_root / 'demo.json').write_text(
                        payload, encoding='utf-8',
                    )
                    with self.assertRaisesRegex(ValueError, 'backup state'):
                        backup_state.load_state(config, 'demo')

    def test_load_state_does_not_hide_read_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'state'
            state_root.mkdir()
            path = state_root / 'demo.json'
            path.write_text('{}', encoding='utf-8')
            with mock.patch.object(
                backup_state.os, 'open', side_effect=OSError('unreadable'),
            ), self.assertRaisesRegex(ValueError, 'unreadable'):
                backup_state.load_state(
                    {'state_root': str(state_root)}, 'demo',
                )

    def test_load_state_refuses_symlink_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / 'state'
            state_root.mkdir()
            outside = root / 'outside.json'
            outside.write_text('{"last_result": "success"}', encoding='utf-8')
            (state_root / 'demo.json').symlink_to(outside)

            with self.assertRaisesRegex(ValueError, 'backup state'):
                backup_state.load_state(
                    {'state_root': str(state_root)}, 'demo',
                )

    def test_save_state_creates_custom_private_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'custom' / 'state'
            backup_state.save_state(
                {'state_root': str(state_root)}, 'demo',
                {'last_result': 'success'},
            )
            self.assertEqual(
                json.loads((state_root / 'demo.json').read_text(encoding='utf-8')),
                {'last_result': 'success'},
            )
            self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)

    def test_invalid_last_success_timestamp_fails_closed(self):
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
                with self.assertRaisesRegex(ValueError, 'last_success_at'):
                    backup_state.due_status({}, value, now)

    def test_naive_last_attempt_timestamp_fails_closed(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '0 * * * *', 'retry_after': '30m'},
        }
        now = backup_state.dt.datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)
        with mock.patch.object(backup_state, 'load_state', return_value={
            'last_attempt_at': '2026-07-14T12:00:00',
            'last_result': 'failed',
        }):
            with self.assertRaisesRegex(ValueError, 'last_attempt_at'):
                backup_state.due_status({}, value, now)

    def test_load_state_rejects_unknown_fields_and_invalid_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / 'state'
            state_root.mkdir()
            path = state_root / 'demo.json'
            for payload, message in (
                ({'unexpected': True}, 'unsupported'),
                ({'last_result': 'maybe'}, 'last_result'),
                ({'last_duration_seconds': -1}, 'last_duration_seconds'),
                ({'last_duration_seconds': float('nan')}, 'last_duration_seconds'),
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, message):
                        backup_state.load_state(
                            {'state_root': str(state_root)}, 'demo',
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

    def test_indeterminate_running_attempt_is_not_repeated_in_same_occurrence(self):
        value = {
            'service': 'demo',
            'schedule': {'cron': '0 * * * *'},
        }
        now = backup_state.dt.datetime(2026, 7, 15, 12, 5, tzinfo=timezone.utc)
        with mock.patch.object(backup_state, 'load_state', return_value={
            'last_attempt_at': '2026-07-15T12:01:00+00:00',
            'last_result': 'running',
        }):
            due, reason, next_time = backup_state.due_status({}, value, now)
            later_due, _, _ = backup_state.due_status(
                {},
                value,
                backup_state.dt.datetime(
                    2026, 7, 15, 13, 5, tzinfo=timezone.utc,
                ),
            )

        self.assertFalse(due)
        self.assertIn('attempted at', reason)
        self.assertEqual(
            next_time,
            backup_state.dt.datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(later_due)

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

    def test_retention_oserror_does_not_turn_snapshot_into_failed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            saved = []

            def fake_run(cmd, **_kwargs):
                if cmd[:2] == ['restic', 'forget']:
                    raise OSError('could not start retention')
                return mock.Mock(stdout='')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(
                        backup, 'save_state',
                        side_effect=lambda _c, _s, state:
                        saved.append(copy.deepcopy(state)),
                    ), mock.patch.object(
                        backup, 'stage_service', return_value=root,
                    ), mock.patch.object(
                        backup, 'restic_env', return_value={},
                    ), mock.patch.object(backup, 'run', side_effect=fake_run):
                self.assertTrue(backup.backup_one({'host_id': 'host'}, value))

            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertIn(
                'OSError: could not start retention',
                saved[-1]['last_retention_error'],
            )

    def test_precommit_failure_preserves_pending_retention_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            saved = []
            with mock.patch.object(backup, 'load_state', return_value={
                'last_retention_error': 'forget still pending',
            }), mock.patch.object(
                backup, 'save_state',
                side_effect=lambda _c, _s, state:
                saved.append(copy.deepcopy(state)),
            ), mock.patch.object(
                backup, 'stage_service', side_effect=RuntimeError('staging failed'),
            ), self.assertRaisesRegex(RuntimeError, 'staging failed'):
                backup.backup_one({'host_id': 'host'}, value)

            self.assertEqual(
                saved[-1]['last_retention_error'], 'forget still pending',
            )

    def test_successful_retention_clears_pending_retention_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            saved = []
            with mock.patch.object(backup, 'load_state', return_value={
                'last_retention_error': 'forget still pending',
            }), mock.patch.object(
                backup, 'save_state',
                side_effect=lambda _c, _s, state:
                saved.append(copy.deepcopy(state)),
            ), mock.patch.object(
                backup, 'stage_service', return_value=root,
            ), mock.patch.object(
                backup, 'restic_env', return_value={},
            ), mock.patch.object(backup, 'run'):
                self.assertTrue(backup.backup_one({'host_id': 'host'}, value))

            self.assertIsNone(saved[-1]['last_retention_error'])

    def test_final_state_save_failure_does_not_mark_snapshot_failed_or_remove_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            staging_root = root / 'staging'
            stage = staging_root / value['service']
            stage.mkdir(parents=True)
            marker = stage / 'snapshot-data'
            marker.write_text('complete', encoding='utf-8')
            saved = []

            def fake_save(_config, _service, state):
                saved.append(copy.deepcopy(state))
                if len(saved) == 2:
                    raise OSError('state filesystem unavailable')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(
                        backup, 'save_state', side_effect=fake_save,
                    ) as save_mock, mock.patch.object(
                        backup, 'stage_service', return_value=stage,
                    ), mock.patch.object(
                        backup, 'restic_env', return_value={},
                    ), mock.patch.object(backup, 'run'), \
                    self.assertRaisesRegex(
                        OSError, 'state filesystem unavailable',
                    ):
                backup.backup_one({
                    'host_id': 'host',
                    'staging_root': str(staging_root),
                }, value, apply_retention=False)

            self.assertEqual(save_mock.call_count, 2)
            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertTrue(marker.exists())

    def test_source_race_records_failed_state_instead_of_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })
            (Path(value['_dir']) / 'data').mkdir()
            saved = []

            def fail_during_sync(_config, manifest_value):
                with mock.patch.object(
                    storage, '_copy_path_source', side_effect=FileNotFoundError,
                ):
                    return storage.sync_paths(manifest_value, root / 'stage')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(
                        backup, 'save_state',
                        side_effect=lambda _c, _s, state:
                        saved.append(copy.deepcopy(state)),
                    ), mock.patch.object(
                        backup, 'stage_service', side_effect=fail_during_sync,
                    ), self.assertRaisesRegex(ValueError, 'missing source'):
                backup.backup_one({'host_id': 'host'}, value)

            self.assertEqual(saved[-1]['last_result'], 'failed')
            self.assertIn('ValueError: missing source', saved[-1]['last_error'])

    def test_failed_manifest_removes_partial_stage_without_starting_restic_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            staging_root = root / 'staging'
            stage = staging_root / value['service']
            saved = []

            def fail_after_partial_stage(_config, _manifest):
                stage.mkdir(parents=True)
                staging_root.chmod(0o700)
                stage.chmod(0o700)
                (stage / 'partial').write_text('incomplete', encoding='utf-8')
                raise RuntimeError('source staging failed')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(
                        backup, 'save_state',
                        side_effect=lambda _c, _s, state:
                        saved.append(copy.deepcopy(state)),
                    ), mock.patch.object(
                        backup, 'stage_service', side_effect=fail_after_partial_stage,
                    ), mock.patch.object(backup, 'run') as run_mock, \
                    self.assertRaisesRegex(RuntimeError, 'source staging failed'):
                backup.backup_one({
                    'host_id': 'host',
                    'staging_root': str(staging_root),
                }, value)

            self.assertFalse(stage.exists())
            self.assertEqual(saved[-1]['last_result'], 'failed')
            self.assertFalse(any(
                call.args[0][:2] == ['restic', 'backup']
                for call in run_mock.call_args_list
            ))

    def test_success_action_runs_after_restic_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            events = []

            def fake_run(command, **_kwargs):
                if command[:2] == ['restic', 'backup']:
                    events.append('restic')
                return mock.Mock(stdout='')

            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(backup, 'save_state'), \
                    mock.patch.object(backup, 'stage_service', return_value=root), \
                    mock.patch.object(backup, 'restic_env', return_value={}), \
                    mock.patch.object(backup, 'run', side_effect=fake_run), \
                    mock.patch.object(
                        backup, 'run_success_actions',
                        side_effect=lambda _m: events.append('on_success') or [],
                    ):
                self.assertTrue(backup.backup_one(
                    {'host_id': 'host'}, value, apply_retention=False,
                ))

            self.assertEqual(events, ['restic', 'on_success'])

    def test_staging_failure_passes_reason_to_failure_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            failure = RuntimeError('staging failed')
            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(backup, 'save_state'), \
                    mock.patch.object(
                        backup, 'stage_service', side_effect=failure,
                    ), mock.patch.object(
                        backup, 'run_failure_actions', return_value=[],
                    ) as on_failure, self.assertRaises(RuntimeError) as caught:
                backup.backup_one({'host_id': 'host'}, value)

            self.assertIs(caught.exception, failure)
            on_failure.assert_called_once_with(
                value, error=failure, phase='staging',
            )

    def test_success_action_failure_triggers_failure_action_but_state_is_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            failure = RuntimeError('notification failed')
            saved = []
            with mock.patch.object(backup, 'load_state', return_value={}), \
                    mock.patch.object(
                        backup, 'save_state', side_effect=lambda _c, _s, state:
                        saved.append(copy.deepcopy(state)),
                    ), mock.patch.object(
                        backup, 'stage_service', return_value=root,
                    ), mock.patch.object(
                        backup, 'restic_env', return_value={},
                    ), mock.patch.object(backup, 'run'), mock.patch.object(
                        backup, 'run_success_actions', side_effect=failure,
                    ), mock.patch.object(
                        backup, 'run_failure_actions', return_value=[],
                    ) as on_failure, self.assertRaises(RuntimeError) as caught:
                backup.backup_one(
                    {'host_id': 'host'}, value, apply_retention=False,
                )

            self.assertIs(caught.exception, failure)
            self.assertEqual(saved[-1]['last_result'], 'success')
            on_failure.assert_called_once_with(
                value, error=failure, phase='on_success',
            )

if __name__ == '__main__':
    unittest.main()
