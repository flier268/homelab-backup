import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from http.client import IncompleteRead
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import VERSION


REPOSITORY = 'flier268/homelab-backup'
RELEASE_BASE_URL = f'https://github.com/{REPOSITORY}/releases/latest/download'
INSTALL_ROOT = Path('/usr/local/lib/homelab-backup')
CHECKSUM_MAX_BYTES = 64 * 1024
EXTRACT_MAX_BYTES = 512 * 1024 * 1024
EXTRACT_MAX_MEMBERS = 10_000
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
ARCHIVE_RE = re.compile(
    r'^homelab-backup-([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz$',
)
CHECKSUM_RE = re.compile(
    r'^([0-9a-fA-F]{64})  (homelab-backup-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz)$',
)
RELEASE_NAME_RE = re.compile(r'^release\.[A-Za-z0-9]+$')


class UpgradeError(RuntimeError):
    pass


def _request(url):
    return Request(
        url,
        headers={
            'Accept': 'application/octet-stream',
            'User-Agent': f'homelab-backup/{VERSION}',
        },
    )


def _content_length(response, *, stage):
    value = response.headers.get('Content-Length')
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as err:
        raise UpgradeError(
            f'{stage} returned an invalid Content-Length header'
        ) from err
    if length < 0:
        raise UpgradeError(f'{stage} returned an invalid Content-Length header')
    return length


def _open_download(url, *, stage):
    try:
        return urlopen(_request(url), timeout=30)
    except HTTPError as err:
        raise UpgradeError(
            f'{stage} failed: GitHub returned HTTP {err.code}'
        ) from err
    except (URLError, OSError) as err:
        reason = getattr(err, 'reason', err)
        raise UpgradeError(f'{stage} failed: {reason}') from err


def _download_checksum():
    with _open_download(
        f'{RELEASE_BASE_URL}/SHA256SUMS',
        stage='checksum download',
    ) as response:
        content_length = _content_length(response, stage='checksum download')
        if content_length is not None and content_length > CHECKSUM_MAX_BYTES:
            raise UpgradeError('checksum download exceeds the 64 KiB limit')
        try:
            raw = response.read(CHECKSUM_MAX_BYTES + 1)
        except (IncompleteRead, OSError) as err:
            raise UpgradeError(f'checksum download failed: {err}') from err
    if len(raw) > CHECKSUM_MAX_BYTES:
        raise UpgradeError('checksum download exceeds the 64 KiB limit')
    if content_length is not None and len(raw) != content_length:
        raise UpgradeError('checksum download was incomplete')
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError as err:
        raise UpgradeError('checksum file is not ASCII text') from err
    lines = text.splitlines()
    if len(lines) != 1:
        raise UpgradeError('checksum file must contain exactly one record')
    match = CHECKSUM_RE.fullmatch(lines[0])
    if match is None:
        raise UpgradeError('checksum file has an invalid record')
    digest, archive_name = match.groups()
    archive_match = ARCHIVE_RE.fullmatch(archive_name)
    if archive_match is None:
        raise UpgradeError('checksum file names an invalid release archive')
    return archive_match.group(1), archive_name, digest.lower()


def _download_archive(archive_name, expected_digest, destination):
    digest = hashlib.sha256()
    downloaded = 0
    with _open_download(
        f'{RELEASE_BASE_URL}/{archive_name}',
        stage='release archive download',
    ) as response:
        content_length = _content_length(
            response, stage='release archive download',
        )
        try:
            with destination.open('xb') as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
        except (IncompleteRead, OSError) as err:
            raise UpgradeError(f'release archive download failed: {err}') from err
    if content_length is not None and downloaded != content_length:
        raise UpgradeError('release archive download was incomplete')
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise UpgradeError(
            'release archive SHA-256 mismatch: '
            f'expected {expected_digest}, got {actual_digest}'
        )


def _member_path(member, expected_root):
    raw_parts = member.name.split('/')
    if member.name.startswith('/') or any(part in ('', '.', '..') for part in raw_parts):
        if not (member.isdir() and raw_parts[-1] == '' and all(
            part not in ('', '.', '..') for part in raw_parts[:-1]
        )):
            raise UpgradeError(f'release archive has an unsafe path: {member.name!r}')
        raw_parts = raw_parts[:-1]
    path = PurePosixPath(*raw_parts)
    if not path.parts or path.parts[0] != expected_root:
        raise UpgradeError(
            f'release archive member is outside {expected_root!r}: {member.name!r}'
        )
    return path


