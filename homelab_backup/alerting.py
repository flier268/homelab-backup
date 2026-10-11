"""Optional push notifications (ntfy or healthchecks.io style URLs).

The URL itself is a secret (ntfy topics and healthchecks UUIDs grant access),
so it is never stored in config.yaml. ``alert.notify_url_file`` names a
root-owned file holding a single https URL.
"""
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

from .security import read_control_text

NOTIFY_TIMEOUT_SECONDS = 10


def notification_url(c):
    path = (c.get('alert') or {}).get('notify_url_file')
    if not path:
        return None
    try:
        text = read_control_text(path)
    except FileNotFoundError:
        print(f'WARNING: notification URL file is missing: {path}', file=sys.stderr)
        return None
    except (OSError, ValueError) as err:
        print(
            f'WARNING: notification URL file is unusable: {path}: {err}',
            file=sys.stderr,
        )
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    url = lines[0] if lines else ''
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        print(
            f'WARNING: notification URL file does not contain an http(s) URL: '
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


def notify(c, title, message, *, ok=False, opener=None):
    """Send one notification; returns True when delivered.

    ntfy receives only problems (``ok=False``). healthchecks.io receives
    both: success pings keep its dead-man's switch quiet and failures go to
    ``<url>/fail``. Delivery problems are warnings, never exceptions.
    """
    url = notification_url(c)
    if url is None:
        return False
    opener = opener or _request
    kind = notification_kind(url)
    body = f'{title}\n\n{message}'.encode('utf-8')[:10000]
    if kind == 'healthchecks':
        target = url.rstrip('/') + ('' if ok else '/fail')
        headers = {'Content-Type': 'text/plain; charset=utf-8'}
    else:
        if ok:
            return False
        target = url
        headers = {
            'Title': title.encode('ascii', 'replace').decode('ascii'),
            'Priority': 'high',
            'Tags': 'warning',
            'Content-Type': 'text/plain; charset=utf-8',
        }
    try:
        opener(target, body, headers)
        return True
    except (urllib.error.URLError, OSError, socket.timeout, ValueError) as err:
        print(f'WARNING: notification delivery failed: {err}', file=sys.stderr)
        return False
