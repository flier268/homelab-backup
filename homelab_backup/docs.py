import os
import re
import stat
import webbrowser
from pathlib import Path


INSTALL_ROOT = Path('/usr/local/lib/homelab-backup')


class DocsError(RuntimeError):
    pass


def _installed_guide():
    raw_release = os.environ.get('HOMELAB_BACKUP_RELEASE_ROOT')
    if not raw_release:
        raise DocsError(
            'docs is only available from the installed '
            '/usr/local/sbin/backupctl'
        )
    try:
        release = Path(raw_release).resolve(strict=True)
        releases_root = (INSTALL_ROOT / 'releases').resolve(strict=True)
    except OSError as err:
        raise DocsError(f'installed release layout is invalid: {err}') from err
    if (
        release.parent != releases_root
        or re.fullmatch(r'release\.[A-Za-z0-9]+', release.name) is None
    ):
        raise DocsError('installed release layout is invalid')

    guide = release / 'GUIDE.html'
    try:
        metadata = guide.lstat()
    except OSError as err:
        raise DocsError(f'installed documentation is unavailable: {err}') from err
    allowed_owners = {0, os.geteuid()}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in allowed_owners
        or metadata.st_mode & 0o022
    ):
        raise DocsError('installed documentation is not a protected regular file')
    return guide


def open_docs():
    guide = _installed_guide()
    try:
        opened = webbrowser.open(guide.as_uri())
    except (OSError, webbrowser.Error) as err:
        raise DocsError(f'could not open installed documentation: {err}') from err
    if not opened:
        raise DocsError(
            f'could not find a browser; open this file manually: {guide}'
        )


def cmd_docs(_args):
    open_docs()
