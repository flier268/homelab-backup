#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge]

Remove homelab-backup services, launcher, releases, and helper images.
By default, configuration and local working data are preserved.

  --purge  Also remove /etc/homelab-backup, /var/lib/homelab-backup,
           and /var/cache/homelab-backup. Restic repositories and data
           below /srv are never removed.
  -h, --help
           Show this help.
EOF
}

PURGE=0
case "${1:-}" in
  '') ;;
  --purge) PURGE=1 ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
if (( $# > 1 )); then
  usage >&2
  exit 2
fi
if (( EUID != 0 )); then
  echo 'ERROR: uninstall.sh must be run as root.' >&2
  exit 1
fi

LIB_ROOT=/usr/local/lib/homelab-backup
RELEASES_ROOT="$LIB_ROOT/releases"
LAUNCHER=/usr/local/sbin/backupctl
UNIT_ROOT=/etc/systemd/system
UNIT_NAMES=(
  homelab-backup.service
  homelab-backup.timer
  homelab-backup-maintenance.service
  homelab-backup-maintenance.timer
)
TIMER_NAMES=(
  homelab-backup.timer
  homelab-backup-maintenance.timer
)
SERVICE_NAMES=(
  homelab-backup.service
  homelab-backup-maintenance.service
)

# Serialize with install.sh whenever an installation root exists.
if [[ -d "$LIB_ROOT" && ! -L "$LIB_ROOT" ]]; then
  exec 9>"$LIB_ROOT/install.lock"
  if ! flock -n 9; then
    echo 'ERROR: another homelab-backup installation or removal is running.' >&2
    exit 1
  fi
elif [[ -e "$LIB_ROOT" || -L "$LIB_ROOT" ]]; then
  echo "ERROR: installation root is not a real directory: $LIB_ROOT" >&2
  exit 1
fi

unit_is_installed() {
  [[ -e "$UNIT_ROOT/$1" || -L "$UNIT_ROOT/$1" ]]
}

stop_unit() {
  unit=$1
  systemctl stop "$unit" || true
  if systemctl is-active --quiet "$unit"; then
    echo "ERROR: systemd unit is still active: $unit" >&2
    exit 1
  fi
}

for unit in "${TIMER_NAMES[@]}"; do
  if unit_is_installed "$unit"; then
    systemctl disable --now "$unit" || true
    if systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit"; then
      echo "ERROR: could not disable and stop systemd timer: $unit" >&2
      exit 1
    fi
  fi
done
for unit in "${SERVICE_NAMES[@]}"; do
  if unit_is_installed "$unit"; then
    stop_unit "$unit"
  fi
done

# Refuse to remove releases that are still used by a manually started process.
LEASE_FDS=()
if [[ -d "$RELEASES_ROOT" && ! -L "$RELEASES_ROOT" ]]; then
  for release in "$RELEASES_ROOT"/release.*; do
    [[ -e "$release" ]] || continue
    if [[ ! -d "$release" || -L "$release" || ! -f "$release/.lease" ||
          -L "$release/.lease" ]]; then
      echo "ERROR: unsafe installed release; refusing removal: $release" >&2
      exit 1
    fi
    exec {lease_fd}<"$release/.lease"
    if ! flock -n -x "$lease_fd"; then
      echo "ERROR: an active process still uses release: $release" >&2
      exit 1
    fi
    LEASE_FDS+=("$lease_fd")
  done
elif [[ -e "$RELEASES_ROOT" || -L "$RELEASES_ROOT" ]]; then
  echo "ERROR: release store is not a real directory: $RELEASES_ROOT" >&2
  exit 1
fi

# Read the installer-owned image tags before deleting their release metadata.
HELPER_IMAGES=()
if [[ -d "$RELEASES_ROOT" ]]; then
  for metadata in "$RELEASES_ROOT"/release.*/volume-helper-image; do
    [[ -f "$metadata" && ! -L "$metadata" ]] || continue
    if ! helper_image="$(python3 -c '
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read().strip()
if raw.startswith("{"):
    print(json.loads(raw)["tag"])
else:
    print(raw)
' "$metadata")"; then
      echo "WARNING: invalid helper image metadata; image was not removed: $metadata" >&2
      continue
    fi
    if [[ "$helper_image" =~ ^homelab/volume-rsync:release\.[A-Za-z0-9]+$ ]]; then
      HELPER_IMAGES+=("$helper_image")
    else
      echo "WARNING: unsafe helper image tag; image was not removed: $helper_image" >&2
    fi
  done
fi

rm -f -- "$LAUNCHER"
for unit in "${UNIT_NAMES[@]}"; do
  rm -f -- "$UNIT_ROOT/$unit"
done
systemctl daemon-reload
systemctl reset-failed "${UNIT_NAMES[@]}" >/dev/null 2>&1 || true

for helper_image in "${HELPER_IMAGES[@]}"; do
  if ! docker image rm "$helper_image" >/dev/null 2>&1; then
    echo "WARNING: helper image is absent or still in use: $helper_image" >&2
  fi
done

if [[ -d "$LIB_ROOT" ]]; then
  rm -rf -- "$LIB_ROOT"
fi
rm -rf -- /run/homelab-backup

if (( PURGE )); then
  rm -rf -- \
    /etc/homelab-backup \
    /var/lib/homelab-backup \
    /var/cache/homelab-backup
  echo 'Uninstalled homelab-backup and purged its local configuration and working data.'
else
  echo 'Uninstalled homelab-backup.'
  echo 'Preserved /etc/homelab-backup, /var/lib/homelab-backup, and /var/cache/homelab-backup.'
fi
echo 'System packages, Restic repositories, and data below /srv were not removed.'
