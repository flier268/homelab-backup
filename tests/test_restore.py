import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homelab_backup import restore as backupctl


class RepositoryBoundaryTests(unittest.TestCase):
    def test_invalid_service_tag_is_rejected(self):
        result = mock.Mock(stdout=json.dumps([{'tags': ['service:/etc']}]))
        with mock.patch.object(backupctl, 'run', return_value=result), \
                mock.patch.object(backupctl, 'restic_env', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'invalid service tag'):
                backupctl.repository_services({'host_id': 'host'})

class RestoredManifestTests(unittest.TestCase):

    def test_invalid_snapshot_manifest_does_not_replace_local_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restored = root / 'restored'
            meta = restored / '_meta'
            meta.mkdir(parents=True)
            (meta / 'backup.yaml').write_text(
                'version: 1\nservice: wrong\n', encoding='utf-8',
            )
            service_dir = root / 'services' / 'demo'
            service_dir.mkdir(parents=True)
            target = service_dir / 'backup.yaml'
            original = 'original local manifest\n'
            target.write_text(original, encoding='utf-8')

            with self.assertRaises(SystemExit):
                backupctl.prepare_restored_manifest(
                    {'services_root': str(root / 'services')},
                    'demo', restored, policy='restore',
                )

            self.assertEqual(target.read_text(encoding='utf-8'), original)

class RestoreCommandTests(unittest.TestCase):

    def test_noninteractive_apply_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=True, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'validate_docker_environment'), \
                mock.patch.object(backupctl, 'validate_trusted_roots'), \
                mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaisesRegex(SystemExit, '1'):
                backupctl.cmd_restore({'trusted_data_roots': []}, args)

        restore_mock.assert_not_called()

    def test_noninteractive_download_only_requires_explicit_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=False, apply=False, start=False,
            restore_manifest=False, keep_manifest=False, snapshot='latest',
        )
        with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(backupctl, 'restore_one') as restore_mock:
            with self.assertRaises(SystemExit):
                backupctl.cmd_restore({}, args)
        restore_mock.assert_not_called()

    def test_noninteractive_download_only_runs_with_yes(self):
        args = mock.Mock(
            services=['demo'], all=False, yes=True, apply=False, start=False,
            restore_manifest=False, keep_manifest=True, snapshot='latest',
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_data = {'host_id': 'host', 'lock_file': str(Path(tmp) / 'lock')}
            restored = (mock.Mock(), Path(tmp) / 'restored')
            with mock.patch.object(backupctl, 'repository_services', return_value=['demo']), \
                    mock.patch.object(backupctl.sys.stdin, 'isatty', return_value=False), \
                    mock.patch.object(backupctl, 'restore_one', return_value=restored) as restore_mock:
                backupctl.cmd_restore(config_data, args)
        restore_mock.assert_called_once()

if __name__ == '__main__':
    unittest.main()
