import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import security


class MountPolicyTests(unittest.TestCase):
    def record(self, mount_point, filesystem='ext4', mount_id=1):
        return security.MountRecord(
            mount_id, 0, '8:1', Path('/'), Path(mount_point), filesystem,
            '/dev/test',
        )

    def test_allowlist_accepts_ext4_xfs_and_btrfs_only(self):
        for filesystem in ('ext4', 'xfs', 'btrfs'):
            with self.subTest(filesystem=filesystem):
                security.validate_mount_boundary(
                    '/srv/data', records=[self.record('/', filesystem)],
                )
        for filesystem in ('ext2', 'ext3', 'zfs', 'nfs', 'fuse'):
            with self.subTest(filesystem=filesystem), \
                    self.assertRaisesRegex(ValueError, 'unsupported filesystem'):
                security.validate_mount_boundary(
                    '/srv/data', records=[self.record('/', filesystem)],
                )

    def test_nested_mount_below_payload_is_rejected(self):
        records = [
            self.record('/', 'ext4', 1),
            self.record('/srv/data/app/cache', 'ext4', 2),
        ]
        with self.assertRaisesRegex(ValueError, 'nested mount'):
            security.validate_mount_boundary(
                '/srv/data', records=records,
            )

    def test_control_root_rejects_unsupported_filesystem_and_nested_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsupported = [self.record('/', 'nfs')]
            with self.assertRaisesRegex(ValueError, 'unsupported filesystem'):
                security.validate_control_root(root, records=unsupported)

            nested = [
                self.record('/', 'ext4', 1),
                self.record(root / 'nested', 'ext4', 2),
            ]
            with self.assertRaisesRegex(ValueError, 'nested mount'):
                security.validate_control_root(root, records=nested)


class PayloadPolicyTests(unittest.TestCase):
    def test_btrfs_nested_subvolume_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / 'payload'
            payload.mkdir()
            result = SimpleNamespace(returncode=0, stdout='ID 257 path child\n')
            with mock.patch.object(security.os, 'geteuid', return_value=0), \
                    self.assertRaisesRegex(ValueError, 'nested Btrfs'):
                security.validate_payload(
                    payload, filesystem_type='btrfs', run=lambda *_a, **_k: result,
                )

    def test_socket_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / 'payload'
            payload.mkdir()
            socket_path = payload / 'socket'
            import socket
            handle = socket.socket(socket.AF_UNIX)
            try:
                handle.bind(str(socket_path))
                with self.assertRaisesRegex(ValueError, 'unsupported payload'):
                    security.validate_payload(payload)
            finally:
                handle.close()


class ManagedLeafTests(unittest.TestCase):
    def test_trusted_root_itself_is_not_a_managed_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            trusted = Path(tmp) / 'data'
            trusted.mkdir()
            records = [security.MountRecord(
                1, 0, '8:1', Path('/'), Path('/'), 'ext4', '/dev/test',
            )]
            with self.assertRaisesRegex(ValueError, 'strictly below'):
                security.validate_managed_leaf(trusted, [trusted], records=records)

    def test_payload_descendants_may_be_untrusted_but_not_control_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            trusted = Path(tmp) / 'data'
            leaf = trusted / 'immich'
            writable = leaf / 'database'
            writable.mkdir(parents=True)
            trusted.chmod(0o755)
            leaf.chmod(0o755)
            writable.chmod(0o777)
            nested = writable / 'file'
            nested.write_text('data', encoding='utf-8')
            records = [security.MountRecord(
                1, 0, '8:1', Path('/'), Path('/'), 'ext4', '/dev/test',
            )]

            security.validate_managed_leaf(leaf, [trusted], records=records)
            with self.assertRaisesRegex(ValueError, 'group/world writable'):
                security.validate_managed_leaf(nested, [trusted], records=records)


class AtomicPublicationTests(unittest.TestCase):
    def test_publish_callback_runs_before_parent_fsync_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            source.write_text('snapshot', encoding='utf-8')
            events = []
            real_fsync = security.os.fsync
            calls = 0

            def fsync(fd):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('late fsync failure')
                return real_fsync(fd)

            with mock.patch.object(security.os, 'fsync', side_effect=fsync), \
                    self.assertRaisesRegex(OSError, 'late fsync'):
                security.atomic_copy_file(
                    source, target, require_absent=True,
                    on_publish=lambda identity: events.append(identity),
                )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0], (
                target.stat().st_dev, target.stat().st_ino,
            ))

    def test_require_absent_publish_does_not_overwrite_racing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            source.write_text('snapshot', encoding='utf-8')
            real_link = security.os.link

            def race(*args, **kwargs):
                target.write_text('concurrent', encoding='utf-8')
                return real_link(*args, **kwargs)

            with mock.patch.object(security.os, 'link', side_effect=race), \
                    self.assertRaises(FileExistsError):
                security.atomic_copy_file(source, target, require_absent=True)

            self.assertEqual(target.read_text(encoding='utf-8'), 'concurrent')

    def test_json_fsyncs_file_before_replace_and_parent_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'state.json'
            events = []
            real_fsync = security.os.fsync
            real_replace = security.os.replace

            def fsync(fd):
                events.append('fsync')
                return real_fsync(fd)

            def replace(*args, **kwargs):
                events.append('replace')
                return real_replace(*args, **kwargs)

            with mock.patch.object(security.os, 'fsync', side_effect=fsync), \
                    mock.patch.object(security.os, 'replace', side_effect=replace):
                security.atomic_write_json(target, {'ok': True})

            self.assertEqual(events, ['fsync', 'replace', 'fsync'])

    def test_replace_failure_preserves_old_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'state.json'
            target.write_text('{"old": true}\n', encoding='utf-8')
            with mock.patch.object(
                security.os, 'replace', side_effect=OSError('injected failure'),
            ), self.assertRaisesRegex(OSError, 'injected'):
                security.atomic_write_json(target, {'old': False})
            self.assertEqual(target.read_text(encoding='utf-8'), '{"old": true}\n')
            self.assertEqual(list(Path(tmp).glob('.*.tmp')), [])


class DockerWriterTests(unittest.TestCase):
    def test_readonly_bind_and_volume_mounts_are_not_writers(self):
        responses = iter([
            SimpleNamespace(returncode=0, stdout='container\n'),
            SimpleNamespace(returncode=0, stdout='''[{
              "Id": "container",
              "Mounts": [
                {"Type": "bind", "Source": "/srv/data/app", "RW": false},
                {"Type": "volume", "Name": "demo_data", "RW": false}
              ]
            }]'''),
        ])
        self.assertEqual(
            security.docker_mount_users(
                ['/srv/data/app'], ['demo_data'], run=lambda *_a, **_k: next(responses),
            ),
            (),
        )


if __name__ == '__main__':
    unittest.main()
