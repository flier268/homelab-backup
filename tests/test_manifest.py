import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from homelab_backup import manifest as manifest_module
from tests.helpers import manifest


class ComposeControlTests(unittest.TestCase):
    def test_compose_run_uses_only_explicit_protected_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, compose={
                'files': ['compose.yaml'], 'env_file': 'compose.env',
            })
            env_file = Path(value['_dir']) / 'compose.env'
            env_file.write_text('IMAGE_TAG=stable\n', encoding='utf-8')
            env_file.chmod(0o600)
            captured = {}

            def runner(command, **kwargs):
                captured['command'] = command
                captured['env'] = kwargs['env']
                return mock.Mock(stdout='{}')

            with mock.patch.dict(os.environ, {
                'COMPOSE_FILE': '/tmp/evil.yaml',
                'COMPOSE_ENV_FILES': '/tmp/evil.env',
                'UNTRUSTED_VALUE': 'evil',
            }):
                manifest_module.compose_run(
                    value, ['config'], runner=runner, capture=True,
                )

            command = captured['command']
            self.assertIn(str(Path(value['_dir']) / 'compose.yaml'), command)
            self.assertIn(str(env_file), command)
            self.assertNotIn('/tmp/evil.yaml', command)
            self.assertNotIn('/tmp/evil.env', command)
            self.assertFalse(any(
                key.startswith('COMPOSE_') for key in captured['env']
            ))
            self.assertNotIn('UNTRUSTED_VALUE', captured['env'])

    def test_compose_run_rejects_writable_or_symlink_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            compose = Path(value['_dir']) / 'compose.yaml'
            compose.chmod(0o666)
            with self.assertRaisesRegex(ValueError, 'group/world writable'):
                manifest_module.compose_cmd(value)

            compose.unlink()
            outside = root / 'outside.yaml'
            outside.write_text('services: {}\n', encoding='utf-8')
            outside.chmod(0o600)
            compose.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                manifest_module.compose_cmd(value)

    def test_missing_env_file_is_rejected_and_implicit_dotenv_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root)
            dotenv = Path(value['_dir']) / '.env'
            dotenv.write_text('COMPOSE_FILE=/tmp/evil.yaml\n', encoding='utf-8')
            command = manifest_module.compose_cmd(value)
            self.assertIn('/dev/null', command)

            value['compose'] = {'env_file': 'missing.env'}
            with self.assertRaises(FileNotFoundError):
                manifest_module.compose_cmd(value)


