#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ARCHIVE="$ROOT_DIR/configs/homelab-backup-configs.zip.age"
RUNTIME_DIR=/run
WORK_DIR=""
TTY_STATE=""
assume_yes=false
CONFIG_LOCK_FDS=()
VALIDATOR_RELEASE_FDS=()
VALIDATOR_PYTHON=""
VALIDATOR_MODULE=""

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

resolve_validator_runtime() {
  local release lease_fd
  if [[ -x /usr/local/lib/homelab-backup/current/venv/bin/python &&
        -d /usr/local/lib/homelab-backup/current/app ]]; then
    release="$(readlink -f -- /usr/local/lib/homelab-backup/current)"
    [[ "$release" == /usr/local/lib/homelab-backup/releases/release.* &&
       -x "$release/venv/bin/python" && -d "$release/app" ]] || {
      printf 'ERROR: installed validator release is invalid: %s\n' "$release" >&2
      return 1
    }
    if [[ -f "$release/.lease" && ! -L "$release/.lease" ]]; then
      exec {lease_fd}<"$release/.lease"
      flock -s "$lease_fd"
      VALIDATOR_RELEASE_FDS+=("$lease_fd")
    fi
    VALIDATOR_PYTHON="$release/venv/bin/python"
    VALIDATOR_MODULE="$release/app"
  elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    VALIDATOR_PYTHON="$ROOT_DIR/.venv/bin/python"
    VALIDATOR_MODULE="$ROOT_DIR"
  else
    VALIDATOR_PYTHON=python3
    VALIDATOR_MODULE="$ROOT_DIR"
  fi
}

preflight_config_bundle() {
  local source_root=$1 python_path module_path
  resolve_validator_runtime
  python_path="$VALIDATOR_PYTHON"
  module_path="$VALIDATOR_MODULE"
  PYTHONPATH="$module_path" "$python_path" - "$source_root" <<'PY'
import configparser
import os
from pathlib import Path
import stat
import sys

from homelab_backup import config as config_module
from homelab_backup.common import load_yaml

root = Path(sys.argv[1]) / 'configs'
password_path = root / 'restic-password'
rclone_path = root / 'rclone.conf'
config_path = root / 'config.yaml'
for path in (password_path, rclone_path, config_path):
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f'ERROR: restored config member is not a regular file: {path}')

password = password_path.read_bytes()
if not password.strip() or b'\0' in password:
    raise SystemExit('ERROR: restic password must be non-empty and contain no NUL bytes')

config_module.CFG = config_path
config = load_yaml(config_path)
config_module._validate_config_header(config)
trusted_roots = config_module._normalize_trusted_roots(config)
config_module._validate_optional_sections(config)
config_module._validate_root_separation(config, trusted_roots)
if config['password_file'] != '/etc/homelab-backup/restic-password':
    raise SystemExit(
        'ERROR: restored config password_file must name the bundled restic password'
    )
if config['rclone_config'] != '/etc/homelab-backup/rclone/rclone.conf':
    raise SystemExit(
        'ERROR: restored config rclone_config must name the bundled rclone config'
    )

parser = configparser.RawConfigParser(strict=True)
try:
    with rclone_path.open(encoding='utf-8') as source:
        parser.read_file(source)
except (UnicodeError, configparser.Error) as error:
    raise SystemExit(f'ERROR: invalid rclone config: {error}') from error
if not parser.sections():
    raise SystemExit('ERROR: rclone config must contain at least one remote')
for section in parser.sections():
    if not parser.get(section, 'type', fallback='').strip():
        raise SystemExit(f'ERROR: rclone remote {section!r} is missing a type')
repository = config['repository']
if repository.startswith('rclone:'):
    parts = repository.split(':', 2)
    if len(parts) != 3 or not parts[1] or not parser.has_section(parts[1]):
        raise SystemExit(
            'ERROR: configured repository references a missing rclone remote'
        )
PY
}

config_lock_file() {
  local config_path=$1 python_path module_path
  resolve_validator_runtime
  python_path="$VALIDATOR_PYTHON"
  module_path="$VALIDATOR_MODULE"
  PYTHONPATH="$module_path" "$python_path" - "$config_path" <<'PY'
from pathlib import Path
import sys

from homelab_backup.common import load_yaml

config = load_yaml(Path(sys.argv[1]))
lock_file = config.get('lock_file')
if not isinstance(lock_file, str) or not lock_file.startswith('/'):
    raise SystemExit('ERROR: config lock_file must be an absolute path')
print(lock_file)
PY
}

acquire_config_lock() {
  local lock_file=$1 lock_fd old_umask
  python3 - "$lock_file" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
parent = path.parent
metadata = os.lstat(parent)
if not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit(f'ERROR: lock parent is not a real directory: {parent}')
if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
    raise SystemExit(f'ERROR: lock parent is not private and root-controlled: {parent}')
try:
    metadata = os.lstat(path)
except FileNotFoundError:
    pass
else:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise SystemExit(f'ERROR: lock file is not a root-owned regular file: {path}')
PY
  old_umask="$(umask)"
  umask 077
  exec {lock_fd}<>"$lock_file"
  umask "$old_umask"
  chmod 0600 "$lock_file"
  flock -x "$lock_fd"
  CONFIG_LOCK_FDS+=("$lock_fd")
}

acquire_config_locks() {
  local lock_file previous="" sorted
  sorted="$(printf '%s\n' "$@" | LC_ALL=C sort -u)"
  while IFS= read -r lock_file; do
    [[ -n "$lock_file" && "$lock_file" != "$previous" ]] || continue
    acquire_config_lock "$lock_file"
    previous=$lock_file
  done <<< "$sorted"
}

