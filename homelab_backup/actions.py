import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

from .common import CommandError, _print_command_failure
from .identity import account, action_user, fixed_environment, subprocess_identity

ACTION_OPERATIONS = ('backup', 'restore')
ACTION_PHASES = ('before', 'after', 'finally', 'on_success', 'on_failure')
LEGACY_ACTION_PHASES = ('before', 'finally', 'on_success', 'on_failure')


class ActionTimeoutError(RuntimeError):
    def __init__(self, name, timeout):
        self.name = name
        self.timeout = timeout
        super().__init__(f'action {name!r} timed out after {timeout} seconds')


def _validate_root_executable(candidate):
    candidate = Path(candidate)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
            or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        return None
    current = candidate.parent
    while True:
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
                or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            return None
        if current == current.parent:
            break
        current = current.parent
    return str(candidate)


def action_executable(command, *, run_as):
    executable = command[0]
    if '/' in executable:
        candidate = Path(executable)
        if not candidate.is_absolute():
            return None
    else:
        candidate = shutil.which(
            executable,
            path=fixed_environment(run_as)['PATH'],
        )
        if candidate is None:
            return None
        candidate = Path(candidate)
    if account(run_as).uid == 0:
        if candidate == Path('/bin/bash'):
            try:
                candidate = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                return None
        return _validate_root_executable(candidate)
    return str(candidate) if candidate.is_file() and not candidate.is_symlink() else None


def action_style(m):
    actions = m.get('actions') or {}
    return 'nested' if any(key in actions for key in ACTION_OPERATIONS) else 'legacy'


def action_entries(m, operation, phase):
    actions = m.get('actions') or {}
    if action_style(m) == 'nested':
        return ((actions.get(operation) or {}).get(phase) or [])
    if operation == 'backup' and phase in LEGACY_ACTION_PHASES:
        return actions.get(phase, []) or []
    return []


def legacy_action_messages(m):
    messages = []
    actions = m.get('actions') or {}
    if actions and action_style(m) == 'legacy':
        messages.append(
            'legacy flat actions are deprecated; migrate to actions.backup'
        )
    consistency = m.get('consistency') or {}
    if 'before' in consistency or 'after' in consistency:
        messages.append(
            'consistency.before/after are deprecated; migrate to '
            'actions.backup.before/finally scripts'
        )
    return messages


def warn_legacy_actions(m):
    for message in legacy_action_messages(m):
        print(f'WARNING: {m["service"]}: {message}', file=sys.stderr)


def _run_action(
        action, m, *, operation='backup', phase='before', extra_env=None,
):
    script = action.get('script')
    if script is None:
        command = list(action['command'])
        display_command = command
    else:
        command = ['/bin/bash', '-euo', 'pipefail', '-c', script]
        display_command = [
            '/bin/bash', '-euo', 'pipefail', '-c', '<inline script>',
        ]
    timeout = action.get('timeout', 30)
    user = action_user(action)
    record = account(user)
    env = fixed_environment(user)
    env.update({
        'BACKUPCTL_OPERATION': operation,
        'BACKUPCTL_ACTION_PHASE': phase,
        'BACKUPCTL_SERVICE': m['service'],
    })
    if extra_env:
        env.update(extra_env)
    executable = action_executable(command, run_as=user)
    if executable is None:
        raise RuntimeError(
            f'action {action["name"]!r} executable was not found: {command[0]}'
        )
    command[0] = executable
    prefix = (
        f'+ [{operation}.{phase} {action["name"]!r} '
        f'as {record.uid}:{record.gid}]'
    )
    if script is None:
        print(prefix, ' '.join(shlex.quote(item) for item in command))
    else:
        print(prefix, '/bin/bash -euo pipefail -c <inline script>')
    process = subprocess.Popen(
        command,
        cwd=m['_dir'],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **subprocess_identity(user),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        if stdout:
            print(stdout, end='')
        if stderr:
            print(stderr, end='', file=sys.stderr)
        raise ActionTimeoutError(action['name'], timeout)
    if process.returncode:
        raise CommandError(
            display_command, process.returncode, cwd=m['_dir'],
            stdout=stdout, stderr=stderr,
        )
    if stdout:
        print(stdout, end='')
    if stderr:
        print(stderr, end='', file=sys.stderr)


def run_actions(m, operation, phase, *, extra_env=None):
    failures = []
    for action in action_entries(m, operation, phase):
        try:
            _run_action(
                action, m, operation=operation, phase=phase,
                extra_env=extra_env,
            )
        except Exception as err:
            if action.get('required', True):
                raise
            result = 'timeout' if isinstance(err, ActionTimeoutError) else 'failed'
            if isinstance(err, CommandError):
                _print_command_failure(
                    err,
                    context=f'Optional {phase} action {action["name"]!r} failed',
                )
            print(
                f'WARNING: optional {phase} action {action["name"]!r} '
                f'{result}: {err}',
                file=sys.stderr,
            )
            failures.append({
                'phase': phase, 'name': action['name'], 'result': result,
            })
    return failures


def run_before_actions(m):
    return run_actions(m, 'backup', 'before')


def run_after_actions(m):
    return run_actions(m, 'backup', 'after')


def run_finally_actions(m):
    return run_actions(m, 'backup', 'finally')


def run_success_actions(m):
    return run_actions(m, 'backup', 'on_success')


def failure_environment(m, *, error, phase):
    reason = ' '.join(str(error).replace('\x00', '').splitlines())[:4096]
    secondary = ' | '.join(getattr(error, '__notes__', ()))[:4096]
    return {
        'BACKUPCTL_FAILURE_PHASE': phase,
        'BACKUPCTL_FAILURE_TYPE': type(error).__name__,
        'BACKUPCTL_FAILURE_REASON': reason,
        'BACKUPCTL_FAILURE_SERVICE': m['service'],
        'BACKUPCTL_FAILURE_SECONDARY': secondary,
    }


def run_failure_actions(m, *, error, phase):
    return run_actions(
        m, 'backup', 'on_failure',
        extra_env=failure_environment(m, error=error, phase=phase),
    )
