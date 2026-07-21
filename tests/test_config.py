import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import config, manifest as manifest_module
from tests.helpers import manifest


class ManifestValidationTests(unittest.TestCase):
    def test_validation_precedence_keeps_consistency_before_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            value['consistency'] = {'mode': 'invalid'}
            value['compose'] = []
            with self.assertRaisesRegex(
                ValueError, 'invalid consistency.mode',
            ):
                manifest_module.validate_manifest(value)

    def test_version_must_be_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in (None, 2, True):
                value = manifest(root)
                if version is None:
                    value.pop('version')
                else:
                    value['version'] = version
                with self.subTest(version=version), \
                        self.assertRaisesRegex(ValueError, 'version must be 1'):
                    manifest_module.validate_manifest(value)

    def test_impossible_calendar_schedule_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), schedule={'cron': '0 0 31 2 *'})
            with self.assertRaisesRegex(ValueError, 'never matches'):
                manifest_module.validate_manifest(value)

    def test_service_must_be_a_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                manifest_module.validate_manifest(manifest(Path(tmp), service=123))

    def test_consistency_schema_is_validated(self):
        invalid = (
            [],
            {'mode': 'stop', 'services': 'db'},
            {'mode': 'hooks', 'before': 'echo freeze'},
            {'mode': 'hooks', 'after': [1]},
            {'mode': 'stop', 'timeout': True},
            {'mode': 'stop', 'timeout': 0},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for consistency in invalid:
                with self.subTest(consistency=consistency):
                    with self.assertRaises(ValueError):
                        manifest_module.validate_manifest(manifest(root, consistency=consistency))

    def test_boolean_is_not_a_retention_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                manifest_module.validate_manifest(manifest(Path(tmp), retention={'keep_last': True}))

    def test_sources_must_be_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                manifest_module.validate_manifest(manifest(Path(tmp), sources=[]))

    def test_source_id_must_be_a_safe_single_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            for unsafe in ('/etc', '../etc', 'a/b', '.', '..'):
                with self.subTest(unsafe=unsafe):
                    value = manifest(Path(tmp), sources={
                        'paths': [{'id': unsafe, 'path': 'data'}],
                        'volumes': [],
                    })
                    with self.assertRaises(ValueError):
                        manifest_module.validate_manifest(value)

    def test_path_and_volume_required_fields_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_sources = (
                {'paths': [{'id': 'data'}], 'volumes': []},
                {'paths': [], 'volumes': [{'id': 'data'}]},
                {'paths': [], 'volumes': [
                    {'id': 'data', 'name': 'one', 'compose_volume': 'two'}
                ]},
            )
            for sources in invalid_sources:
                with self.subTest(sources=sources):
                    with self.assertRaises(ValueError):
                        manifest_module.validate_manifest(manifest(root, sources=sources))

    def test_docker_volume_name_cannot_be_a_host_path_or_mount_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for unsafe in ('/etc', '../data', 'name,readonly', 'name:/dst'):
                with self.subTest(unsafe=unsafe):
                    value = manifest(root, sources={
                        'paths': [],
                        'volumes': [{'id': 'data', 'name': unsafe}],
                    })
                    with self.assertRaisesRegex(ValueError, 'Docker volume name'):
                        manifest_module.validate_manifest(value)

    def test_unknown_manifest_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = (
                {'unexpected': True},
                {'compose': {'files': ['compose.yaml'], 'file': 'compose.yml'}},
                {'sources': {'path': [], 'paths': [], 'volumes': []}},
                {'sources': {
                    'paths': [{'id': 'data', 'path': 'data', 'excludes': ['secret']}],
                    'volumes': [],
                }},
                {'sources': {
                    'paths': [],
                    'volumes': [{'id': 'db', 'name': 'db', 'optional': True}],
                }},
            )
            for override in invalid:
                with self.subTest(override=override):
                    with self.assertRaisesRegex(ValueError, 'unsupported'):
                        manifest_module.validate_manifest(manifest(root, **override))

    def test_hooks_are_only_allowed_in_hooks_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for mode in ('none', 'stop'):
                with self.subTest(mode=mode):
                    value = manifest(root, consistency={
                        'mode': mode, 'before': ['echo freeze'], 'after': [],
                    })
                    with self.assertRaisesRegex(ValueError, 'only valid with mode hooks'):
                        manifest_module.validate_manifest(value)

    def test_duplicate_or_overlapping_path_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for second in ('data', 'data/child'):
                with self.subTest(second=second):
                    value = manifest(root, sources={
                        'paths': [
                            {'id': 'first', 'path': './data'},
                            {'id': 'second', 'path': second},
                        ],
                        'volumes': [],
                    })
                    with self.assertRaisesRegex(ValueError, 'overlapping path target'):
                        manifest_module.validate_manifest(value)

    def test_manifest_directory_must_match_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp))
            value['_dir'] = str(Path(tmp) / 'other-name')
            with self.assertRaisesRegex(ValueError, 'directory'):
                manifest_module.validate_manifest(value)


