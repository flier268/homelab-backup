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

    def _container_running(self, **run_kwargs):
        with mock.patch.object(actions, 'run', **run_kwargs):
            return actions.container_running('db')

    def test_inspect_true_and_false_are_certain(self):
        self.assertTrue(self._container_running(
            return_value=mock.Mock(stdout='true\n')))
        self.assertFalse(self._container_running(
            return_value=mock.Mock(stdout='false\n')))

    def test_missing_container_is_certainly_not_running(self):
        for kwargs in (
            {'stderr': 'Error: No such container: db'},
            {'stderr': 'error: no such object: db'},
            {'stdout': 'Error: No Such Object: db'},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertIs(self._container_running(
                    side_effect=common.CommandError(['docker'], 1, **kwargs),
                ), False)

    def test_unknown_container_state_is_not_false(self):
        for kwargs in (
            {'side_effect': FileNotFoundError('docker')},
            {'side_effect': PermissionError('docker.sock')},
            {'side_effect': common.CommandError(
                ['docker'], 1,
                stderr='Cannot connect to the Docker daemon at '
                       'unix:///var/run/docker.sock. Is the docker daemon running?',
            )},
            {'side_effect': common.CommandError(
                ['docker'], 1, stderr='permission denied while trying to connect',
            )},
            {'return_value': mock.Mock(stdout='<no value>\n')},
            {'return_value': mock.Mock(stdout='')},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self._container_running(**kwargs))

    def test_unknown_state_runs_action_with_warning(self):
        for error in (
            FileNotFoundError('docker'),
            common.CommandError(['docker'], 1, stderr='daemon unreachable'),
        ):
            with self.subTest(error=error):
                ran = []
                with mock.patch.object(actions, 'run', side_effect=error), \
                        mock.patch.object(
                            actions, '_run_action',
                            side_effect=lambda action, *_a, **_k: ran.append(
                                action['name']),
                        ), redirect_stderr(StringIO()) as err:
                    failures = actions.run_before_actions(demo([
                        {'name': 'dump', 'command': ['dump'],
                         'skip_if_not_running': 'db'},
                    ]))
                self.assertEqual(ran, ['dump'])
                self.assertEqual(failures, [])
                self.assertIn('WARNING: could not determine', err.getvalue())
                self.assertNotIn('skipping', err.getvalue())

    def test_unknown_state_lets_required_action_failure_surface(self):
        with mock.patch.object(actions, 'run', side_effect=OSError('docker')), \
                mock.patch.object(
                    actions, '_run_action', side_effect=RuntimeError('dump failed'),
                ), redirect_stderr(StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'dump failed'):
            actions.run_before_actions(demo([
                {'name': 'dump', 'command': ['dump'], 'skip_if_not_running': 'db'},
            ]))

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

    def test_rejects_restore_actions(self):
        # A restore must never silently skip a step.
        for phase in ('before', 'after', 'finally', 'on_success', 'on_failure'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                m = manifest(Path(tmp), actions={'restore': {phase: [{
                    'name': 'load', 'command': ['/bin/true'], 'run_as': 'root',
                    'skip_if_not_running': 'db',
                }]}})
                with self.assertRaisesRegex(
                    ValueError, r'actions\.restore\.\w+\[0\]\.skip_if_not_running',
                ):
                    manifest_module.validate_manifest(m)

    def test_rejects_invalid_values(self):
        for value in ('', [], [''], True, ['bad name'], '-leading'):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, 'skip_if_not_running',
            ):
                self._validate(value)


if __name__ == '__main__':
    unittest.main()
