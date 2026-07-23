import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_sourced(script_name, command, *, input_text=None, env=None):
    return subprocess.run(
        [
            'bash', '-euo', 'pipefail', '-c',
            f'source "$1"; {command}', 'bash', str(ROOT / script_name),
        ],
        text=True,
        input=input_text,
        capture_output=True,
        env=env,
        check=False,
    )


class RestoreConfigsTests(unittest.TestCase):
    @staticmethod
    def _write_config_bundle(root, *, password='restic-secret',
                             rclone='[onedrive]\ntype = onedrive\n',
                             version=1):
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

    def test_config_bundle_publish_rejects_symlink_target_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source' / 'configs'
            referent = root / 'referent'
            target = root / 'target'
            source.mkdir(parents=True)
            referent.mkdir()
            target.symlink_to(referent, target_is_directory=True)
            for name in ('restic-password', 'rclone.conf', 'config.yaml'):
                (source / name).write_text(name, encoding='utf-8')

            uid = os.geteuid()
            gid = os.getegid()
            result = run_sourced(
                'restore-configs.sh',
                'publish_config_bundle '
                f'{shlex.quote(str(root / "source"))} '
                f'{shlex.quote(str(target))} {uid} {gid}',
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('real directory', result.stderr)
            self.assertEqual(list(referent.iterdir()), [])

    def test_restore_copies_untrusted_archive_before_decryption(self):
        script = (ROOT / 'restore-configs.sh').read_text(encoding='utf-8')
        copy = script.index('copy_untrusted_regular_file')
        decrypt = script.index('age --decrypt')
        self.assertLess(copy, decrypt)
        self.assertIn('"$WORK_DIR/configs.zip.age"', script)
        self.assertNotIn('minisign', script)
        self.assertNotIn('--sha256', script)

    def test_invalid_archive_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'invalid.zip'
            destination = root / 'extracted'
            with zipfile.ZipFile(archive, 'w') as value:
                value.writestr('configs/unexpected', 'secret')

            result = run_sourced(
                'restore-configs.sh',
                'validate_and_extract_archive '
                f'{shlex.quote(str(archive))} {shlex.quote(str(destination))}',
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unexpected or missing', result.stderr)
            self.assertFalse(destination.exists())

    def test_restore_rejects_non_tmpfs_plaintext_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_sourced(
                'restore-configs.sh', f'require_runtime_tmpfs {tmp!r}',
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tmpfs', result.stderr)

    def test_restore_accepts_only_encrypted_archive_and_pasted_identity(self):
        script = (ROOT / 'restore-configs.sh').read_text(encoding='utf-8')
        self.assertIn('if ((EUID != 0)); then', script)
        self.assertIn('homelab-backup-configs.zip.age', script)
        self.assertIn('age --decrypt -i -', script)
        self.assertNotIn('verify_archive_sha256', script)
        self.assertIn('require_runtime_tmpfs "$RUNTIME_DIR"', script)
        self.assertNotIn('sudo install', script)
        self.assertNotIn('$CONFIGS_DIR/restic-password', script)

    def test_restore_validates_archive_members_before_installing(self):
        script = (ROOT / 'restore-configs.sh').read_text(encoding='utf-8')
        extraction = script.index('validate_and_extract_archive ')
        validation = script.index('preflight_config_bundle "$WORK_DIR/extracted"')
        publish = script.index('publish_config_bundle "$WORK_DIR/extracted"')
        self.assertLess(extraction, validation)
        self.assertLess(validation, publish)

    def test_config_bundle_publish_has_rollback_for_partial_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source' / 'configs'
            target = root / 'target'
            (source).mkdir(parents=True)
            (target / 'rclone').mkdir(parents=True)
            target.chmod(0o700)
            (target / 'rclone').chmod(0o700)
            new_values = {
                'restic-password': 'new-password',
                'rclone/rclone.conf': 'new-rclone',
                'config.yaml': 'new-config',
            }
            old_values = {
                'restic-password': 'old-password',
                'rclone/rclone.conf': 'old-rclone',
                'config.yaml': 'old-config',
            }
            for relative, value in new_values.items():
                source_file = source / Path(relative).name
                source_file.write_text(value, encoding='utf-8')
            for relative, value in old_values.items():
                target_file = target / relative
                target_file.write_text(value, encoding='utf-8')

            uid = os.geteuid()
            gid = os.getegid()
            command = (
                'publish_config_bundle '
                f'{shlex.quote(str(root / "source"))} '
                f'{shlex.quote(str(target))} {uid} {gid} 1'
            )
            failed = run_sourced('restore-configs.sh', command)

            self.assertNotEqual(failed.returncode, 0)
            for relative, value in old_values.items():
                self.assertEqual(
                    (target / relative).read_text(encoding='utf-8'), value,
                )

            succeeded = run_sourced(
                'restore-configs.sh',
                'publish_config_bundle '
                f'{shlex.quote(str(root / "source"))} '
                f'{shlex.quote(str(target))} {uid} {gid}',
            )
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            for relative, value in new_values.items():
                self.assertEqual(
                    (target / relative).read_text(encoding='utf-8'), value,
                )

    def test_config_bundle_publish_switches_complete_directory_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source' / 'configs'
            target = root / 'target'
            source.mkdir(parents=True)
            (target / 'rclone').mkdir(parents=True)
            target.chmod(0o700)
            (target / 'rclone').chmod(0o700)
            stale = root / '.target.restore.next.crashed'
            stale.mkdir()
            for name, value in {
                'restic-password': 'new-password',
                'rclone.conf': 'new-rclone',
                'config.yaml': 'new-config',
            }.items():
                (source / name).write_text(value, encoding='utf-8')
            (target / 'restic-password').write_text(
                'old-password', encoding='utf-8',
            )
            (target / 'rclone' / 'rclone.conf').write_text(
                'old-rclone', encoding='utf-8',
            )
            (target / 'config.yaml').write_text(
                'old-config', encoding='utf-8',
            )
            result = run_sourced(
                'restore-configs.sh',
                'publish_config_bundle '
                f'{shlex.quote(str(root / "source"))} '
                f'{shlex.quote(str(target))} {os.geteuid()} {os.getegid()}',
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (target / 'config.yaml').read_text(encoding='utf-8'),
                'new-config',
            )
            self.assertEqual(
                (target / 'restic-password').read_text(encoding='utf-8'),
                'new-password',
            )
            self.assertFalse(stale.exists())
            self.assertEqual(
                len(list(root.glob('.target.restore.retired.*'))), 0,
            )

    def test_config_bundle_post_commit_sync_failure_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            self._write_config_bundle(source)
            (target / 'rclone').mkdir(parents=True)
            target.chmod(0o700)
            (target / 'rclone').chmod(0o700)
            for relative, value in {
                'restic-password': 'old-password',
                'rclone/rclone.conf': 'old-rclone',
                'config.yaml': 'old-config',
            }.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding='utf-8')

            result = run_sourced(
                'restore-configs.sh',
                'publish_config_bundle '
                f'{shlex.quote(str(source))} {shlex.quote(str(target))} '
                f'{os.geteuid()} {os.getegid()} 0 1',
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('was published', result.stderr)
            self.assertEqual(
                (target / 'restic-password').read_text(encoding='utf-8'),
                'restic-secret',
            )
            self.assertEqual(list(root.glob('.target.restore.retired.*')), [])

    def test_config_bundle_preflight_rejects_invalid_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / 'valid'
            self._write_config_bundle(valid)
            result = run_sourced(
                'restore-configs.sh',
                f'preflight_config_bundle {shlex.quote(str(valid))}',
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            invalid_schema = root / 'invalid-schema'
            self._write_config_bundle(invalid_schema, version=2)
            result = run_sourced(
                'restore-configs.sh',
                f'preflight_config_bundle {shlex.quote(str(invalid_schema))}',
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('version must be 1', result.stderr)

            invalid_password = root / 'invalid-password'
            self._write_config_bundle(invalid_password, password=' \n')
            result = run_sourced(
                'restore-configs.sh',
                f'preflight_config_bundle {shlex.quote(str(invalid_password))}',
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('restic password', result.stderr)

            invalid_rclone = root / 'invalid-rclone'
            self._write_config_bundle(
                invalid_rclone, rclone='[other]\ntype = local\n',
            )
            result = run_sourced(
                'restore-configs.sh',
                f'preflight_config_bundle {shlex.quote(str(invalid_rclone))}',
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('rclone remote', result.stderr)

    def test_restore_preflight_and_lock_precede_publication(self):
        script = (ROOT / 'restore-configs.sh').read_text(encoding='utf-8')
        preflight = script.index(
            'preflight_config_bundle "$WORK_DIR/extracted"',
        )
        lock = script.index('acquire_config_locks "$current_lock"')
        publish = script.index(
            'publish_config_bundle "$WORK_DIR/extracted"',
        )
        self.assertLess(preflight, lock)
        self.assertLess(lock, publish)


class DeploymentScriptTests(unittest.TestCase):
    def test_release_archive_uses_permissions_accepted_by_upgrader(self):
        workflow = (
            ROOT / '.github' / 'workflows' / 'ci-release.yml'
        ).read_text(encoding='utf-8')
        attributes = (
            ROOT / '.gitattributes'
        ).read_text(encoding='utf-8').splitlines()

        self.assertIn('git -c tar.umask=0022 archive', workflow)
        self.assertEqual(attributes, [
            '/.codegraph export-ignore',
            '/.github export-ignore',
            '/.gitignore export-ignore',
        ])

    def test_git_archive_commit_failure_restores_file_and_exact_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / 'repository'
            configs = repository / 'configs'
            configs.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repository)], check=True)
            subprocess.run(
                ['git', '-C', str(repository), 'config', 'user.name', 'Test'],
                check=True,
            )
            subprocess.run(
                ['git', '-C', str(repository), 'config',
                 'user.email', 'test@example.invalid'],
                check=True,
            )
            archive = configs / 'homelab-backup-configs.zip.age'
            archive.write_bytes(b'base')
            subprocess.run(
                ['git', '-C', str(repository), 'add', '--',
                 'configs/homelab-backup-configs.zip.age'],
                check=True,
            )
            subprocess.run(
                ['git', '-C', str(repository), 'commit', '-qm', 'base'],
                check=True,
            )
            archive.write_bytes(b'previous-index')
            subprocess.run(
                ['git', '-C', str(repository), 'add', '--',
                 'configs/homelab-backup-configs.zip.age'],
                check=True,
            )
            archive.write_bytes(b'previous-working-tree')
            hook = repository / '.git' / 'hooks' / 'pre-commit'
            hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
            hook.chmod(0o755)
            replacement = root / 'replacement.age'
            replacement.write_bytes(b'new-ciphertext')

            command = (
                f'ROOT_DIR={shlex.quote(str(repository))}; '
                f'CONFIGS_DIR={shlex.quote(str(configs))}; '
                f'GIT_ARCHIVE={shlex.quote(str(archive))}; '
                f'RUNTIME_DIR={shlex.quote(str(root))}; '
                f'WORK_DIR={shlex.quote(str(root / "work"))}; '
                'mkdir -p -m 700 "$WORK_DIR"; '
                f'git_cmd=(git -C {shlex.quote(str(repository))}); '
                'commit_git_archive_transaction '
                f'{shlex.quote(str(replacement))} '
                '"$(id -un)" "$(id -u)" "$(id -g)"'
            )
            result = run_sourced('backup-configs.sh', command)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(archive.read_bytes(), b'previous-working-tree')
            staged = subprocess.check_output(
                ['git', '-C', str(repository), 'show',
                 ':configs/homelab-backup-configs.zip.age'],
            )
            self.assertEqual(staged, b'previous-index')

            hook.unlink()
            succeeded = run_sourced('backup-configs.sh', command)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            committed = subprocess.check_output(
                ['git', '-C', str(repository), 'show',
                 'HEAD:configs/homelab-backup-configs.zip.age'],
            )
            self.assertEqual(committed, b'new-ciphertext')

    def test_rotation_copy_rejects_oversized_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'oversized.age'
            output = root / 'copied.age'
            source.touch()
            os.truncate(source, 128 * 1024 * 1024 + 1)
            user = subprocess.check_output(
                ['id', '-un'], text=True,
            ).strip()
            uid = subprocess.check_output(
                ['id', '-u'], text=True,
            ).strip()
            result = run_sourced(
                'backup-configs.sh',
                'copy_rotation_archive_as_user '
                f'{shlex.quote(str(source))} {shlex.quote(user)} '
                f'{uid} {shlex.quote(str(output))}',
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('128 MiB', result.stderr)
            self.assertFalse(output.exists())

    def test_config_backup_accepts_runtime_tmpfs_and_rejects_disk(self):
        accepted = run_sourced(
            'backup-configs.sh', 'require_runtime_tmpfs /dev/shm',
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            rejected = run_sourced(
                'backup-configs.sh', f'require_runtime_tmpfs {tmp!r}',
            )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('tmpfs', rejected.stderr)

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which('runuser'),
        'cross-UID publication requires root and runuser',
    )
    def test_archive_publication_is_traversable_by_output_user(self):
        user = 'nobody'
        uid = subprocess.check_output(['id', '-u', user], text=True).strip()
        gid = subprocess.check_output(['id', '-g', user], text=True).strip()
        with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
            root = Path(tmp)
            source = root / 'ciphertext.age'
            destination_dir = root / 'output'
            destination = destination_dir / 'archive.age'
            source.write_bytes(b'encrypted-only')
            destination_dir.mkdir()
            os.chown(root, int(uid), int(gid))
            os.chown(destination_dir, int(uid), int(gid))
            command = (
                f'RUNTIME_DIR=/dev/shm; WORK_DIR={str(root)!r}; '
                f'publish_ciphertext_for_user '
                f'{str(source)!r} {user!r} {uid!r} {gid!r} '
                f'{str(destination)!r} create'
            )
            result = run_sourced('backup-configs.sh', command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.read_bytes(), b'encrypted-only')
            self.assertEqual(destination.stat().st_uid, int(uid))

    def test_archive_publication_rolls_back_after_late_fsync_failure(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
            root = Path(tmp)
            work = root / 'work'
            work.mkdir(mode=0o700)
            source = root / 'new.age'
            archive = root / 'archive.age'
            source.write_bytes(b'new-ciphertext')
            archive.write_bytes(b'old-ciphertext')
            user = subprocess.check_output(['id', '-un'], text=True).strip()
            command = (
                f'RUNTIME_DIR={shlex.quote(str(root))}; '
                f'WORK_DIR={shlex.quote(str(work))}; '
                'publish_ciphertext_for_user '
                f'{shlex.quote(str(source))} {shlex.quote(user)} '
                '"$(id -u)" "$(id -g)" '
                f'{shlex.quote(str(archive))} replace 1'
            )

            result = run_sourced('backup-configs.sh', command)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(archive.read_bytes(), b'old-ciphertext')
            self.assertEqual(list(root.glob('.archive.age.*')), [])

    def test_config_backup_revalidates_generation_after_locking(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        function = script.split(
            'acquire_consistent_global_operation_lock() {', 1,
        )[1].split('\n}', 1)[0]
        self.assertGreaterEqual(function.count('configured_lock_file'), 2)
        self.assertIn('confirmed_lock', function)
        self.assertIn('continue', function)
        self.assertIn('acquire_consistent_global_operation_lock', script)

    def test_config_backup_pins_helper_runtime_before_lock_path_subshell(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolutions = Path(tmp) / 'resolutions'
            command = (
                'CONFIG_OPS_PYTHON=""; CONFIG_OPS_MODULE=""; '
                'resolve_config_ops_runtime() { '
                '  [[ -z "$CONFIG_OPS_PYTHON" ]] || return 0; '
                f'  printf x >> {shlex.quote(str(resolutions))}; '
                '  CONFIG_OPS_PYTHON=/pinned/python; '
                '  CONFIG_OPS_MODULE=/pinned/module; '
                '}; '
                'configured_lock_file() { '
                '  resolve_config_ops_runtime; printf "/tmp/config.lock\\n"; '
                '}; '
                'acquire_global_operation_lock() { '
                '  [[ "$CONFIG_OPS_PYTHON" == /pinned/python ]]; '
                '}; '
                'acquire_consistent_global_operation_lock'
            )

            result = run_sourced('backup-configs.sh', command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(resolutions.read_text(encoding='utf-8'), 'x')

    def test_config_scripts_share_one_pinned_helper_runtime(self):
        adapter = (ROOT / 'config-ops-runtime.sh').read_text(encoding='utf-8')
        for name in ('backup-configs.sh', 'restore-configs.sh'):
            script = (ROOT / name).read_text(encoding='utf-8')
            self.assertIn('source "$ROOT_DIR/config-ops-runtime.sh"', script)
        self.assertIn('resolve_config_ops_runtime', adapter)
        self.assertIn('[[ -z "$CONFIG_OPS_PYTHON" ]] || return 0', adapter)
        self.assertIn(
            'readlink -f -- /usr/local/lib/homelab-backup/current', adapter,
        )
        self.assertIn('flock -s "$lease_fd"', adapter)

    def test_installer_anchors_relative_sources_to_its_own_directory(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('ROOT_DIR=', script)
        self.assertIn('cd -- "$ROOT_DIR"', script)

    def test_uninstaller_removes_installed_assets_but_preserves_data_by_default(self):
        script = (ROOT / 'uninstall.sh').read_text(encoding='utf-8')
        self.assertIn('systemctl disable --now "$unit"', script)
        self.assertIn('systemctl daemon-reload', script)
        self.assertIn('rm -f -- "$LAUNCHER"', script)
        self.assertIn('docker image rm "$helper_image"', script)
        self.assertIn('rm -rf -- "$LIB_ROOT"', script)
        purge_block = script.split('if (( PURGE )); then', 1)[1]
        self.assertIn('/etc/homelab-backup', purge_block)
        self.assertIn('/var/lib/homelab-backup', purge_block)
        self.assertIn('/var/cache/homelab-backup', purge_block)
        self.assertNotIn('rm -rf -- /srv', script)
        self.assertNotIn('apt-get remove', script)

    def test_uninstaller_refuses_to_remove_an_active_release(self):
        script = (ROOT / 'uninstall.sh').read_text(encoding='utf-8')
        lease = script.index('exec {lease_fd}<"$release/.lease"')
        exclusive = script.index('flock -n -x "$lease_fd"', lease)
        removal = script.index('rm -rf -- "$LIB_ROOT"', exclusive)
        self.assertLess(lease, exclusive)
        self.assertLess(exclusive, removal)
        self.assertIn('an active process still uses release', script)

    def test_uninstaller_help_does_not_require_root(self):
        result = subprocess.run(
            ['bash', str(ROOT / 'uninstall.sh'), '--help'],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sudo ./uninstall.sh [--purge]', result.stdout)

    def test_installer_installs_package(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('LIB_ROOT=/usr/local/lib/homelab-backup', script)
        self.assertNotIn('rm -rf -- "$LIB_ROOT"', script)
        self.assertIn('RELEASES_ROOT="$LIB_ROOT/releases"', script)
        self.assertIn('CURRENT_ROOT="$LIB_ROOT/current"', script)
        self.assertIn('RELEASE_NEXT=', script)
        self.assertIn(
            'homelab_backup/*.py "$RELEASE_NEXT/app/homelab_backup/"',
            script,
        )
        self.assertIn('homelab_backup.config_ops', script)

    def test_installer_atomically_publishes_app_and_venv_as_one_release(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        venv = script.index('python3 -m venv "$RELEASE_NEXT/venv"')
        app = script.index(
            'homelab_backup/*.py "$RELEASE_NEXT/app/homelab_backup/"',
        )
        helper = script.index(
            'docker build -t "$HELPER_IMAGE" -f Dockerfile.volume-rsync .',
        )
        units = script.index('systemctl daemon-reload')
        publish = script.index('mv -Tf -- "$CURRENT_NEXT" "$CURRENT_ROOT"')
        publish_launcher = script.index(
            'mv -Tf -- "$LAUNCHER_NEXT" /usr/local/sbin/backupctl',
        )
        self.assertLess(venv, publish)
        self.assertLess(app, publish)
        self.assertLess(helper, publish)
        self.assertLess(units, publish)
        self.assertLess(publish_launcher, publish)
        self.assertIn(
            '"$RELEASE_NEXT/venv/bin/python" -m pip install',
            script,
        )
        self.assertNotIn('python3 -m venv --upgrade', script)
        self.assertIn(
            'docker run --rm --network none "$HELPER_IMAGE_ID" rsync --version',
            script,
        )

    def test_installer_does_not_replace_launcher_before_release_is_ready(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        stage_launcher = script.index(
            'install -m 0755 backupctl "$LAUNCHER_NEXT"',
        )
        helper_test = script.index(
            'docker run --rm --network none "$HELPER_IMAGE_ID" rsync --version',
        )
        daemon_reload = script.index('systemctl daemon-reload')
        publish_release = script.index(
            'mv -Tf -- "$CURRENT_NEXT" "$CURRENT_ROOT"',
        )
        publish_launcher = script.index(
            'mv -Tf -- "$LAUNCHER_NEXT" /usr/local/sbin/backupctl',
        )

        self.assertLess(stage_launcher, helper_test)
        self.assertLess(helper_test, publish_release)
        self.assertLess(daemon_reload, publish_release)
        self.assertLess(publish_launcher, publish_release)
        self.assertNotIn(
            'install -m 0755 backupctl /usr/local/sbin/backupctl',
            script,
        )
        self.assertIn(
            '"$RELEASE_NEXT/volume-helper-image"',
            script,
        )

    def test_installed_launcher_pins_release_across_python_reexec(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'usr' / 'local'
            launcher = local / 'sbin' / 'backupctl'
            install_root = local / 'lib' / 'homelab-backup'
            releases = install_root / 'releases'
            release_a = releases / 'release.A'
            release_b = releases / 'release.B'
            launcher.parent.mkdir(parents=True)
            releases.mkdir(parents=True)
            shutil.copy2(ROOT / 'backupctl', launcher)
            for release, label in ((release_a, 'A'), (release_b, 'B')):
                package = release / 'app' / 'homelab_backup'
                package.mkdir(parents=True)
                (package / '__init__.py').write_text('', encoding='utf-8')
                (package / 'cli.py').write_text(
                    'import os\n'
                    'from pathlib import Path\n'
                    'def main():\n'
                    '    release = Path(__file__).resolve().parents[2].name\n'
                    '    helper = os.environ.get('
                    '"HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE")\n'
                    '    print(f"{release}|{helper}")\n',
                    encoding='utf-8',
                )
                (release / 'volume-helper-image').write_text(
                    json.dumps({
                        'tag': f'homelab/volume-rsync:release.{label}',
                        'image_id': 'sha256:' + label.lower() * 64,
                    }) + '\n',
                    encoding='utf-8',
                )
                (release / 'volume-helper-image').chmod(0o600)
                (release / '.lease').touch(mode=0o644)
            subprocess.run(
                [sys.executable, '-m', 'venv', str(release_a / 'venv')],
                check=True,
            )
            python = release_a / 'venv' / 'bin' / 'python'
            python_real = python.with_name('python-real')
            python.rename(python_real)
            python.write_text(
                '#!/bin/sh\n'
                f'ln -sfn releases/release.B {shlex.quote(str(install_root / "current"))}\n'
                'exec "$(dirname "$0")/python-real" "$@"\n',
                encoding='utf-8',
            )
            python.chmod(0o755)
            (install_root / 'current').symlink_to('releases/release.A')

            result = subprocess.run(
                [str(launcher)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                f'release.A|sha256:{"a" * 64}',
            )
            self.assertEqual(
                os.readlink(install_root / 'current'),
                'releases/release.B',
            )

    def test_installer_rolls_back_shared_assets_before_discarding_release(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        publish_launcher = script.index(
            'mv -Tf -- "$LAUNCHER_NEXT" /usr/local/sbin/backupctl',
        )
        publish_current = script.index(
            'mv -Tf -- "$CURRENT_NEXT" "$CURRENT_ROOT"',
        )
        disable_rollback = script.index('trap - EXIT', publish_current)

        self.assertLess(publish_launcher, publish_current)
        self.assertLess(publish_current, disable_rollback)
        self.assertIn('if (( CURRENT_PUBLISHED )); then', script)
        self.assertIn('if (( LAUNCHER_PUBLISHED )); then', script)
        self.assertIn('if (( PREVIOUS_PUBLISHED )); then', script)
        self.assertIn('if (( UNITS_PUBLISHED )); then', script)
        self.assertIn(
            'systemctl daemon-reload ||\n'
            '      echo "WARNING: systemd daemon-reload failed during rollback"',
            script,
        )
        self.assertIn('(( ! CURRENT_PUBLISHED ))', script)
        self.assertIn('flock -n 9', script)

    def test_installer_retains_helper_metadata_when_image_pruning_fails(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        pruning = script.split(
            'for release in "$RELEASES_ROOT"/release.*; do', 1,
        )[1]
        inspect_image = pruning.index(
            'docker image ls --quiet --no-trunc "$obsolete_helper"',
        )
        remove_image = pruning.index('docker image rm "$obsolete_helper"')
        retain = pruning.index('continue', remove_image)
        remove_release = pruning.index('rm -rf -- "$release"')

        self.assertLess(inspect_image, remove_image)
        self.assertLess(remove_image, retain)
        self.assertLess(retain, remove_release)
        self.assertIn('retaining release metadata for retry', pruning)

    def test_installer_versions_helper_image_with_release_publication(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        build = script.index(
            'docker build -t "$HELPER_IMAGE" -f Dockerfile.volume-rsync .',
        )
        metadata = script.index(
            '"$HELPER_IMAGE" "$HELPER_IMAGE_ID" > '
            '"$RELEASE_NEXT/volume-helper-image"',
        )
        publish = script.index('mv -Tf -- "$CURRENT_NEXT" "$CURRENT_ROOT"')

        self.assertIn(
            'HELPER_IMAGE="homelab/volume-rsync:$RELEASE_NAME"',
            script,
        )
        self.assertLess(build, metadata)
        self.assertLess(metadata, publish)
        self.assertNotIn('docker build -t homelab/volume-rsync:1', script)
        self.assertIn("docker image inspect --format '{{.Id}}'", script)

    def test_volume_helper_dependencies_are_fully_pinned(self):
        dockerfile = (ROOT / 'Dockerfile.volume-rsync').read_text(
            encoding='utf-8',
        )
        self.assertRegex(dockerfile, r'FROM alpine:3\.22@sha256:[0-9a-f]{64}')
        for package in ('acl', 'attr', 'coreutils', 'rsync'):
            self.assertRegex(dockerfile, rf'\b{package}=[0-9][^\s\\]*')

    def test_installer_keeps_only_current_and_previous_releases(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('PREVIOUS_ROOT="$LIB_ROOT/previous"', script)
        self.assertIn('OLD_RELEASE=', script)
        self.assertIn('for release in "$RELEASES_ROOT"/release.*', script)
        self.assertIn('rm -rf -- "$release"', script)

    def test_installer_does_not_prune_a_release_with_an_active_lease(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        pruning = script.split(
            'for release in "$RELEASES_ROOT"/release.*; do', 1,
        )[1]
        lease = pruning.index('"$release/.lease"')
        exclusive = pruning.index('flock -n -x', lease)
        removal = pruning.index('rm -rf -- "$release"', exclusive)
        self.assertLess(lease, exclusive)
        self.assertLess(exclusive, removal)
        self.assertIn('active process', pruning[exclusive:removal])

    def test_installer_uses_locked_application_dependencies(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        requirements_input = (ROOT / 'requirements.in').read_text(encoding='utf-8')
        requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('python3-venv', script)
        self.assertIn(' age', script)
        self.assertNotIn(' minisign', script)
        self.assertIn("sys.version_info < (3, 10)", script)
        self.assertIn('python3 -m venv "$RELEASE_NEXT/venv"', script)
        self.assertIn(
            '"$RELEASE_NEXT/venv/bin/python" -m pip install',
            script,
        )
        self.assertIn('--require-hashes', script)
        self.assertNotIn('--target', script)
        self.assertEqual(
            requirements_input.splitlines(),
            ['cronsim', 'pydantic>=2.12,<3', 'PyYAML'],
        )
        self.assertIn('cronsim==2.7', requirements)
        self.assertIn('pydantic==2.13.4', requirements)
        self.assertIn('pyyaml==6.0.3', requirements.lower())
        self.assertIn('--hash=sha256:', requirements)
        pyyaml_lock = requirements.lower().split('pyyaml==6.0.3', 1)[1]
        self.assertGreaterEqual(pyyaml_lock.count('--hash=sha256:'), 60)

    def test_config_archive_is_encrypted_before_leaving_runtime_tmpfs(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('require_runtime_tmpfs "$RUNTIME_DIR"', script)
        self.assertIn('age --encrypt -R -', script)
        self.assertIn('.zip.age', script)
        self.assertIn('trap cleanup EXIT', script)
        self.assertNotIn('ZIP itself is not encrypted', script)

    def test_config_archive_uses_only_age_encryption(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertNotIn('minisign', script)
        self.assertNotIn('.minisig', script)
        self.assertNotIn('archive_sha256', script)
        self.assertNotIn('TRUSTED_SHA256', script)

    def test_config_backup_is_root_only(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('if ((EUID != 0)); then', script)
        self.assertNotIn('sudo cat', script)

    def test_git_stages_only_the_encrypted_archive(self):
        ignored = (ROOT / '.gitignore').read_text(encoding='utf-8')
        for path in (
            'configs/restic-password', 'configs/rclone.conf', 'configs/config.yaml',
        ):
            self.assertIn(path, ignored)
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('configs/homelab-backup-configs.zip.age', script)
        self.assertIn('add -f --', script)
        self.assertNotIn('.minisig', script)
        self.assertNotIn('add -f -- \\\n      configs/restic-password', script)
        self.assertNotIn('for legacy in', script)
        self.assertNotIn('runuser --user "$git_user" -- rm -f', script)

    def test_private_git_confirmation_precedes_automatic_force_add(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        main = script.split('main() {', 1)[1]
        self.assertIn('remote -v', script)
        self.assertIn("Type PRIVATE to continue with Git", script)
        self.assertIn("[[ \"$private_confirmation\" != 'PRIVATE' ]]", script)
        self.assertLess(
            main.index('Type PRIVATE'),
            main.index('commit_git_archive_transaction'),
        )
        self.assertNotIn('Run git add and commit', script)

    def test_config_git_commands_always_run_as_a_non_root_user(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('git_user=', script)
        self.assertIn('git_uid=', script)
        self.assertIn('((git_uid == 0))', script)
        self.assertIn('runuser --user "$git_user" -- git', script)
        self.assertIn('publish_ciphertext_for_user', script)
        self.assertNotIn('git -c "safe.directory=', script)

    def test_decrypted_archive_has_output_and_member_size_limits(self):
        for name in ('backup-configs.sh', 'restore-configs.sh'):
            script = (ROOT / name).read_text(encoding='utf-8')
            self.assertIn('ulimit -f 131072', script)
            self.assertIn('16 * 1024 * 1024', script)

    def test_config_key_rotation_decrypts_validates_and_atomically_reencrypts(self):
        script = (ROOT / 'backup-configs.sh').read_text(encoding='utf-8')
        self.assertIn('--rotate', script)
        rotation = script.split('rotate_key() {', 1)[1].split('\n}', 1)[0]
        copy = rotation.index('copy_rotation_archive_as_user')
        decrypt = rotation.index('decrypt_with_pasted_identity')
        validate = rotation.index('validate_archive')
        encrypt = rotation.index('encrypt_archive')
        publish = rotation.index('publish_ciphertext_for_user')
        self.assertLess(copy, decrypt)
        self.assertLess(decrypt, validate)
        self.assertLess(validate, encrypt)
        self.assertLess(encrypt, publish)

    @unittest.skipUnless(
        shutil.which('age') and shutil.which('ssh-keygen'),
        'real rotation test requires age and ssh-keygen',
    )
    def test_key_rotation_rejects_wrong_key_and_preserves_archive(self):
        age = shutil.which('age')
        with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
            root = Path(tmp)
            old_key = root / 'old-key'
            new_key = root / 'new-key'
            wrong_key = root / 'wrong-key'
            for key in (old_key, new_key, wrong_key):
                subprocess.run(
                    ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                    check=True,
                )

            plain = root / 'configs.zip'
            with zipfile.ZipFile(plain, 'w') as archive:
                archive.writestr('configs/restic-password', 'restic')
                archive.writestr('configs/rclone.conf', 'rclone')
                archive.writestr('configs/config.yaml', 'version: 1\n')
            encrypted = root / 'configs.zip.age'
            old_recipient = old_key.with_suffix('.pub').read_text(encoding='utf-8')
            new_recipient = new_key.with_suffix('.pub').read_text(encoding='utf-8')
            subprocess.run(
                [age, '--encrypt', '-R', '-', '-o', str(encrypted), str(plain)],
                input=old_recipient,
                text=True,
                check=True,
            )
            original = encrypted.read_bytes()
            env = os.environ.copy()
            if os.geteuid() == 0:
                rotation_user = 'nobody'
                rotation_uid = int(subprocess.check_output(
                    ['id', '-u', rotation_user], text=True,
                ))
                rotation_gid = int(subprocess.check_output(
                    ['id', '-g', rotation_user], text=True,
                ))
                os.chown(root, rotation_uid, rotation_gid)
                os.chown(encrypted, rotation_uid, rotation_gid)
                env['SUDO_USER'] = rotation_user
            env['NEW_RECIPIENT'] = new_recipient.strip()
            command = (
                'RUNTIME_DIR=/dev/shm; '
                f'WORK_DIR={shlex.quote(str(root / "work"))}; '
                'rm -rf -- "$WORK_DIR"; mkdir -m 700 "$WORK_DIR"; '
                'read_recipient() { RECIPIENT="$NEW_RECIPIENT"; }; '
                f'rotate_key {shlex.quote(str(encrypted))}'
            )
            wrong = run_sourced(
                'backup-configs.sh', command,
                input_text=wrong_key.read_text(encoding='utf-8'), env=env,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertEqual(encrypted.read_bytes(), original)

            rotated = run_sourced(
                'backup-configs.sh', command,
                input_text=old_key.read_text(encoding='utf-8'), env=env,
            )
            self.assertEqual(rotated.returncode, 0, rotated.stderr)
            self.assertNotEqual(encrypted.read_bytes(), original)

            output = root / 'decrypted.zip'
            subprocess.run(
                [age, '--decrypt', '-i', str(new_key), '-o', str(output), str(encrypted)],
                check=True,
            )
            self.assertEqual(output.read_bytes(), plain.read_bytes())
            old_result = subprocess.run(
                [age, '--decrypt', '-i', str(old_key), str(encrypted)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            self.assertNotEqual(old_result.returncode, 0)

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which('age')
        and shutil.which('ssh-keygen')
        and shutil.which('runuser'),
        'rotation privilege-boundary test requires root, age, and runuser',
    )
    def test_key_rotation_cannot_follow_a_swapped_ancestor_symlink(self):
        age = shutil.which('age')
        user = 'nobody'
        uid = int(subprocess.check_output(['id', '-u', user], text=True))
        gid = int(subprocess.check_output(['id', '-g', user], text=True))
        with tempfile.TemporaryDirectory(dir='/dev/shm') as tmp:
            root = Path(tmp)
            original_dir = root / 'original'
            protected_dir = root / 'protected'
            work_dir = root / 'work'
            original_dir.mkdir()
            protected_dir.mkdir()
            work_dir.mkdir()
            os.chown(root, uid, gid)
            os.chown(original_dir, uid, gid)

            key = root / 'key'
            subprocess.run(
                ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                check=True,
            )
            recipient = key.with_suffix('.pub').read_text(encoding='utf-8')
            plain = root / 'configs.zip'
            with zipfile.ZipFile(plain, 'w') as archive:
                archive.writestr('configs/restic-password', 'restic')
                archive.writestr('configs/rclone.conf', 'rclone')
                archive.writestr('configs/config.yaml', 'version: 1\n')

            original = original_dir / 'archive.age'
            subprocess.run(
                [age, '--encrypt', '-R', '-', '-o', str(original), str(plain)],
                input=recipient,
                text=True,
                check=True,
            )
            os.chown(original, uid, gid)
            original_bytes = original.read_bytes()
            protected = protected_dir / 'archive.age'
            protected.write_bytes(b'root-owned-sentinel')
            protected.chmod(0o600)
            link = root / 'current'
            link.symlink_to(original_dir, target_is_directory=True)

            env = os.environ.copy()
            env.update({
                'NEW_RECIPIENT': recipient.strip(),
                'SWAP_LINK': str(link),
                'SWAP_TARGET': str(protected_dir),
                'SUDO_USER': user,
            })
            command = (
                'RUNTIME_DIR=/dev/shm; '
                f'WORK_DIR={shlex.quote(str(work_dir))}; '
                'trap cleanup EXIT; '
                'read_recipient() { '
                'ln -sfn "$SWAP_TARGET" "$SWAP_LINK"; '
                'RECIPIENT="$NEW_RECIPIENT"; }; '
                f'rotate_key {shlex.quote(str(link / "archive.age"))}'
            )
            result = run_sourced(
                'backup-configs.sh', command,
                input_text=key.read_text(encoding='utf-8'), env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(protected.read_bytes(), b'root-owned-sentinel')
            self.assertEqual(original.read_bytes(), original_bytes)

    def test_weekly_maintenance_waits_for_the_global_lock(self):
        unit = (
            ROOT / 'systemd' / 'homelab-backup-maintenance.service'
        ).read_text(encoding='utf-8')
        self.assertNotIn('--no-wait', unit)

    def test_repository_check_runs_as_maintenance_cleanup(self):
        unit = (
            ROOT / 'systemd' / 'homelab-backup-maintenance.service'
        ).read_text(encoding='utf-8')
        self.assertIn(
            'ExecStopPost=/usr/local/sbin/backupctl check',
            unit,
        )
        self.assertEqual(unit.count('ExecStart='), 1)


if __name__ == '__main__':
    unittest.main()
