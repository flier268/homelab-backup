import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from homelab_backup import backup, restore, restore_apply
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
            )

            result = backup.BackupWorkflow(
                config, value, dependencies=dependencies,
            ).execute()

            self.assertTrue(result)
            self.assertEqual(
                events,
                [
                    'stage', 'space', ('restic', 'backup'),
                    'success-actions', ('restic', 'forget'),
                ],
            )
            self.assertEqual(saved[0]['last_result'], 'running')
            self.assertEqual(saved[-1]['last_result'], 'success')

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
            events, ['stop', 'preflight', 'restore-data', 'rollback'],
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
                open_parent=lambda path, _roots:
                (os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY), path.name),
                open_path=lambda path, _roots:
                (os.open(path, os.O_RDONLY), path.stat()),
                object_state=lambda _descriptor: current_state[0],
                remove_entry=lambda _parent, name: removals.append(name),
            )
            claim = restore_apply.DataPathClaim(
                target, (tmp,), identity=identity, state='published',
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
                open_parent=lambda path, _roots:
                (os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY), path.name),
            )

            changed = restore_apply.DataAncestorClaim(
                target, (identity[0], identity[1] + 1), (tmp,),
            )
            changed.rollback(dependencies)
            self.assertTrue(target.exists())

            child = target / 'concurrent'
            child.touch()
            claim = restore_apply.DataAncestorClaim(target, identity, (tmp,))
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
