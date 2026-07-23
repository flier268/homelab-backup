import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from homelab_backup import upgrade


class DownloadResponse(io.BytesIO):
    def __init__(self, content, *, content_length=None):
        super().__init__(content)
        self.headers = {}
        if content_length is not None:
            self.headers['Content-Length'] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def release_archive(
    version='1.0.6', extra=(), *, include_installer=True,
    installer_mode=0o755,
):
    output = io.BytesIO()
    root = f'homelab-backup-{version}'
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        root_info = tarfile.TarInfo(f'{root}/')
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        archive.addfile(root_info)
        if include_installer:
            installer = b'#!/usr/bin/env bash\nexit 0\n'
            installer_info = tarfile.TarInfo(f'{root}/install.sh')
            installer_info.mode = installer_mode
            installer_info.size = len(installer)
            archive.addfile(installer_info, io.BytesIO(installer))
        for info, content in extra:
            if content is not None:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                archive.addfile(info)
    return output.getvalue()


class InstalledLayout:
    def __init__(self, root):
        self.install_root = Path(root) / 'lib' / 'homelab-backup'
        self.release = self.install_root / 'releases' / 'release.current'

    def __enter__(self):
        self.release.mkdir(parents=True)
        (self.install_root / 'current').symlink_to(
            Path('releases') / self.release.name,
        )
        self.install_patch = mock.patch.object(
            upgrade, 'INSTALL_ROOT', self.install_root,
        )
        self.environment_patch = mock.patch.dict(
            os.environ,
            {'HOMELAB_BACKUP_RELEASE_ROOT': str(self.release)},
        )
        self.install_patch.start()
        self.environment_patch.start()
        return self

    def __exit__(self, *args):
        self.environment_patch.stop()
        self.install_patch.stop()


