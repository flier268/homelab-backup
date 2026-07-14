import copy
import datetime as dt
import importlib.machinery
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
backupctl = importlib.machinery.SourceFileLoader(
    'backupctl_under_test', str(ROOT / 'backupctl')
).load_module()


def manifest(tmp_path, **overrides):
    service_dir = tmp_path / 'demo'
    service_dir.mkdir(exist_ok=True)
    manifest_path = service_dir / 'backup.yaml'
    manifest_path.write_text('version: 1\nservice: demo\n', encoding='utf-8')
    value = {
        '_path': str(manifest_path),
        '_dir': str(service_dir),
        'version': 1,
        'service': 'demo',
        'schedule': {'cron': '0 0 * * *'},
        'retention': {'keep_last': 1},
        'consistency': {'mode': 'none'},
        'sources': {'paths': [], 'volumes': []},
    }
    value.update(overrides)
    return value


class CronTests(unittest.TestCase):
    @staticmethod
    def brute_previous(spec, now, search_minutes):
        candidate = now.replace(second=0, microsecond=0)
        for _ in range(search_minutes + 1):
            if backupctl.cron_matches(spec, candidate):
                return candidate
            candidate -= dt.timedelta(minutes=1)
        return None

    @staticmethod
    def brute_next(spec, now, search_minutes):
        candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(search_minutes):
            if backupctl.cron_matches(spec, candidate):
                return candidate
            candidate += dt.timedelta(minutes=1)
        return None

    def test_optimized_search_matches_minute_by_minute_reference(self):
        cases = (
            ('*/15 * * * *', dt.datetime(2026, 7, 13, 12, 7), 24 * 60),
            ('0 2,8,14,20 * * *', dt.datetime(2026, 7, 13, 12, 7), 24 * 60),
            ('0 0 * * mon', dt.datetime(2026, 7, 15, 12, 7), 8 * 24 * 60),
            ('0 0 29 2 *', dt.datetime(2024, 3, 1, 12, 7), 3 * 24 * 60),
        )
        for expression, now, window in cases:
            with self.subTest(expression=expression):
                spec = backupctl.parse_cron(expression)
                self.assertEqual(
                    backupctl.cron_previous(spec, now, window),
                    self.brute_previous(spec, now, window),
                )
                self.assertEqual(
                    backupctl.cron_next(spec, now, window),
                    self.brute_next(spec, now, window),
                )

    def test_sunday_ranges_accept_seven_and_names(self):
        self.assertEqual(
            backupctl.parse_cron('0 0 * * 5-7')['weekdays'],
            {0, 5, 6},
        )
        self.assertEqual(
            backupctl.parse_cron('0 0 * * fri-sun')['weekdays'],
            {0, 5, 6},
        )

    def test_step_from_single_value_continues_to_field_maximum(self):
        self.assertEqual(
            backupctl.parse_cron('5/10 * * * *')['minutes'],
            {5, 15, 25, 35, 45, 55},
        )

    def test_star_step_keeps_day_field_wildcard_semantics(self):
        spec = backupctl.parse_cron('0 0 */1 * mon')
        tuesday = dt.datetime(2026, 7, 14, 0, 0)
        self.assertFalse(backupctl.cron_matches(spec, tuesday))

    def test_impossible_calendar_schedule_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), schedule={'cron': '0 0 31 2 *'})
            with self.assertRaisesRegex(ValueError, 'never matches'):
                backupctl.validate_manifest(value)


