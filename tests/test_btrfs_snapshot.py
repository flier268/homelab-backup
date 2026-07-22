import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import btrfs_snapshot
from homelab_backup import manifest as _manifest_module  # preload before os mocks


FILESYSTEM_UUID = '11111111-2222-3333-4444-555555555555'
SOURCE_UUID = '22222222-3333-4444-5555-666666666666'
SNAPSHOT_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class BtrfsIdentityTests(unittest.TestCase):
    @staticmethod
    def entry(phase='ready'):
        return {
            'phase': phase,
            'source_id': 'world',
            'source_path': '/data/container/world',
            'source_subvolume_id': 256,
            'source_uuid': SOURCE_UUID,
            'filesystem_uuid': FILESYSTEM_UUID,
            'trusted_root': '/data',
            'workspace_path': '/data/.homelab-backup-snapshots',
            'snapshot_path': '/data/.homelab-backup-snapshots/demo-op-world',
            'snapshot_subvolume_id': None if phase == 'creating' else 300,
            'snapshot_uuid': None if phase == 'creating' else SNAPSHOT_UUID,
        }

    @classmethod
    def state(cls, phase='ready'):
        return {
            'version': 1, 'service': 'demo', 'operation_id': 'op',
            'snapshots': [cls.entry(phase)],
        }

    def test_subvolume_show_parser_includes_parent_uuid(self):
        parsed = btrfs_snapshot._parse_subvolume_show(
            '/data/world',
            f'UUID: {SNAPSHOT_UUID}\nParent UUID: {SOURCE_UUID}\n'
            'Subvolume ID: 256\nFlags: readonly\n',
        )
        self.assertEqual(parsed['parent_uuid'], SOURCE_UUID)
        self.assertTrue(parsed['readonly'])

    def test_legacy_journal_shape_is_rejected_without_migration(self):
        value = {
            'version': 1, 'service': 'demo', 'operation_id': '0' * 32,
            'snapshots': [{
                'source_path': '/data/world',
                'snapshot_path': '/data/.homelab-backup-snapshots/old',
            }],
        }
        with mock.patch.object(
            btrfs_snapshot, 'read_control_text', return_value=json.dumps(value),
        ), self.assertRaisesRegex(RuntimeError, 'invalid Btrfs snapshot state'):
            btrfs_snapshot._load_state({'state_root': '/state'}, 'demo')

    def test_plain_btrfs_directory_is_not_a_snapshot_source(self):
        result = SimpleNamespace(
            returncode=1, stdout='',
            stderr='ERROR: Not a Btrfs subvolume: Invalid argument',
        )
        metadata = SimpleNamespace(st_mode=0o040755)
        with mock.patch.object(btrfs_snapshot, 'run', return_value=result):
            self.assertIsNone(btrfs_snapshot.subvolume_details(
                '/proc/self/fd/7', allow_plain=True,
                filesystem_type='btrfs', opened_metadata=metadata,
            ))

    def test_subvolume_inspection_failure_includes_command_diagnostics(self):
        result = SimpleNamespace(
            returncode=1, stdout='', stderr='ERROR: cannot access device',
        )
        metadata = SimpleNamespace(st_mode=0o040755)
        with mock.patch.object(btrfs_snapshot, 'run', return_value=result), \
                self.assertRaisesRegex(RuntimeError, 'cannot access device'):
            btrfs_snapshot.subvolume_details(
                '/proc/self/fd/7', allow_plain=True,
                filesystem_type='btrfs', opened_metadata=metadata,
            )

    def test_workspace_is_under_trusted_root_not_container_owned_parent(self):
        self.assertEqual(
            btrfs_snapshot.snapshot_parent('/data'),
            Path('/data/.homelab-backup-snapshots'),
        )
        self.assertNotEqual(
            btrfs_snapshot.snapshot_parent('/data'),
            Path('/data/container/.homelab-backup-snapshots'),
        )

    def _identity_patches(self, details=None):
        details = details or {
            'subvolume_id': 300, 'uuid': SNAPSHOT_UUID,
            'parent_uuid': SOURCE_UUID, 'readonly': True,
        }
        return (
            mock.patch.object(btrfs_snapshot, 'validate_control_directory'),
            mock.patch.object(
                btrfs_snapshot.os, 'lstat', side_effect=[
                    SimpleNamespace(st_mode=0o040700, st_uid=0, st_gid=0),
                    SimpleNamespace(st_mode=0o040700, st_uid=0, st_gid=0),
                ],
            ),
            mock.patch.object(
                btrfs_snapshot, 'filesystem_uuid', return_value=FILESYSTEM_UUID,
            ),
            mock.patch.object(
                btrfs_snapshot, 'subvolume_details', return_value=details,
            ),
        )

    def test_cleanup_does_not_depend_on_live_source(self):
        with self._identity_patches()[0] as _validate, \
                self._identity_patches()[1] as _lstat, \
                self._identity_patches()[2] as _filesystem, \
                self._identity_patches()[3] as details:
            snapshot, _identity = btrfs_snapshot._validate_cleanup_identity(
                self.entry(),
            )
        self.assertEqual(snapshot, Path(self.entry()['snapshot_path']))
        details.assert_called_once_with(snapshot)

    def test_cleanup_rejects_each_snapshot_identity_mismatch(self):
        cases = (
            ({'subvolume_id': 999, 'uuid': SNAPSHOT_UUID,
              'parent_uuid': SOURCE_UUID, 'readonly': True}, 'snapshot subvolume ID'),
            ({'subvolume_id': 300, 'uuid': 'f' * 32,
              'parent_uuid': SOURCE_UUID, 'readonly': True}, 'snapshot UUID'),
            ({'subvolume_id': 300, 'uuid': SNAPSHOT_UUID,
              'parent_uuid': 'f' * 32, 'readonly': True}, 'parent UUID'),
            ({'subvolume_id': 300, 'uuid': SNAPSHOT_UUID,
              'parent_uuid': SOURCE_UUID, 'readonly': False}, 'not read-only'),
        )
        for details, reason in cases:
            patches = self._identity_patches(details)
            with self.subTest(reason=reason), patches[0], patches[1], patches[2], \
                    patches[3], self.assertRaisesRegex(RuntimeError, reason):
                btrfs_snapshot._validate_cleanup_identity(self.entry())

    def test_cleanup_rejects_filesystem_or_workspace_identity_mismatch(self):
        entry = self.entry()
        with mock.patch.object(btrfs_snapshot, 'validate_control_directory'), \
                mock.patch.object(
                    btrfs_snapshot.os, 'lstat', side_effect=[
                        SimpleNamespace(st_mode=0o040700, st_uid=0, st_gid=0),
                        SimpleNamespace(st_mode=0o040700, st_uid=0, st_gid=0),
                    ],
                ), mock.patch.object(
                    btrfs_snapshot, 'filesystem_uuid', return_value='f' * 32,
                ), self.assertRaisesRegex(RuntimeError, 'filesystem UUID'):
            btrfs_snapshot._validate_cleanup_identity(entry)

        entry['workspace_path'] = '/data/container/.homelab-backup-snapshots'
        with self.assertRaisesRegex(RuntimeError, 'unexpected snapshot path'):
            btrfs_snapshot._validate_cleanup_identity(entry)

    def test_creating_recovery_adopts_existing_snapshot_before_delete(self):
        state = self.state('creating')
        snapshot = Path(state['snapshots'][0]['snapshot_path'])
        details = {
            'subvolume_id': 300, 'uuid': SNAPSHOT_UUID,
            'parent_uuid': SOURCE_UUID, 'readonly': True,
        }
        saved = []
        with mock.patch.object(btrfs_snapshot, '_load_state', return_value=state), \
                mock.patch.object(
                    btrfs_snapshot, '_snapshot_exists', side_effect=[True, True, False],
                ), mock.patch.object(
                    btrfs_snapshot, '_validate_cleanup_identity',
                    side_effect=[(snapshot, details), (snapshot, details)],
                ), mock.patch.object(
                    btrfs_snapshot, '_save_state',
                    side_effect=lambda _c, value: saved.append(copy.deepcopy(value)),
                ), mock.patch.object(btrfs_snapshot, 'run') as runner, \
                mock.patch.object(btrfs_snapshot, '_clear_state') as clear, \
                mock.patch.object(btrfs_snapshot, '_remove_empty_workspace'):
            btrfs_snapshot.cleanup_snapshot_state({}, 'demo')
        self.assertEqual(saved[0]['snapshots'][0]['phase'], 'deleting')
        self.assertEqual(saved[0]['snapshots'][0]['snapshot_uuid'], SNAPSHOT_UUID)
        self.assertEqual(runner.call_args_list[0].args[0][:3], ['btrfs', 'subvolume', 'delete'])
        clear.assert_called_once_with({}, 'demo')

    def test_creating_recovery_clears_absent_intent(self):
        state = self.state('creating')
        with mock.patch.object(btrfs_snapshot, '_load_state', return_value=state), \
                mock.patch.object(btrfs_snapshot, '_snapshot_exists', return_value=False), \
                mock.patch.object(btrfs_snapshot, '_clear_state') as clear, \
                mock.patch.object(btrfs_snapshot, '_remove_empty_workspace'):
            btrfs_snapshot.cleanup_snapshot_state({}, 'demo')
        clear.assert_called_once_with({}, 'demo')

    def test_deleting_recovery_commits_absent_snapshot(self):
        state = self.state('deleting')
        with mock.patch.object(btrfs_snapshot, '_load_state', return_value=state), \
                mock.patch.object(btrfs_snapshot, '_snapshot_exists', return_value=False), \
                mock.patch.object(btrfs_snapshot, 'run') as runner, \
                mock.patch.object(btrfs_snapshot, '_clear_state') as clear, \
                mock.patch.object(btrfs_snapshot, '_remove_empty_workspace'):
            btrfs_snapshot.cleanup_snapshot_state({}, 'demo')
        runner.assert_not_called()
        clear.assert_called_once_with({}, 'demo')

    def test_transaction_journals_intent_then_uses_pinned_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'container' / 'world'
            source_path.mkdir(parents=True)
            fd = os.open(source_path, os.O_RDONLY | os.O_DIRECTORY)
            manifest = {
                'service': 'demo', '_dir': str(root),
                'sources': {'paths': [{'id': 'world', 'path': str(source_path)}]},
            }
            config = {
                'state_root': str(root / 'state'),
                'trusted_data_roots': [str(root)],
            }
            source_details = {
                'subvolume_id': 256, 'uuid': SOURCE_UUID,
                'parent_uuid': None, 'readonly': False,
            }
            snapshot_details = {
                'subvolume_id': 300, 'uuid': SNAPSHOT_UUID,
                'parent_uuid': SOURCE_UUID, 'readonly': True,
            }
            saved = []
            commands = []
            renamed_parent = root / 'renamed-container'

            def fake_run(command, **kwargs):
                commands.append((command, kwargs))
                if command[:3] == ['btrfs', 'subvolume', 'snapshot']:
                    source_path.parent.rename(renamed_parent)
                return SimpleNamespace(returncode=0, stdout='')

            transaction = btrfs_snapshot.SnapshotTransaction(
                config, manifest, lambda _path: (),
            )
            with mock.patch.object(btrfs_snapshot, 'cleanup_snapshot_state'), \
                    mock.patch.object(
                        btrfs_snapshot, 'open_data_path', return_value=(fd, os.fstat(fd)),
                    ), mock.patch.object(
                        btrfs_snapshot, 'containing_mount',
                        return_value=SimpleNamespace(filesystem_type='btrfs'),
                    ), mock.patch.object(
                        btrfs_snapshot, '_validate_workspace',
                        return_value=root / btrfs_snapshot.SNAPSHOT_DIRECTORY,
                    ), mock.patch.object(
                        btrfs_snapshot, 'filesystem_uuid', return_value=FILESYSTEM_UUID,
                    ), mock.patch.object(
                        btrfs_snapshot, '_save_state',
                        side_effect=lambda _c, value: saved.append(copy.deepcopy(value)),
                    ), mock.patch.object(
                        btrfs_snapshot, 'subvolume_details',
                        side_effect=[source_details, snapshot_details],
                    ), mock.patch.object(btrfs_snapshot, 'run', side_effect=fake_run):
                overrides = transaction.create()

            self.assertEqual(saved[0]['snapshots'][0]['phase'], 'creating')
            self.assertEqual(saved[-1]['snapshots'][0]['phase'], 'ready')
            snapshot_call = next(item for item in commands if item[0][2] == 'snapshot')
            self.assertEqual(snapshot_call[0][4], f'/proc/self/fd/{fd}')
            self.assertEqual(snapshot_call[1]['pass_fds'], (fd,))
            self.assertFalse(source_path.exists())
            self.assertTrue((renamed_parent / 'world').exists())
            self.assertIn('world', overrides)


if __name__ == '__main__':
    unittest.main()
