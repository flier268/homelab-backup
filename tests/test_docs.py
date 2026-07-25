import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homelab_backup import docs


class InstalledLayout:
    def __init__(self, root):
        self.install_root = Path(root) / 'lib' / 'homelab-backup'
        self.release = self.install_root / 'releases' / 'release.current'

    def __enter__(self):
        self.release.mkdir(parents=True)
        (self.install_root / 'current').symlink_to(
            Path('releases') / self.release.name,
        )
        self.guide = self.release / 'GUIDE.html'
        self.guide.write_text('<!doctype html>\n', encoding='utf-8')
        self.guide.chmod(0o644)
        self.install_patch = mock.patch.object(
            docs, 'INSTALL_ROOT', self.install_root,
        )
        self.environment_patch = mock.patch.dict(
            os.environ,
            {'HOMELAB_BACKUP_RELEASE_ROOT': str(self.release)},
        )
        self.install_patch.start()
        self.environment_patch.start()
        return self

    def __exit__(self, *_args):
        self.environment_patch.stop()
        self.install_patch.stop()


class DocsTests(unittest.TestCase):
    def test_docs_opens_installed_guide_in_default_browser(self):
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout, \
                mock.patch.object(
                    docs.webbrowser, 'open', return_value=True,
                ) as opener:
            docs.open_docs()

        opener.assert_called_once_with(layout.guide.as_uri())

    def test_docs_keeps_using_release_pinned_by_launcher(self):
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout:
            newer = layout.install_root / 'releases' / 'release.newer'
            newer.mkdir()
            current = layout.install_root / 'current'
            current.unlink()
            current.symlink_to(Path('releases') / newer.name)
            with mock.patch.object(
                docs.webbrowser, 'open', return_value=True,
            ) as opener:
                docs.open_docs()

        opener.assert_called_once_with(layout.guide.as_uri())

    def test_docs_rejects_missing_guide(self):
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout:
            layout.guide.unlink()
            with self.assertRaisesRegex(
                docs.DocsError, 'documentation is unavailable',
            ):
                docs.open_docs()

    def test_docs_rejects_symlinked_guide(self):
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout:
            layout.guide.unlink()
            target = layout.release / 'other.html'
            target.write_text('<!doctype html>\n', encoding='utf-8')
            layout.guide.symlink_to(target.name)
            with self.assertRaisesRegex(
                docs.DocsError, 'protected regular file',
            ):
                docs.open_docs()

    def test_docs_reports_missing_browser_with_manual_path(self):
        with tempfile.TemporaryDirectory() as tmp, InstalledLayout(tmp) as layout, \
                mock.patch.object(
                    docs.webbrowser, 'open', return_value=False,
                ):
            with self.assertRaises(docs.DocsError) as raised:
                docs.open_docs()
            self.assertIn(str(layout.guide), str(raised.exception))


if __name__ == '__main__':
    unittest.main()
