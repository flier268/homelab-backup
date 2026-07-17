import datetime as dt
import fcntl
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

def die(msg):
    print(f'ERROR: {msg}', file=sys.stderr)
    raise SystemExit(1)


class CommandError(RuntimeError):
    def __init__(self, cmd, returncode, cwd=None, stdout='', stderr=''):
        self.cmd = [str(x) for x in cmd]
        self.returncode = returncode
        self.cwd = str(cwd) if cwd else None
        self.stdout = stdout or ''
        self.stderr = stderr or ''
        self.reported = False
        super().__init__(f'command failed with exit code {returncode}')


def _print_command_failure(err, *, context=None):
    err.reported = True
    if context:
        print(f'ERROR: {context}', file=sys.stderr)
    print(f'ERROR: command exited with status {err.returncode}', file=sys.stderr)
    if err.cwd:
        print(f'  working directory: {err.cwd}', file=sys.stderr)
    print('  command: ' + ' '.join(shlex.quote(x) for x in err.cmd), file=sys.stderr)
    if err.stderr.strip():
        print('  stderr:', file=sys.stderr)
        for line in err.stderr.rstrip().splitlines():
            print(f'    {line}', file=sys.stderr)
    if err.stdout.strip():
        print('  stdout:', file=sys.stderr)
        for line in err.stdout.rstrip().splitlines():
            print(f'    {line}', file=sys.stderr)


def run(cmd, *, cwd=None, env=None, check=True, capture=False, pass_fds=()):
    printable = ' '.join(shlex.quote(str(x)) for x in cmd)
    print('+', printable)
    must_capture = capture or check
    result = subprocess.run(
        cmd, cwd=cwd, env=env, check=False, text=True,
        capture_output=must_capture, pass_fds=pass_fds,
    )
    if check and result.returncode != 0:
        raise CommandError(cmd, result.returncode, cwd=cwd, stdout=result.stdout, stderr=result.stderr)
    if must_capture and not capture:
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
    return result


def load_yaml(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def restic_env(c):
    env = os.environ.copy()
    env.update({
        'RESTIC_REPOSITORY': c['repository'],
        'RESTIC_PASSWORD_FILE': c['password_file'],
        'RESTIC_CACHE_DIR': c['cache_root'],
        'RCLONE_CONFIG': c['rclone_config'],
    })
    bwlimit = (c.get('rclone') or {}).get('bwlimit')
    if bwlimit:
        env['RCLONE_BWLIMIT'] = str(bwlimit)
    return env


class GlobalLock:
    def __init__(self, path, nonblocking=False):
        self.path = Path(path)
        self.nonblocking = nonblocking
        self.handle = None

    def __enter__(self):
        from .security import ensure_control_directory

        ensure_control_directory(self.path.parent)
        parent_fd = os.open(
            self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            fd = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        os.fchmod(fd, 0o600)
        self.handle = os.fdopen(fd, 'a+', encoding='utf-8')
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if self.nonblocking else 0)
        try:
            fcntl.flock(self.handle.fileno(), flags)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f'pid={os.getpid()} started={dt.datetime.now().astimezone().isoformat()}\n')
        self.handle.flush()
        return True

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
