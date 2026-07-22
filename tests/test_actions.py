import subprocess
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_backup import actions, common, identity


class BeforeActionTests(unittest.TestCase):
    def test_numeric_uid_gid_does_not_require_passwd_entry(self):
        record = identity.account('12345:23456')
        self.assertEqual((record.uid, record.gid), (12345, 23456))
        self.assertEqual(identity.fixed_environment('12345:23456')['HOME'], '/')
        with mock.patch.object(identity.os, 'geteuid', return_value=0):
            self.assertEqual(identity.subprocess_identity('12345:23456'), {
                'user': 12345, 'group': 23456, 'extra_groups': [],
            })
    def manifest(self, before):
        user = pwd.getpwuid(os.geteuid()).pw_name
        return {
            'service': 'demo',
            '_dir': '/srv/stacks/demo',
            'actions': {'before': [
                {'run_as': user, **item} for item in before
            ]},
        }

    def test_actions_run_in_manifest_order(self):
        before = [
            {'name': 'one', 'command': ['one']},
            {'name': 'two', 'command': ['two']},
        ]
        calls = []
        with mock.patch.object(
            actions, '_run_action', side_effect=lambda item, _m: calls.append(item['name']),
        ):
            self.assertEqual(actions.run_before_actions(self.manifest(before)), [])
        self.assertEqual(calls, ['one', 'two'])

    def test_finally_actions_use_the_same_runner(self):
        value = self.manifest([])
        value['actions']['finally'] = [
            {'name': 'resume', 'command': ['resume'], 'run_as': 'root'},
        ]
        with mock.patch.object(actions, '_run_action') as runner:
            self.assertEqual(actions.run_finally_actions(value), [])
        runner.assert_called_once_with(value['actions']['finally'][0], value)

    def test_failure_actions_receive_sanitized_failure_environment(self):
        value = self.manifest([])
        value['actions']['on_failure'] = [
            {'name': 'notify', 'command': ['notify'], 'run_as': 'root'},
        ]
        error = RuntimeError('disk\x00 full')
        with mock.patch.object(actions, '_run_action') as runner:
            self.assertEqual(actions.run_failure_actions(
                value, error=error, phase='staging',
            ), [])
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs['extra_env'], {
            'BACKUPCTL_FAILURE_PHASE': 'staging',
            'BACKUPCTL_FAILURE_TYPE': 'RuntimeError',
            'BACKUPCTL_FAILURE_REASON': 'disk full',
            'BACKUPCTL_FAILURE_SERVICE': 'demo',
            'BACKUPCTL_FAILURE_SECONDARY': '',
        })

    def test_optional_failure_is_recorded_and_later_actions_continue(self):
        before = [
            {'name': 'optional', 'command': ['one'], 'required': False},
            {'name': 'later', 'command': ['two']},
        ]
        calls = []

        def run_action(item, _manifest):
            calls.append(item['name'])
            if item['name'] == 'optional':
                raise RuntimeError('unavailable')

        with mock.patch.object(actions, '_run_action', side_effect=run_action):
            failures = actions.run_before_actions(self.manifest(before))
        self.assertEqual(calls, ['optional', 'later'])
        self.assertEqual(failures, [{
            'phase': 'before', 'name': 'optional', 'result': 'failed',
        }])

    def test_required_failure_aborts(self):
        before = [{'name': 'save', 'command': ['save']}]
        failure = common.CommandError(['save'], 1)
        with mock.patch.object(actions, '_run_action', side_effect=failure), \
                self.assertRaises(common.CommandError):
            actions.run_before_actions(self.manifest(before))

    def test_runner_uses_argv_fixed_environment_and_process_group(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ('saved\n', '')
        action = {
            'name': 'save', 'command': ['save-tool', 'world'], 'timeout': 8,
            'run_as': pwd.getpwuid(os.geteuid()).pw_name,
        }
        identity = {'user': 123, 'group': 456, 'extra_groups': []}
        with mock.patch.object(actions, 'action_executable', return_value='/usr/bin/save-tool'), \
                mock.patch.object(actions, 'subprocess_identity', return_value=identity), \
                mock.patch.object(actions.subprocess, 'Popen', return_value=process) as popen:
            actions._run_action(action, self.manifest([]))
        kwargs = popen.call_args.kwargs
        self.assertEqual(popen.call_args.args[0], ['/usr/bin/save-tool', 'world'])
        self.assertEqual(kwargs['cwd'], '/srv/stacks/demo')
        self.assertEqual(
            kwargs['env']['HOME'], pwd.getpwuid(os.geteuid()).pw_dir,
        )
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['user'], 123)
        self.assertEqual(kwargs['group'], 456)
        self.assertEqual(kwargs['extra_groups'], [])
        process.communicate.assert_called_once_with(timeout=8)

    def test_relative_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = Path(tmp)
            script = service / 'scripts' / 'save'
            script.parent.mkdir()
            script.write_text('#!/bin/sh\n', encoding='utf-8')
            script.chmod(0o700)
            self.assertIsNone(
                actions.action_executable(
                    ['./scripts/save'],
                    run_as=pwd.getpwuid(os.geteuid()).pw_name,
                )
            )

    def test_timeout_terminates_the_process_group(self):
        process = mock.Mock(pid=321, returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(['save'], 3), ('', ''),
        ]
        action = {
            'name': 'save', 'command': ['save'], 'timeout': 3,
            'run_as': pwd.getpwuid(os.geteuid()).pw_name,
        }
        with mock.patch.object(actions, 'action_executable', return_value='/usr/bin/save'), \
                mock.patch.object(actions.subprocess, 'Popen', return_value=process), \
                mock.patch.object(actions.os, 'killpg') as killpg, \
                self.assertRaises(actions.ActionTimeoutError):
            actions._run_action(action, self.manifest([]))
        killpg.assert_called_once_with(321, actions.signal.SIGTERM)

    def test_root_action_rejects_symlink_and_writable_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / 'tool'
            executable.write_text('#!/bin/sh\n', encoding='utf-8')
            executable.chmod(0o777)
            link = root / 'link'
            link.symlink_to(executable)
            self.assertIsNone(actions._validate_root_executable(executable))
            self.assertIsNone(actions._validate_root_executable(link))


if __name__ == '__main__':
    unittest.main()
