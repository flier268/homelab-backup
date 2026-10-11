"""Stop-the-bleeding change 1: no bind probe, no image-ID pinned helper."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import backup, restore, staging, storage
from homelab_backup.common import CommandError

ROOT = Path(__file__).resolve().parents[1]
TAG = 'homelab/volume-rsync:release.test'


class BindProbeRemovedTests(unittest.TestCase):
    def test_bind_probe_no_longer_exists_anywhere(self):
        for module in (storage, staging, backup, restore):
            self.assertFalse(
                hasattr(module, 'validate_docker_bind_probe'), module.__name__,
            )
        for path in (ROOT / 'homelab_backup').glob('*.py'):
            self.assertNotIn(
                'bind_probe', path.read_text(encoding='utf-8'), path.name,
            )

    def _stage_context(self, resolved):
        config = {'volume_helper_image': TAG}
        value = {'service': 'demo', 'consistency': {'mode': 'stop'}}
        with mock.patch.object(staging, 'requires_docker', return_value=True), \
                mock.patch.object(staging, 'validate_docker_environment'), \
                mock.patch.object(staging, 'compose_model', return_value={}), \
                mock.patch.object(staging, 'validate_runtime_sources'), \
                mock.patch.object(
                    staging, 'resolved_volume_sources', return_value=resolved,
                ), \
                mock.patch.object(staging, 'compose_identity', return_value={}), \
                mock.patch.object(
                    staging, 'ensure_volume_helper_image',
                ) as ensure:
            staging._build_stage_context(config, value, Path('/x'), 'stop', False)
        return ensure, config

    def test_docker_stage_without_volumes_needs_no_helper_image(self):
        ensure, _ = self._stage_context([])
        ensure.assert_not_called()

    def test_docker_stage_with_volumes_ensures_helper_image(self):
        ensure, config = self._stage_context([{'id': 'data'}])
        ensure.assert_called_once_with(config)


class EnsureHelperImageTests(unittest.TestCase):
    def test_present_image_is_not_rebuilt(self):
        with mock.patch.object(storage, 'run') as run:
            self.assertEqual(
                storage.ensure_volume_helper_image({'volume_helper_image': TAG}),
                TAG,
            )
        run.assert_called_once_with(
            ['docker', 'image', 'inspect', TAG], capture=True,
        )

    def test_missing_image_is_rebuilt_from_bundled_dockerfile(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ['docker', 'image', 'inspect']:
                raise CommandError(cmd, 1, stderr='No such image')
            return mock.Mock(stdout='')

        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / 'Dockerfile.volume-rsync'
            dockerfile.write_text('FROM scratch\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {
                storage.HELPER_DOCKERFILE_ENV: str(dockerfile),
            }), mock.patch.object(storage, 'run', side_effect=fake_run), \
                    redirect_stderr(StringIO()) as err:
                storage.ensure_volume_helper_image({'volume_helper_image': TAG})
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[1][:6],
            ['docker', 'build', '--tag', TAG, '--file', str(dockerfile)],
        )
        self.assertIn('rebuilding', err.getvalue())

    def test_missing_image_without_dockerfile_fails_with_remediation(self):
        env = {k: v for k, v in os.environ.items()
               if k != storage.HELPER_DOCKERFILE_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(
                    storage, 'run',
                    side_effect=CommandError(['docker'], 1, stderr='no'),
                ), self.assertRaises(RuntimeError) as raised:
            storage.ensure_volume_helper_image({'volume_helper_image': TAG})
        self.assertIn('install.sh', str(raised.exception))

    def test_relative_dockerfile_env_is_ignored(self):
        with mock.patch.dict(os.environ, {
            storage.HELPER_DOCKERFILE_ENV: 'Dockerfile.volume-rsync',
        }):
            self.assertIsNone(storage.volume_helper_dockerfile())


class LauncherHelperMetadataTests(unittest.TestCase):
    def _launch(self, metadata, *, bundle_dockerfile):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        local = Path(tmp) / 'usr' / 'local'
        launcher = local / 'sbin' / 'backupctl'
        install_root = local / 'lib' / 'homelab-backup'
        release = install_root / 'releases' / 'release.test'
        package = release / 'app' / 'homelab_backup'
        launcher.parent.mkdir(parents=True)
        package.mkdir(parents=True)
        shutil.copy2(ROOT / 'backupctl', launcher)
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'cli.py').write_text(
            'import os\n'
            'def main():\n'
            '    print(os.environ.get('
            '"HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE"))\n'
            '    print(os.environ.get('
            '"HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_DOCKERFILE"))\n',
            encoding='utf-8',
        )
        (release / 'volume-helper-image').write_text(
            json.dumps(metadata) + '\n', encoding='utf-8',
        )
        (release / 'volume-helper-image').chmod(0o600)
        (release / '.lease').touch(mode=0o644)
        if bundle_dockerfile:
            (release / 'Dockerfile.volume-rsync').write_text(
                'FROM scratch\n', encoding='utf-8',
            )
        subprocess.run(
            [sys.executable, '-m', 'venv', '--without-pip',
             str(release / 'venv')],
            check=True,
        )
        (install_root / 'current').symlink_to('releases/release.test')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('HOMELAB_BACKUP_')}
        return release, subprocess.run(
            [str(launcher)], text=True, capture_output=True, check=False,
            env=env,
        )

    def test_launcher_exports_tag_not_image_id(self):
        release, result = self._launch(
            {'tag': TAG, 'image_id': 'sha256:' + 'a' * 64},
            bundle_dockerfile=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        image, dockerfile = result.stdout.splitlines()
        self.assertEqual(image, TAG)
        self.assertEqual(
            Path(dockerfile).resolve(),
            (release / 'Dockerfile.volume-rsync').resolve(),
        )

    def test_launcher_accepts_tag_only_metadata_without_dockerfile(self):
        _, result = self._launch({'tag': TAG}, bundle_dockerfile=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [TAG, 'None'])

    def test_launcher_still_rejects_unsafe_tag(self):
        _, result = self._launch(
            {'tag': 'evil/image:latest'}, bundle_dockerfile=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('helper image metadata is invalid', result.stderr)

    def test_installer_bundles_helper_dockerfile_in_release(self):
        script = (ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertIn(
            'install -m 0644 Dockerfile.volume-rsync '
            '"$RELEASE_NEXT/Dockerfile.volume-rsync"',
            script,
        )


def dockerfile_instructions(text):
    """Yield (instruction, arguments) with continuation lines joined."""
    logical = ''
    for line in text.splitlines():
        stripped = line.strip()
        if not logical and (not stripped or stripped.startswith('#')):
            continue
        if stripped.endswith('\\'):
            logical += stripped[:-1] + ' '
            continue
        logical += stripped
        instruction, _, arguments = ' '.join(logical.split()).partition(' ')
        yield instruction.upper(), arguments
        logical = ''
    if logical:
        instruction, _, arguments = ' '.join(logical.split()).partition(' ')
        yield instruction.upper(), arguments


class HelperDockerfilePinTests(unittest.TestCase):
    """Every input of the helper image is pinned, so rebuilds are repeatable."""

    def setUp(self):
        self.instructions = list(dockerfile_instructions(
            (ROOT / 'Dockerfile.volume-rsync').read_text(encoding='utf-8'),
        ))

    def test_base_image_is_pinned_by_tag_and_digest(self):
        bases = [args for name, args in self.instructions if name == 'FROM']
        self.assertTrue(bases)
        for base in bases:
            self.assertRegex(
                base, r'^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+'
                      r'@sha256:[0-9a-f]{64}(\s+AS\s+\S+)?$',
            )

    def test_every_apk_package_has_an_exact_version(self):
        packages = []
        for name, args in self.instructions:
            if name != 'RUN':
                continue
            for command in re.split(r'&&|;', args):
                words = command.split()
                if words[:2] != ['apk', 'add']:
                    continue
                packages += [word for word in words[2:] if not word.startswith('-')]
        self.assertTrue(packages)
        for package in packages:
            with self.subTest(package=package):
                self.assertRegex(package, r'^[a-z0-9][a-z0-9._+-]*=[0-9][^=\s]*$')

    def test_parser_flags_unpinned_input(self):
        text = 'FROM alpine:3.22\nRUN apk add --no-cache \\\n    rsync \\\n    acl=1-r0\n'
        instructions = list(dockerfile_instructions(text))
        self.assertEqual(instructions, [
            ('FROM', 'alpine:3.22'),
            ('RUN', 'apk add --no-cache rsync acl=1-r0'),
        ])


if __name__ == '__main__':
    unittest.main()
