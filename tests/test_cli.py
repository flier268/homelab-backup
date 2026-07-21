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
from homelab_backup import common


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_restore_parser_accepts_explicit_low_space_override(self):
        args = cli.build_parser().parse_args([
            'restore', 'demo', '--allow-low-space', '--yes',
        ])

        self.assertTrue(args.allow_low_space)

    def test_cleanup_restores_parser_supports_selected_and_batch_modes(self):
        selected = cli.build_parser().parse_args([
            'cleanup-restores', 'demo/20260717-120000-000000001', '--yes',
        ])
        batch = cli.build_parser().parse_args([
            'cleanup-restores', '--all', '--yes',
        ])

        self.assertEqual(
            selected.targets, ['demo/20260717-120000-000000001'],
        )
        self.assertFalse(selected.all)
        self.assertTrue(selected.yes)
        self.assertEqual(batch.targets, [])
        self.assertTrue(batch.all)
        self.assertTrue(batch.yes)

    def test_non_root_subcommand_is_rejected_before_loading_config(self):
        with mock.patch.object(cli.os, 'geteuid', return_value=1000), \
                mock.patch.object(cli, 'cfg') as cfg_mock, \
                self.assertRaisesRegex(SystemExit, '1'):
            cli.main(['list'])
        cfg_mock.assert_not_called()

    def test_main_reloads_config_if_lock_file_changes_before_acquisition(self):
        old = {'lock_file': '/run/old.lock'}
        current = {'lock_file': '/run/current.lock'}

        class TrackingLock:
            paths = []

            def __init__(self, path, nonblocking=False):
                self.path = path

            def __enter__(self):
                self.paths.append(self.path)
                return True

            def __exit__(self, *_args):
                pass

        command = mock.Mock()
        with mock.patch.object(cli.os, 'geteuid', return_value=0), \
                mock.patch.object(
                    cli, 'config_lock_file',
                    side_effect=[old['lock_file'], current['lock_file']],
                ), mock.patch.object(
                    cli, 'cfg', side_effect=[current, current],
                ), mock.patch.object(cli, 'GlobalLock', TrackingLock), \
                mock.patch.dict(cli.COMMANDS, {'list': command}):
            cli.main(['list'])

        self.assertEqual(
            TrackingLock.paths, [old['lock_file'], current['lock_file']],
        )
        command.assert_called_once_with(current, mock.ANY)

    def test_no_wait_does_not_block_while_loading_consistent_config(self):
        class BusyLock:
            def __init__(self, _path, nonblocking=False):
                self.nonblocking = nonblocking

            def __enter__(self):
                self.assert_nonblocking = self.nonblocking
                return False

            def __exit__(self, *_args):
                pass

        command = mock.Mock()
        with mock.patch.object(cli.os, 'geteuid', return_value=0), \
                mock.patch.object(
                    cli, 'config_lock_file', return_value='/run/current.lock',
                ), mock.patch.object(cli, 'cfg') as cfg_mock, \
                mock.patch.object(cli, 'GlobalLock', BusyLock), \
                mock.patch.dict(cli.COMMANDS, {'check': command}):
            cli.main(['check', '--no-wait'])

        cfg_mock.assert_not_called()
        command.assert_not_called()

    def test_global_lock_is_reentrant_for_command_level_locking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'locks' / 'backupctl.lock'
            with common.GlobalLock(path) as outer:
                with common.GlobalLock(path, nonblocking=True) as inner:
                    self.assertTrue(outer)
                    self.assertTrue(inner)
            with common.GlobalLock(path, nonblocking=True) as acquired_again:
                self.assertTrue(acquired_again)

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
            release_root = install_root / 'releases' / 'release.test'
            package_root = release_root / 'app'
            venv_root = release_root / 'venv'
            sbin.mkdir()
            package_root.mkdir(parents=True)
            (install_root / 'current').symlink_to(
                Path('releases') / release_root.name,
                target_is_directory=True,
            )
            (release_root / 'volume-helper-image').write_text(
                'homelab/volume-rsync:release.test\n', encoding='utf-8',
            )
            (release_root / '.lease').touch(mode=0o644)
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
