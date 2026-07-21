#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'ERROR: vm-guest-test.sh must run as root.\n' >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BTRFS_DEVICE=/dev/disk/by-id/virtio-homelab-btrfs
BTRFS_ROOT=/mnt/homelab-backup-btrfs
HELPER_IMAGE=homelab/volume-rsync:1
VENV=/opt/homelab-backup-test-venv

for command in btrfs docker mkfs.btrfs mountpoint python3 rsync shellcheck; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'ERROR: missing guest command: %s\n' "$command" >&2
    exit 1
  }
done

[[ -b "$BTRFS_DEVICE" ]] || {
  printf 'ERROR: dedicated Btrfs test disk is missing: %s\n' "$BTRFS_DEVICE" >&2
  exit 1
}

if ! blkid -p -s TYPE -o value "$BTRFS_DEVICE" 2>/dev/null | grep -qx btrfs; then
  wipefs --all "$BTRFS_DEVICE"
  mkfs.btrfs -q -f -L homelab-backup-test "$BTRFS_DEVICE"
fi
install -d -o root -g root -m 0700 "$BTRFS_ROOT"
if ! mountpoint -q "$BTRFS_ROOT"; then
  mount -t btrfs -o noatime "$BTRFS_DEVICE" "$BTRFS_ROOT"
fi

systemctl is-active --quiet docker || systemctl start docker
docker build -q -t "$HELPER_IMAGE" -f "$ROOT_DIR/Dockerfile.volume-rsync" "$ROOT_DIR" >/dev/null

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install \
  --disable-pip-version-check --require-hashes -r "$ROOT_DIR/requirements.txt"

sudo -u tester "$VENV/bin/python" -m py_compile \
  "$ROOT_DIR/backupctl" "$ROOT_DIR"/homelab_backup/*.py
bash -n \
  "$ROOT_DIR/install.sh" \
  "$ROOT_DIR/backup-configs.sh" \
  "$ROOT_DIR/restore-configs.sh" \
  "$ROOT_DIR/config-ops-runtime.sh"
shellcheck \
  "$ROOT_DIR/install.sh" \
  "$ROOT_DIR/backup-configs.sh" \
  "$ROOT_DIR/restore-configs.sh" \
  "$ROOT_DIR/config-ops-runtime.sh"

cd -- "$ROOT_DIR"
sudo -u tester "$VENV/bin/python" -m unittest discover -s tests -v

HOMELAB_BACKUP_INTEGRATION=1 \
HOMELAB_BACKUP_BTRFS_ROOT="$BTRFS_ROOT" \
HOMELAB_BACKUP_VOLUME_HELPER_IMAGE="$HELPER_IMAGE" \
  "$VENV/bin/python" -m unittest tests.test_integration -v

printf '\nVM integration tests completed successfully.\n'
