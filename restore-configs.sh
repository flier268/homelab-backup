#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/config-ops-runtime.sh"
DEFAULT_ARCHIVE="$ROOT_DIR/configs/homelab-backup-configs.zip.age"
RUNTIME_DIR=/run
WORK_DIR=""
TTY_STATE=""
assume_yes=false
CONFIG_LOCK_FDS=()

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

preflight_config_bundle() {
  run_config_ops preflight-bundle "$1"
}

config_lock_file() {
  run_config_ops lock-path "$1"
}

acquire_config_lock() {
  local lock_file=$1 lock_fd old_umask
  run_config_ops validate-lock "$lock_file"
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
  local arguments=(
    publish-bundle "$source_root" "$target_root"
    --owner-uid "$owner_uid" --owner-gid "$owner_gid"
  )
  ((fail_after == 0)) || arguments+=(--fail-after)
  ((fail_after_commit == 0)) || arguments+=(--fail-after-commit)
  run_config_ops "${arguments[@]}"
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
