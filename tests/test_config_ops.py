import os
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from homelab_backup import config_ops


class ConfigOpsTests(unittest.TestCase):
    @staticmethod
    def _write_config_bundle(root, *, password='restic-secret',
                             rclone='[onedrive]\ntype = onedrive\n', version=1):
        configs = root / 'configs'
        configs.mkdir(parents=True)
        (configs / 'restic-password').write_text(password, encoding='utf-8')
        (configs / 'rclone.conf').write_text(rclone, encoding='utf-8')
        (configs / 'config.yaml').write_text(
            textwrap.dedent(f'''\
                version: {version}
                host_id: server-a
                services_root: /srv/stacks
                trusted_data_roots: [/srv/stacks, /srv/data]
                repository: rclone:onedrive:Backups/restic/server-a
                password_file: /etc/homelab-backup/restic-password
                rclone_config: /etc/homelab-backup/rclone/rclone.conf
                staging_root: /var/lib/homelab-backup/staging
                restore_root: /var/lib/homelab-backup/restores
                state_root: /var/lib/homelab-backup/state
                cache_root: /var/cache/homelab-backup/restic
                lock_file: /run/homelab-backup/backupctl.lock
                volume_helper_image: homelab/volume-rsync:release.test
            '''),
            encoding='utf-8',
        )
        return configs

    @staticmethod
    def _write_live_bundle(target, values):
        (target / 'rclone').mkdir(parents=True)
        target.chmod(0o700)
        (target / 'rclone').chmod(0o700)
        for relative, value in values.items():
            path = target / relative
            path.write_text(value, encoding='utf-8')

    def test_lock_path_requires_an_absolute_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.yaml'
            config.write_text('lock_file: /run/demo.lock\n', encoding='utf-8')
            config.chmod(0o600)
            self.assertEqual(config_ops.config_lock_path(config), '/run/demo.lock')
            config.write_text('lock_file: relative.lock\n', encoding='utf-8')
            with self.assertRaisesRegex(SystemExit, 'absolute path'):
                config_ops.config_lock_path(config)

    def test_lock_path_rejects_symlinked_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target.yaml'
            target.write_text('lock_file: /run/demo.lock\n', encoding='utf-8')
            config = root / 'config.yaml'
            config.symlink_to(target)

            with mock.patch(
                'homelab_backup.security.validate_control_directory',
            ), self.assertRaisesRegex(SystemExit, 'regular file'):
                config_ops.config_lock_path(config)

    def test_lock_validation_rejects_a_symlink_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target'
            target.write_text('', encoding='utf-8')
            lock = root / 'lock'
            lock.symlink_to(target)
            with self.assertRaisesRegex(SystemExit, 'regular file'):
                config_ops.validate_lock_path(lock)

    def test_preflight_rejects_invalid_bundle_content(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                config_ops.config_module, 'CFG', config_ops.config_module.CFG,
        ):
            root = Path(tmp)
            valid = root / 'valid'
            self._write_config_bundle(valid)
            config_ops.preflight_bundle(valid)

            invalid_schema = root / 'invalid-schema'
            self._write_config_bundle(invalid_schema, version=2)
            errors = StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit):
                config_ops.preflight_bundle(invalid_schema)
            self.assertIn('version must be 1', errors.getvalue())

            invalid_password = root / 'invalid-password'
            self._write_config_bundle(invalid_password, password=' \n')
            with self.assertRaisesRegex(SystemExit, 'restic password'):
                config_ops.preflight_bundle(invalid_password)

            invalid_rclone = root / 'invalid-rclone'
            self._write_config_bundle(
                invalid_rclone, rclone='[other]\ntype = local\n',
            )
            with self.assertRaisesRegex(SystemExit, 'rclone remote'):
                config_ops.preflight_bundle(invalid_rclone)

    def test_archive_publication_rolls_back_after_late_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'new.age'
            archive = root / 'archive.age'
            source.write_bytes(b'new-ciphertext')
            archive.write_bytes(b'old-ciphertext')

            with self.assertRaisesRegex(OSError, 'late publication failure'):
                config_ops.publish_archive(
                    source, archive, 'replace', fail_after_publish=True,
                )

            self.assertEqual(archive.read_bytes(), b'old-ciphertext')
            self.assertEqual(list(root.glob('.archive.age.*')), [])

    def test_archive_publication_uses_exit_75_when_rollback_sync_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'new.age'
            archive = root / 'archive.age'
            source.write_bytes(b'new-ciphertext')
            archive.write_bytes(b'old-ciphertext')

            with mock.patch.object(
                    config_ops.os, 'fsync', side_effect=(None, OSError('sync failed')),
            ), self.assertRaises(SystemExit) as caught:
                config_ops.publish_archive(
                    source, archive, 'replace', fail_after_publish=True,
                )

            self.assertEqual(caught.exception.code, 75)
            self.assertEqual(archive.read_bytes(), b'old-ciphertext')

    def test_bundle_publication_rolls_back_partial_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            self._write_config_bundle(source)
            old_values = {
                'restic-password': 'old-password',
                'rclone/rclone.conf': 'old-rclone',
                'config.yaml': 'old-config',
            }
            self._write_live_bundle(target, old_values)

            with self.assertRaisesRegex(RuntimeError, 'injected'):
                config_ops.publish_bundle(
                    source, target,
                    owner_uid=os.geteuid(), owner_gid=os.getegid(),
                    fail_after=True,
                )

            for relative, value in old_values.items():
                self.assertEqual((target / relative).read_text(), value)

    def test_bundle_publication_switches_complete_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            self._write_config_bundle(source)
            self._write_live_bundle(target, {
                'restic-password': 'old-password',
                'rclone/rclone.conf': 'old-rclone',
                'config.yaml': 'old-config',
            })
            stale = root / '.target.restore.next.crashed'
            stale.mkdir()

            config_ops.publish_bundle(
                source, target,
                owner_uid=os.geteuid(), owner_gid=os.getegid(),
            )

            self.assertEqual(
                (target / 'restic-password').read_text(), 'restic-secret',
            )
            self.assertFalse(stale.exists())
            self.assertEqual(list(root.glob('.target.restore.retired.*')), [])

    def test_post_commit_sync_failure_keeps_published_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            self._write_config_bundle(source)
            self._write_live_bundle(target, {
                'restic-password': 'old-password',
                'rclone/rclone.conf': 'old-rclone',
                'config.yaml': 'old-config',
            })

            config_ops.publish_bundle(
                source, target,
                owner_uid=os.geteuid(), owner_gid=os.getegid(),
                fail_after_commit=True,
            )

            self.assertEqual(
                (target / 'restic-password').read_text(), 'restic-secret',
            )
            self.assertEqual(list(root.glob('.target.restore.retired.*')), [])

    def test_bundle_publication_rejects_symlink_target_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            referent = root / 'referent'
            target = root / 'target'
            self._write_config_bundle(source)
            referent.mkdir()
            target.symlink_to(referent, target_is_directory=True)

            with self.assertRaisesRegex(SystemExit, 'real directory'):
                config_ops.publish_bundle(
                    source, target,
                    owner_uid=os.geteuid(), owner_gid=os.getegid(),
                )
            self.assertEqual(list(referent.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