class GlobalConfigValidationTests(unittest.TestCase):
    @staticmethod
    def config_data(root):
        return {
            'version': 1,
            'host_id': 'host', 'repository': 'repo',
            'password_file': str(root / 'password'),
            'rclone_config': str(root / 'rclone.conf'),
            'cache_root': str(root / 'cache'),
            'volume_helper_image': 'helper',
            'state_root': str(root / 'state'),
            'lock_file': str(root / 'lock'),
            'services_root': str(root / 'services'),
            'staging_root': str(root / 'staging'),
            'restore_root': str(root / 'restore'),
            'trusted_data_roots': [str(root / 'services')],
        }

    def test_global_version_must_be_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            for version in (None, 2, True):
                config_data = self.config_data(root)
                if version is None:
                    config_data.pop('version')
                else:
                    config_data['version'] = version
                with self.subTest(version=version), \
                        mock.patch.object(config, 'CFG', config_path), \
                        mock.patch.object(config, 'load_yaml', return_value=config_data), \
                        self.assertRaisesRegex(SystemExit, '1'):
                    config.cfg()

    def test_installed_release_helper_image_overrides_persistent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            value = self.config_data(root)
            with mock.patch.object(config, 'CFG', config_path), \
                    mock.patch.object(config, 'load_yaml', return_value=value), \
                    mock.patch.dict(
                        'os.environ',
                        {
                            'HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE':
                                'homelab/volume-rsync:release.test',
                        },
                    ):
                loaded = config.cfg()

        self.assertEqual(
            loaded['volume_helper_image'],
            'homelab/volume-rsync:release.test',
        )

    def test_trusted_data_roots_are_required_absolute_and_non_overlapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            cases = (None, [], ['relative'], [str(root / 'data'), str(root / 'data/app')])
            for roots in cases:
                value = self.config_data(root)
                if roots is None:
                    value.pop('trusted_data_roots')
                else:
                    value['trusted_data_roots'] = roots
                with self.subTest(roots=roots), \
                        mock.patch.object(config, 'CFG', config_path), \
                        mock.patch.object(config, 'load_yaml', return_value=value), \
                        self.assertRaisesRegex(SystemExit, '1'):
                    config.cfg()

    def test_optional_sections_must_be_mappings_with_known_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            invalid = (
                {'rclone': ['not-a-mapping']},
                {'check': 'not-a-mapping'},
                {'rclone': {'unknown': 'value'}},
                {'check': {'unknown': 'value'}},
                {'rclone': {'bwlimit': 10}},
                {'check': {'read_data_subset': 5}},
            )
            for override in invalid:
                values = dict(self.config_data(root), **override)
                with self.subTest(override=override), \
                        mock.patch.object(config, 'CFG', config_path), \
                        mock.patch.object(config, 'load_yaml', return_value=values), \
                        self.assertRaises(SystemExit):
                    config.cfg()

    def test_unknown_global_config_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            values = dict(self.config_data(root), unexpected=True)
            with mock.patch.object(config, 'CFG', config_path), \
                    mock.patch.object(config, 'load_yaml', return_value=values), \
                    self.assertRaises(SystemExit):
                config.cfg()

    def test_optional_section_errors_precede_root_separation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            values = self.config_data(root)
            values['rclone'] = []
            values['staging_root'] = values['restore_root']
            error = StringIO()
            with mock.patch.object(config, 'CFG', config_path), \
                    mock.patch.object(config, 'load_yaml', return_value=values), \
                    redirect_stderr(error), self.assertRaises(SystemExit):
                config.cfg()

            self.assertEqual(
                error.getvalue(),
                f'ERROR: {config_path}: rclone must be a mapping\n',
            )

    def test_primary_roots_must_be_pairwise_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            base = self.config_data(root)
            names = ('services_root', 'staging_root', 'restore_root')
            for pair_index, left_name in enumerate(names):
                for right_name in names[pair_index + 1:]:
                    for relation in ('equal', 'left-parent', 'right-parent'):
                        roots = {name: root / name for name in names}
                        shared = root / f'{left_name}-{right_name}'
                        if relation == 'equal':
                            roots[left_name] = shared
                            roots[right_name] = shared
                        elif relation == 'left-parent':
                            roots[left_name] = shared
                            roots[right_name] = shared / 'child'
                        else:
                            roots[left_name] = shared / 'child'
                            roots[right_name] = shared
                        config_data = dict(base, **{
                            name: str(path) for name, path in roots.items()
                        })
                        with self.subTest(pair=(left_name, right_name), relation=relation), \
                                mock.patch.object(config, 'CFG', config_path), \
                                mock.patch.object(config, 'load_yaml', return_value=config_data):
                            with self.assertRaises(SystemExit):
                                config.cfg()

    def test_destructive_roots_must_not_overlap_control_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'config.yaml'
            config_path.touch()
            base = self.config_data(root)
            destructive_roots = ('staging_root', 'restore_root')
            protected_paths = (
                'password_file', 'rclone_config', 'cache_root',
                'state_root', 'lock_file',
            )
            for destructive in destructive_roots:
                for protected in protected_paths:
                    for relation in ('equal', 'control-inside', 'root-inside'):
                        shared = root / f'{destructive}-{protected}'
                        values = dict(base)
                        if relation == 'equal':
                            values[destructive] = str(shared)
                            values[protected] = str(shared)
                        elif relation == 'control-inside':
                            values[destructive] = str(shared)
                            values[protected] = str(shared / 'control')
                        else:
                            values[destructive] = str(shared / 'work')
                            values[protected] = str(shared)
                        with self.subTest(
                            destructive=destructive,
                            protected=protected,
                            relation=relation,
                        ), mock.patch.object(config, 'CFG', config_path), \
                                mock.patch.object(config, 'load_yaml', return_value=values):
                            with self.assertRaises(SystemExit):
                                config.cfg()


if __name__ == '__main__':
    unittest.main()