publish_config_bundle() {
  local source_root=$1 target_root=$2
  local owner_uid=${3:-0} owner_gid=${4:-0} fail_after=${5:-0}
  local fail_after_commit=${6:-0}
  python3 - "$source_root" "$target_root" \
    "$owner_uid" "$owner_gid" "$fail_after" "$fail_after_commit" <<'PY'
import ctypes
import os
from pathlib import Path
import shutil
import stat
import tempfile
import sys

source_root = Path(sys.argv[1])
target_root = Path(sys.argv[2])
owner_uid = int(sys.argv[3])
owner_gid = int(sys.argv[4])
fail_after = int(sys.argv[5])
fail_after_commit = int(sys.argv[6])
members = (
    ('configs/restic-password', 'restic-password'),
    ('configs/rclone.conf', 'rclone/rclone.conf'),
    ('configs/config.yaml', 'config.yaml'),
)

def validate_control_directory(path):
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f'ERROR: config control path is not a real directory: {path}')
    if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
        raise SystemExit(f'ERROR: config control directory has the wrong owner: {path}')
    if metadata.st_mode & 0o022:
        raise SystemExit(f'ERROR: config control directory is writable by other users: {path}')

def fsync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

parent = target_root.parent
validate_control_directory(parent)
next_prefix = f'.{target_root.name}.restore.next.'
retired_prefix = f'.{target_root.name}.restore.retired.'
for stale in parent.iterdir():
    if not (
        stale.name.startswith(next_prefix)
        or stale.name.startswith(retired_prefix)
    ):
        continue
    metadata = os.lstat(stale)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != owner_uid:
        raise SystemExit(f'ERROR: unsafe stale config generation: {stale}')
    shutil.rmtree(stale)
fsync_directory(parent)

generation = Path(tempfile.mkdtemp(prefix=next_prefix, dir=parent))
committed = False

try:
    for source_name, relative in members:
        source = source_root / source_name
        staged = generation / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, staged)
        os.chown(staged, owner_uid, owner_gid)
        os.chmod(staged, 0o600)
        fsync_file(staged)
    for directory in (generation / 'rclone', generation):
        os.chown(directory, owner_uid, owner_gid)
        os.chmod(directory, 0o700)
        fsync_directory(directory)
    if fail_after:
        raise RuntimeError('injected configuration publication failure')

    if target_root.exists() or target_root.is_symlink():
        validate_control_directory(target_root)
        rclone_root = target_root / 'rclone'
        validate_control_directory(rclone_root)
        for _source_name, relative in members:
            target = target_root / relative
            metadata = os.lstat(target)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f'refusing non-regular config target: {target}')
        retired = generation.with_name(
            retired_prefix + generation.name.removeprefix(next_prefix)
        )
        os.replace(generation, retired)
        generation = retired
        fsync_directory(parent)
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if renameat2(
            -100, os.fsencode(generation), -100, os.fsencode(target_root), 2,
        ) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    else:
        os.replace(generation, target_root)
    committed = True
    try:
        if fail_after_commit:
            raise OSError('injected post-commit directory sync failure')
        fsync_directory(parent)
    except OSError as error:
        # The generation exchange is already the visible commit point. Under
        # the documented no-power-loss boundary, report degraded durability
        # without telling the caller that the old generation is still active.
        print(
            f'WARNING: config generation was published, but its parent '
            f'directory could not be synced: {error}',
            file=sys.stderr,
        )
finally:
    if not committed and generation.exists() and not generation.is_symlink():
        shutil.rmtree(generation)
        fsync_directory(parent)
    elif committed and generation.exists() and not generation.is_symlink():
        # All config readers participate in the generation locks. Once the
        # exchange commits, nobody can still acquire the retired generation.
        try:
            shutil.rmtree(generation)
            fsync_directory(parent)
        except OSError as error:
            print(
                f'WARNING: could not remove retired plaintext config '
                f'generation {generation}: {error}',
                file=sys.stderr,
            )
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
command -v flock >/dev/null 2>&1 || { printf 'ERROR: flock is required.\n' >&2; exit 1; }
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

preflight_config_bundle "$WORK_DIR/extracted"
new_lock="$(config_lock_file "$WORK_DIR/extracted/configs/config.yaml")"
current_lock=/run/homelab-backup/backupctl.lock
if [[ -f /etc/homelab-backup/config.yaml &&
      ! -L /etc/homelab-backup/config.yaml ]]; then
  if ! current_lock="$(config_lock_file /etc/homelab-backup/config.yaml)"; then
    printf 'WARNING: live config is invalid; using the default global lock for recovery.\n' >&2
    current_lock=/run/homelab-backup/backupctl.lock
  fi
fi
acquire_config_locks "$current_lock" "$new_lock"
if [[ -f /etc/homelab-backup/config.yaml &&
      ! -L /etc/homelab-backup/config.yaml ]]; then
  confirmed_lock="$(config_lock_file /etc/homelab-backup/config.yaml 2>/dev/null || true)"
  [[ "$confirmed_lock" == "$current_lock" ]] || \
    [[ -z "$confirmed_lock" &&
       "$current_lock" == /run/homelab-backup/backupctl.lock ]] || \
    die 'live config changed while acquiring its global operation lock'
fi
publish_config_bundle "$WORK_DIR/extracted" /etc/homelab-backup

printf 'Restored encrypted recovery configuration into /etc/homelab-backup.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
