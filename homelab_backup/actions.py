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
        return _validate_root_executable(candidate)
    return str(candidate) if candidate.is_file() and not candidate.is_symlink() else None


def _run_action(action, m, *, extra_env=None):
    command = list(action['command'])
    timeout = action.get('timeout', 30)
    user = action_user(action)
    record = account(user)
    env = fixed_environment(user)
    if extra_env:
        env.update(extra_env)
    executable = action_executable(command, run_as=user)
    if executable is None:
        raise RuntimeError(
            f'action {action["name"]!r} executable was not found: {command[0]}'
        )
    command[0] = executable
    print(
        f'+ [as {record.uid}:{record.gid}]',
        ' '.join(shlex.quote(item) for item in command),
    )
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
            command, process.returncode, cwd=m['_dir'],
            stdout=stdout, stderr=stderr,
        )
    if stdout:
        print(stdout, end='')
    if stderr:
        print(stderr, end='', file=sys.stderr)


def run_actions(m, phase, *, extra_env=None):
    failures = []
    for action in (m.get('actions') or {}).get(phase, []):
        try:
            if extra_env is None:
                _run_action(action, m)
            else:
                _run_action(action, m, extra_env=extra_env)
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
    return run_actions(m, 'before')


def run_finally_actions(m):
    return run_actions(m, 'finally')


def run_success_actions(m):
    return run_actions(m, 'on_success')


def run_failure_actions(m, *, error, phase):
    reason = ' '.join(str(error).replace('\x00', '').splitlines())[:4096]
    secondary = ' | '.join(getattr(error, '__notes__', ()))[:4096]
    return run_actions(m, 'on_failure', extra_env={
        'BACKUPCTL_FAILURE_PHASE': phase,
        'BACKUPCTL_FAILURE_TYPE': type(error).__name__,
        'BACKUPCTL_FAILURE_REASON': reason,
        'BACKUPCTL_FAILURE_SERVICE': m['service'],
        'BACKUPCTL_FAILURE_SECONDARY': secondary,
    })
