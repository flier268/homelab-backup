#!/usr/bin/env bash
set -euo pipefail

if ((EUID != 0)); then
  printf 'ERROR: backup-configs.sh must be run as root; use sudo %s.\n' "$0" >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$ROOT_DIR/configs"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"

SOURCES=(
  "/etc/homelab-backup/restic-password"
  "/etc/homelab-backup/rclone/rclone.conf"
  "/etc/homelab-backup/config.yaml"
)
DESTINATIONS=(
  "$CONFIGS_DIR/restic-password"
  "$CONFIGS_DIR/rclone.conf"
  "$CONFIGS_DIR/config.yaml"
)

missing=()
for source in "${SOURCES[@]}"; do
  [[ -f "$source" ]] || missing+=("$source")
done
if ((${#missing[@]})); then
  printf 'ERROR: required source files are missing:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

mkdir -p "$CONFIGS_DIR"
for i in "${!SOURCES[@]}"; do
  install -o root -g root -m 0600 "${SOURCES[$i]}" "${DESTINATIONS[$i]}"
done

printf 'Configuration files copied to %s\n' "$CONFIGS_DIR"
printf '\nWARNING: restic-password and rclone.conf are secrets.\n'
printf 'Only commit them to a private repository whose access is strictly controlled.\n\n'

if [[ ! -t 0 ]]; then
  printf 'ERROR: interactive terminal required to choose Git or ZIP output.\n' >&2
  exit 1
fi

printf 'How should these files be preserved?\n'
printf '  1) Git add / commit, then optionally push\n'
printf '  2) Create a timestamped ZIP archive\n'
printf '  q) Cancel\n'
read -r -p 'Choose [1/2/q]: ' choice

case "$choice" in
  1)
    git_cmd=(git -c "safe.directory=$ROOT_DIR" -C "$ROOT_DIR")
    if ! "${git_cmd[@]}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      printf 'ERROR: %s is not inside a Git repository.\n' "$ROOT_DIR" >&2
      exit 1
    fi
    printf '\nConfigured Git remotes:\n'
    if [[ -n "$("${git_cmd[@]}" remote)" ]]; then
      "${git_cmd[@]}" remote -v
    else
      printf '  (none; this commit could still be pushed after a remote is added)\n'
    fi
    printf '\nWARNING: this Git commit contains Restic and rclone credentials.\n'
    printf 'Verify that every destination repository is private and access-controlled.\n'
    read -r -p 'Type PRIVATE to continue with Git: ' private_confirmation
    if [[ "$private_confirmation" != 'PRIVATE' ]]; then
      printf 'Git operation cancelled. Secret files remain ignored.\n'
      exit 0
    fi
    unrelated_staged=()
    while IFS= read -r -d '' path; do
      case "$path" in
        configs/restic-password|configs/rclone.conf|configs/config.yaml) ;;
        *) unrelated_staged+=("$path") ;;
      esac
    done < <("${git_cmd[@]}" diff --cached --name-only -z --)
    if ((${#unrelated_staged[@]})); then
      printf 'ERROR: refusing to include unrelated staged changes in the config backup commit:\n' >&2
      printf '  - %s\n' "${unrelated_staged[@]}" >&2
      printf 'Commit or unstage them separately, then run this script again.\n' >&2
      exit 1
    fi
    "${git_cmd[@]}" add -f -- \
      configs/restic-password configs/rclone.conf configs/config.yaml
    if "${git_cmd[@]}" diff --cached --quiet -- \
      configs/restic-password configs/rclone.conf configs/config.yaml; then
      printf 'No configuration changes to commit.\n'
    else
      "${git_cmd[@]}" commit -m "Backup recovery configs $TIMESTAMP"
    fi
    read -r -p 'Push the current branch to its configured remote? [y/N]: ' do_push
    if [[ "$do_push" =~ ^[Yy]$ ]]; then
      "${git_cmd[@]}" push
    fi
    ;;
  2)
    umask 077
    archive="$ROOT_DIR/homelab-backup-configs-$TIMESTAMP.zip"
    if command -v zip >/dev/null 2>&1; then
      (cd "$ROOT_DIR" && zip -q -9 "$archive" \
        configs/restic-password configs/rclone.conf configs/config.yaml)
    else
      python3 - "$ROOT_DIR" "$archive" <<'PY'
from pathlib import Path
import sys, zipfile
root = Path(sys.argv[1])
archive = Path(sys.argv[2])
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for name in ('restic-password', 'rclone.conf', 'config.yaml'):
        p = root / 'configs' / name
        z.write(p, Path('configs') / name)
PY
    fi
    chmod 600 "$archive"
    printf 'Created encrypted-material archive (ZIP itself is not encrypted): %s\n' "$archive"
    printf 'Store it in an encrypted password manager or offline encrypted media.\n'
    ;;
  q|Q)
    printf 'Cancelled. Files remain in %s\n' "$CONFIGS_DIR"
    ;;
  *)
    printf 'ERROR: invalid choice.\n' >&2
    exit 1
    ;;
esac
