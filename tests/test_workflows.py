import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import backup, restore, restore_apply
from homelab_backup.fingerprint import snapshot_context_fingerprint
from tests.helpers import manifest


class WorkflowDependencyTests(unittest.TestCase):
    def rollback_dependencies(self, **overrides):
        values = {
            'run_command': lambda *_args, **_kwargs: None,
            'cleanup': lambda callback, _label: callback(),
            'volume_owned': lambda *_args: False,
            'open_parent': lambda *_args: None,
            'open_path': lambda *_args: None,
            'object_state': lambda *_args: None,
            'remove_entry': lambda *_args: None,
            'clear_leaf': lambda *_args: None,
        }
        values.update(overrides)
        return restore_apply.RollbackDependencies(**values)

    def test_backup_workflow_uses_injected_ports_across_commit_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            config = {
                'host_id': 'host',
                'repository': 'repo-one',
                'staging_root': str(Path(tmp) / 'stage-root'),
            }
            stage = Path(tmp) / 'stage'
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            events = []
            saved = []
            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {},
                save_state=lambda _c, _service, state: saved.append(dict(state)),
                stage=lambda _c, _m: events.append('stage') or stage,
                check_space=lambda *_args, **_kwargs: events.append('space'),
                run_command=lambda command, **_kwargs:
                events.append(tuple(command[:2])),
                restic_environment=lambda _c: {},
                success_actions=lambda _m: events.append('success-actions'),
                failure_actions=lambda *_args, **_kwargs:
                events.append('failure-actions'),
                cleanup=lambda callback, _label: callback(),
                remove_stage=lambda _path: events.append('remove-stage'),
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: 'sha256:' + '1' * 64,
                repository_id=lambda _c: 'a' * 64,
            )

            result = backup.BackupWorkflow(
                config, value, dependencies=dependencies,
            ).execute()

            self.assertTrue(result)
            self.assertEqual(
                events,
                [
                    'stage', 'space', ('restic', 'backup'),
                    'remove-stage', 'success-actions',
                ],
            )
            self.assertEqual(saved[0]['last_result'], 'running')
            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertEqual(
                saved[-1]['last_snapshot_fingerprint'],
                'sha256:' + '1' * 64,
            )

    def test_backup_cleanup_failure_is_postcommit_and_preserves_first_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            config = {
                'host_id': 'host',
                'repository': 'repo-one',
                'staging_root': str(Path(tmp) / 'stage-root'),
            }
            stage = Path(tmp) / 'stage'
            cleanup_failure = OSError('staging filesystem unavailable')
            action_failure = RuntimeError('notification failed')
            state_failure = OSError('state filesystem unavailable')
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            events = []
            saved = []
            stderr = io.StringIO()

            def failure_actions(_manifest, *, error, phase):
                events.append(('failure-actions', phase, error))

            def save_state(_config, _service, state):
                saved.append(dict(state))
                if len(saved) == 2:
                    raise state_failure

            def remove_stage(_path):
                raise cleanup_failure

            def success_actions(_manifest):
                raise action_failure

            def run_command(command, **_kwargs):
                events.append(tuple(command[:2]))

            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {},
                save_state=save_state,
                stage=lambda _c, _m: stage,
                check_space=lambda *_args, **_kwargs: None,
                run_command=run_command,
                restic_environment=lambda _c: {},
                success_actions=success_actions,
                failure_actions=failure_actions,
                cleanup=lambda callback, _label: callback(),
                remove_stage=remove_stage,
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: 'sha256:' + '1' * 64,
                repository_id=lambda _c: 'a' * 64,
            )

            with redirect_stderr(stderr), self.assertRaises(OSError) as caught:
                backup.BackupWorkflow(
                    config, value, dependencies=dependencies,
                ).execute()

            self.assertIs(caught.exception, cleanup_failure)
            self.assertEqual(saved[-1]['last_result'], 'success')
            self.assertEqual(
                events,
                [
                    ('restic', 'backup'),
                    ('failure-actions', 'cleanup', cleanup_failure),
                    ('failure-actions', 'on_success', action_failure),
                    ('failure-actions', 'state', state_failure),
                ],
            )
            notes = getattr(caught.exception, '__notes__', ())
            self.assertTrue(any(
                'RuntimeError: notification failed' in note for note in notes
            ))
            self.assertTrue(any(
                'OSError: state filesystem unavailable' in note for note in notes
            ))
            self.assertIn(
                'state update failed: '
                'OSError: state filesystem unavailable',
                stderr.getvalue(),
            )

    def test_unchanged_backup_skips_snapshot_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            fingerprint = 'sha256:' + '1' * 64
            config = {
                'host_id': 'host',
                'repository': 'repo-one',
                'staging_root': str(Path(tmp) / 'stage-root'),
            }
            repository_id = 'a' * 64
            commands = []
            stage = Path(tmp) / 'stage'
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {
                    'last_snapshot_fingerprint': fingerprint,
                    'last_snapshot_context':
                        snapshot_context_fingerprint(config, repository_id),
                },
                save_state=lambda *_args: None,
                stage=lambda _c, _m: stage,
                check_space=lambda *_args, **_kwargs: None,
                run_command=lambda command, **_kwargs:
                commands.append(command),
                restic_environment=lambda _c: {},
                success_actions=lambda _m: None,
                failure_actions=lambda *_args, **_kwargs: None,
                cleanup=lambda callback, _label: callback(),
                remove_stage=lambda _path: None,
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: fingerprint,
                repository_id=lambda _c: repository_id,
            )

            backup.BackupWorkflow(
                config,
                value,
                dependencies=dependencies,
            ).execute()

            self.assertFalse(any(
                command[:2] == ['restic', 'backup']
                for command in commands
            ))
            self.assertFalse(any(
                command[:2] == ['restic', 'forget']
                for command in commands
            ))

    def test_setting_can_create_snapshot_when_content_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(
                Path(tmp), create_snapshot_if_unchanged=True,
            )
            fingerprint = 'sha256:' + '1' * 64
            config = {
                'host_id': 'host',
                'repository': 'repo-one',
                'staging_root': str(Path(tmp) / 'stage-root'),
            }
            repository_id = 'a' * 64
            commands = []
            stage = Path(tmp) / 'stage'
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {
                    'last_snapshot_fingerprint': fingerprint,
                    'last_snapshot_context':
                        snapshot_context_fingerprint(config, repository_id),
                },
                save_state=lambda *_args: None,
                stage=lambda _c, _m: stage,
                check_space=lambda *_args, **_kwargs: None,
                run_command=lambda command, **_kwargs:
                commands.append(command),
                restic_environment=lambda _c: {},
                success_actions=lambda _m: None,
                failure_actions=lambda *_args, **_kwargs: None,
                cleanup=lambda callback, _label: callback(),
                remove_stage=lambda _path: None,
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: fingerprint,
                repository_id=lambda _c: repository_id,
            )

            backup.BackupWorkflow(
                config,
                value,
                dependencies=dependencies,
            ).execute()

            self.assertTrue(any(
                command[:2] == ['restic', 'backup']
                for command in commands
            ))

    def test_repository_replacement_at_same_address_does_not_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            fingerprint = 'sha256:' + '1' * 64
            config = {
                'host_id': 'host',
                'repository': 'repo-one',
                'staging_root': str(Path(tmp) / 'stage-root'),
            }
            commands = []
            stage = Path(tmp) / 'stage'
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {
                    'last_snapshot_fingerprint': fingerprint,
                    'last_snapshot_context':
                        snapshot_context_fingerprint(config, 'a' * 64),
                },
                save_state=lambda *_args: None,
                stage=lambda _c, _m: stage,
                check_space=lambda *_args, **_kwargs: None,
                run_command=lambda command, **_kwargs:
                commands.append(command),
                restic_environment=lambda _c: {},
                success_actions=lambda _m: None,
                failure_actions=lambda *_args, **_kwargs: None,
                cleanup=lambda callback, _label: callback(),
                remove_stage=lambda _path: None,
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: fingerprint,
                repository_id=lambda _c: 'b' * 64,
            )

            backup.BackupWorkflow(
                config, value, dependencies=dependencies,
            ).execute()

            self.assertTrue(any(
                command[:2] == ['restic', 'backup']
                for command in commands
            ))

    def test_fingerprint_failure_is_reported_as_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            failure = OSError('cannot read xattr')
            phases = []
            stage = Path(tmp) / 'stage'
            moments = iter((
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 1, 1, 0, 0, 5, tzinfo=dt.timezone.utc),
            ))
            dependencies = backup.BackupDependencies(
                now=lambda: next(moments),
                load_state=lambda _c, _service: {},
                save_state=lambda *_args: None,
                stage=lambda _c, _m: stage,
                check_space=lambda *_args, **_kwargs: None,
                run_command=lambda *_args, **_kwargs:
                self.fail('Restic must not run after fingerprint failure'),
                restic_environment=lambda _c: {},
                success_actions=lambda _m: None,
                failure_actions=lambda _m, *, error, phase:
                phases.append((error, phase)),
                cleanup=lambda callback, _label: callback(),
                remove_stage=lambda _path: None,
                print_command_failure=lambda *_args, **_kwargs: None,
                fingerprint=lambda _stage: (_ for _ in ()).throw(failure),
                repository_id=lambda _c: 'a' * 64,
            )

            with self.assertRaises(OSError) as caught:
                backup.BackupWorkflow(
                    {
                        'host_id': 'host',
                        'repository': 'repo-one',
                        'staging_root': str(Path(tmp) / 'stage-root'),
                    },
                    value,
                    dependencies=dependencies,
                ).execute()

            self.assertIs(caught.exception, failure)
            self.assertEqual(phases, [(failure, 'staging')])

    def test_repository_id_is_loaded_from_restic_config(self):
        config = {'repository': 'repo'}
        result = SimpleNamespace(stdout=json.dumps({'id': 'a' * 64}))
        with mock.patch.object(
            backup, 'restic_env', return_value={'RESTIC_REPOSITORY': 'repo'},
        ), mock.patch.object(
            backup, 'run', return_value=result,
        ) as run_mock:
            self.assertEqual(backup.restic_repository_id(config), 'a' * 64)

        run_mock.assert_called_once_with(
            ['restic', 'cat', 'config'],
            env={'RESTIC_REPOSITORY': 'repo'},
            capture=True,
        )

    def test_invalid_repository_config_id_is_rejected(self):
        with mock.patch.object(
            backup, 'restic_env', return_value={},
        ), mock.patch.object(
            backup, 'run',
            return_value=SimpleNamespace(stdout='{"id": "invalid"}'),
        ), self.assertRaisesRegex(RuntimeError, 'repository ID'):
            backup.restic_repository_id({})

    def test_restore_download_workflow_uses_deterministic_workspace(self):
        events = []
        root = Path('/restore')
        restored_manifest = {'service': 'demo'}
        dependencies = restore.RestoreDownloadDependencies(
            check_space=lambda *_args, **_kwargs: events.append('space'),
            ensure_private_directory=lambda path:
            events.append(('directory', str(path))) or Path(path),
            restore_id=lambda: '20260101-000000-000000001',
            run_command=lambda command, **_kwargs:
            events.append(tuple(command[:2])),
            restic_environment=lambda _c: {},
            prepare_manifest=lambda *_args, **_kwargs: restored_manifest,
        )

        value, workspace = restore.RestoreDownloadWorkflow(
            dependencies,
        ).restore(
            {'restore_root': str(root), 'host_id': 'host'},
            'demo', 'latest', 'keep',
        )

        self.assertIs(value, restored_manifest)
        self.assertEqual(
            workspace,
            root / 'demo' / '20260101-000000-000000001',
        )
        self.assertIn(('restic', 'restore'), events)

    def test_apply_workflow_rolls_back_and_preserves_primary_error(self):
        events = []
        plan = SimpleNamespace(
            running_services=(), mode='rebuild', manifest={},
        )
        failure = RuntimeError('restore failed')

        def restore_data(_c, _plan, _changes, _operation_id):
            events.append('restore-data')
            raise failure

        dependencies = restore_apply.RestoreApplyDependencies(
            prepare_plan=lambda *_args: plan,
            stop_services=lambda _plan: events.append('stop'),
            dynamic_preflight=lambda *_args: events.append('preflight'),
            restore_data=restore_data,
            publish_controls=lambda *_args: events.append('publish'),
            rollback=lambda _ledger: events.append('rollback'),
            restart_services=lambda *_args: events.append('restart'),
            cleanup=lambda callback, _label: callback(),
            compose=lambda *_args, **_kwargs: None,
            run_command=lambda *_args, **_kwargs: None,
            operation_id=lambda: 'operation',
        )

        with self.assertRaises(RuntimeError) as caught:
            restore_apply.RestoreApplyWorkflow(dependencies).apply(
                {}, {}, '/restore',
            )

        self.assertIs(caught.exception, failure)
        self.assertEqual(
            events, ['stop', 'preflight', 'publish', 'restore-data', 'rollback'],
        )

    def test_rollback_ledger_runs_in_reverse_and_continues_after_failure(self):
        events = []

        class Claim:
            def __init__(self, label, fails=False):
                self.label = label
                self.fails = fails

            def rollback(self, _dependencies):
                events.append(self.label)
                if self.fails:
                    raise RuntimeError('secondary cleanup failure')

        def cleanup(callback, _label):
            try:
                callback()
            except RuntimeError:
                events.append('suppressed')

        dependencies = restore_apply.RollbackDependencies(
            run_command=lambda *_args, **_kwargs: None,
            cleanup=cleanup,
            volume_owned=lambda *_args: False,
            open_parent=lambda *_args: None,
            open_path=lambda *_args: None,
            object_state=lambda *_args: None,
            remove_entry=lambda *_args: None,
            clear_leaf=lambda *_args: None,
        )
        ledger = restore_apply.RollbackLedger([
            Claim('first'), Claim('second', fails=True), Claim('third'),
        ])

        ledger.rollback(dependencies)

        self.assertEqual(
            events, ['third', 'second', 'suppressed', 'first'],
        )

    def test_volume_claim_only_removes_volume_owned_by_this_operation(self):
        commands = []
        dependencies = self.rollback_dependencies(
            run_command=lambda command: commands.append(command),
            volume_owned=lambda name, operation:
            (name, operation) == ('demo-data', 'operation'),
        )
        claim = restore_apply.VolumeClaim(
            'demo-data', 'operation', owned=False,
        )

        claim.rollback(dependencies)
        claim.owned = True
        claim.rollback(dependencies)

        self.assertEqual(
            commands, [['docker', 'volume', 'rm', 'demo-data']],
        )

    def test_data_path_claim_requires_ownership_identity_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'payload'
            target.write_text('restored', encoding='utf-8')
            metadata = target.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            removals = []
            current_state = ['published']
            dependencies = self.rollback_dependencies(
                open_parent=lambda path:
                (os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY), path.name),
                open_path=lambda path:
                (os.open(path, os.O_RDONLY), path.stat()),
                object_state=lambda _descriptor: current_state[0],
                remove_entry=lambda _parent, name: removals.append(name),
            )
            claim = restore_apply.DataPathClaim(
                target, identity=identity, state='published',
            )

            claim.rollback(dependencies)
            claim.owned = True
            claim.identity = (identity[0], identity[1] + 1)
            claim.rollback(dependencies)
            claim.identity = identity
            current_state[0] = 'modified'
            claim.rollback(dependencies)
            current_state[0] = 'published'
            claim.rollback(dependencies)

            self.assertEqual(removals, ['payload'])

            target.unlink()
            claim.rollback(dependencies)
            self.assertEqual(removals, ['payload'])

    def test_data_ancestor_claim_removes_only_same_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'created'
            target.mkdir()
            metadata = target.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            dependencies = self.rollback_dependencies(
                open_parent=lambda path:
                (os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY), path.name),
            )

            changed = restore_apply.DataAncestorClaim(
                target, (identity[0], identity[1] + 1),
            )
            changed.rollback(dependencies)
            self.assertTrue(target.exists())

            child = target / 'concurrent'
            child.touch()
            claim = restore_apply.DataAncestorClaim(target, identity)
            claim.rollback(dependencies)
            self.assertTrue(target.exists())

            child.unlink()
            claim.rollback(dependencies)
            self.assertFalse(target.exists())
            claim.rollback(dependencies)

    def test_control_path_claim_requires_ownership_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'backup.yaml'
            target.touch()
            identity = restore_apply._path_identity(target)
            removals = []
            dependencies = self.rollback_dependencies(
                clear_leaf=lambda path: removals.append(path),
            )
            claim = restore_apply.ControlPathClaim(target)

            claim.rollback(dependencies)
            claim.owned = True
            claim.identity = (identity[0], identity[1] + 1)
            claim.rollback(dependencies)
            claim.identity = identity
            claim.rollback(dependencies)
            self.assertEqual(removals, [target])

            target.unlink()
            claim.rollback(dependencies)
            self.assertEqual(removals, [target])


if __name__ == '__main__':
    unittest.main()