class ManifestValidationTests(unittest.TestCase):
    def test_service_must_be_a_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                backupctl.validate_manifest(manifest(Path(tmp), service=123))

    def test_consistency_schema_is_validated(self):
        invalid = (
            [],
            {'mode': 'stop', 'services': 'db'},
            {'mode': 'hooks', 'before': 'echo freeze'},
            {'mode': 'hooks', 'after': [1]},
            {'mode': 'stop', 'timeout': True},
            {'mode': 'stop', 'timeout': 0},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for consistency in invalid:
                with self.subTest(consistency=consistency):
                    with self.assertRaises(ValueError):
                        backupctl.validate_manifest(manifest(root, consistency=consistency))

    def test_boolean_is_not_a_retention_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                backupctl.validate_manifest(manifest(Path(tmp), retention={'keep_last': True}))

    def test_sources_must_be_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                backupctl.validate_manifest(manifest(Path(tmp), sources=[]))

    def test_source_id_must_be_a_safe_single_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            for unsafe in ('/etc', '../etc', 'a/b', '.', '..'):
                with self.subTest(unsafe=unsafe):
                    value = manifest(Path(tmp), sources={
                        'paths': [{'id': unsafe, 'path': 'data'}],
                        'volumes': [],
                    })
                    with self.assertRaises(ValueError):
                        backupctl.validate_manifest(value)

    def test_path_and_volume_required_fields_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_sources = (
                {'paths': [{'id': 'data'}], 'volumes': []},
                {'paths': [], 'volumes': [{'id': 'data'}]},
                {'paths': [], 'volumes': [
                    {'id': 'data', 'name': 'one', 'compose_volume': 'two'}
                ]},
            )
            for sources in invalid_sources:
                with self.subTest(sources=sources):
                    with self.assertRaises(ValueError):
                        backupctl.validate_manifest(manifest(root, sources=sources))

    def test_unknown_manifest_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = (
                {'unexpected': True},
                {'compose': {'files': ['compose.yaml'], 'file': 'compose.yml'}},
                {'sources': {'path': [], 'paths': [], 'volumes': []}},
                {'sources': {
                    'paths': [{'id': 'data', 'path': 'data', 'excludes': ['secret']}],
                    'volumes': [],
                }},
                {'sources': {
                    'paths': [],
                    'volumes': [{'id': 'db', 'name': 'db', 'optional': True}],
                }},
            )
            for override in invalid:
                with self.subTest(override=override):
                    with self.assertRaisesRegex(ValueError, 'unsupported'):
                        backupctl.validate_manifest(manifest(root, **override))

    def test_hooks_are_only_allowed_in_hooks_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for mode in ('none', 'stop'):
                with self.subTest(mode=mode):
                    value = manifest(root, consistency={
                        'mode': mode, 'before': ['echo freeze'], 'after': [],
                    })
                    with self.assertRaisesRegex(ValueError, 'only valid with mode hooks'):
                        backupctl.validate_manifest(value)

    def test_duplicate_or_overlapping_path_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for second in ('data', 'data/child'):
                with self.subTest(second=second):
                    value = manifest(root, sources={
                        'paths': [
                            {'id': 'first', 'path': './data'},
                            {'id': 'second', 'path': second},
                        ],
                        'volumes': [],
                    })
                    with self.assertRaisesRegex(ValueError, 'overlapping path target'):
                        backupctl.validate_manifest(value)

    def test_manifest_directory_must_match_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            value['_dir'] = str(Path(tmp) / 'other-name')
            with self.assertRaisesRegex(ValueError, 'directory'):
                backupctl.validate_manifest(value)


class ConfigValidationTests(unittest.TestCase):
    def test_staging_root_must_not_overlap_services_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            config = {
                'host_id': 'host',
                'services_root': str(root / 'services'),
                'repository': 'rclone:backup',
                'password_file': str(root / 'password'),
                'rclone_config': str(root / 'rclone.conf'),
                'staging_root': str(root / 'services' / '.staging'),
                'restore_root': str(root / 'restore'),
                'cache_root': str(root / 'cache'),
                'volume_helper_image': 'helper',
                'state_root': str(root / 'state'),
                'lock_file': str(root / 'backupctl.lock'),
            }
            with mock.patch.object(backupctl, 'CFG', config_path), \
                    mock.patch.object(backupctl, 'load_yaml', return_value=config):
                with self.assertRaises(SystemExit):
                    backupctl.cfg()


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
        with mock.patch.object(backupctl, 'sync_paths'), \
                mock.patch.object(backupctl, 'sync_volumes'), \
                mock.patch.object(backupctl, 'hooks'):
            backupctl.stage_service(self.config, value)

        self.assertFalse(stale.exists())

    def test_staging_overlap_is_rejected_before_deletion(self):
        value = manifest(self.root)
        sentinel = Path(value['_dir']) / 'compose.yaml'
        sentinel.write_text('services: {}\n', encoding='utf-8')
        config = {'staging_root': str(self.root)}

        with mock.patch.object(backupctl, 'sync_paths') as sync_mock:
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                backupctl.stage_service(config, value)

        self.assertTrue(sentinel.exists())
        sync_mock.assert_not_called()

    def test_none_mode_does_not_run_hooks(self):
        value = manifest(self.root, consistency={'mode': 'none'})
        with mock.patch.object(backupctl, 'hooks') as hooks_mock, \
                mock.patch.object(backupctl, 'sync_paths'), \
                mock.patch.object(backupctl, 'sync_volumes'):
            backupctl.stage_service(self.config, value)

        hooks_mock.assert_not_called()

    def test_after_hook_runs_when_staging_fails(self):
        calls = []
        value = manifest(self.root, consistency={'mode': 'hooks'})

        def record_hook(_manifest, name):
            calls.append(name)

        with mock.patch.object(backupctl, 'hooks', side_effect=record_hook), \
                mock.patch.object(backupctl, 'sync_paths', side_effect=RuntimeError('sync failed')):
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                backupctl.stage_service(self.config, value)

        self.assertEqual(calls, ['before', 'after'])

    def test_after_hook_runs_when_before_hook_fails(self):
        calls = []
        value = manifest(self.root, consistency={'mode': 'hooks'})

        def record_hook(_manifest, name):
            calls.append(name)
            if name == 'before':
                raise RuntimeError('before failed')

        with mock.patch.object(backupctl, 'hooks', side_effect=record_hook):
            with self.assertRaisesRegex(RuntimeError, 'before failed'):
                backupctl.stage_service(self.config, value)

        self.assertEqual(calls, ['before', 'after'])

    def test_restart_failure_is_not_suppressed(self):
        value = manifest(self.root, consistency={'mode': 'stop', 'services': ['app']})

        def fake_run(cmd, **kwargs):
            if 'up' in cmd and kwargs.get('check', True):
                raise backupctl.CommandError(cmd, 1)
            return mock.Mock(stdout='')

        with mock.patch.object(backupctl, 'hooks'), \
                mock.patch.object(backupctl, 'sync_paths'), \
                mock.patch.object(backupctl, 'sync_volumes'), \
                mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                mock.patch.object(backupctl, 'run', side_effect=fake_run):
            with self.assertRaises(backupctl.CommandError):
                backupctl.stage_service(self.config, value)


class BackupStateTests(unittest.TestCase):
    def test_retention_failure_does_not_turn_snapshot_into_failed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            config = {'host_id': 'host'}
            saved = []

            def fake_run(cmd, **_kwargs):
                if cmd[:2] == ['restic', 'forget']:
                    raise backupctl.CommandError(cmd, 1, stderr='temporary failure')
                return mock.Mock(stdout='')

            with mock.patch.object(backupctl, 'load_state', return_value={}), \
                    mock.patch.object(backupctl, 'save_state', side_effect=lambda _c, _s, state: saved.append(copy.deepcopy(state))), \
                    mock.patch.object(backupctl, 'stage_service', return_value=root), \
                    mock.patch.object(backupctl, 'restic_env', return_value={}), \
                    mock.patch.object(backupctl, 'run', side_effect=fake_run):
                self.assertTrue(backupctl.backup_one(config, value))

            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertIn('temporary failure', saved[-1]['last_retention_error'])


class CommandTests(unittest.TestCase):
    def test_noninteractive_restore_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=True, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaisesRegex(SystemExit, '1'):
                backupctl.cmd_restore({}, args)

        restore_mock.assert_not_called()

    def test_snapshots_always_filters_by_host(self):
        args = mock.Mock(service=None)
        with mock.patch.object(backupctl, 'restic_env', return_value={}), \
                mock.patch.object(backupctl, 'run') as run_mock:
            backupctl.cmd_snapshots({'host_id': 'server-a'}, args)

        self.assertEqual(
            run_mock.call_args.args[0],
            ['restic', 'snapshots', '--host', 'server-a'],
        )

    def test_unlock_removes_only_stale_repository_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'lock_file': str(Path(tmp) / 'backupctl.lock')}
            environment = {'RESTIC_REPOSITORY': 'test-repository'}
            with mock.patch.object(
                backupctl, 'restic_env', return_value=environment,
            ), mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.cmd_unlock(config, mock.Mock())

        run_mock.assert_called_once_with(['restic', 'unlock'], env=environment)

    def test_unlock_refuses_while_another_backupctl_process_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = str(Path(tmp) / 'backupctl.lock')
            with backupctl.GlobalLock(lock_file), \
                    mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaises(SystemExit):
                    backupctl.cmd_unlock({'lock_file': lock_file}, mock.Mock())

        run_mock.assert_not_called()

    def test_apply_reports_restart_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'stop', 'services': ['app']})

            def fake_run(cmd, **kwargs):
                if 'up' in cmd and kwargs.get('check', True):
                    raise backupctl.CommandError(cmd, 1)
                return mock.Mock(stdout='')

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(backupctl, 'sync_volumes'), \
                    mock.patch.object(backupctl, 'run', side_effect=fake_run):
                with self.assertRaises(backupctl.CommandError):
                    backupctl.apply_one({}, value, root)

    def test_apply_failure_does_not_restart_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, consistency={'mode': 'stop', 'services': ['app']})

            with mock.patch.object(backupctl, 'compose_files_exist', return_value=True), \
                    mock.patch.object(backupctl, 'running_services', return_value=['app']), \
                    mock.patch.object(
                        backupctl, 'sync_volumes', side_effect=RuntimeError('restore failed'),
                    ), mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                    backupctl.apply_one({}, value, root, start_services=True)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertFalse(any('up' in command for command in commands))

    def test_status_exposes_retention_failure(self):
        value = {'service': 'demo', 'schedule': {'cron': '0 0 * * *'}}
        state = {'last_result': 'success', 'last_retention_error': 'forget failed'}
        output = io.StringIO()
        with mock.patch.object(backupctl, 'manifests', return_value=[value]), \
                mock.patch.object(backupctl, 'due_status', return_value=(False, 'next', None)), \
                mock.patch.object(backupctl, 'load_state', return_value=state), \
                redirect_stdout(output):
            backupctl.cmd_status({}, mock.Mock())

        self.assertEqual(
            json.loads(output.getvalue())['last_retention_error'],
            'forget failed',
        )


