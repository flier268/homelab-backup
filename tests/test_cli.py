import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_repository_launcher_supports_help_and_version(self):
        for argument in ('--help', '--version'):
            with self.subTest(argument=argument):
                result = subprocess.run(
                    [str(ROOT / 'backupctl'), argument],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if argument == '--version':
                    self.assertEqual(result.stdout.strip(), 'backupctl 1.0.0')

    def test_installed_layout_launcher_supports_help_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            sbin = prefix / 'sbin'
            package_root = prefix / 'lib' / 'homelab-backup'
            sbin.mkdir()
            package_root.mkdir(parents=True)
            shutil.copy2(ROOT / 'backupctl', sbin / 'backupctl')
            shutil.copytree(ROOT / 'homelab_backup', package_root / 'homelab_backup')
            for argument in ('--help', '--version'):
                with self.subTest(argument=argument):
                    result = subprocess.run(
                        [str(sbin / 'backupctl'), argument],
                        cwd=prefix, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if argument == '--version':
                        self.assertEqual(result.stdout.strip(), 'backupctl 1.0.0')


if __name__ == '__main__':
    unittest.main()
