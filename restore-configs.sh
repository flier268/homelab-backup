#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$ROOT_DIR/configs"

assume_yes=false
case "${1:-}" in
  --yes)
    assume_yes=true
    shift
    ;;
  -h|--help)
    printf 'Usage: %s [--yes]\n' "${0##*/}"
    exit 0
    ;;
  '') ;;
  *)
    printf 'ERROR: unknown argument: %s\n' "$1" >&2
    printf 'Usage: %s [--yes]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
if (($#)); then
  printf 'ERROR: unexpected arguments\n' >&2
  printf 'Usage: %s [--yes]\n' "${0##*/}" >&2
  exit 2
fi

required=(restic-password rclone.conf config.yaml)
missing=()
for name in "${required[@]}"; do
  [[ -f "$CONFIGS_DIR/$name" ]] || missing+=("$name")
done

if ((${#missing[@]})); then
  printf 'Required recovery files are missing from %s:\n' "$CONFIGS_DIR" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  printf '\nPlace the missing files in configs/ and run this script again.\n' >&2
  printf 'Expected files:\n' >&2
  printf '  configs/restic-password\n  configs/rclone.conf\n  configs/config.yaml\n' >&2
  exit 2
fi

if [[ "$assume_yes" == false && ! -t 0 ]]; then
  printf 'ERROR: non-interactive restore requires --yes.\n' >&2
  exit 2
fi
if [[ "$assume_yes" == false ]]; then
  read -r -p 'Restore configs/ into /etc/homelab-backup and overwrite existing files? [y/N]: ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || { printf 'Cancelled.\n'; exit 0; }
fi

sudo install -d -o root -g root -m 0700 /etc/homelab-backup/rclone
sudo install -o root -g root -m 0600 \
  "$CONFIGS_DIR/restic-password" /etc/homelab-backup/restic-password
sudo install -o root -g root -m 0600 \
  "$CONFIGS_DIR/rclone.conf" /etc/homelab-backup/rclone/rclone.conf
sudo install -o root -g root -m 0600 \
  "$CONFIGS_DIR/config.yaml" /etc/homelab-backup/config.yaml

printf 'Restored:\n'
printf '  /etc/homelab-backup/restic-password\n'
printf '  /etc/homelab-backup/rclone/rclone.conf\n'
printf '  /etc/homelab-backup/config.yaml\n'
