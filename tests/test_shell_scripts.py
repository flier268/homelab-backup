import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RestoreConfigsTests(unittest.TestCase):
    def test_noninteractive_restore_requires_explicit_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'restore-configs.sh'
            shutil.copy2(ROOT / 'restore-configs.sh', script)
            configs = root / 'configs'
            configs.mkdir()
            for name in ('restic-password', 'rclone.conf', 'config.yaml'):
                (configs / name).write_text('test', encoding='utf-8')

            bin_dir = root / 'bin'
            bin_dir.mkdir()
            sentinel = root / 'sudo-was-called'
            fake_sudo = bin_dir / 'sudo'
            fake_sudo.write_text(
                '#!/usr/bin/env bash\nprintf called > "$SUDO_SENTINEL"\nexit 99\n',
                encoding='utf-8',
            )
            fake_sudo.chmod(0o755)
            env = os.environ.copy()
            env['PATH'] = f'{bin_dir}:{env["PATH"]}'
            env['SUDO_SENTINEL'] = str(sentinel)

            result = subprocess.run(
                [str(script)],
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(sentinel.exists())
            self.assertIn('--yes', result.stderr)


class DeploymentScriptTests(unittest.TestCase):
    def test_installer_anchors_relative_sources_to_its_own_directory(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('ROOT_DIR=', script)
        self.assertIn('cd -- "$ROOT_DIR"', script)

    def test_installer_installs_package(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('LIB_ROOT=/usr/local/lib/homelab-backup', script)
        self.assertNotIn('rm -rf -- "$LIB_ROOT"', script)
        self.assertIn('homelab_backup/*.py "$LIB_ROOT/homelab_backup/"', script)

    def test_config_zip_is_created_under_private_umask(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        zip_branch = script.split('  2)', 1)[1].split('  q|Q)', 1)[0]
        self.assertIn('umask 077', zip_branch)

    def test_config_backup_is_root_only(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('if ((EUID != 0)); then', script)
        self.assertNotIn('sudo cat', script)

    def test_config_secrets_are_ignored_until_explicitly_force_added(self):
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8')
        for path in (
            'configs/restic-password', 'configs/rclone.conf', 'configs/config.yaml',
        ):
            self.assertIn(path, ignored)
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('add -f --', script)

    def test_private_git_confirmation_precedes_automatic_force_add(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('remote -v', script)
        self.assertIn("Type PRIVATE to continue with Git", script)
        self.assertIn("[[ \"$private_confirmation\" != 'PRIVATE' ]]", script)
        self.assertLess(script.index('Type PRIVATE'), script.index('add -f --'))
        self.assertNotIn('Run git add and commit', script)

    def test_weekly_maintenance_waits_for_the_global_lock(self):
        unit = (
            ROOT / 'systemd' / 'homelab-backup-maintenance.service'
        ).read_text(encoding='utf-8')
        self.assertNotIn('--no-wait', unit)


if __name__ == '__main__':
    unittest.main()
