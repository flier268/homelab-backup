import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import common, storage
from tests.helpers import manifest


class RuntimeValidationTests(unittest.TestCase):
    def test_restore_volume_is_journaled_before_ownership_inspection(self):
        journal = []
        with mock.patch.object(
            storage, 'run', return_value=SimpleNamespace(stdout='demo_db\n'),
        ), mock.patch.object(
            storage, 'docker_volume_details', side_effect=RuntimeError('inspect failed'),
        ), self.assertRaisesRegex(RuntimeError, 'inspect failed'):
            storage.create_restore_volume(
                'demo_db', service='demo', source={'id': 'db'},
                operation_id='operation', on_created=journal.append,
            )

        self.assertEqual(journal, ['demo_db'])

    def test_restore_volume_must_carry_this_operations_label(self):
        details = {
            'Labels': {
                'io.homelab-backup.service': 'demo',
                'io.homelab-backup.source': 'db',
                'io.homelab-backup.operation': 'another-operation',
            },
        }
        with mock.patch.object(
            storage, 'run', return_value=SimpleNamespace(stdout='demo_db\n'),
        ) as run_mock, mock.patch.object(
            storage, 'docker_volume_details', return_value=details,
        ), self.assertRaisesRegex(RuntimeError, 'operation'):
            storage.create_restore_volume(
                'demo_db', service='demo', source={'id': 'db'},
                operation_id='this-operation',
            )

        self.assertFalse(any(
            call.args[0][:3] == ['docker', 'volume', 'rm']
            for call in run_mock.call_args_list
        ))

    def test_unexpected_volume_create_result_removes_the_new_volume(self):
        with mock.patch.object(
            storage, 'run',
            side_effect=[SimpleNamespace(stdout='unexpected\n'), SimpleNamespace()],
        ) as run_mock:
            with self.assertRaisesRegex(RuntimeError, 'unexpected volume'):
                storage.create_restore_volume(
                    'demo_db', service='demo', source={'id': 'db'},
                )

        self.assertEqual(
            run_mock.call_args_list[1].args[0],
            ['docker', 'volume', 'rm', 'demo_db'],
        )

    def test_docker_environment_rejects_endpoint_overrides(self):
        for variable in ('DOCKER_HOST', 'DOCKER_CONTEXT'):
            with self.subTest(variable=variable), mock.patch.dict(
                os.environ, {variable: 'attacker-controlled'}, clear=False,
            ), mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, variable):
                    storage.validate_docker_environment()

            run_mock.assert_not_called()

    def test_docker_environment_requires_local_rootful_unix_socket(self):
        cases = [
            ('"tcp://host:2375"', '[]', 'local rootful'),
            ('"unix:///run/user/1000/docker.sock"', '[]', 'local rootful'),
            ('"unix:///var/run/docker.sock"', '["name=rootless"]', 'rootless'),
        ]
        for endpoint, options, message in cases:
            with self.subTest(endpoint=endpoint), mock.patch.object(
                storage, 'run', side_effect=[
                    SimpleNamespace(stdout=endpoint), SimpleNamespace(stdout=options),
                ],
            ), self.assertRaisesRegex(RuntimeError, message):
                storage.validate_docker_environment()

    def test_required_path_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'missing'}],
                'volumes': [],
            })
            with self.assertRaisesRegex(ValueError, 'missing required source'):
                storage.validate_runtime_sources({}, value, {})

    def test_runtime_validation_rejects_include_for_file_and_symlink_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            (stack / 'file').write_text('payload', encoding='utf-8')
            (stack / 'link').symlink_to('file')

            for name in ('file', 'link'):
                with self.subTest(name=name):
                    value = manifest(root, sources={
                        'paths': [{
                            'id': name, 'path': name,
                            'include': ['payload/**'],
                        }],
                        'volumes': [],
                    })
                    with self.assertRaisesRegex(
                        ValueError, 'only supported for directory',
                    ):
                        storage.validate_runtime_sources({}, value, {})

    def test_declared_volume_is_inspected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), sources={
                'paths': [],
                'volumes': [{'id': 'db', 'name': 'demo-db'}],
            })
            with mock.patch.object(storage, 'run') as run_mock:
                storage.validate_runtime_sources({}, value, {})
            self.assertEqual(
                run_mock.call_args.args[0],
                ['docker', 'volume', 'inspect', 'demo-db'],
            )

    def test_missing_required_volume_aborts_before_docker_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{'id': 'db', 'name': 'missing-db'}],
            })

            with mock.patch.object(
                storage, 'run', side_effect=common.CommandError(
                    ['docker', 'volume', 'inspect', 'missing-db'], 1,
                    stderr='Error: No such volume: missing-db',
                ),
            ) as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, stage,
                    )

            self.assertEqual(run_mock.call_count, 1)
            self.assertEqual(
                run_mock.call_args.args[0],
                ['docker', 'volume', 'inspect', 'missing-db'],
            )

    def test_missing_optional_volume_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{
                    'id': 'db', 'name': 'missing-db', 'required': False,
                }],
            })

            with mock.patch.object(
                storage, 'run', side_effect=common.CommandError(
                    ['docker', 'volume', 'inspect', 'missing-db'], 1,
                    stderr='Error: No such volume: missing-db',
                ),
            ) as run_mock:
                storage.sync_volumes(
                    {'volume_helper_image': 'helper'}, value, stage,
                )

            self.assertEqual(run_mock.call_count, 1)

    def test_optional_volume_does_not_hide_docker_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{
                    'id': 'db', 'name': 'demo-db', 'required': False,
                }],
            })
            failure = common.CommandError(
                ['docker', 'volume', 'inspect', 'demo-db'], 1,
                stderr='permission denied while trying to connect to the Docker daemon socket',
            )
            with mock.patch.object(storage, 'run', side_effect=failure):
                with self.assertRaises(common.CommandError):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'}, value, root / 'stage',
                    )

    def test_duplicate_resolved_volumes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), sources={
                'paths': [],
                'volumes': [
                    {'id': 'direct', 'name': 'project_db'},
                    {'id': 'logical', 'compose_volume': 'db'},
                ],
            })
            model = {'volumes': {'db': {'name': 'project_db'}}}
            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(ValueError, 'duplicate Docker volume target'):
                    storage.validate_runtime_sources({}, value, model)
            run_mock.assert_not_called()

    def test_compose_resolved_volume_name_cannot_be_a_host_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = manifest(Path(tmp), sources={
                'paths': [],
                'volumes': [{'id': 'db', 'compose_volume': 'db'}],
            })
            model = {'volumes': {'db': {'name': '/etc'}}}
            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(ValueError, 'Docker volume name'):
                    storage.validate_runtime_sources({}, value, model)
            run_mock.assert_not_called()

    def test_volume_helper_uses_explicit_named_volume_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            stage.mkdir()
            value = manifest(root, sources={
                'paths': [],
                'volumes': [{'id': 'db', 'name': 'demo-db'}],
            })
            with mock.patch.object(storage, 'docker_volume_exists', return_value=True), \
                    mock.patch.object(storage, 'run') as run_mock:
                storage.sync_volumes(
                    {'volume_helper_image': 'helper'}, value, stage,
                )

            command = run_mock.call_args.args[0]
            self.assertIn('--mount', command)
            self.assertIn('type=volume,src=demo-db,dst=/src,readonly', command)
            self.assertNotIn('demo-db:/src:ro', command)
            self.assertNotIn('-v', command)
            self.assertNotIn('bind-create-src', ' '.join(command))
            self.assertNotIn('--user', command)
            self.assertNotIn('--super', command)

    def test_pre_resolved_volume_name_is_revalidated_at_docker_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            (stage / 'volumes' / 'db').mkdir(parents=True)
            source = {'id': 'db', 'name': 'demo-db'}
            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(ValueError, 'Docker volume name'):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'},
                        {'sources': {'volumes': [source]}},
                        stage,
                        restore=True,
                        resolved=[(source, '/etc')],
                    )
            run_mock.assert_not_called()

    def test_volume_restore_revalidates_staged_directory_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            referent = root / 'referent'
            referent.mkdir()
            staged = stage / 'volumes' / 'db'
            staged.parent.mkdir(parents=True)
            staged.symlink_to(referent, target_is_directory=True)
            source = {'id': 'db', 'name': 'demo-db'}

            with mock.patch.object(storage, 'run') as run_mock:
                with self.assertRaisesRegex(RuntimeError, 'real directory'):
                    storage.sync_volumes(
                        {'volume_helper_image': 'helper'},
                        {'sources': {'volumes': [source]}},
                        stage,
                        restore=True,
                        resolved=[(source, 'demo-db')],
                    )

            run_mock.assert_not_called()


