import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homelab_backup import (
    common, restore_apply, restore_plan, storage,
)
from tests.helpers import manifest, path_inventory, write_restore_inventory


class RestoreSourceTests(unittest.TestCase):
    def test_existing_directory_target_rejects_nested_mount_before_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            target = base / 'live'
            target.mkdir()
            source = {'id': 'saved', 'path': str(target)}

            with mock.patch.object(
                restore_apply, 'validate_data_mount_boundary',
                side_effect=ValueError(
                    f'nested mount is not supported: {target / "cache"}'
                ),
            ), mock.patch.object(
                restore_apply, 'run',
            ) as run_mock, self.assertRaisesRegex(ValueError, 'nested mount'):
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={},
                )

            run_mock.assert_not_called()

    def test_existing_directory_restore_detects_mount_topology_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            target = base / 'live'
            target.mkdir()
            source = {'id': 'saved', 'path': str(target)}
            initial = ((1, 0, '8:1', '/', '/', 'ext4', '/dev/test'), ())
            changed = ((2, 0, '8:1', '/', '/', 'ext4', '/dev/test'), ())

            with mock.patch.object(
                restore_apply, 'validate_data_mount_boundary',
                side_effect=[initial, changed],
            ), mock.patch.object(
                restore_apply, 'run',
            ) as run_mock, self.assertRaisesRegex(
                RuntimeError, 'mount topology changed during restore',
            ):
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={},
                )

            self.assertIn('--one-file-system', run_mock.call_args.args[0])

    def test_existing_nested_target_may_be_inside_container_owned_data_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            (staged / 'world.sav').write_text('snapshot', encoding='utf-8')
            data_root = base / 'palworld'
            target = data_root / 'Pal' / 'Saved'
            target.mkdir(parents=True)
            (target / 'world.sav').write_text('old', encoding='utf-8')
            data_root.chmod(0o777)
            (data_root / 'Pal').chmod(0o777)
            source = {'id': 'saved', 'path': str(target)}
            try:
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={},
                )
            finally:
                (data_root / 'Pal').chmod(0o755)
                data_root.chmod(0o755)

            self.assertEqual(
                (target / 'world.sav').read_text(encoding='utf-8'), 'snapshot',
            )

    def test_restore_keeps_writing_to_pinned_target_after_parent_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            (staged / 'world.sav').write_text('snapshot', encoding='utf-8')
            parent = base / 'palworld' / 'Pal'
            target = parent / 'Saved'
            target.mkdir(parents=True)
            victim = base / 'victim'
            (victim / 'Saved').mkdir(parents=True)
            (victim / 'Saved' / 'world.sav').write_text('victim', encoding='utf-8')
            source = {'id': 'saved', 'path': str(target)}
            real_run = common.run
            swapped = False

            def swap_then_run(command, **kwargs):
                nonlocal swapped
                if not swapped:
                    moved = parent.with_name('Pal-moved')
                    parent.rename(moved)
                    parent.symlink_to(victim, target_is_directory=True)
                    swapped = True
                return real_run(command, **kwargs)

            with mock.patch.object(
                restore_apply, 'run', side_effect=swap_then_run,
            ):
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={},
                )

            self.assertEqual(
                (base / 'palworld' / 'Pal-moved' / 'Saved' / 'world.sav').read_text(
                    encoding='utf-8',
                ),
                'snapshot',
            )
            self.assertEqual(
                (victim / 'Saved' / 'world.sav').read_text(encoding='utf-8'),
                'victim',
            )

    def test_rebuild_recreates_missing_data_ancestors_with_snapshot_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            (staged / 'world.sav').write_text('snapshot', encoding='utf-8')
            target = base / 'palworld' / 'Pal' / 'Saved'
            source = {'id': 'saved', 'path': str(target)}
            ids = {'uid': os.geteuid(), 'gid': os.getegid()}
            inventory = {'paths': [{
                'id': 'saved', 'path': str(target),
                'type': 'directory', 'present': True,
                'ancestors': [
                    {'path': 'palworld', 'mode': 0o750, **ids},
                    {'path': 'palworld/Pal', 'mode': 0o770, **ids},
                ],
            }]}

            restore_apply.restore_path_source(
                {'_dir': str(base)}, restored, source, inventory,
                c={}, rebuild=True,
            )

            self.assertEqual(
                stat.S_IMODE((base / 'palworld').stat().st_mode), 0o750,
            )
            self.assertEqual(
                stat.S_IMODE((base / 'palworld' / 'Pal').stat().st_mode), 0o770,
            )
            self.assertEqual(target.joinpath('world.sav').read_text(), 'snapshot')

    def test_rebuild_old_snapshot_refuses_to_guess_missing_ancestor_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            target = base / 'palworld' / 'Pal' / 'Saved'
            source = {'id': 'saved', 'path': str(target)}

            with self.assertRaisesRegex(RuntimeError, 'lacks metadata'):
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={}, rebuild=True,
                )

    def test_rebuild_directory_is_not_published_until_rsync_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'saved'
            staged.mkdir(parents=True)
            (staged / 'world.sav').write_text('snapshot', encoding='utf-8')
            target = base / 'palworld' / 'Saved'
            target.mkdir(parents=True)
            source = {'id': 'saved', 'path': str(target)}

            def write_partial_then_fail(command, *, pass_fds=(), **_kwargs):
                destination_fd = pass_fds[0]
                Path(f'/proc/self/fd/{destination_fd}/partial').write_text(
                    'partial', encoding='utf-8',
                )
                raise RuntimeError('rsync failed')

            with mock.patch.object(
                restore_apply, 'run', side_effect=write_partial_then_fail,
            ), self.assertRaisesRegex(RuntimeError, 'rsync failed'):
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, restored, source,
                    {'paths': [{
                        'id': 'saved', 'path': str(target),
                        'type': 'directory', 'present': True,
                    }]},
                    c={}, rebuild=True,
                )

            self.assertEqual(list(target.iterdir()), [])

    def test_missing_required_path_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {'id': 'data', 'path': str(Path(tmp) / 'target')}
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )

    def test_missing_optional_present_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'), 'required': False,
            }
            with self.assertRaisesRegex(RuntimeError, 'artifact is missing'):
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )

    def test_inventory_marks_absent_optional_path_without_guessing_its_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'),
                'required': False,
            }
            inventory = {'paths': [{
                'id': 'data', 'path': source['path'], 'type': 'file',
                'present': False,
            }]}

            with mock.patch.object(restore_apply, 'run') as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, inventory,
                )

            run_mock.assert_not_called()

    def test_missing_volume_source_never_runs_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            root.mkdir()
            value = {'sources': {'volumes': [{'id': 'db', 'name': 'demo-db'}]}}
            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'missing'):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root, restore=True,
                    )
            run_mock.assert_not_called()

    def test_volume_restore_rejects_staged_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            referent = Path(tmp) / 'host-data'
            referent.mkdir()
            staged = root / 'volumes' / 'db'
            staged.parent.mkdir(parents=True)
            staged.symlink_to(referent, target_is_directory=True)
            value = {'sources': {'volumes': [{'id': 'db', 'name': 'demo-db'}]}}

            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'real directory'):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root,
                        restore=True,
                    )

            run_mock.assert_not_called()

    def test_hardlinks_are_rebuilt_only_inside_destination_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            restored = base / 'restore'
            staged = restored / 'paths' / 'data'
            staged.mkdir(parents=True)
            first = staged / 'first'
            first.write_text('snapshot', encoding='utf-8')
            (staged / 'second').hardlink_to(first)

            outside = base / 'sentinel'
            outside.write_text('outside', encoding='utf-8')
            target = base / 'live'
            target.mkdir()
            (target / 'first').hardlink_to(outside)
            source = {'id': 'data', 'path': str(target)}
            inventory = path_inventory(source)['paths']

            restore_apply.restore_path_source(
                {'_dir': str(base)}, restored, source,
                {'version': 1, 'paths': inventory},
            )

            self.assertEqual(outside.read_text(encoding='utf-8'), 'outside')
            self.assertEqual((target / 'first').stat().st_ino, (target / 'second').stat().st_ino)
            self.assertNotEqual(outside.stat().st_ino, (target / 'first').stat().st_ino)

    def test_path_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            source = {
                'id': 'data', 'path': str(Path(tmp) / 'target'),
                'exclude': ['logs/**'],
            }
            target = Path(tmp) / 'target'
            target.mkdir()
            with mock.patch.object(restore_apply, 'run') as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, path_inventory(source),
                )
            self.assertIn('--exclude', run_mock.call_args.args[0])
            self.assertIn('logs/**', run_mock.call_args.args[0])

    def test_filtered_path_restore_updates_managed_content_and_preserves_other_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'restore'
            staged = root / 'paths' / 'data'
            (staged / 'world').mkdir(parents=True)
            (staged / 'world' / 'current.dat').write_text(
                'snapshot', encoding='utf-8',
            )
            target = base / 'target'
            (target / 'world' / 'cache').mkdir(parents=True)
            (target / 'junk').mkdir()
            (target / 'junk-empty').mkdir()
            (target / 'world' / 'cache' / 'empty-dir').mkdir()
            (target / 'world' / 'stale.dat').write_text('stale', encoding='utf-8')
            (target / 'world' / 'cache' / 'keep.bin').write_text(
                'cache', encoding='utf-8',
            )
            (target / 'junk' / 'keep.txt').write_text('local', encoding='utf-8')
            source = {
                'id': 'data', 'path': str(target),
                'include': ['world/**'],
                'exclude': ['world/cache/**'],
            }

            restore_apply.restore_path_source(
                {'_dir': str(base)}, root, source, path_inventory(source),
            )

            self.assertEqual(
                (target / 'world' / 'current.dat').read_text(encoding='utf-8'),
                'snapshot',
            )
            self.assertFalse((target / 'world' / 'stale.dat').exists())
            self.assertEqual(
                (target / 'world' / 'cache' / 'keep.bin').read_text(encoding='utf-8'),
                'cache',
            )
            self.assertEqual(
                (target / 'junk' / 'keep.txt').read_text(encoding='utf-8'),
                'local',
            )
            self.assertTrue((target / 'junk-empty').is_dir())
            self.assertTrue((target / 'world' / 'cache' / 'empty-dir').is_dir())

    def test_rebuild_does_not_reapply_path_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'selected.txt').write_text('snapshot', encoding='utf-8')
            target = base / 'target'
            source = {
                'id': 'data', 'path': str(target),
                'include': ['selected.txt'], 'exclude': ['cache/**'],
            }
            inventory = path_inventory(source)
            inventory['paths'][0]['ancestors'] = []

            with mock.patch.object(
                restore_apply, 'run', wraps=common.run,
            ) as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': str(base)}, root, source, inventory,
                    c={}, rebuild=True,
                )

            command = run_mock.call_args.args[0]
            self.assertNotIn('--include', command)
            self.assertNotIn('--exclude', command)
            self.assertEqual(
                (target / 'selected.txt').read_text(encoding='utf-8'),
                'snapshot',
            )

    def test_volume_restore_preserves_excluded_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            (root / 'volumes' / 'db').mkdir(parents=True)
            value = {'sources': {'volumes': [{
                'id': 'db', 'name': 'demo-db', 'exclude': ['cache/**'],
            }]}}
            with mock.patch.object(storage, 'run') as run_mock:
                storage.sync_volumes(
                    {'volume_helper_image': 'helper'}, value, root, restore=True,
                )
            command = run_mock.call_args.args[0]
            self.assertIn('--mount', command)
            self.assertIn('type=volume,src=demo-db,dst=/dst', command)
            self.assertIn('--exclude', command)
            self.assertIn('cache/**', command)

    def test_file_source_uses_snapshot_inventory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'env'
            staged.mkdir(parents=True)
            old_file = staged / 'old.env'
            old_file.write_text('secret', encoding='utf-8')
            target = Path(tmp) / 'new.env'
            source = {'id': 'env', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'env', 'path': 'old.env', 'type': 'file',
            }]}
            with mock.patch.object(
                restore_apply, 'run', wraps=common.run,
            ) as run_mock:
                restore_apply.restore_path_source(
                    {'_dir': tmp}, root, source, inventory,
                )
            self.assertEqual(run_mock.call_args.args[0][-2], str(old_file))

    def test_file_snapshot_replaces_existing_directory_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.mkdir()
            (target / 'stale.txt').write_text('stale', encoding='utf-8')
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'payload.txt', 'type': 'file',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding='utf-8'), 'snapshot')

    def test_directory_snapshot_replaces_existing_file_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.write_text('stale', encoding='utf-8')
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'data', 'type': 'directory',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_dir())
            self.assertEqual(
                (target / 'payload.txt').read_text(encoding='utf-8'),
                'snapshot',
            )

    def test_type_change_does_not_follow_live_target_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            (staged / 'payload.txt').write_text('snapshot', encoding='utf-8')
            victim = Path(tmp) / 'victim'
            victim.mkdir()
            sentinel = victim / 'keep-me'
            sentinel.write_text('important', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.symlink_to(victim, target_is_directory=True)
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'payload.txt', 'type': 'file',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding='utf-8'), 'snapshot')
            self.assertTrue(sentinel.exists())

    def test_symlink_snapshot_recreates_link_without_following_referent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'restore'
            staged = root / 'paths' / 'data'
            staged.mkdir(parents=True)
            archived = staged / 'old-link'
            archived.symlink_to('../missing-target')
            victim = Path(tmp) / 'victim'
            victim.mkdir()
            sentinel = victim / 'keep-me'
            sentinel.write_text('important', encoding='utf-8')
            target = Path(tmp) / 'live-target'
            target.symlink_to(victim, target_is_directory=True)
            source = {'id': 'data', 'path': str(target)}
            inventory = {'paths': [{
                'id': 'data', 'path': 'old-link', 'type': 'symlink',
            }]}

            restore_apply.restore_path_source(
                {'_dir': tmp}, root, source, inventory,
            )

            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), Path('../missing-target'))
            self.assertTrue(sentinel.exists())

