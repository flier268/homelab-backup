import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from homelab_backup import btrfs_snapshot, security, storage


INTEGRATION = os.environ.get('HOMELAB_BACKUP_INTEGRATION') == '1'


@unittest.skipUnless(
    INTEGRATION and os.geteuid() == 0 and shutil.which('docker'),
    'set HOMELAB_BACKUP_INTEGRATION=1 and run as root with Docker',
)
class DockerIntegrationTests(unittest.TestCase):
    def test_volume_estimate_uses_apparent_size_for_sparse_files(self):
        image = os.environ.get(
            'HOMELAB_BACKUP_VOLUME_HELPER_IMAGE', 'homelab/volume-rsync:1',
        )
        volume = f'homelab-backup-test-{uuid.uuid4().hex}'
        subprocess.run(
            ['docker', 'volume', 'create', volume], check=True,
            capture_output=True,
        )
        try:
            subprocess.run([
                'docker', 'run', '--rm', '--network', 'none',
                '--mount', f'type=volume,src={volume},dst=/data',
                image, 'truncate', '-s', '16M', '/data/sparse',
            ], check=True, capture_output=True)
            size = storage.estimate_volume_source(
                {'volume_helper_image': image},
                {'id': 'data', 'required': True}, volume,
            )
            self.assertGreaterEqual(size, 16 * 1024 * 1024)
        finally:
            subprocess.run(
                ['docker', 'volume', 'rm', '-f', volume], check=False,
                capture_output=True,
            )

    def test_local_bind_and_named_volume_round_trip(self):
        image = os.environ.get(
            'HOMELAB_BACKUP_VOLUME_HELPER_IMAGE', 'homelab/volume-rsync:1',
        )
        volume = f'homelab-backup-test-{uuid.uuid4().hex}'
        subprocess.run(['docker', 'volume', 'create', volume], check=True, capture_output=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp)
                (source / 'token').write_text('round-trip', encoding='utf-8')
                subprocess.run([
                    'docker', 'run', '--rm', '--network', 'none',
                    '--mount', f'type=bind,src={source},dst=/src,readonly',
                    '--mount', f'type=volume,src={volume},dst=/dst',
                    image, 'cp', '/src/token', '/dst/token',
                ], check=True, capture_output=True, text=True)
                result = subprocess.run([
                    'docker', 'run', '--rm', '--network', 'none',
                    '--mount', f'type=volume,src={volume},dst=/src,readonly',
                    image, 'cat', '/src/token',
                ], check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), 'round-trip')
        finally:
            subprocess.run(
                ['docker', 'volume', 'rm', '-f', volume], check=False, capture_output=True,
            )


@unittest.skipUnless(
    INTEGRATION and os.geteuid() == 0 and shutil.which('btrfs')
    and os.environ.get('HOMELAB_BACKUP_BTRFS_ROOT'),
    'set HOMELAB_BACKUP_INTEGRATION=1 and HOMELAB_BACKUP_BTRFS_ROOT on Btrfs',
)
class BtrfsIntegrationTests(unittest.TestCase):
    def test_readonly_snapshot_excludes_later_writes_and_cleans_state(self):
        root = Path(os.environ['HOMELAB_BACKUP_BTRFS_ROOT'])
        token = uuid.uuid4().hex
        payload = root / f'homelab-backup-source-{token}'
        state_root = root / f'.homelab-backup-state-{token}'
        subprocess.run(
            ['btrfs', 'subvolume', 'create', payload],
            check=True, capture_output=True,
        )
        state_root.mkdir(mode=0o700)
        manifest = {
            'service': f'test-{token}',
            'sources': {'paths': [{
                'id': 'data', 'path': str(payload),
            }]},
        }
        transaction = None
        try:
            (payload / 'world.sav').write_text('before', encoding='utf-8')
            transaction = btrfs_snapshot.SnapshotTransaction(
                {
                    'state_root': str(state_root),
                    'trusted_data_roots': [str(root)],
                }, manifest, lambda _path: (),
            )
            snapshot = transaction.create()['data']
            (payload / 'world.sav').write_text('after', encoding='utf-8')
            self.assertEqual(
                (snapshot / 'world.sav').read_text(encoding='utf-8'), 'before',
            )
            transaction.cleanup()
            self.assertFalse(snapshot.exists())
            self.assertFalse(any(state_root.iterdir()))
        finally:
            if transaction is not None:
                try:
                    transaction.cleanup()
                except Exception:
                    pass
            subprocess.run(
                ['btrfs', 'subvolume', 'delete', payload],
                check=False, capture_output=True,
            )
            try:
                state_root.rmdir()
            except OSError:
                pass

    def test_nested_subvolume_is_rejected(self):
        root = Path(os.environ['HOMELAB_BACKUP_BTRFS_ROOT'])
        payload = root / f'homelab-backup-test-{uuid.uuid4().hex}'
        nested = payload / 'snapshot'
        subprocess.run(['btrfs', 'subvolume', 'create', payload], check=True, capture_output=True)
        try:
            subprocess.run(
                ['btrfs', 'subvolume', 'create', nested], check=True, capture_output=True,
            )
            with self.assertRaisesRegex(ValueError, 'nested Btrfs'):
                security.validate_payload(payload, filesystem_type='btrfs')
            fd = os.open(
                payload, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                with self.assertRaisesRegex(ValueError, 'nested Btrfs'):
                    security.validate_payload_fd(
                        fd, payload, filesystem_type='btrfs',
                    )
            finally:
                os.close(fd)
        finally:
            subprocess.run(
                ['btrfs', 'subvolume', 'delete', nested], check=False, capture_output=True,
            )
            subprocess.run(
                ['btrfs', 'subvolume', 'delete', payload], check=False, capture_output=True,
            )


if __name__ == '__main__':
    unittest.main()