class PathSyncTests(unittest.TestCase):
    def test_path_filter_args_are_ordered_with_excludes_first(self):
        source = {
            'include': ['world/**', 'server.properties'],
            'exclude': ['world/cache/**'],
        }

        self.assertEqual(storage.build_path_filter_args(source), [
            '--exclude', 'world/cache/**',
            '--include', '*/',
            '--include', 'world/**',
            '--include', 'server.properties',
            '--exclude', '*',
            '--prune-empty-dirs',
        ])
        self.assertEqual(
            storage.build_path_filter_args(
                source, protect_destination_dirs=True,
            ),
            [
                '--exclude', 'world/cache/**',
                '--filter', 'P */',
                '--include', '*/',
                '--include', 'world/**',
                '--include', 'server.properties',
                '--exclude', '*',
                '--prune-empty-dirs',
            ],
        )
        self.assertEqual(
            storage.build_path_filter_args({'exclude': ['cache/**']}),
            ['--exclude', 'cache/**'],
        )
        self.assertEqual(storage.build_path_filter_args({}), [])

    def test_include_copies_only_selected_paths_and_prunes_empty_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            (payload / 'world' / 'region').mkdir(parents=True)
            (payload / 'world' / 'cache').mkdir()
            (payload / 'junk' / 'nested').mkdir(parents=True)
            (payload / 'world' / 'region' / 'r.0.0.mca').write_text(
                'region', encoding='utf-8',
            )
            (payload / 'world' / 'cache' / 'drop.bin').write_text(
                'drop', encoding='utf-8',
            )
            (payload / 'junk' / 'nested' / 'drop.txt').write_text(
                'drop', encoding='utf-8',
            )
            (payload / 'server.properties').write_text(
                'online-mode=true', encoding='utf-8',
            )
            value = manifest(root, sources={
                'paths': [{
                    'id': 'data', 'path': 'data',
                    'include': ['world/**', 'server.properties'],
                    'exclude': ['world/cache/**'],
                }],
                'volumes': [],
            })

            storage.sync_paths(value, root / 'stage')

            archived = root / 'stage' / 'paths' / 'data'
            self.assertTrue((archived / 'world' / 'region' / 'r.0.0.mca').is_file())
            self.assertTrue((archived / 'server.properties').is_file())
            self.assertFalse((archived / 'world' / 'cache').exists())
            self.assertFalse((archived / 'junk').exists())

    def test_include_is_rejected_for_file_and_symlink_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            (stack / 'file').write_text('payload', encoding='utf-8')
            (stack / 'link').symlink_to('file')
            for name in ('file', 'link'):
                with self.subTest(name=name):
                    value = manifest(root, sources={
                        'paths': [{
                            'id': name, 'path': name, 'include': ['payload/**'],
                        }],
                        'volumes': [],
                    })
                    with self.assertRaisesRegex(
                        ValueError, 'only supported for directory',
                    ):
                        storage.sync_paths(value, root / f'stage-{name}')

    def test_filtered_size_estimate_uses_selected_rsync_file_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            (payload / 'keep').mkdir(parents=True)
            (payload / 'drop').mkdir()
            (payload / 'keep' / 'small.txt').write_text('small', encoding='utf-8')
            (payload / 'drop' / 'large.bin').write_bytes(b'x' * 1024 * 1024)
            source = {
                'id': 'data', 'path': 'data', 'include': ['keep/**'],
            }
            value = manifest(root, sources={
                'paths': [source], 'volumes': [],
            })

            estimate = storage.estimate_path_source(
                {'trusted_data_roots': [str(root)]},
                value,
                source,
                allocation_unit=4096,
            )

            self.assertEqual(estimate, 5 + 2 * 4096)

    def test_excluded_content_is_not_counted_in_size_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            (payload / 'keep').mkdir(parents=True)
            (payload / 'drop').mkdir()
            (payload / 'keep' / 'small.txt').write_text('small', encoding='utf-8')
            (payload / 'drop' / 'large.bin').write_bytes(b'x' * 1024 * 1024)
            source = {
                'id': 'data', 'path': 'data', 'exclude': ['drop/**'],
            }
            value = manifest(root, sources={
                'paths': [source], 'volumes': [],
            })

            estimate = storage.estimate_path_source(
                {'trusted_data_roots': [str(root)]},
                value,
                source,
                allocation_unit=4096,
            )

            self.assertLess(estimate, 1024 * 1024)

    def test_backup_estimate_uses_staging_filesystem_allocation_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = {'id': 'data', 'path': 'data'}
            value = manifest(root, sources={
                'paths': [source], 'volumes': [],
            })
            filesystem = SimpleNamespace(f_frsize=8192)
            with mock.patch.object(
                storage.os, 'statvfs', return_value=filesystem,
            ) as statvfs, mock.patch.object(
                storage, 'estimate_path_source', return_value=0,
            ) as estimate:
                storage.estimate_backup_size(
                    {'staging_root': str(root)},
                    value,
                    resolved=[],
                    staging_path=root / 'stage',
                )

            self.assertEqual(estimate.call_args.kwargs['allocation_unit'], 8192)
            statvfs.assert_called_once_with(root / 'stage')

    def test_filtered_size_estimate_rejects_invalid_rsync_stats(self):
        source = {'id': 'data', 'include': ['keep/**']}
        with mock.patch.object(
            storage, 'run', return_value=SimpleNamespace(stdout='invalid\n'),
        ), self.assertRaisesRegex(RuntimeError, 'invalid filtered size estimate'):
            storage._estimate_filtered_directory(1, source, 4096)

    def test_ancestor_metadata_comes_from_the_pinned_source_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'parent/data'}],
                'volumes': [],
            })
            parent = Path(value['_dir']) / 'parent'
            payload = parent / 'data'
            payload.mkdir(parents=True)
            (payload / 'value').write_text('snapshot', encoding='utf-8')
            parent.chmod(0o750)
            original_copy = storage._copy_path_source

            def copy_then_replace_parent(*args, **kwargs):
                result = original_copy(*args, **kwargs)
                parent.rename(parent.with_name('parent-original'))
                parent.mkdir(mode=0o777)
                return result

            with mock.patch.object(
                storage, '_copy_path_source', side_effect=copy_then_replace_parent,
            ):
                entry = storage.sync_paths(
                    {'trusted_data_roots': [str(root)]},
                    value,
                    root / 'stage',
                )[0]

            metadata = {
                item['path']: item for item in entry['ancestors']
            }
            self.assertEqual(metadata['demo/parent']['mode'], 0o750)

    def test_copy_reuses_source_fd_from_ancestor_metadata_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'parent/data'}],
                'volumes': [],
            })
            parent = Path(value['_dir']) / 'parent'
            payload = parent / 'data'
            payload.mkdir(parents=True)
            (payload / 'value').write_text('original', encoding='utf-8')

            with mock.patch.object(
                storage, 'open_data_path', wraps=storage.open_data_path,
            ) as reopen:
                storage.sync_paths(
                    {'trusted_data_roots': [str(root)]},
                    value,
                    root / 'stage',
                )

            # The logical path is reopened once after copying to verify that
            # it still names the pinned object, never to select what to copy.
            reopen.assert_called_once_with(
                payload, [str(root)],
            )
            archived = root / 'stage' / 'paths' / 'data' / 'value'
            self.assertEqual(archived.read_text(encoding='utf-8'), 'original')

    def test_source_override_keeps_logical_ancestor_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'parent/data'}],
                'volumes': [],
            })
            parent = Path(value['_dir']) / 'parent'
            payload = parent / 'data'
            payload.mkdir(parents=True)
            (payload / 'value').write_text('live', encoding='utf-8')
            parent.chmod(0o750)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            (snapshot / 'value').write_text('snapshot', encoding='utf-8')

            entry = storage.sync_paths(
                {'trusted_data_roots': [str(root)]},
                value,
                root / 'stage',
                source_overrides={'data': snapshot},
            )[0]

            archived = root / 'stage' / 'paths' / 'data' / 'value'
            self.assertEqual(archived.read_text(encoding='utf-8'), 'snapshot')
            metadata = {
                item['path']: item for item in entry['ancestors']
            }
            self.assertEqual(metadata['demo/parent']['mode'], 0o750)

    def test_nested_source_may_be_selected_inside_container_owned_data_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{
                    'id': 'saved',
                    'path': 'palworld/Pal/Saved',
                }],
                'volumes': [],
            })
            data_root = Path(value['_dir']) / 'palworld'
            saved = data_root / 'Pal' / 'Saved'
            saved.mkdir(parents=True)
            (saved / 'world.sav').write_text('world', encoding='utf-8')
            data_root.chmod(0o777)
            (data_root / 'Pal').chmod(0o777)
            try:
                storage.validate_runtime_sources(
                    {'trusted_data_roots': [str(root)]}, value, {},
                )
                storage.sync_paths(
                    {'trusted_data_roots': [str(root)]},
                    value,
                    root / 'stage',
                )
            finally:
                (data_root / 'Pal').chmod(0o755)
                data_root.chmod(0o755)

            self.assertEqual(
                (root / 'stage' / 'paths' / 'saved' / 'world.sav').read_text(
                    encoding='utf-8',
                ),
                'world',
            )
            entry = storage.sync_paths(
                {'trusted_data_roots': [str(root)]},
                value,
                root / 'stage-2',
            )[0]
            self.assertEqual(
                [item['path'] for item in entry['ancestors']],
                ['demo', 'demo/palworld', 'demo/palworld/Pal'],
            )
            self.assertEqual(entry['ancestors'][-1]['mode'], 0o755)

    def test_nested_source_rejects_symlinked_intermediate_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{
                    'id': 'saved',
                    'path': 'palworld/Pal/Saved',
                }],
                'volumes': [],
            })
            data_root = Path(value['_dir']) / 'palworld'
            outside = root / 'outside'
            (outside / 'Saved').mkdir(parents=True)
            data_root.mkdir()
            (data_root / 'Pal').symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, 'real directory'):
                storage.sync_paths(
                    {'trusted_data_roots': [str(root)]},
                    value,
                    root / 'stage',
                )

    def test_source_replacement_uses_pinned_fd_and_fails_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            payload.mkdir(parents=True)
            (payload / 'safe').write_text('safe', encoding='utf-8')
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'secret').write_text('secret', encoding='utf-8')
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })
            observed = {}

            def swap_source(command, **kwargs):
                observed['command'] = command
                observed['pass_fds'] = kwargs.get('pass_fds')
                payload.rename(payload.with_name('original'))
                payload.symlink_to(outside, target_is_directory=True)
                return mock.Mock(stdout='')

            with mock.patch.object(storage, 'run', side_effect=swap_source), \
                    self.assertRaisesRegex(RuntimeError, 'changed during backup'):
                storage.sync_paths(value, root / 'stage')

            self.assertTrue(any(
                str(item).startswith('/proc/self/fd/')
                for item in observed['command']
            ))
            self.assertNotIn(str(payload), observed['command'])
            self.assertTrue(observed['pass_fds'])

    def test_required_source_disappearing_during_sync_is_a_regular_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            payload.mkdir(parents=True)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })

            with mock.patch.object(
                storage, '_copy_path_source', side_effect=FileNotFoundError,
            ), self.assertRaisesRegex(ValueError, 'missing source'):
                storage.sync_paths(value, root / 'stage')

    def test_fifo_payload_is_rejected_before_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            payload.mkdir(parents=True)
            os.mkfifo(payload / 'pipe')
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })

            with mock.patch.object(storage, 'rsync') as rsync_mock:
                with self.assertRaisesRegex(ValueError, 'unsupported payload'):
                    storage.sync_paths(value, root / 'stage')
            rsync_mock.assert_not_called()

    def test_payload_hardlinks_are_preserved_inside_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / 'demo' / 'data'
            payload.mkdir(parents=True)
            first = payload / 'first'
            second = payload / 'second'
            first.write_text('shared', encoding='utf-8')
            os.link(first, second)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}], 'volumes': [],
            })

            storage.sync_paths(value, root / 'stage')

            archived = root / 'stage' / 'paths' / 'data'
            self.assertEqual(
                (archived / 'first').stat().st_ino,
                (archived / 'second').stat().st_ino,
            )


    def test_symlink_sources_are_archived_as_links_without_following_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            referent_file = root / 'referent-file'
            referent_file.write_text('secret', encoding='utf-8')
            referent_dir = root / 'referent-dir'
            referent_dir.mkdir()
            (referent_dir / 'secret').write_text('secret', encoding='utf-8')
            (stack / 'file-link').symlink_to(referent_file)
            (stack / 'dir-link').symlink_to(referent_dir, target_is_directory=True)
            (stack / 'dangling-link').symlink_to(root / 'missing')
            value = manifest(root, sources={
                'paths': [
                    {'id': name, 'path': name}
                    for name in ('file-link', 'dir-link', 'dangling-link')
                ],
                'volumes': [],
            })

            inventory = storage.sync_paths(value, root / 'stage')

            self.assertEqual([entry['type'] for entry in inventory], ['symlink'] * 3)
            for name in ('file-link', 'dir-link', 'dangling-link'):
                archived = root / 'stage' / 'paths' / name / name
                self.assertTrue(archived.is_symlink())
                self.assertEqual(archived.readlink(), (stack / name).readlink())

    def test_top_level_symlink_timestamp_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'link', 'path': 'link'}], 'volumes': [],
            })
            link = Path(value['_dir']) / 'link'
            link.symlink_to('missing')
            timestamp = 1_700_000_000_123_456_789
            os.utime(link, ns=(timestamp, timestamp), follow_symlinks=False)

            storage.sync_paths(value, root / 'stage')

            archived = root / 'stage' / 'paths' / 'link' / 'link'
            self.assertEqual(archived.lstat().st_mtime_ns, link.lstat().st_mtime_ns)

    def test_optional_missing_path_is_recorded_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{
                    'id': 'optional', 'path': 'missing', 'required': False,
                }],
                'volumes': [],
            })

            inventory = storage.sync_paths(value, root / 'stage')

            self.assertEqual(inventory, [{
                'id': 'optional', 'path': 'missing', 'type': None,
                'present': False, 'capture_method': 'quiesced-copy',
            }])

    def test_inventory_type_is_captured_by_the_sync_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            (stack / 'file.txt').write_text('file', encoding='utf-8')
            (stack / 'directory').mkdir()
            value = manifest(root, sources={
                'paths': [
                    {'id': 'file', 'path': 'file.txt'},
                    {'id': 'directory', 'path': 'directory'},
                ],
                'volumes': [],
            })

            with mock.patch.object(storage, 'run'), \
                    mock.patch.object(storage, 'rsync'):
                inventory = storage.sync_paths(value, root / 'stage')

            self.assertEqual(inventory, [
                {'id': 'file', 'path': 'file.txt', 'type': 'file', 'present': True,
                 'capture_method': 'quiesced-copy'},
                {'id': 'directory', 'path': 'directory', 'type': 'directory',
                 'present': True, 'capture_method': 'quiesced-copy'},
            ])

    def test_final_sync_removes_slot_when_optional_source_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            stack.mkdir()
            source_path = stack / 'data'
            source_path.write_text('pre-sync secret', encoding='utf-8')
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [{
                    'id': 'data', 'path': 'data', 'required': False,
                }],
                'volumes': [],
            })
            storage.sync_paths(value, stage)
            source_path.unlink()

            inventory = storage.sync_paths(value, stage)

            self.assertEqual(inventory[0]['present'], False)
            self.assertFalse((stage / 'paths' / 'data').exists())

    def test_final_file_sync_replaces_directory_slot_without_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'demo' / 'data'
            source_path.mkdir(parents=True)
            (source_path / 'stale-secret.txt').write_text(
                'secret', encoding='utf-8',
            )
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}],
                'volumes': [],
            })
            storage.sync_paths(value, stage)
            (source_path / 'stale-secret.txt').unlink()
            source_path.rmdir()
            source_path.write_text('final-file', encoding='utf-8')

            inventory = storage.sync_paths(value, stage)

            slot = stage / 'paths' / 'data'
            self.assertEqual(inventory[0]['type'], 'file')
            self.assertEqual(
                sorted(path.name for path in slot.iterdir()), ['data'],
            )
            self.assertEqual(
                (slot / 'data').read_text(encoding='utf-8'), 'final-file',
            )

    def test_directory_resync_removes_stale_slot_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'demo' / 'data'
            source_path.mkdir(parents=True)
            payload = source_path / 'payload.txt'
            payload.write_text('first', encoding='utf-8')
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}],
                'volumes': [],
            })
            storage.sync_paths(value, stage)
            slot = stage / 'paths' / 'data'
            (slot / 'stale.txt').write_text('stale', encoding='utf-8')
            payload.write_text('second', encoding='utf-8')

            storage.sync_paths(value, stage)

            self.assertFalse((slot / 'stale.txt').exists())
            self.assertEqual(
                (slot / 'payload.txt').read_text(encoding='utf-8'), 'second',
            )

    def test_final_directory_sync_deletes_prior_file_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / 'demo' / 'data'
            source_path.parent.mkdir(parents=True)
            source_path.write_text('pre-sync file', encoding='utf-8')
            stage = root / 'stage'
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'data'}],
                'volumes': [],
            })
            storage.sync_paths(value, stage)
            slot = stage / 'paths' / 'data'
            source_path.unlink()
            source_path.mkdir()
            (source_path / 'final.txt').write_text('final', encoding='utf-8')

            inventory = storage.sync_paths(value, stage)

            self.assertEqual(inventory[0]['type'], 'directory')
            self.assertFalse((slot / 'data').exists())
            self.assertEqual(
                sorted(path.name for path in slot.iterdir()), ['final.txt'],
            )


if __name__ == '__main__':
    unittest.main()