class RestoreApplyPreflightTests(unittest.TestCase):

    def test_apply_rejects_directory_inventory_backed_by_staged_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / 'demo' / 'data'
            target.mkdir(parents=True)
            restore_root = base / 'restore'
            staged_parent = restore_root / 'paths'
            staged_parent.mkdir(parents=True)
            (staged_parent / 'data').symlink_to(target, target_is_directory=True)
            value = {
                'service': 'demo', '_dir': str(base / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'compose': {'files': ['compose.yaml']},
                'consistency': {'mode': 'external'},
                'sources': {
                    'paths': [{'id': 'data', 'path': str(target)}],
                    'volumes': [],
                },
            }
            write_restore_inventory(restore_root, paths=[{
                'id': 'data', 'path': str(target), 'type': 'directory',
            }])
            with mock.patch.object(restore_plan, 'running_services') as running_mock, \
                    mock.patch.object(restore_apply, 'restore_path_source') as path_mock:
                with self.assertRaisesRegex(RuntimeError, 'directory artifact is missing'):
                    restore_apply.apply_one({}, value, restore_root)
            running_mock.assert_not_called()
            path_mock.assert_not_called()

    def test_apply_rejects_inventory_from_another_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            meta = restore_root / '_meta'
            meta.mkdir(parents=True)
            (meta / 'inventory.json').write_text(json.dumps({
                'version': 1,
                'service': 'other-service',
                'paths': [],
                'volumes': [],
                'compose': {},
            }), encoding='utf-8')
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'external'},
            }

            with mock.patch.object(restore_plan, 'running_services') as running_mock:
                with self.assertRaisesRegex(RuntimeError, 'other-service'):
                    restore_apply.apply_one({}, value, restore_root)

            running_mock.assert_not_called()

    def test_apply_rejects_missing_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore_root = root / 'restore'
            restore_root.mkdir()
            value = {
                'service': 'demo', '_dir': str(root / 'demo'),
                'version': 1, 'schedule': {'cron': '0 0 * * *'},
                'retention': {'keep_last': 1},
                'sources': {'paths': [], 'volumes': []},
                'consistency': {'mode': 'external'},
            }

            with self.assertRaisesRegex(RuntimeError, 'inventory is missing'):
                restore_apply.apply_one({}, value, restore_root)

