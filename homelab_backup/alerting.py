"""Optional push notifications (ntfy or healthchecks.io style URLs).

The URL itself is a secret (ntfy topics and healthchecks UUIDs grant access),
so it is never stored in config.yaml. ``alert.notify_url_file`` names a
root-owned file holding a single https URL.

Delivered alerts are remembered in ``<state_root>/_alerts.json`` so repeated
runs only notify on change or after ``alert.renotify_after``. Service state
files must start with an alphanumeric character, so this name never collides.
Every read-modify-write holds ``<state_root>/_alerts.lock`` (never the global
backup lock, so alerting keeps working while a backup is stuck). Alert state
is best effort: read/write/lock problems only warn and never prevent a
notification from being sent.
"""
import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
import stat
import sys
import time
import urllib.parse
import urllib.request

from .schedule import parse_duration
from .security import (
    atomic_write_json, ensure_control_directory, lexical_absolute,
    read_control_text,
)

NOTIFY_TIMEOUT_SECONDS = 10
ALERT_STATE_NAME = '_alerts.json'
ALERT_LOCK_NAME = '_alerts.lock'
ALERT_LOCK_TIMEOUT_SECONDS = 10
ALERT_LOCK_RETRY_SECONDS = 0.1
DEFAULT_RENOTIFY_AFTER = '24h'


def renotify_after_seconds(c):
    value = (c.get('alert') or {}).get('renotify_after', DEFAULT_RENOTIFY_AFTER)
    return parse_duration(value, 'alert.renotify_after')


def alert_state_path(c):
    return lexical_absolute(c['state_root']) / ALERT_STATE_NAME


def load_alert_state(c):
    """Return the remembered alert state, or {} when it is unusable."""
    if not c.get('state_root'):
        return {}
    path = alert_state_path(c)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as err:
        print(f'WARNING: could not open alert state {path}: {err}', file=sys.stderr)
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not a regular file')
        with os.fdopen(fd, encoding='utf-8') as source:
            fd = -1
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError('not a JSON mapping')
    except (OSError, ValueError) as err:
        print(
            f'WARNING: ignoring unreadable alert state {path}: {err}',
            file=sys.stderr,
        )
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return data


def save_alert_state(c, state):
    if not c.get('state_root'):
        return
    try:
        ensure_control_directory(c['state_root'])
        atomic_write_json(alert_state_path(c), state)
    except (OSError, ValueError) as err:
        print(f'WARNING: could not save alert state: {err}', file=sys.stderr)


def alert_lock_sleep(seconds):
    time.sleep(seconds)


def _acquire_alert_lock(c):
    """Return a descriptor holding the alert lock, or None (fail open)."""
    root = lexical_absolute(c['state_root'])
    path = root / ALERT_LOCK_NAME
    fd = -1
    try:
        ensure_control_directory(root)
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not a regular file')
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + ALERT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f'still held after {ALERT_LOCK_TIMEOUT_SECONDS}s',
                    ) from None
                alert_lock_sleep(ALERT_LOCK_RETRY_SECONDS)
    except (OSError, ValueError) as err:
        if fd >= 0:
            os.close(fd)
        print(
            f'WARNING: could not lock alert state {path}: {err}; '
            'continuing without the lock',
            file=sys.stderr,
        )
        return None


@contextlib.contextmanager
def alert_state_transaction(c):
    """Yield the alert state under ``_alerts.lock`` and save it on exit.

    The state is saved only when the block changed it and did not raise.
    Lock problems fail open with a warning so notifications still go out.
    Not reentrant: flock conflicts between descriptors of one process too.
    """
    if not c.get('state_root'):
        yield {}
        return
    fd = _acquire_alert_lock(c)
    try:
        state = load_alert_state(c)
        original = copy.deepcopy(state)
        yield state
        if state != original:
            save_alert_state(c, state)
    finally:
        if fd is not None:
            os.close(fd)


def parse_timestamp(value):
    """Return an aware datetime parsed from an ISO string, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def notified_within(entry, c, now):
    """True when ``entry['notified_at']`` lies within alert.renotify_after."""
    if not isinstance(entry, dict):
        return False
    notified_at = parse_timestamp(entry.get('notified_at'))
    if notified_at is None or notified_at > now:
        return False
    return (now - notified_at).total_seconds() < renotify_after_seconds(c)


def notification_url(c):
    path = (c.get('alert') or {}).get('notify_url_file')
    if not path:
        return None
    try:
        # private=True: the URL is a secret, so the file must be mode 0600
        # (or stricter), checked on the same descriptor that is read.
        text = read_control_text(path, private=True)
    except FileNotFoundError:
        print(f'WARNING: notification URL file is missing: {path}', file=sys.stderr)
        return None
    except (OSError, ValueError) as err:
        # The error text names only the path and mode, never the content.
        print(
            f'WARNING: notification URL file is unusable: {path}: {err}',
            file=sys.stderr,
        )
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    url = lines[0] if lines else ''
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc:
        print(
            f'WARNING: notification URL file does not contain an https URL: '
            f'{path}',
            file=sys.stderr,
        )
        return None
    return url


def notification_kind(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if host == 'hc-ping.com' or host.endswith('.hc-ping.com') \
            or '/ping/' in parsed.path:
        return 'healthchecks'
    return 'ntfy'


def _request(url, data, headers):
    request = urllib.request.Request(
        url, data=data, headers=headers, method='POST',
    )
    with urllib.request.urlopen(request, timeout=NOTIFY_TIMEOUT_SECONDS) as response:
        return response.status


def notify(c, title, message, *, ok=False, recovered=False, opener=None):
    """Send one notification; returns True when delivered.

    ntfy receives problems (``ok=False``) and, with ``recovered=True``, one
    normal-priority recovery message; plain OK heartbeats are skipped.
    healthchecks.io receives both: success pings keep its dead-man's switch
    quiet (and mark recovery) and failures go to ``<url>/fail``. Delivery
    problems are warnings, never exceptions.
    """
    url = notification_url(c)
    if url is None:
        return False
    opener = opener or _request
    kind = notification_kind(url)
    # Dropping the bytes of a character split by the cap keeps valid UTF-8.
    body = f'{title}\n\n{message}'.encode('utf-8', 'replace')[:10000] \
        .decode('utf-8', 'ignore').encode('utf-8')
    if kind == 'healthchecks':
        target = url.rstrip('/') + ('' if ok else '/fail')
        headers = {'Content-Type': 'text/plain; charset=utf-8'}
    else:
        if ok and not recovered:
            return False
        target = url
        headers = {
            'Title': title.encode('ascii', 'replace').decode('ascii'),
            'Priority': 'default' if ok else 'high',
            'Tags': 'white_check_mark' if ok else 'warning',
            'Content-Type': 'text/plain; charset=utf-8',
        }
    try:
        opener(target, body, headers)
        return True
    except Exception as err:  # noqa: BLE001 - delivery must never raise
        # Only the type name: messages may embed the secret URL.
        print(
            f'WARNING: notification delivery failed: {type(err).__name__}',
            file=sys.stderr,
        )
        return False
