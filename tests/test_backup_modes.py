import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from homelab_backup import backup, capture, staging
from tests.helpers import manifest


class CaptureModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            'staging_root': str(self.root / 'staging'),
            'state_root': str(self.root / 'state'),
            'trusted_data_roots': [str(self.root)],
            'volume_helper_image': 'helper',
        }
        self.saved_inventory = None

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _ensure(path, replace=False):
        path = Path(path)
        if replace and path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_json(self, path, value):
        Path(path).write_text(json.dumps(value), encoding='utf-8')
        if Path(path).name == 'inventory.json':
            self.saved_inventory = value

    def common_patches(self):
        return (
            mock.patch.object(staging, 'cleanup_snapshot_state'),
            mock.patch.object(staging, 'validate_trusted_roots'),
            mock.patch.object(staging, 'ensure_private_directory', side_effect=self._ensure),
            mock.patch.object(staging, 'validate_docker_environment'),
            mock.patch.object(staging, 'validate_docker_bind_probe'),
            mock.patch.object(staging, 'compose_model', return_value={
                'name': 'demo', 'services': {'app': {}}, 'volumes': {},
            }),
            mock.patch.object(staging, 'validate_runtime_sources'),
            mock.patch.object(staging, 'resolved_volume_sources', return_value=[]),
            mock.patch.object(staging, 'compose_identity', return_value={
                'project_name': 'demo', 'services': ['app'],
                'compose_files': ['compose.yaml'], 'volumes': [],
            }),
            mock.patch.object(capture, 'validate_path_payloads'),
            mock.patch.object(capture, 'estimate_backup_size', return_value=0),
            mock.patch.object(capture, '_check_backup_space'),
            mock.patch.object(staging, 'atomic_copy_file', side_effect=shutil.copy2),
            mock.patch.object(staging, 'atomic_write_json', side_effect=self._write_json),
        )

    def test_live_writer_is_best_effort_and_optional_action_downgrades_inventory(self):
        value = manifest(self.root, consistency={'mode': 'live'}, sources={
            'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
        })
        entry = {
            'id': 'data', 'path': 'data', 'type': 'directory', 'present': True,
            'capture_method': 'best-effort', 'writers': ['container'],
        }
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[{
                    'phase': 'before', 'name': 'checkpoint', 'result': 'failed',
                }],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'docker_writer_maps',
                return_value=({'data': ('container',)}, {}),
            ))
            sync_paths = stack.enter_context(mock.patch.object(
                capture, 'sync_paths', return_value=[entry],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            backup.stage_service(self.config, value)

        self.assertEqual(
            sync_paths.call_args.kwargs['capture_methods']['data'],
            'best-effort',
        )
        self.assertEqual(
            self.saved_inventory['consistency']['guarantee'],
            'best-effort',
        )
        self.assertEqual(
            self.saved_inventory['consistency']['writers'], ['container'],
        )

    def test_snapshot_override_is_staged_and_cleanup_runs_in_finally(self):
        value = manifest(self.root, consistency={'mode': 'snapshot'}, sources={
            'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
        })
        physical = self.root / '.snapshots' / 'data'
        transaction = mock.Mock()
        events = []
        transaction.create.return_value = {'data': physical}
        transaction.cleanup.side_effect = lambda: events.append('cleanup')
        entry = {
            'id': 'data', 'path': 'data', 'type': 'directory', 'present': True,
            'capture_method': 'btrfs-snapshot',
        }
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(staging, 'cleanup_snapshot_state'))
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', side_effect=lambda _m: (
                    events.append('finally') or []
                ),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'SnapshotTransaction', return_value=transaction,
            ))
            stack.enter_context(mock.patch.object(
                capture, 'docker_writer_maps', return_value=({}, {}),
            ))
            sync_paths = stack.enter_context(mock.patch.object(
                capture, 'sync_paths', return_value=[entry],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            backup.stage_service(self.config, value)

        self.assertEqual(
            sync_paths.call_args.kwargs['source_overrides'], {'data': physical},
        )
        self.assertEqual(
            sync_paths.call_args.kwargs['capture_methods']['data'],
            'btrfs-snapshot',
        )
        transaction.cleanup.assert_called_once_with()
        self.assertEqual(events, ['cleanup', 'finally'])
        self.assertEqual(
            self.saved_inventory['consistency']['guarantee'],
            'btrfs-snapshot',
        )

    def test_snapshot_cleanup_runs_when_staging_fails(self):
        value = manifest(self.root, consistency={'mode': 'snapshot'})
        transaction = mock.Mock()
        transaction.create.return_value = {}
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(staging, 'cleanup_snapshot_state'))
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'SnapshotTransaction', return_value=transaction,
            ))
            stack.enter_context(mock.patch.object(
                capture, 'docker_writer_maps', return_value=({}, {}),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_paths', side_effect=RuntimeError('sync failed'),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                backup.stage_service(self.config, value)
        transaction.cleanup.assert_called_once_with()

    def test_stale_snapshot_cleanup_runs_before_actions_in_non_snapshot_mode(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        events = []
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'cleanup_snapshot_state',
                side_effect=lambda _c, _service: events.append('cleanup'),
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions',
                side_effect=lambda _m: events.append('before') or [],
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'validate_no_docker_writers',
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_paths', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            backup.stage_service(self.config, value)
        self.assertEqual(events[:2], ['cleanup', 'before'])

    def test_each_consistency_mode_dispatches_to_exactly_one_handler(self):
        expected = {'hooks', 'stop', 'external', 'live', 'snapshot'}
        self.assertEqual(set(capture._MODE_HANDLERS), expected)
        for mode in sorted(expected):
            result = capture._CaptureResult([], [])
            handler = mock.Mock(return_value=result)
            context = capture._StageContext(
                config={}, manifest={}, stage=self.root, mode=mode,
                resolved_volumes=[], identity={}, allow_low_space=False,
            )
            with self.subTest(mode=mode), mock.patch.dict(
                    capture._MODE_HANDLERS, {mode: handler}):
                self.assertIs(capture._capture_stage(context), result)
            handler.assert_called_once_with(context)

    def test_capture_failure_runs_finally_exactly_once(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        failure = RuntimeError('capture failed')
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            finalizer = stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, '_capture_stage', side_effect=failure,
            ))
            with self.assertRaises(RuntimeError) as caught:
                backup.stage_service(self.config, value)
        self.assertIs(caught.exception, failure)
        finalizer.assert_called_once_with(value)

    def test_missing_mode_handler_still_runs_finally_once(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            finalizer = stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.dict(
                capture._MODE_HANDLERS, {}, clear=True,
            ))
            with self.assertRaisesRegex(
                    RuntimeError, 'unsupported consistency mode'):
                backup.stage_service(self.config, value)
        finalizer.assert_called_once_with(value)

    def test_mode_cleanup_precedes_finally_for_hooks_and_stop(self):
        for mode in ('hooks', 'stop'):
            value = manifest(self.root, consistency={'mode': mode})
            events = []
            patches = self.common_patches()
            with self.subTest(mode=mode), ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                stack.enter_context(mock.patch.object(
                    staging, 'run_before_actions', return_value=[],
                ))
                stack.enter_context(mock.patch.object(
                    staging, 'run_finally_actions',
                    side_effect=lambda _m: events.append('finally') or [],
                ))
                stack.enter_context(mock.patch.object(
                    capture, 'sync_paths', return_value=[],
                ))
                stack.enter_context(mock.patch.object(
                    capture, 'sync_volumes', return_value=[],
                ))
                if mode == 'hooks':
                    stack.enter_context(mock.patch.object(
                        capture, 'hooks',
                        side_effect=lambda _m, phase: (
                            events.append('cleanup') if phase == 'after'
                            else None
                        ),
                    ))
                else:
                    stack.enter_context(mock.patch.object(
                        capture, 'running_services', return_value=['app'],
                    ))
                    stack.enter_context(mock.patch.object(
                        capture, 'compose_run',
                        side_effect=lambda _m, command, **_kwargs: (
                            events.append('cleanup')
                            if command[0] == 'start' else None
                        ),
                    ))
                backup.stage_service(self.config, value)
            self.assertEqual(events, ['cleanup', 'finally'])

    def test_cleanup_and_finally_failures_preserve_capture_error(self):
        value = manifest(self.root, consistency={'mode': 'snapshot'})
        failure = RuntimeError('capture failed')
        transaction = mock.Mock()
        transaction.create.return_value = {}
        transaction.cleanup.side_effect = RuntimeError('cleanup failed')
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            finalizer = stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions',
                side_effect=RuntimeError('finally failed'),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'SnapshotTransaction', return_value=transaction,
            ))
            stack.enter_context(mock.patch.object(
                capture, 'docker_writer_maps', return_value=({}, {}),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_paths', side_effect=failure,
            ))
            with self.assertRaises(RuntimeError) as caught:
                backup.stage_service(self.config, value)
        self.assertIs(caught.exception, failure)
        transaction.cleanup.assert_called_once_with()
        finalizer.assert_called_once_with(value)

    def test_required_finally_failure_aborts_before_inventory_is_written(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', side_effect=RuntimeError('resume failed'),
            ))
            stack.enter_context(mock.patch.object(
                capture, 'validate_no_docker_writers',
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_paths', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            with self.assertRaisesRegex(RuntimeError, 'resume failed'):
                backup.stage_service(self.config, value)
        self.assertIsNone(self.saved_inventory)

    def test_finally_failure_does_not_replace_before_failure(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        before_failure = RuntimeError('quiesce failed')
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', side_effect=before_failure,
            ))
            finalizer = stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', side_effect=RuntimeError('resume failed'),
            ))
            with self.assertRaises(RuntimeError) as caught:
                backup.stage_service(self.config, value)
        self.assertIs(caught.exception, before_failure)
        finalizer.assert_called_once_with(value)

    def test_finally_runs_when_docker_preflight_fails(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        finalizer = mock.Mock(return_value=[])
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', finalizer,
            ))
            stack.enter_context(mock.patch.object(
                staging, 'compose_model', side_effect=RuntimeError('bad compose'),
            ))
            with self.assertRaisesRegex(RuntimeError, 'bad compose'):
                backup.stage_service(self.config, value)
        finalizer.assert_called_once_with(value)

    def test_optional_finally_failure_downgrades_inventory(self):
        value = manifest(self.root, consistency={'mode': 'external'})
        patches = self.common_patches()
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                staging, 'run_before_actions', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                staging, 'run_finally_actions', return_value=[{
                    'phase': 'finally', 'name': 'resume', 'result': 'failed',
                }],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'validate_no_docker_writers',
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_paths', return_value=[],
            ))
            stack.enter_context(mock.patch.object(
                capture, 'sync_volumes', return_value=[],
            ))
            backup.stage_service(self.config, value)
        self.assertEqual(
            self.saved_inventory['consistency']['guarantee'], 'best-effort',
        )
        self.assertEqual(
            self.saved_inventory['consistency']['optional_action_failures'],
            [{'phase': 'finally', 'name': 'resume', 'result': 'failed'}],
        )


if __name__ == '__main__':
    unittest.main()