class RuntimeValidationTests(unittest.TestCase):
    def test_required_path_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'missing'}],
                'volumes': [],
            })
            with self.assertRaisesRegex(ValueError, 'missing required source'):
                backupctl.validate_runtime_sources({}, value, {})

    def test_declared_volume_is_inspected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), sources={
                'paths': [],
                'volumes': [{'id': 'db', 'name': 'demo-db'}],
            })
            with mock.patch.object(backupctl, 'run') as run_mock:
                backupctl.validate_runtime_sources({}, value, {})
            self.assertEqual(
                run_mock.call_args.args[0],
                ['docker', 'volume', 'inspect', 'demo-db'],
            )

    def test_missing_required_volume_aborts_before_docker_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{'id': 'db', 'name': 'missing-db'}],
            })

            with mock.patch.object(
                backupctl, 'run', side_effect=backupctl.CommandError(
                    ['docker', 'volume', 'inspect', 'missing-db'], 1,
                    stderr='Error: No such volume: missing-db',
                ),
            ) as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                    backupctl.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, stage,
                    )

            self.assertEqual(run_mock.call_count, 1)
            self.assertEqual(
                run_mock.call_args.args[0],
                ['docker', 'volume', 'inspect', 'missing-db'],
            )

    def test_missing_optional_volume_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{
                    'id': 'db', 'name': 'missing-db', 'required': False,
                }],
            })

            with mock.patch.object(
                backupctl, 'run', side_effect=backupctl.CommandError(
                    ['docker', 'volume', 'inspect', 'missing-db'], 1,
                    stderr='Error: No such volume: missing-db',
                ),
            ) as run_mock:
                backupctl.sync_volumes(
                    {'volume_helper_image': 'helper'}, value, stage,
                )

            self.assertEqual(run_mock.call_count, 1)

    def test_optional_volume_does_not_hide_docker_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{
                    'id': 'db', 'name': 'demo-db', 'required': False,
                }],
            })
            failure = backupctl.CommandError(
                ['docker', 'volume', 'inspect', 'demo-db'], 1,
                stderr='permission denied while trying to connect to the Docker daemon socket',
            )
            with mock.patch.object(backupctl, 'run', side_effect=failure):
                with self.assertRaises(backupctl.CommandError):
                    backupctl.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root / 'stage',
                    )

    def test_duplicate_resolved_volumes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), sources={
                'paths': [],
                'volumes': [
                    {'id': 'direct', 'name': 'project_db'},
                    {'id': 'logical', 'compose_volume': 'db'},
                ],
            })
            model = {'volumes': {'db': {'name': 'project_db'}}}
            with mock.patch.object(backupctl, 'run') as run_mock:
                with self.assertRaisesRegex(ValueError, 'duplicate Docker volume target'):
                    backupctl.validate_runtime_sources({}, value, model)
            run_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