def _validated_members(archive, version):
    expected_root = f'homelab-backup-{version}'
    members = archive.getmembers()
    if not members or len(members) > EXTRACT_MAX_MEMBERS:
        raise UpgradeError('release archive has an invalid number of members')
    seen = set()
    total_size = 0
    install_member = None
    for member in members:
        path = _member_path(member, expected_root)
        normalized = path.as_posix()
        if normalized in seen:
            raise UpgradeError(
                f'release archive contains a duplicate path: {normalized!r}'
            )
        seen.add(normalized)
        if not (member.isdir() or member.isreg()):
            raise UpgradeError(
                f'release archive contains an unsupported member: {member.name!r}'
            )
        if member.isreg():
            if member.size < 0:
                raise UpgradeError(
                    f'release archive has an invalid member size: {member.name!r}'
                )
            total_size += member.size
            if total_size > EXTRACT_MAX_BYTES:
                raise UpgradeError('release archive expands beyond the 512 MiB limit')
        if normalized == f'{expected_root}/install.sh':
            if (
                not member.isreg()
                or member.size == 0
                or not member.mode & 0o111
                or member.mode & 0o022
            ):
                raise UpgradeError('release archive contains an invalid install.sh')
            install_member = member
    if install_member is None:
        raise UpgradeError('release archive is missing install.sh')
    return expected_root, members


def _extract_archive(archive_path, destination, version):
    try:
        with tarfile.open(archive_path, mode='r:gz') as archive:
            expected_root, members = _validated_members(archive, version)
            for member in members:
                relative = _member_path(member, expected_root)
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(member.mode & 0o777)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UpgradeError(
                        f'cannot read release archive member: {member.name!r}'
                    )
                with source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, DOWNLOAD_CHUNK_BYTES)
                target.chmod(member.mode & 0o777)
    except UpgradeError:
        raise
    except (OSError, tarfile.TarError) as err:
        raise UpgradeError(f'release archive validation failed: {err}') from err
    return destination / f'homelab-backup-{version}'


def _version_tuple(version):
    if VERSION_RE.fullmatch(version) is None:
        raise UpgradeError(f'invalid semantic version: {version!r}')
    return tuple(int(part) for part in version.split('.'))


def _installed_release():
    raw_release = os.environ.get('HOMELAB_BACKUP_RELEASE_ROOT')
    if not raw_release:
        raise UpgradeError(
            'upgrade is only available from the installed /usr/local/sbin/backupctl'
        )
    try:
        release = Path(raw_release).resolve(strict=True)
        current = (INSTALL_ROOT / 'current').resolve(strict=True)
        releases_root = (INSTALL_ROOT / 'releases').resolve(strict=True)
    except OSError as err:
        raise UpgradeError(f'installed release layout is invalid: {err}') from err
    if (
        release != current
        or release.parent != releases_root
        or RELEASE_NAME_RE.fullmatch(release.name) is None
    ):
        raise UpgradeError('installed release layout is invalid')
    return release


def _run_installer(source_root):
    try:
        result = subprocess.run(
            ['/bin/bash', 'install.sh'],
            cwd=source_root,
            check=False,
        )
    except OSError as err:
        raise UpgradeError(f'installer could not be started: {err}') from err
    if result.returncode:
        raise UpgradeError(f'installer failed with exit status {result.returncode}')


def upgrade():
    _installed_release()
    print(f'Current version: {VERSION}')
    latest_version, archive_name, expected_digest = _download_checksum()
    current = _version_tuple(VERSION)
    latest = _version_tuple(latest_version)
    print(f'Latest stable version: {latest_version}')
    if latest == current:
        print('Already up to date.')
        return
    if latest < current:
        raise UpgradeError(
            f'latest stable release {latest_version} is older than '
            f'installed version {VERSION}; refusing to downgrade'
        )
    with tempfile.TemporaryDirectory(prefix='homelab-backup-upgrade-') as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / archive_name
        extract_root = temporary_root / 'source'
        extract_root.mkdir(mode=0o700)
        print(f'Downloading homelab-backup {latest_version}...')
        _download_archive(archive_name, expected_digest, archive_path)
        print('SHA-256 verified. Validating release archive...')
        source_root = _extract_archive(
            archive_path, extract_root, latest_version,
        )
        print(f'Installing homelab-backup {latest_version}...')
        _run_installer(source_root)
    print(f'Upgrade to homelab-backup {latest_version} completed.')


def cmd_upgrade(_args):
    upgrade()
