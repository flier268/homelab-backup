#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ARCHIVE="$ROOT_DIR/configs/homelab-backup-configs.zip.age"
RUNTIME_DIR=/run
WORK_DIR=""
TTY_STATE=""
assume_yes=false

usage() {
  printf 'Usage: sudo %s [--yes] [ARCHIVE.zip.age]\n' "${0##*/}"
}

restore_terminal() {
  if [[ -n "$TTY_STATE" && -t 0 ]]; then
    stty "$TTY_STATE"
    TTY_STATE=""
    printf '\n' >&2
  fi
}

cleanup() {
  restore_terminal
  [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_runtime_tmpfs() {
  local runtime_dir=$1 filesystem
  [[ -d "$runtime_dir" && ! -L "$runtime_dir" ]] || die "runtime directory is not a real directory: $runtime_dir"
  filesystem="$(stat -f -c '%T' -- "$runtime_dir")" || die "cannot inspect runtime filesystem: $runtime_dir"
  [[ "$filesystem" == tmpfs ]] || die "runtime directory must be on tmpfs, found $filesystem: $runtime_dir"
}

validate_and_extract_archive() {
  local archive=$1 destination=$2
  python3 - "$archive" "$destination" <<'PY'
from pathlib import Path
import shutil
import sys
import zipfile

expected = {
    'configs/restic-password',
    'configs/rclone.conf',
    'configs/config.yaml',
}
destination = Path(sys.argv[2])
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    if len(names) != len(expected) or set(names) != expected:
        raise SystemExit('ERROR: archive contains unexpected or missing files')
    if any(info.file_size > 16 * 1024 * 1024 for info in archive.infolist()):
        raise SystemExit('ERROR: archive member exceeds the 16 MiB config limit')
    damaged = archive.testzip()
    if damaged is not None:
        raise SystemExit(f'ERROR: archive member is damaged: {damaged}')
    for name in expected:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, target.open('xb') as output:
            shutil.copyfileobj(source, output)
PY
}

copy_untrusted_regular_file() {
  local input=$1 output=$2 maximum_size=$3
  python3 - "$input" "$output" "$maximum_size" <<'PY'
import os
import stat
import sys

source = sys.argv[1]
destination = sys.argv[2]
maximum_size = int(sys.argv[3])
source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    metadata = os.fstat(source_fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f'ERROR: input is not a regular file: {source}')
    if metadata.st_size > maximum_size:
        raise SystemExit('ERROR: input exceeds its size limit')
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        copied = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            copied += len(chunk)
            if copied > maximum_size:
                raise SystemExit('ERROR: input exceeds its size limit')
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_fd, remaining)
                if written == 0:
                    raise OSError('short write while copying untrusted input')
                remaining = remaining[written:]
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
finally:
    os.close(source_fd)
PY
}

publish_config_bundle() {
  local source_root=$1 target_root=$2
  local owner_uid=${3:-0} owner_gid=${4:-0} fail_after=${5:-0}
  python3 - "$source_root" "$target_root" \
    "$owner_uid" "$owner_gid" "$fail_after" <<'PY'
import os
from pathlib import Path
import shutil
import tempfile
import sys

source_root = Path(sys.argv[1])
target_root = Path(sys.argv[2])
owner_uid = int(sys.argv[3])
owner_gid = int(sys.argv[4])
fail_after = int(sys.argv[5])
members = (
    ('configs/restic-password', 'restic-password'),
    ('configs/rclone.conf', 'rclone/rclone.conf'),
    ('configs/config.yaml', 'config.yaml'),
)
target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
rclone_root = target_root / 'rclone'
if rclone_root.is_symlink():
    raise SystemExit(f'ERROR: refusing symlink config directory: {rclone_root}')
rclone_root.mkdir(mode=0o700, exist_ok=True)
os.chmod(target_root, 0o700)
os.chmod(rclone_root, 0o700)
transaction = Path(tempfile.mkdtemp(prefix='.restore-configs.', dir=target_root))
new_root = transaction / 'new'
old_root = transaction / 'old'
published = []
saved = []

def fsync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def rollback_config_bundle():
    for relative in reversed(published):
        target = target_root / relative
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    for relative in reversed(saved):
        os.replace(old_root / relative, target_root / relative)

try:
    for source_name, relative in members:
        source = source_root / source_name
        staged = new_root / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, staged)
        os.chown(staged, owner_uid, owner_gid)
        os.chmod(staged, 0o600)
        fsync_file(staged)

    for _source_name, relative in members:
        target = target_root / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError(f'refusing non-regular config target: {target}')

    for _source_name, relative in members:
        target = target_root / relative
        previous = old_root / relative
        if target.exists():
            previous.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, previous)
            saved.append(relative)
        os.replace(new_root / relative, target)
        published.append(relative)
        if fail_after and len(published) == fail_after:
            raise RuntimeError('injected configuration publication failure')
    for directory in (rclone_root, target_root):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
except BaseException:
    rollback_config_bundle()
    raise
finally:
    shutil.rmtree(transaction, ignore_errors=True)
PY
}

main() {
if ((EUID != 0)); then
  printf 'ERROR: restore-configs.sh must be run as root; use sudo %s.\n' "$0" >&2
  exit 1
fi
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

while (($#)); do
  case "$1" in
    --yes) assume_yes=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) usage >&2; exit 2 ;;
    *) break ;;
  esac
done
(($# <= 1)) || { usage >&2; exit 2; }
archive="${1:-$DEFAULT_ARCHIVE}"

command -v age >/dev/null 2>&1 || { printf 'ERROR: age is required; run install.sh first.\n' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf 'ERROR: python3 is required.\n' >&2; exit 1; }
[[ -f "$archive" && ! -L "$archive" ]] || {
  printf 'ERROR: encrypted archive is not a regular non-symlink file: %s\n' "$archive" >&2
  exit 2
}
if [[ "$assume_yes" == false && ! -t 0 ]]; then
  printf 'ERROR: non-interactive restore requires --yes.\n' >&2
  exit 2
fi
if [[ "$assume_yes" == false ]]; then
  read -r -p 'Decrypt this archive and overwrite /etc/homelab-backup configs? [y/N]: ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || { printf 'Cancelled.\n'; exit 0; }
fi

require_runtime_tmpfs "$RUNTIME_DIR"
WORK_DIR="$(mktemp -d "$RUNTIME_DIR/homelab-backup-restore.XXXXXX")"
chmod 0700 "$WORK_DIR"
copy_untrusted_regular_file \
  "$archive" "$WORK_DIR/configs.zip.age" $((128 * 1024 * 1024))

printf 'Paste the complete SSH private key, then press Ctrl-D.\n' >&2
printf 'The key is passed directly to age and is not stored by this script.\n' >&2
if [[ -t 0 ]]; then
  TTY_STATE="$(stty -g)"
  stty -echo
fi
set +e
(ulimit -f 131072; age --decrypt -i - -o "$WORK_DIR/configs.zip" "$WORK_DIR/configs.zip.age")
status=$?
set -e
restore_terminal
if ((status != 0)); then
  printf 'ERROR: decryption failed; no system config was changed.\n' >&2
  exit "$status"
fi

# Boundary: accept exactly the three recovery files and extract each known
# member explicitly. Extra paths, traversal entries, and damaged ZIPs fail
# before anything under /etc is changed.
validate_and_extract_archive "$WORK_DIR/configs.zip" "$WORK_DIR/extracted"

publish_config_bundle "$WORK_DIR/extracted" /etc/homelab-backup

printf 'Restored encrypted recovery configuration into /etc/homelab-backup.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
