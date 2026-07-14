import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homelab_backup import common, storage
from tests.helpers import manifest


class RuntimeValidationTests(unittest.TestCase):
    def test_required_path_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest(root, sources={
                'paths': [{'id': 'data', 'path': 'missing'}],
                'volumes': [],
            })
            with self.assertRaisesRegex(ValueError, 'missing required source'):
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


if __name__ == '__main__':
    unittest.main()
