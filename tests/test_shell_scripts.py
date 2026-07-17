import os
import shlex
import shutil
import subprocess
import tempfile
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
        validation = script.index("expected = {")
        publish = script.index('publish_config_bundle "$WORK_DIR/extracted"')
        self.assertLess(validation, publish)
        for member in (
            'configs/restic-password', 'configs/rclone.conf', 'configs/config.yaml',
        ):
            self.assertIn(repr(member), script)

    def test_config_bundle_publish_has_rollback_for_partial_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source' / 'configs'
            target = root / 'target'
            (source).mkdir(parents=True)
            (target / 'rclone').mkdir(parents=True)
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


class DeploymentScriptTests(unittest.TestCase):
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

    def test_installer_anchors_relative_sources_to_its_own_directory(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('ROOT_DIR=', script)
        self.assertIn('cd -- "$ROOT_DIR"', script)

    def test_installer_installs_package(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('LIB_ROOT=/usr/local/lib/homelab-backup', script)
        self.assertNotIn('rm -rf -- "$LIB_ROOT"', script)
        self.assertIn('APP_ROOT="$LIB_ROOT/app"', script)
        self.assertIn('APP_NEXT=', script)
        self.assertIn('homelab_backup/*.py "$APP_NEXT/homelab_backup/"', script)
        self.assertIn('mv -- "$APP_NEXT" "$APP_ROOT"', script)

    def test_installer_replaces_the_application_tree_without_stale_modules(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        build = script.index('APP_NEXT=')
        publish = script.index('mv -- "$APP_NEXT" "$APP_ROOT"')
        self.assertLess(build, publish)
        self.assertIn('mv -- "$APP_ROOT" "$APP_PREVIOUS"', script)
        self.assertIn('rm -rf -- "$APP_PREVIOUS"', script)

        block = 'APP_NEXT=' + script.split(
            'APP_NEXT=', 1,
        )[1].split('install -m 0755 backupctl', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            lib_root = Path(tmp) / 'lib'
            old_package = lib_root / 'app' / 'homelab_backup'
            old_package.mkdir(parents=True)
            (old_package / 'removed.py').write_text('stale', encoding='utf-8')
            env = os.environ.copy()
            env.update({
                'LIB_ROOT': str(lib_root),
                'APP_ROOT': str(lib_root / 'app'),
            })

            subprocess.run(
                ['bash', '-euo', 'pipefail', '-c', block],
                cwd=ROOT, env=env, check=True,
            )

            self.assertFalse((old_package / 'removed.py').exists())
            self.assertTrue(
                (lib_root / 'app' / 'homelab_backup' / 'backup.py').is_file()
            )

    def test_installer_uses_locked_application_dependencies(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        requirements_input = (ROOT / 'requirements.in').read_text(encoding='utf-8')
        requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('python3-venv', script)
        self.assertIn(' age', script)
        self.assertNotIn(' minisign', script)
        self.assertIn("sys.version_info < (3, 10)", script)
        self.assertIn('VENV_ROOT="$LIB_ROOT/venv"', script)
        self.assertIn('python3 -m venv', script)
        self.assertIn('"$VENV_ROOT/bin/python" -m pip install', script)
        self.assertIn('--require-hashes', script)
        self.assertNotIn('--target', script)
        self.assertEqual(requirements_input.splitlines(), ['cronsim', 'PyYAML'])
        self.assertIn('cronsim==2.7', requirements)
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
        self.assertIn('remote -v', script)
        self.assertIn("Type PRIVATE to continue with Git", script)
        self.assertIn("[[ \"$private_confirmation\" != 'PRIVATE' ]]", script)
        self.assertLess(script.index('Type PRIVATE'), script.index('add -f --'))
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
