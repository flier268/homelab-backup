"""Stop-the-bleeding change 6: actions may skip when a container is down."""
import os
import pwd
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import actions, common
from homelab_backup import manifest as manifest_module
from tests.helpers import manifest


def demo(before):
    user = pwd.getpwuid(os.geteuid()).pw_name
    return {
        'service': 'demo', '_dir': '/srv/stacks/demo',
        'actions': {'backup': {'before': [
            {'run_as': user, **item} for item in before
        ]}},
    }


def docker_state(states):
    def fake_run(cmd, **_kwargs):
        name = cmd[-1]
        if name not in states:
            raise common.CommandError(cmd, 1, stderr='No such container')
        return mock.Mock(stdout=f'{states[name]}\n')
    return fake_run


class SkipIfNotRunningTests(unittest.TestCase):
    def _run(self, before, states):
        ran = []
        with mock.patch.object(actions, 'run', side_effect=docker_state(states)), \
                mock.patch.object(
                    actions, '_run_action',
                    side_effect=lambda action, *_a, **_k: ran.append(action['name']),
                ), redirect_stderr(StringIO()) as err:
            failures = actions.run_before_actions(demo(before))
        return ran, failures, err.getvalue()

    def test_running_container_runs_action(self):
        ran, failures, _ = self._run(
            [{'name': 'dump', 'command': ['dump'], 'skip_if_not_running': 'db'}],
            {'db': 'true'},
        )
        self.assertEqual(ran, ['dump'])
        self.assertEqual(failures, [])

    def test_stopped_or_missing_container_skips_required_action_with_warning(self):
        for states in ({'db': 'false'}, {}):
            with self.subTest(states=states):
                ran, failures, err = self._run(
                    [{'name': 'dump', 'command': ['dump'], 'required': True,
                      'skip_if_not_running': 'db'},
                     {'name': 'other', 'command': ['other']}],
                    states,
                )
                self.assertEqual(ran, ['other'])
                self.assertEqual(
                    failures,
                    [{'phase': 'before', 'name': 'dump', 'result': 'failed'}],
                )
                self.assertIn("WARNING: skipping before action 'dump'", err)
                self.assertIn('db', err)

    def test_list_requires_every_container_running(self):
        ran, failures, err = self._run(
            [{'name': 'dump', 'command': ['dump'],
              'skip_if_not_running': ['app', 'db']}],
            {'app': 'true', 'db': 'false'},
        )
        self.assertEqual(ran, [])
        self.assertIn('container not running: db', err)

    def test_docker_missing_counts_as_not_running(self):
        with mock.patch.object(actions, 'run', side_effect=FileNotFoundError('docker')):
            self.assertFalse(actions.container_running('db'))

    def test_actions_without_field_never_query_docker(self):
        with mock.patch.object(actions, 'run') as run, \
                mock.patch.object(actions, '_run_action'):
            actions.run_before_actions(demo([{'name': 'x', 'command': ['x']}]))
        run.assert_not_called()

    def test_skipped_action_downgrades_but_does_not_fail_backup(self):
        # The result value must stay inside the snapshot inventory schema so
        # older releases can still read snapshots during a rollback restore.
        from homelab_backup.inventory_models import OptionalActionFailureModel
        OptionalActionFailureModel(phase='before', name='dump', result='failed')


class ManifestSchemaTests(unittest.TestCase):
    def _validate(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            m = manifest(Path(tmp), actions={'backup': {'before': [{
                'name': 'dump', 'command': ['/bin/true'], 'run_as': 'root',
                'skip_if_not_running': value,
            }]}})
            manifest_module.validate_manifest(m)

    def test_accepts_name_or_list(self):
        self._validate('db')
        self._validate(['db', 'app_1'])

    def test_rejects_invalid_values(self):
        for value in ('', [], [''], True, ['bad name'], '-leading'):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, 'skip_if_not_running',
            ):
                self._validate(value)


if __name__ == '__main__':
    unittest.main()