class UpgradeTests(unittest.TestCase):
    def checksum(self, archive, version='1.0.6'):
        name = f'homelab-backup-{version}.tar.gz'
        digest = hashlib.sha256(archive).hexdigest()
        return f'{digest}  {name}\n'.encode('ascii')

    def test_repository_layout_is_rejected_before_network_access(self):
        with mock.patch.dict(
            os.environ, {'HOMELAB_BACKUP_RELEASE_ROOT': ''},
        ), mock.patch.object(upgrade, 'urlopen') as opener:
            with self.assertRaisesRegex(
                upgrade.UpgradeError, 'only available from the installed',
            ):
                upgrade.upgrade()
        opener.assert_not_called()

    def test_same_version_is_a_noop_without_archive_download(self):
        checksum = (
            b'0' * 64 + b'  homelab-backup-1.0.5.tar.gz\n'
        )
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp), \
                mock.patch.object(
                    upgrade, 'urlopen',
                    return_value=DownloadResponse(
                        checksum, content_length=len(checksum),
                    ),
                ) as opener, mock.patch.object(
                    upgrade, '_run_installer',
                ) as installer:
            upgrade.upgrade()

        self.assertEqual(opener.call_count, 1)
        installer.assert_not_called()

    def test_new_version_is_verified_extracted_and_installed_then_cleaned(self):
        archive = release_archive()
        checksum = self.checksum(archive)
        responses = [
            DownloadResponse(checksum, content_length=len(checksum)),
            DownloadResponse(archive, content_length=len(archive)),
        ]
        installer_root = None

        def run_installer(source_root):
            nonlocal installer_root
            installer_root = source_root
            self.assertTrue((source_root / 'install.sh').is_file())

        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout, \
                mock.patch.object(
                    upgrade, 'urlopen', side_effect=responses,
                ), mock.patch.object(
                    upgrade, '_run_installer', side_effect=run_installer,
                ) as installer:
            old_target = os.readlink(layout.install_root / 'current')
            upgrade.upgrade()
            self.assertEqual(
                os.readlink(layout.install_root / 'current'), old_target,
            )

        installer.assert_called_once()
        self.assertIsNotNone(installer_root)
        self.assertFalse(installer_root.exists())

    def test_installer_failure_still_cleans_temporary_tree(self):
        archive = release_archive()
        checksum = self.checksum(archive)
        responses = [
            DownloadResponse(checksum),
            DownloadResponse(archive),
        ]
        installer_root = None

        def fail_installer(source_root):
            nonlocal installer_root
            installer_root = source_root
            raise upgrade.UpgradeError('installer failed with exit status 2')

        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp), \
                mock.patch.object(
                    upgrade, 'urlopen', side_effect=responses,
                ), mock.patch.object(
                    upgrade, '_run_installer', side_effect=fail_installer,
                ):
            with self.assertRaisesRegex(upgrade.UpgradeError, 'exit status 2'):
                upgrade.upgrade()

        self.assertIsNotNone(installer_root)
        self.assertFalse(installer_root.exists())

    def test_older_latest_release_is_rejected_without_archive_download(self):
        checksum = (
            b'0' * 64 + b'  homelab-backup-1.0.4.tar.gz\n'
        )
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp), \
                mock.patch.object(
                    upgrade, 'urlopen',
                    return_value=DownloadResponse(checksum),
                ) as opener, mock.patch.object(
                    upgrade, '_run_installer',
                ) as installer:
            with self.assertRaisesRegex(upgrade.UpgradeError, 'refusing to downgrade'):
                upgrade.upgrade()

        self.assertEqual(opener.call_count, 1)
        installer.assert_not_called()

    def test_checksum_download_rejects_network_and_content_errors(self):
        valid = b'0' * 64 + b'  homelab-backup-1.0.6.tar.gz\n'
        cases = (
            ('network', URLError('offline'), 'checksum download failed'),
            (
                'too-large',
                DownloadResponse(b'x' * (upgrade.CHECKSUM_MAX_BYTES + 1)),
                '64 KiB',
            ),
            (
                'truncated',
                DownloadResponse(valid, content_length=len(valid) + 1),
                'incomplete',
            ),
            (
                'multiple',
                DownloadResponse(valid + valid),
                'exactly one',
            ),
            (
                'invalid',
                DownloadResponse(b'not-a-checksum\n'),
                'invalid record',
            ),
        )
        for label, response_or_error, message in cases:
            patcher = mock.patch.object(
                upgrade, 'urlopen',
                side_effect=response_or_error
                if isinstance(response_or_error, Exception) else None,
                return_value=None
                if isinstance(response_or_error, Exception) else response_or_error,
            )
            with self.subTest(label=label), patcher:
                with self.assertRaisesRegex(upgrade.UpgradeError, message):
                    upgrade._download_checksum()

    def test_archive_download_rejects_truncation_and_digest_mismatch(self):
        content = b'archive-data'
        cases = (
            (
                'truncated',
                DownloadResponse(content, content_length=len(content) + 1),
                hashlib.sha256(content).hexdigest(),
                'incomplete',
            ),
            (
                'digest',
                DownloadResponse(content),
                '0' * 64,
                'SHA-256 mismatch',
            ),
        )
        for label, response, digest, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(
                        upgrade, 'urlopen', return_value=response,
                    ):
                destination = Path(tmp) / 'release.tar.gz'
                with self.assertRaisesRegex(upgrade.UpgradeError, message):
                    upgrade._download_archive(
                        'homelab-backup-1.0.6.tar.gz',
                        digest,
                        destination,
                    )

    def test_archive_validation_rejects_unsafe_members(self):
        root = 'homelab-backup-1.0.6'
        cases = []
        traversal = tarfile.TarInfo(f'{root}/../escape')
        traversal.mode = 0o644
        cases.append(('traversal', ((traversal, b'x'),), True, 'unsafe path'))
        outside = tarfile.TarInfo('another-root/file')
        outside.mode = 0o644
        cases.append(('outside', ((outside, b'x'),), True, 'outside'))
        symlink = tarfile.TarInfo(f'{root}/link')
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = '/etc/passwd'
        cases.append(('symlink', ((symlink, None),), True, 'unsupported member'))
        hardlink = tarfile.TarInfo(f'{root}/hardlink')
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = f'{root}/install.sh'
        cases.append(('hardlink', ((hardlink, None),), True, 'unsupported member'))
        device = tarfile.TarInfo(f'{root}/device')
        device.type = tarfile.CHRTYPE
        cases.append(('device', ((device, None),), True, 'unsupported member'))
        fifo = tarfile.TarInfo(f'{root}/fifo')
        fifo.type = tarfile.FIFOTYPE
        cases.append(('fifo', ((fifo, None),), True, 'unsupported member'))
        wrong_version = tarfile.TarInfo('homelab-backup-1.0.7/file')
        wrong_version.mode = 0o644
        cases.append(
            ('wrong-version', ((wrong_version, b'x'),), True, 'outside'),
        )
        cases.append(('missing-installer', (), False, 'missing install.sh'))

        for label, extra, include_installer, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / 'release.tar.gz'
                archive_path.write_bytes(
                    release_archive(
                        extra=extra, include_installer=include_installer,
                    )
                )
                destination = Path(tmp) / 'source'
                destination.mkdir()
                with self.assertRaisesRegex(upgrade.UpgradeError, message):
                    upgrade._extract_archive(
                        archive_path, destination, '1.0.6',
                    )

    def test_archive_validation_rejects_unsafe_installer_permissions(self):
        for mode in (0o644, 0o777):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / 'release.tar.gz'
                archive_path.write_bytes(release_archive(installer_mode=mode))
                destination = Path(tmp) / 'source'
                destination.mkdir()
                with self.assertRaisesRegex(
                    upgrade.UpgradeError, 'invalid install.sh',
                ):
                    upgrade._extract_archive(
                        archive_path, destination, '1.0.6',
                    )

    def test_installer_runs_bash_in_release_root_and_propagates_failure(self):
        source_root = Path('/tmp/source')
        with mock.patch.object(
            upgrade.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 3),
        ) as runner:
            with self.assertRaisesRegex(upgrade.UpgradeError, 'exit status 3'):
                upgrade._run_installer(source_root)

        runner.assert_called_once_with(
            ['/bin/bash', 'install.sh'],
            cwd=source_root,
            check=False,
        )


if __name__ == '__main__':
    unittest.main()