class RestoreApplyLifecycleTests(unittest.TestCase):

    def test_restore_actions_wrap_data_and_restart_in_phase_order(self):
        root = Path('/restore')
        value = {
            'service': 'demo',
            'consistency': {'mode': 'files'},
            'actions': {
                'restore': {
                    phase: [{
                        'name': phase, 'command': ['/usr/bin/true'],
                        'run_as': 'root',
                    }]
                    for phase in (
                        'before', 'after', 'finally',
                        'on_success', 'on_failure',
                    )
                },
            },
        }
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        events = []

        def action_phase(_plan, _root, phase, **_kwargs):
            events.append(phase)
            return []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services',
            side_effect=lambda _plan: events.append('stop') or [],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
            side_effect=lambda *_args: events.append('preflight'),
        ), mock.patch.object(
            restore_apply, '_restore_data',
            side_effect=lambda *_args: events.append('data'),
        ), mock.patch.object(
            restore_apply, '_publish_controls',
            side_effect=lambda *_args: events.append('publish'),
        ), mock.patch.object(
            restore_apply, '_restart_services',
            side_effect=lambda *_args: events.append('restart'),
        ), mock.patch.object(
            restore_apply, '_run_restore_actions', side_effect=action_phase,
        ):
            restore_apply.apply_one({}, value, root)

        self.assertEqual(events, [
            'stop', 'preflight', 'before', 'data', 'publish',
            'after', 'finally', 'restart', 'on_success',
        ])

    def test_restore_after_failure_runs_finally_and_failure_but_not_restart(self):
        root = Path('/restore')
        value = {
            'service': 'demo',
            'consistency': {'mode': 'files'},
            'actions': {'restore': {
                phase: [{
                    'name': phase, 'command': ['/usr/bin/true'],
                    'run_as': 'root',
                }]
                for phase in ('before', 'after', 'finally', 'on_failure')
            }},
        }
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        events = []

        def action_phase(_plan, _root, phase, **_kwargs):
            events.append(phase)
            if phase == 'after':
                raise RuntimeError('import failed')
            return []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services', return_value=[],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
        ), mock.patch.object(
            restore_apply, '_restore_data',
        ), mock.patch.object(
            restore_apply, '_publish_controls',
        ), mock.patch.object(
            restore_apply, '_restart_services',
        ) as restart, mock.patch.object(
            restore_apply, '_run_restore_actions', side_effect=action_phase,
        ), self.assertRaisesRegex(RuntimeError, 'import failed'):
            restore_apply.apply_one({}, value, root)

        self.assertEqual(events, ['before', 'after', 'finally', 'on_failure'])
        restart.assert_not_called()

    def test_restore_failure_reconciles_compose_after_on_failure_actions(self):
        root = Path('/restore')
        value = {
            'service': 'demo',
            'actions': {'restore': {
                phase: [{
                    'name': phase, 'command': ['/usr/bin/true'],
                    'run_as': 'root',
                }]
                for phase in ('before', 'after', 'finally', 'on_failure')
            }},
        }
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        events = []

        def action_phase(_plan, _root, phase, **_kwargs):
            events.append(phase)
            if phase == 'after':
                raise RuntimeError('import failed')
            return []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services', return_value=[],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
        ), mock.patch.object(
            restore_apply, '_restore_data',
        ), mock.patch.object(
            restore_apply, '_publish_controls',
        ), mock.patch.object(
            restore_apply, '_run_restore_actions', side_effect=action_phase,
        ), mock.patch.object(
            restore_apply, '_stop_hook_started_services',
            side_effect=lambda _plan: events.append('reconcile'),
        ), self.assertRaisesRegex(RuntimeError, 'import failed'):
            restore_apply.apply_one({}, value, root)

        self.assertEqual([
            'before', 'after', 'finally', 'on_failure', 'reconcile',
        ], events)

    def test_restore_failure_environment_uses_triggering_phase(self):
        root = Path('/restore')
        action = {
            'name': 'notify', 'command': ['/usr/bin/true'], 'run_as': 'root',
        }
        value = {
            'service': 'demo',
            'actions': {'restore': {'on_failure': [action]}},
        }
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        with mock.patch.object(
                restore_apply, 'run_actions', return_value=[],
        ) as runner:
            restore_apply._run_restore_actions(
                plan, root, 'on_failure',
                error=RuntimeError('import failed'),
                failure_phase='after',
            )

        environment = runner.call_args.kwargs['extra_env']
        self.assertEqual(environment['BACKUPCTL_FAILURE_PHASE'], 'after')

    def test_on_success_failure_reconciles_after_on_failure_actions(self):
        root = Path('/restore')
        value = {
            'service': 'demo',
            'actions': {'restore': {
                phase: [{
                    'name': phase, 'command': ['/usr/bin/true'],
                    'run_as': 'root',
                }]
                for phase in ('on_success', 'on_failure')
            }},
        }
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), ('app',), (), 'demo',
        )
        events = []

        def action_phase(_plan, _root, phase, **_kwargs):
            events.append(phase)
            if phase == 'on_success':
                raise RuntimeError('notification failed')
            return []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services', return_value=['app'],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
        ), mock.patch.object(
            restore_apply, '_restore_data',
        ), mock.patch.object(
            restore_apply, '_publish_controls',
        ), mock.patch.object(
            restore_apply, '_run_restore_actions', side_effect=action_phase,
        ), mock.patch.object(
            restore_apply, '_restart_services',
            side_effect=lambda *_args: events.append('reconcile'),
        ), self.assertRaisesRegex(RuntimeError, 'notification failed'):
            restore_apply.apply_one({}, value, root)

        self.assertEqual([
            'before', 'after', 'finally', 'reconcile',
            'on_success', 'on_failure', 'reconcile',
        ], events)

    def test_apply_runs_preflight_data_publication_and_restart_in_order(self):
        root = Path('/restore')
        value = {'service': 'demo'}
        plan = restore_plan.RestorePlan(
            root, {}, value, 'existing', (), (), (), (), 'demo',
        )
        events = []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services',
            side_effect=lambda _plan: events.append('stop') or [],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
            side_effect=lambda _config, _plan: events.append('preflight'),
        ), mock.patch.object(
            restore_apply, '_restore_data',
            side_effect=lambda _config, _plan, _changed, _operation:
            events.append('data'),
        ), mock.patch.object(
            restore_apply, '_publish_controls',
            side_effect=lambda _config, _manifest, _plan, _changed:
            events.append('publish'),
        ), mock.patch.object(
            restore_apply, '_restart_services',
            side_effect=lambda _plan, _targets, _start: events.append('restart'),
        ):
            restore_apply.apply_one({}, value, root)

        self.assertEqual(
            events, ['stop', 'preflight', 'data', 'publish', 'restart'],
        )

    def test_rebuild_publishes_compose_before_creating_and_restoring_volumes(self):
        root = Path('/restore')
        value = {'service': 'demo'}
        plan = restore_plan.RestorePlan(
            root, {}, value, 'rebuild', (), (), (), (), 'demo',
        )
        events = []

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_stop_running_services',
            side_effect=lambda _plan: events.append('stop') or [],
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight',
            side_effect=lambda _config, _plan: events.append('preflight'),
        ), mock.patch.object(
            restore_apply, '_restore_data',
            side_effect=lambda _config, _plan, _changed, _operation:
            events.append('data'),
        ), mock.patch.object(
            restore_apply, '_publish_controls',
            side_effect=lambda _config, _manifest, _plan, _changed:
            events.append('publish'),
        ), mock.patch.object(
            restore_apply, '_restart_services',
            side_effect=lambda _plan, _targets, _start: events.append('restart'),
        ):
            restore_apply.apply_one({}, value, root)

        self.assertEqual(
            events, ['stop', 'preflight', 'publish', 'data', 'restart'],
        )

    def test_rebuild_compose_volume_is_created_directly_from_model_before_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = {'id': 'db', 'compose_volume': 'db'}
            value = {
                '_dir': str(root / 'demo'),
                '_path': str(root / 'demo' / 'backup.yaml'),
                'service': 'demo',
                'compose': {'files': ['compose.yaml']},
                'sources': {'paths': [], 'volumes': [source]},
            }
            plan = restore_plan.RestorePlan(
                root, {}, value, 'rebuild', ((source, 'demo_db'),),
                ('demo_db',), (), (), 'demo',
            )
            model = {
                'name': 'demo',
                'services': {'app': {'volumes': [{'type': 'volume', 'source': 'db'}]}},
                'volumes': {
                    'db': {
                        'name': 'demo_db',
                        'driver': 'custom-driver',
                        'driver_opts': {'device': '/dev/example'},
                    },
                },
            }
            events = []

            def create(name, *, on_created, **kwargs):
                self.assertEqual(name, 'demo_db')
                self.assertEqual(kwargs['driver'], 'custom-driver')
                self.assertEqual(
                    kwargs['driver_opts'], {'device': '/dev/example'},
                )
                self.assertEqual(kwargs['labels'], {})
                self.assertEqual(kwargs['project_name'], 'demo')
                self.assertEqual(kwargs['operation_id'], 'operation')
                on_created(name)
                events.append('volume-create')

            def restore(_c, _m, _root, **kwargs):
                self.assertEqual(kwargs['resolved'], ((source, 'demo_db'),))
                events.append('restore')

            with mock.patch.object(
                restore_apply, 'compose_model', return_value=model,
            ), mock.patch.object(
                restore_apply, 'docker_volume_exists',
                return_value=False,
            ), mock.patch.object(
                restore_apply, 'sync_volumes', side_effect=restore,
            ), mock.patch.object(
                restore_apply, 'create_restore_volume',
                side_effect=create,
            ) as direct_create, mock.patch.object(
                restore_apply, 'compose_run',
            ) as compose:
                restore_apply._restore_data(
                    {}, plan, restore_apply.RestoreChanges(), 'operation',
                )

            self.assertEqual(events, ['volume-create', 'restore'])
            direct_create.assert_called_once()
            compose.assert_not_called()

    def test_rebuild_rejects_restoring_external_compose_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '_meta').mkdir()
            source = {'id': 'db', 'compose_volume': 'db'}
            value = {
                '_dir': str(root / 'demo'),
                '_path': str(root / 'demo' / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [], 'volumes': [source]},
            }
            plan = restore_plan.RestorePlan(
                root, {}, value, 'rebuild', ((source, 'shared_db'),),
                ('shared_db',), (), (), 'demo',
            )
            model = {
                'name': 'demo',
                'services': {},
                'volumes': {
                    'db': {'name': 'shared_db', 'external': True},
                },
            }

            with mock.patch.object(
                restore_apply, 'compose_model', return_value=model,
            ), mock.patch.object(
                restore_apply, 'create_restore_volume',
            ) as create, mock.patch.object(
                restore_apply, 'sync_volumes',
            ) as restore, self.assertRaisesRegex(
                RuntimeError, 'external Compose volume',
            ):
                restore_apply._restore_data(
                    {}, plan, restore_apply.RestoreChanges(), 'operation',
                )

            create.assert_not_called()
            restore.assert_not_called()

    def test_rebuild_without_start_does_not_touch_compose_project(self):
        root = Path('/restore')
        source = {'id': 'db', 'compose_volume': 'db'}
        plan = restore_plan.RestorePlan(
            root, {}, {'service': 'demo'}, 'rebuild',
            ((source, 'demo_db'),), ('demo_db',), (), (), 'demo',
        )

        with mock.patch.object(restore_apply, 'compose_run') as compose:
            restore_apply._restart_services(plan, [], False)

        compose.assert_not_called()

    def test_rebuild_restore_actions_remove_temporary_containers_without_volumes(self):
        action = {
            'name': 'import', 'command': ['/usr/bin/true'], 'run_as': 'root',
        }
        plan = restore_plan.RestorePlan(
            Path('/restore'), {}, {
                'service': 'demo',
                'actions': {'restore': {'after': [action]}},
            },
            'rebuild', (), (), (), (), 'demo',
        )
        with mock.patch.object(
            restore_apply, 'compose_run',
        ) as compose, mock.patch.object(
            restore_apply, 'running_services',
        ) as running:
            restore_apply._stop_hook_started_services(plan)
            restore_apply._restart_services(plan, [], False)

        self.assertEqual(compose.call_count, 2)
        for call in compose.call_args_list:
            self.assertEqual(
                call.args[1], ['down', '--remove-orphans'],
            )
            self.assertNotIn('-v', call.args[1])
        running.assert_not_called()

    def test_existing_restore_stops_only_services_started_by_actions(self):
        action = {
            'name': 'import', 'command': ['/usr/bin/true'], 'run_as': 'root',
        }
        plan = restore_plan.RestorePlan(
            Path('/restore'), {}, {
                'service': 'demo',
                'actions': {'restore': {'after': [action]}},
            },
            'existing', (), (), ('app',), (), 'demo',
        )
        with mock.patch.object(
            restore_apply, 'running_services',
            return_value=['app', 'postgres'],
        ), mock.patch.object(restore_apply, 'compose_run') as compose:
            restore_apply._restart_services(plan, ['app'], False)

        self.assertEqual(
            [call.args[1] for call in compose.call_args_list],
            [
                ['stop', 'postgres'],
                ['up', '-d', '--no-deps', 'app'],
            ],
        )

    def test_dynamic_rebuild_rejects_snapshot_absent_path_that_appeared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'trusted' / 'demo'
            service_dir.mkdir(parents=True)
            (root / 'trusted').chmod(0o755)
            service_dir.chmod(0o755)
            (root / 'trusted').chmod(0o755)
            service_dir.chmod(0o755)
            target = service_dir / 'optional'
            target.write_text('stale', encoding='utf-8')
            source = {'id': 'optional', 'path': 'optional', 'required': False}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [source], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root, {'paths': [{**source, 'present': False, 'type': None}]},
                value, 'rebuild', (), (), (), (), 'demo',
            )
            with mock.patch.object(restore_apply, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(restore_apply, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()):
                with self.assertRaisesRegex(RuntimeError, 'target appeared'):
                    restore_apply.apply_one(
                        {}, value, root,
                    )

    def test_dynamic_rebuild_rejects_snapshot_absent_volume_that_appeared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = {
                '_dir': str(root / 'trusted' / 'demo'),
                '_path': str(root / 'trusted' / 'demo' / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root, {'paths': []}, value, 'rebuild', (), ('demo_cache',),
                (), (), 'demo',
            )
            with mock.patch.object(restore_apply, 'prepare_restore_plan', return_value=plan), \
                    mock.patch.object(restore_apply, 'docker_mount_conflicts', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_volume_exists', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'volume appeared'):
                    restore_apply.apply_one({}, value, root)

    def test_apply_reports_restart_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            def fake_run(cmd, **kwargs):
                if 'up' in cmd and kwargs.get('check', True):
                    raise common.CommandError(cmd, 1)
                return mock.Mock(stdout='')

            with mock.patch.object(restore_apply, 'compose_files_exist', return_value=True), \
                    mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'sync_volumes'), \
                    mock.patch.object(restore_apply, 'run', side_effect=fake_run):
                with self.assertRaises(common.CommandError):
                    restore_apply.apply_one({}, value, root)

    def test_dynamic_preflight_failure_restarts_previously_running_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(
                        restore_apply, 'docker_mount_conflicts',
                        return_value=('other-container',),
                    ), mock.patch.object(
                        restore_plan, 'docker_mount_conflicts', return_value=(),
                    ), mock.patch.object(restore_apply, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'used by containers'):
                    restore_apply.apply_one({}, value, root)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertTrue(any('up' in command for command in commands))

    def test_preflight_error_is_not_replaced_by_restart_error(self):
        root = Path('/restore')
        value = {'_dir': '/service', 'service': 'demo'}
        plan = restore_plan.RestorePlan(
            root, {'paths': []}, value, 'existing', (), (), ('app',), (), 'demo',
        )
        primary = RuntimeError('preflight conflict')

        def fake_run(_manifest, command, **_kwargs):
            if 'up' in command:
                raise common.CommandError(['docker', 'compose', *command], 1)
            return mock.Mock(stdout='')

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, '_dynamic_preflight', side_effect=primary,
        ), mock.patch.object(
            restore_apply, 'compose_run', side_effect=fake_run,
        ), self.assertRaises(RuntimeError) as caught:
            restore_apply.apply_one({}, value, root)

        self.assertIs(caught.exception, primary)

    def test_partial_stop_failure_attempts_to_restore_original_services(self):
        root = Path('/restore')
        value = {
            '_dir': '/service',
            'service': 'demo',
            'actions': {'restore': {'on_failure': [{
                'name': 'notify',
                'command': ['/usr/bin/true'],
                'run_as': 'root',
            }]}},
        }
        plan = restore_plan.RestorePlan(
            root, {'paths': []}, value, 'existing', (), (), ('one', 'two'), (),
            'demo',
        )
        stop_error = common.CommandError(['docker', 'compose', 'stop'], 1)
        commands = []
        events = []

        def fake_run(_manifest, command, **_kwargs):
            commands.append(command)
            if 'stop' in command:
                raise stop_error
            return mock.Mock(stdout='')

        with mock.patch.object(
            restore_apply, 'prepare_restore_plan', return_value=plan,
        ), mock.patch.object(
            restore_apply, 'compose_run', side_effect=fake_run,
        ), mock.patch.object(
            restore_apply, '_run_restore_actions',
            side_effect=lambda _plan, _root, phase, **kwargs:
            events.append((phase, kwargs.get('failure_phase'))),
        ), mock.patch.object(
            restore_apply, '_stop_hook_started_services',
            side_effect=lambda _plan: events.append(('reconcile', None)),
        ), self.assertRaises(common.CommandError) as caught:
            restore_apply.apply_one({}, value, root)

        self.assertIs(caught.exception, stop_error)
        self.assertEqual(events, [
            ('on_failure', 'stop'),
            ('reconcile', None),
        ])
        self.assertTrue(any('up' in command for command in commands))

    def test_apply_failure_does_not_restart_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_restore_inventory(root)
            value = manifest(root, consistency={'mode': 'stop'})

            with mock.patch.object(restore_apply, 'compose_files_exist', return_value=True), \
                    mock.patch.object(restore_plan, 'running_services', return_value=['app']), \
                    mock.patch.object(restore_plan, 'docker_project_containers', return_value=()), \
                    mock.patch.object(restore_apply, 'docker_project_containers', return_value=()), \
                    mock.patch.object(
                        restore_apply, 'sync_volumes', side_effect=RuntimeError('restore failed'),
                    ), mock.patch.object(restore_apply, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                    restore_apply.apply_one({}, value, root, start_services=True)

            commands = [call.args[0] for call in run_mock.call_args_list]
            self.assertTrue(any('stop' in command for command in commands))
            self.assertFalse(any('up' in command for command in commands))

    def test_failed_rebuild_rolls_back_created_path_and_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'trusted' / 'demo'
            service_dir.mkdir(parents=True)
            (root / 'trusted').chmod(0o755)
            service_dir.chmod(0o755)
            target = service_dir / 'data'
            source = {'id': 'data', 'path': 'data'}
            volume_source = {'id': 'db', 'name': 'demo_db'}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {
                    'paths': [source],
                    'volumes': [volume_source],
                },
            }
            plan = restore_plan.RestorePlan(
                root,
                {'paths': [{**source, 'present': True, 'type': 'directory'}]},
                value, 'rebuild', ((volume_source, 'demo_db'),),
                ('demo_db',), (), (), 'demo',
            )

            def fail_after_creating_target(*_args, **_kwargs):
                raise RuntimeError('restore failed')

            def create_volume(name, *, on_created, **_kwargs):
                on_created(name)

            with mock.patch.object(
                restore_apply, 'prepare_restore_plan', return_value=plan,
            ), mock.patch.object(
                restore_apply, '_dynamic_preflight',
            ), mock.patch.object(
                restore_apply, 'create_restore_volume',
                side_effect=create_volume,
            ), mock.patch.object(
                restore_apply, 'restore_path_source',
                side_effect=fail_after_creating_target,
            ), mock.patch.object(
                restore_apply, 'volume_owned_by_operation', return_value=True,
            ), mock.patch.object(restore_apply, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                    restore_apply.apply_one(
                        {},
                        value, root,
                    )

            self.assertFalse(target.exists())
            self.assertIn(
                ['docker', 'volume', 'rm', 'demo_db'],
                [call.args[0] for call in run_mock.call_args_list],
            )

    def test_failed_rebuild_rolls_back_directly_created_compose_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = {'id': 'db', 'compose_volume': 'db'}
            value = {
                '_dir': str(root / 'demo'),
                '_path': str(root / 'demo' / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [], 'volumes': [source]},
            }
            plan = restore_plan.RestorePlan(
                root, {}, value, 'rebuild', ((source, 'demo_db'),),
                ('demo_db',), (), (), 'demo',
            )
            model = {
                'name': 'demo', 'services': {},
                'volumes': {'db': {'name': 'demo_db'}},
            }
            events = []

            def create(name, *, on_created, **_kwargs):
                on_created(name)
                events.append('create-volume')

            def run(command, **_kwargs):
                if command == ['docker', 'volume', 'rm', 'demo_db']:
                    events.append('remove-volume')

            with mock.patch.object(
                restore_apply, 'prepare_restore_plan', return_value=plan,
            ), mock.patch.object(
                restore_apply, '_dynamic_preflight',
            ), mock.patch.object(
                restore_apply, '_publish_controls',
            ), mock.patch.object(
                restore_apply, 'compose_model', return_value=model,
            ), mock.patch.object(
                restore_apply, 'docker_volume_exists',
                return_value=False,
            ), mock.patch.object(
                restore_apply, 'create_restore_volume', side_effect=create,
            ), mock.patch.object(
                restore_apply, 'volume_owned_by_operation', return_value=True,
            ), mock.patch.object(
                restore_apply, 'sync_volumes',
                side_effect=RuntimeError('restore failed'),
            ), mock.patch.object(
                restore_apply, 'run', side_effect=run,
            ), self.assertRaisesRegex(RuntimeError, 'restore failed'):
                restore_apply.apply_one({}, value, root)

            self.assertEqual(events, ['create-volume', 'remove-volume'])

    def test_rebuild_rollback_preserves_concurrent_target_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'trusted' / 'demo'
            service_dir.mkdir(parents=True)
            target = service_dir / 'data'
            source = {'id': 'data', 'path': 'data'}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [source], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root,
                {'paths': [{**source, 'present': True, 'type': 'directory'}]},
                value, 'rebuild', (), (), (), (), 'demo',
            )

            def publish_then_receive_external_write(*_args, **kwargs):
                (target / 'restored').write_text('snapshot', encoding='utf-8')
                metadata = target.stat()
                kwargs['on_publish']((metadata.st_dev, metadata.st_ino))
                (target / 'foreign').write_text('keep', encoding='utf-8')
                raise RuntimeError('later restore failed')

            with mock.patch.object(
                restore_apply, 'prepare_restore_plan', return_value=plan,
            ), mock.patch.object(
                restore_apply, '_dynamic_preflight',
            ), mock.patch.object(
                restore_apply, 'restore_path_source',
                side_effect=publish_then_receive_external_write,
            ), self.assertRaisesRegex(RuntimeError, 'later restore failed'):
                restore_apply.apply_one(
                    {},
                    value, root,
                )

            self.assertEqual(
                (target / 'foreign').read_text(encoding='utf-8'), 'keep',
            )

    def test_rebuild_rollback_removes_created_ancestors_only_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / 'trusted'
            trusted.mkdir()
            service_dir = trusted / 'demo'
            target = service_dir / 'data'
            source = {'id': 'data', 'path': 'data'}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [source], 'volumes': []},
            }
            ids = {'uid': os.geteuid(), 'gid': os.getegid()}
            plan = restore_plan.RestorePlan(
                root,
                {'paths': [{
                    **source, 'present': True, 'type': 'directory',
                    'ancestors': [{
                        'path': 'demo', 'mode': 0o777, **ids,
                    }],
                }]},
                value, 'rebuild', (), (), (), (), 'demo',
            )

            def receive_external_write(*_args, **_kwargs):
                (service_dir / 'foreign').write_text('keep', encoding='utf-8')
                raise RuntimeError('restore failed')

            with mock.patch.object(
                restore_apply, 'prepare_restore_plan', return_value=plan,
            ), mock.patch.object(
                restore_apply, '_dynamic_preflight',
            ), mock.patch.object(
                restore_apply, 'restore_path_source',
                side_effect=receive_external_write,
            ), self.assertRaisesRegex(RuntimeError, 'restore failed'):
                restore_apply.apply_one(
                    {}, value, root,
                )

            self.assertEqual(
                (service_dir / 'foreign').read_text(encoding='utf-8'), 'keep',
            )

    def test_rebuild_rollback_removes_partial_file_created_by_this_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_dir = root / 'trusted' / 'demo'
            service_dir.mkdir(parents=True)
            (root / 'trusted').chmod(0o755)
            service_dir.chmod(0o755)
            target = service_dir / 'data'
            source = {'id': 'data', 'path': 'data'}
            value = {
                '_dir': str(service_dir),
                '_path': str(service_dir / 'backup.yaml'),
                'service': 'demo',
                'sources': {'paths': [source], 'volumes': []},
            }
            plan = restore_plan.RestorePlan(
                root,
                {'paths': [{**source, 'present': True, 'type': 'file'}]},
                value, 'rebuild', (), (), (), (), 'demo',
            )

            def partial_restore(*_args, **kwargs):
                target.unlink(missing_ok=True)
                target.write_text('partial restore output', encoding='utf-8')
                metadata = target.stat()
                kwargs['on_publish']((metadata.st_dev, metadata.st_ino))
                raise RuntimeError('restore failed')

            with mock.patch.object(
                restore_apply, 'prepare_restore_plan', return_value=plan,
            ), mock.patch.object(
                restore_apply, '_dynamic_preflight',
            ), mock.patch.object(
                restore_apply, 'restore_path_source',
                side_effect=partial_restore,
            ), self.assertRaisesRegex(RuntimeError, 'restore failed'):
                restore_apply.apply_one(
                    {},
                    value, root,
                )

            self.assertFalse(target.exists())

if __name__ == '__main__':
    unittest.main()
