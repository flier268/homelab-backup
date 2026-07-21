#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/config-ops-runtime.sh"
CONFIGS_DIR="$ROOT_DIR/configs"
GIT_ARCHIVE="$CONFIGS_DIR/homelab-backup-configs.zip.age"
RUNTIME_DIR=/run
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
WORK_DIR=""
PUBLISH_DIR=""
PUBLISH_FILE=""
TTY_STATE=""
RECIPIENT=""
CONFIG_BACKUP_LOCK_FD=""

usage() {
  printf 'Usage:\n'
  printf '  sudo %s\n' "${0##*/}"
  printf '  sudo %s --rotate ARCHIVE.zip.age\n' "${0##*/}"
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
  [[ -z "$PUBLISH_DIR" ]] || rm -rf -- "$PUBLISH_DIR"
  [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  local tool
  for tool in age flock python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required; run install.sh first."
  done
}

require_runtime_tmpfs() {
  local runtime_dir=$1 filesystem
  [[ -d "$runtime_dir" && ! -L "$runtime_dir" ]] || die "runtime directory is not a real directory: $runtime_dir"
  filesystem="$(stat -f -c '%T' -- "$runtime_dir")" || die "cannot inspect runtime filesystem: $runtime_dir"
  [[ "$filesystem" == tmpfs ]] || die "runtime directory must be on tmpfs, found $filesystem: $runtime_dir"
}

make_work_dir() {
  WORK_DIR="$(mktemp -d "$RUNTIME_DIR/homelab-backup-configs.XXXXXX")"
  chmod 0700 "$WORK_DIR"
}

prepare_ciphertext_for_user() {
  local source=$1 uid=$2 gid=$3
  # Boundary: the root-only work directory also contains plaintext and must
  # never be traversable by the output user. Copy only ciphertext into a
  # separate private directory, then hand ownership of that directory over.
  PUBLISH_DIR="$(mktemp -d "$RUNTIME_DIR/homelab-backup-publish.XXXXXX")" || return
  chmod 0700 "$PUBLISH_DIR" || { cleanup_publish_dir; return 1; }
  PUBLISH_FILE="$PUBLISH_DIR/archive.zip.age"
  install -o "$uid" -g "$gid" -m 0600 "$source" "$PUBLISH_FILE" || {
    cleanup_publish_dir
    return 1
  }
  chown "$uid:$gid" "$PUBLISH_DIR" || { cleanup_publish_dir; return 1; }
}

cleanup_publish_dir() {
  [[ -z "$PUBLISH_DIR" ]] || rm -rf -- "$PUBLISH_DIR"
  PUBLISH_DIR=""
  PUBLISH_FILE=""
}

copy_file_as_user() {
  local input=$1 user=$2 uid=$3 output=$4 maximum_size=$5
  local status=0
  local runner=()
  if ((EUID != uid)); then
    runner=(runuser --user "$user" --)
  fi
  "${runner[@]}" python3 - "$input" "$maximum_size" > "$output" <<'PY' || status=$?
import os
import stat
import sys

path = sys.argv[1]
maximum_size = int(sys.argv[2])
fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f'ERROR: encrypted archive is not a regular file: {path}')
    if metadata.st_size > maximum_size:
        raise SystemExit(
            f'ERROR: input file exceeds the {maximum_size // (1024 * 1024)} MiB limit'
        )
    with os.fdopen(fd, 'rb', closefd=False) as source:
        copied = 0
        while chunk := source.read(1024 * 1024):
            copied += len(chunk)
            if copied > maximum_size:
                raise SystemExit(
                    f'ERROR: input file exceeds the {maximum_size // (1024 * 1024)} MiB limit'
                )
            sys.stdout.buffer.write(chunk)
finally:
    os.close(fd)
PY
  if ((status != 0)); then
    rm -f -- "$output"
    return "$status"
  fi
  chmod 0600 "$output"
}

copy_rotation_archive_as_user() {
  copy_file_as_user "$1" "$2" "$3" "$4" $((128 * 1024 * 1024))
}