class ManifestSelectionTests(unittest.TestCase):
    def test_manifest_without_valid_service_remains_available_for_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            target = services / 'Unindexed Service'
            target.mkdir(parents=True)
            services.chmod(0o755)
            target.chmod(0o755)
            (target / 'backup.yaml').write_text(
                'version: 1\nname: "Unindexed Service"\n',
                encoding='utf-8',
            )
            (target / 'backup.yaml').chmod(0o600)

            values = manifest_module.manifests(
                {'services_root': str(services)},
                include_disabled=True,
            )

        self.assertEqual(len(values), 1)
        with self.assertRaisesRegex(ValueError, 'missing service'):
            manifest_module.validate_manifest(values[0])

    def test_manifest_consumers_use_the_same_scan_helper(self):
        loaded = [{
            'version': 1,
            'service': 'demo',
            'enabled': True,
            '_path': '/services/demo/backup.yaml',
            '_dir': '/services/demo',
            '_relative_dir': 'demo',
        }]
        scan_result = (loaded, {'demo': loaded}, [])
        with mock.patch.object(
                manifest_module, '_manifest_scan', return_value=scan_result,
        ) as scan:
            values = manifest_module.manifests({})
            selected = manifest_module.find_manifest({}, 'demo')

        self.assertEqual(values, loaded)
        self.assertIs(selected, loaded[0])
        self.assertEqual(scan.call_count, 2)

    def test_nested_manifest_with_spaced_directories_is_discovered_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            target = services / 'Minecraft' / 'Advent of Ascension Plus-2026'
            target.mkdir(parents=True)
            services.chmod(0o755)
            (services / 'Minecraft').chmod(0o755)
            target.chmod(0o755)
            (target / 'backup.yaml').write_text(
                'version: 1\n'
                'service: advent-plus\n'
                'name: "Advent of Ascension Plus-2026"\n',
                encoding='utf-8',
            )
            (target / 'backup.yaml').chmod(0o600)

            values = manifest_module.manifests({
                'services_root': str(services),
            })
            selected = manifest_module.manifest(
                {'services_root': str(services)}, 'advent-plus',
            )

        self.assertEqual(len(values), 1)
        self.assertEqual(
            selected['_relative_dir'],
            'Minecraft/Advent of Ascension Plus-2026',
        )
        self.assertEqual(selected['service'], 'advent-plus')

    def test_duplicate_ids_are_isolated_while_other_services_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            for relative, service, enabled in (
                ('Games/One', 'duplicate', True),
                ('Archive/Two', 'duplicate', False),
                ('Other/Good', 'good', True),
            ):
                target = services / relative
                target.mkdir(parents=True)
                for parent in (target, *target.parents):
                    if parent == Path(tmp):
                        break
                    parent.chmod(0o755)
                (target / 'backup.yaml').write_text(
                    f'version: 1\nservice: {service}\n'
                    f'enabled: {str(enabled).lower()}\n',
                    encoding='utf-8',
                )
                (target / 'backup.yaml').chmod(0o600)
            errors = []

            values = manifest_module.manifests(
                {'services_root': str(services)},
                on_error=lambda path, err: errors.append((path, err)),
            )

            with self.assertRaisesRegex(ValueError, 'duplicate service ID'):
                manifest_module.manifest(
                    {'services_root': str(services)}, 'duplicate',
                )

        self.assertEqual([value['service'] for value in values], ['good'])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(
            'duplicate service ID' in str(error) for _, error in errors
        ))

    def test_directory_symlink_is_not_followed_during_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            outside = root / 'outside' / 'Nested'
            outside.mkdir(parents=True)
            services.mkdir()
            services.chmod(0o755)
            outside.parent.chmod(0o755)
            outside.chmod(0o755)
            (outside / 'backup.yaml').write_text(
                'version: 1\nservice: outside\n', encoding='utf-8',
            )
            (outside / 'backup.yaml').chmod(0o600)
            (services / 'linked').symlink_to(outside.parent, target_is_directory=True)

            values = manifest_module.manifests({
                'services_root': str(services),
            })

        self.assertEqual(values, [])

    def test_invalid_falsey_enabled_is_reported_instead_of_treated_as_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            for name, enabled in (('zero', '0'), ('null', 'null')):
                target = services / name
                target.mkdir(parents=True)
                services.chmod(0o755)
                target.chmod(0o755)
                (target / 'backup.yaml').write_text(
                    f'version: 1\nservice: {name}\nenabled: {enabled}\n',
                    encoding='utf-8',
                )
                (target / 'backup.yaml').chmod(0o600)
            errors = []

            values = manifest_module.manifests(
                {'services_root': str(services)},
                on_error=lambda path, err: errors.append((path, err)),
            )

        self.assertEqual(values, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all('enabled must be boolean' in str(err) for _, err in errors))

    def test_only_exact_false_disables_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            target = services / 'disabled'
            target.mkdir(parents=True)
            services.chmod(0o755)
            target.chmod(0o755)
            (target / 'backup.yaml').write_text(
                'version: 1\nservice: disabled\nenabled: false\n',
                encoding='utf-8',
            )
            (target / 'backup.yaml').chmod(0o600)

            self.assertEqual(
                manifest_module.manifests({'services_root': str(services)}),
                [],
            )
            self.assertEqual(
                len(manifest_module.manifests(
                    {'services_root': str(services)}, include_disabled=True,
                )),
                1,
            )

    def test_explicit_manifest_does_not_load_unrelated_broken_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            bad = services / 'bad'
            good = services / 'good'
            bad.mkdir(parents=True)
            good.mkdir()
            services.chmod(0o755)
            bad.chmod(0o755)
            good.chmod(0o755)
            (bad / 'backup.yaml').write_text('- malformed\n', encoding='utf-8')
            (good / 'backup.yaml').write_text(
                'version: 1\nservice: good\n', encoding='utf-8',
            )
            (bad / 'backup.yaml').chmod(0o600)
            (good / 'backup.yaml').chmod(0o600)

            value = manifest_module.manifest(
                {'services_root': str(services)}, 'good',
            )

        self.assertEqual(value['service'], 'good')
        self.assertEqual(Path(value['_path']).parent.name, 'good')

    def test_explicit_manifest_ignores_broken_leaf_named_like_service_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            broken = services / 'good'
            target = services / 'Nested' / 'Friendly Name'
            broken.mkdir(parents=True)
            target.mkdir(parents=True)
            services.chmod(0o755)
            broken.chmod(0o755)
            (services / 'Nested').chmod(0o755)
            target.chmod(0o755)
            (broken / 'backup.yaml').write_text(
                '- malformed\n', encoding='utf-8',
            )
            (target / 'backup.yaml').write_text(
                'version: 1\nservice: good\n', encoding='utf-8',
            )
            (broken / 'backup.yaml').chmod(0o600)
            (target / 'backup.yaml').chmod(0o600)

            value = manifest_module.manifest(
                {'services_root': str(services)}, 'good',
            )

        self.assertEqual(value['service'], 'good')
        self.assertEqual(value['_relative_dir'], 'Nested/Friendly Name')

    def test_explicit_manifest_rejects_unsafe_service_name_before_loading(self):
        with mock.patch.object(manifest_module, '_load_manifest_yaml') as load_mock, \
                self.assertRaises(ValueError):
            manifest_module.manifest(
                {'services_root': '/srv/services'}, '../outside',
            )

        load_mock.assert_not_called()

    def test_explicit_manifest_rejects_mismatched_service_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            target = services / 'good'
            target.mkdir(parents=True)
            services.chmod(0o755)
            target.chmod(0o755)
            (target / 'backup.yaml').write_text(
                'version: 1\nservice: other\n', encoding='utf-8',
            )
            (target / 'backup.yaml').chmod(0o600)

            with self.assertRaises(ValueError):
                manifest_module.manifest(
                    {'services_root': str(services)}, 'good',
                )

    def test_batch_scan_reports_symlink_leaf_before_loading_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / 'services'
            service = services / 'demo'
            service.mkdir(parents=True)
            services.chmod(0o755)
            service.chmod(0o755)
            outside = root / 'outside.yaml'
            outside.write_text(
                'version: 1\nservice: demo\n', encoding='utf-8',
            )
            (service / 'backup.yaml').symlink_to(outside)
            errors = []

            values = manifest_module.manifests(
                {'services_root': str(services)},
                on_error=lambda path, err: errors.append((path, err)),
            )

        self.assertEqual(values, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('regular file', str(errors[0][1]))

    def test_batch_scan_reports_unprivileged_writable_control_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = Path(tmp) / 'services'
            service = services / 'demo'
            service.mkdir(parents=True)
            services.chmod(0o755)
            (service / 'backup.yaml').write_text(
                'version: 1\nservice: demo\n', encoding='utf-8',
            )
            service.chmod(0o777)
            errors = []

            try:
                values = manifest_module.manifests(
                    {'services_root': str(services)},
                    on_error=lambda path, err: errors.append((path, err)),
                )
            finally:
                service.chmod(0o755)

        self.assertEqual(values, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('group/world writable', str(errors[0][1]))


if __name__ == '__main__':
    unittest.main()
