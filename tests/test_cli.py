import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cronsim
import yaml

from homelab_backup import cli


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_non_root_subcommand_is_rejected_before_loading_config(self):
        with mock.patch.object(cli.os, 'geteuid', return_value=1000), \
                mock.patch.object(cli, 'cfg') as cfg_mock, \
                self.assertRaisesRegex(SystemExit, '1'):
            cli.main(['list'])
        cfg_mock.assert_not_called()

    def test_repository_launcher_supports_help_and_version(self):
        for argument in ('--help', '--version'):
            with self.subTest(argument=argument):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'backupctl'), argument],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if argument == '--version':
                    self.assertEqual(result.stdout.strip(), 'backupctl 1.0.0')

    def test_installed_layout_launcher_supports_help_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            sbin = prefix / 'sbin'
            install_root = prefix / 'lib' / 'homelab-backup'
            package_root = install_root / 'app'
            venv_root = install_root / 'venv'
            sbin.mkdir()
            package_root.mkdir(parents=True)
            subprocess.run(
                [sys.executable, '-m', 'venv', '--without-pip', str(venv_root)],
                check=True,
            )
            venv_python = venv_root / 'bin' / 'python'
            site_packages = Path(subprocess.run(
                [
                    str(venv_python), '-c',
                    'import site; print(site.getsitepackages()[0])',
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip())
            for dependency in (cronsim, yaml):
                dependency_path = Path(dependency.__file__).parent
                shutil.copytree(
                    dependency_path,
                    site_packages / dependency_path.name,
                )
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