publish_ciphertext_for_user() {
  local source=$1 user=$2 uid=$3 gid=$4 archive=$5 mode=$6
  local fail_after_publish=${7:-0} status=0
  local arguments=(publish-archive)
  prepare_ciphertext_for_user "$source" "$uid" "$gid" || return
  arguments+=("$PUBLISH_FILE" "$archive" "$mode")
  ((fail_after_publish == 0)) || arguments+=(--fail-after-publish)
  run_config_ops_as_user "$user" "$uid" "${arguments[@]}" || status=$?
  cleanup_publish_dir
  return "$status"
}

remove_archive_as_user() {
  local archive=$1 user=$2 uid=$3 status=0
  local runner=()
  if ((EUID != uid)); then
    runner=(runuser --user "$user" --)
  fi
  "${runner[@]}" python3 - "$archive" <<'PY' || status=$?
import os
from pathlib import Path
import stat
import sys

archive = Path(sys.argv[1])
directory_fd = os.open(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    try:
        metadata = os.stat(
            archive.name, dir_fd=directory_fd, follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise SystemExit(
                f'ERROR: encrypted archive is not an owned regular file: {archive}'
            )
        os.unlink(archive.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  return "$status"
}

rollback_git_archive_transaction() {
  local archive=$1 user=$2 uid=$3 gid=$4 prior_present=$5
  local prior_index=$6 relative=$7 rollback_failed=0
  if [[ "$prior_present" == true ]]; then
    publish_ciphertext_for_user \
      "$WORK_DIR/git-archive.before" "$user" "$uid" "$gid" \
      "$archive" replace || rollback_failed=1
  else
    remove_archive_as_user "$archive" "$user" "$uid" || rollback_failed=1
  fi

  if [[ -n "$prior_index" ]]; then
    if [[ "$prior_index" =~ ^([0-7]{6})\ ([0-9a-f]{40,64})\ 0$'\t'(.*)$ &&
          "${BASH_REMATCH[3]}" == "$relative" ]]; then
      "${git_cmd[@]}" update-index --add --cacheinfo \
        "${BASH_REMATCH[1]},${BASH_REMATCH[2]},$relative" || rollback_failed=1
    else
      printf 'ERROR: cannot restore unsupported prior Git index entry\n' >&2
      rollback_failed=1
    fi
  else
    "${git_cmd[@]}" rm --cached -q --ignore-unmatch -- "$relative" || \
      rollback_failed=1
  fi
  return "$rollback_failed"
}

commit_git_archive_transaction() {
  local source=$1 user=$2 uid=$3 gid=$4
  local relative=configs/homelab-backup-configs.zip.age
  local prior_index prior_present=false failure_status=0 publication_mode
  local path
  local staged_file="$WORK_DIR/git-staged.before"
  local index_file="$WORK_DIR/git-index.before"
  local unrelated_staged=()

  if ! "${git_cmd[@]}" diff --cached --name-only -z -- > "$staged_file"; then
    return 1
  fi
  while IFS= read -r -d '' path; do
    case "$path" in
      "$relative") ;;
      *) unrelated_staged+=("$path") ;;
    esac
  done < "$staged_file"
  if ((${#unrelated_staged[@]})); then
    printf 'ERROR: refusing to include unrelated staged changes in the config backup commit:\n' >&2
    printf '  - %s\n' "${unrelated_staged[@]}" >&2
    return 1
  fi

  if ! "${git_cmd[@]}" ls-files --stage -- "$relative" > "$index_file"; then
    return 1
  fi
  prior_index="$(<"$index_file")"
  if [[ -n "$prior_index" ]] && ! [[
        "$prior_index" =~ ^[0-7]{6}\ [0-9a-f]{40,64}\ 0$'\t'"$relative"$ ]]; then
    printf 'ERROR: encrypted archive has an unsupported conflicted Git index entry\n' >&2
    return 1
  fi

  if [[ -e "$GIT_ARCHIVE" || -L "$GIT_ARCHIVE" ]]; then
    [[ -f "$GIT_ARCHIVE" && ! -L "$GIT_ARCHIVE" ]] || {
      printf 'ERROR: existing Git archive must be a real file\n' >&2
      return 1
    }
    prior_present=true
    if ! copy_rotation_archive_as_user \
      "$GIT_ARCHIVE" "$user" "$uid" "$WORK_DIR/git-archive.before"; then
      return 1
    fi
    publication_mode=replace
  else
    publication_mode=create
  fi

  if ! publish_ciphertext_for_user \
    "$source" "$user" "$uid" "$gid" "$GIT_ARCHIVE" "$publication_mode"; then
    return 1
  fi
  "${git_cmd[@]}" add -f -- "$relative" || failure_status=$?
  if ((failure_status != 0)); then
    :
  elif "${git_cmd[@]}" diff --cached --quiet -- "$relative"; then
    printf 'No encrypted configuration changes to commit.\n'
    if ! rollback_git_archive_transaction \
      "$GIT_ARCHIVE" "$user" "$uid" "$gid" "$prior_present" \
      "$prior_index" "$relative"; then
      printf 'ERROR: failed to restore the prior archive transaction state\n' >&2
      return 1
    fi
    return 0
  else
    "${git_cmd[@]}" commit --only -m \
      "Backup encrypted recovery configs $TIMESTAMP" -- "$relative" || \
      failure_status=$?
    if ((failure_status == 0)); then
      return 0
    fi
  fi

  rollback_git_archive_transaction \
    "$GIT_ARCHIVE" "$user" "$uid" "$gid" "$prior_present" \
    "$prior_index" "$relative" || \
    printf 'ERROR: failed to restore the prior archive transaction state\n' >&2
  ((failure_status != 0)) || failure_status=1
  return "$failure_status"
}

configured_lock_file() {
  run_config_ops lock-path /etc/homelab-backup/config.yaml
}

acquire_global_operation_lock() {
  local lock_file=$1 old_umask
  run_config_ops validate-lock "$lock_file"
  old_umask="$(umask)"
  umask 077
  exec {CONFIG_BACKUP_LOCK_FD}<>"$lock_file"
  umask "$old_umask"
  chmod 0600 "$lock_file"
  flock -x "$CONFIG_BACKUP_LOCK_FD"
}

acquire_consistent_global_operation_lock() {
  local selected_lock confirmed_lock
  resolve_config_ops_runtime
  while true; do
    selected_lock="$(configured_lock_file)"
    acquire_global_operation_lock "$selected_lock"
    confirmed_lock="$(configured_lock_file)"
    if [[ "$confirmed_lock" == "$selected_lock" ]]; then
      return 0
    fi
    exec {CONFIG_BACKUP_LOCK_FD}>&-
    CONFIG_BACKUP_LOCK_FD=""
    continue
  done
}

read_recipient() {
  [[ -t 0 ]] || die 'an interactive terminal is required to paste the new SSH public key.'
  printf 'Paste the SSH public key that should decrypt this archive.\n' >&2
  read -r -p 'SSH public key: ' RECIPIENT
  case "$RECIPIENT" in
    'ssh-ed25519 '*|'ssh-rsa '*) ;;
    *) die 'only ssh-ed25519 and ssh-rsa public keys are supported.' ;;
  esac
}

decrypt_with_pasted_identity() {
  local input=$1 output=$2 status
  printf 'Paste the complete old SSH private key, then press Ctrl-D.\n' >&2
  printf 'The key is passed directly to age and is not stored by this script.\n' >&2
  if [[ -t 0 ]]; then
    TTY_STATE="$(stty -g)"
    stty -echo
  fi
  set +e
  # Config archives are deliberately small. Cap decrypted output so a corrupt
  # or hostile ciphertext cannot exhaust the runtime filesystem before ZIP
  # member validation runs (ulimit -f uses 512-byte blocks on Linux).
  (ulimit -f 131072; age --decrypt -i - -o "$output" "$input")
  status=$?
  set -e
  restore_terminal
  return "$status"
}

encrypt_archive() {
  local input=$1 output=$2
  printf '%s\n' "$RECIPIENT" | age --encrypt -R - -o "$output" "$input"
  chmod 0600 "$output"
}

validate_archive() {
  python3 - "$1" <<'PY'
import sys
import zipfile

expected = {
    'configs/restic-password',
    'configs/rclone.conf',
    'configs/config.yaml',
}
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    if len(names) != len(expected) or set(names) != expected:
        raise SystemExit('ERROR: archive contains unexpected or missing files')
    if any(info.file_size > 16 * 1024 * 1024 for info in archive.infolist()):
        raise SystemExit('ERROR: archive member exceeds the 16 MiB config limit')
    damaged = archive.testzip()
    if damaged is not None:
        raise SystemExit(f'ERROR: archive member is damaged: {damaged}')
PY
}

create_plain_archive() {
  local source_dir="$WORK_DIR/plain" archive="$WORK_DIR/configs.zip"
  install -d -o root -g root -m 0700 "$source_dir/configs"
  install -o root -g root -m 0600 \
    /etc/homelab-backup/restic-password "$source_dir/configs/restic-password"
  install -o root -g root -m 0600 \
    /etc/homelab-backup/rclone/rclone.conf "$source_dir/configs/rclone.conf"
  install -o root -g root -m 0600 \
    /etc/homelab-backup/config.yaml "$source_dir/configs/config.yaml"
  python3 - "$source_dir" "$archive" <<'PY'
from pathlib import Path
import sys
import zipfile

root = Path(sys.argv[1])
with zipfile.ZipFile(sys.argv[2], 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in ('restic-password', 'rclone.conf', 'config.yaml'):
        archive.write(root / 'configs' / name, f'configs/{name}')
PY
  validate_archive "$archive"
}

rotate_key() {
  local archive=$1 plain="$WORK_DIR/rotation.zip"
  local encrypted="$WORK_DIR/rotation.zip.age"
  local rotation_user rotation_uid rotation_gid
  rotation_user="${SUDO_USER:-$(stat -c '%U' -- "$archive" 2>/dev/null || true)}"
  if ! rotation_uid="$(id -u -- "$rotation_user" 2>/dev/null)" || ((rotation_uid == 0)); then
    die 'key rotation requires a non-root invoking user or archive owner.'
  fi
  rotation_gid="$(id -g -- "$rotation_user")"
  command -v runuser >/dev/null 2>&1 || die 'runuser is required to rotate an archive without root path access.'

  # The user-provided pathname is both read and replaced with that user's
  # privileges. Root only handles the copied ciphertext and plaintext inside
  # the private tmpfs work directory, so changing an ancestor symlink cannot
  # redirect a root write into /etc or another protected directory.
  copy_rotation_archive_as_user \
    "$archive" "$rotation_user" "$rotation_uid" "$encrypted" || \
    die "encrypted archive is not a readable regular non-symlink file for $rotation_user: $archive"
  decrypt_with_pasted_identity "$encrypted" "$plain" || die 'decryption failed; the old archive was not changed.'
  validate_archive "$plain"
  read_recipient

  encrypt_archive "$plain" "$WORK_DIR/replacement.zip.age"
  publish_ciphertext_for_user \
    "$WORK_DIR/replacement.zip.age" \
    "$rotation_user" "$rotation_uid" "$rotation_gid" "$archive" replace || \
    die 're-encryption succeeded, but publication failed; keep the matching new private key and inspect or retry the archive.'
  printf 'Re-encrypted with the new key: %s\n' "$archive"
}

main() {
if ((EUID != 0)); then
  printf 'ERROR: backup-configs.sh must be run as root; use sudo %s.\n' "$0" >&2
  exit 1
fi
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

require_tools
require_runtime_tmpfs "$RUNTIME_DIR"
make_work_dir

if [[ "${1:-}" == '--rotate' ]]; then
  (($# == 2)) || { usage >&2; exit 2; }
  rotate_key "$2"
  exit 0
fi
if (($#)); then
  usage >&2
  exit 2
fi

acquire_consistent_global_operation_lock

for source in \
  /etc/homelab-backup/restic-password \
  /etc/homelab-backup/rclone/rclone.conf \
  /etc/homelab-backup/config.yaml; do
  [[ -f "$source" && ! -L "$source" ]] || die "required source is missing or is a symlink: $source"
done
[[ -t 0 ]] || die 'an interactive terminal is required to choose the output and paste a public key.'

printf 'How should the encrypted recovery archive be preserved?\n'
printf '  1) Replace configs/homelab-backup-configs.zip.age and commit it\n'
printf '  2) Create a timestamped .zip.age file\n'
printf '  q) Cancel\n'
read -r -p 'Choose [1/2/q]: ' choice

case "$choice" in
  1)
    git_user="${SUDO_USER:-$(stat -c '%U' -- "$ROOT_DIR")}"
    if ! git_uid="$(id -u -- "$git_user" 2>/dev/null)" || ((git_uid == 0)); then
      die 'Git backup requires a non-root invoking user or repository owner.'
    fi
    git_gid="$(id -g -- "$git_user")"
    command -v runuser >/dev/null 2>&1 || die 'runuser is required to execute Git without root privileges.'
    [[ ! -L "$CONFIGS_DIR" ]] || die 'configs must not be a symlink.'
    [[ ! -L "$GIT_ARCHIVE" ]] || die 'the encrypted Git archive must not be a symlink.'
    git_cmd=(runuser --user "$git_user" -- git -C "$ROOT_DIR")
    "${git_cmd[@]}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$ROOT_DIR is not inside a Git repository."
    printf '\nConfigured Git remotes:\n'
    if [[ -n "$("${git_cmd[@]}" remote)" ]]; then
      "${git_cmd[@]}" remote -v
    else
      printf '  (none; this commit could still be pushed after a remote is added)\n'
    fi
    printf '\nThe commit contains an age-encrypted recovery archive.\n'
    printf 'Keep the repository private and retain the matching private key separately.\n'
    read -r -p 'Type PRIVATE to continue with Git: ' private_confirmation
    if [[ "$private_confirmation" != 'PRIVATE' ]]; then
      printf 'Git operation cancelled.\n'
      exit 0
    fi
    read_recipient
    create_plain_archive
    encrypt_archive "$WORK_DIR/configs.zip" "$WORK_DIR/configs.zip.age"
    # Root reads the system secrets, but never writes into the user-controlled
    # repository. Only the ciphertext is handed to the invoking user.
    runuser --user "$git_user" -- install -d -m 0755 "$CONFIGS_DIR"
    commit_git_archive_transaction \
      "$WORK_DIR/configs.zip.age" "$git_user" "$git_uid" "$git_gid"
    read -r -p 'Push the current branch to its configured remote? [y/N]: ' do_push
    [[ "$do_push" =~ ^[Yy]$ ]] && "${git_cmd[@]}" push
    ;;
  2)
    read_recipient
    create_plain_archive
    archive="$ROOT_DIR/homelab-backup-configs-$TIMESTAMP.zip.age"
    [[ ! -e "$archive" && ! -L "$archive" ]] || die "output already exists: $archive"
    encrypt_archive "$WORK_DIR/configs.zip" "$WORK_DIR/configs.zip.age"
    output_user="${SUDO_USER:-$(stat -c '%U' -- "$ROOT_DIR")}"
    if output_uid="$(id -u -- "$output_user" 2>/dev/null)" && ((output_uid != 0)); then
      output_gid="$(id -g -- "$output_user")"
      publish_ciphertext_for_user \
        "$WORK_DIR/configs.zip.age" "$output_user" "$output_uid" \
        "$output_gid" "$archive" create
    else
      publish_ciphertext_for_user \
        "$WORK_DIR/configs.zip.age" root 0 0 "$archive" create
    fi
    printf 'Created encrypted recovery archive: %s\n' "$archive"
    ;;
  q|Q)
    printf 'Cancelled.\n'
    ;;
  *)
    die 'invalid choice.'
    ;;
esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
