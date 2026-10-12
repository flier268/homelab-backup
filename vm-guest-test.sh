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

for command in btrfs docker mkfs.btrfs mountpoint rsync shellcheck curl; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'ERROR: missing guest command: %s\n' "$command" >&2
    exit 1
  }
done

if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs |
    sudo -u tester sh -s -- -y --default-toolchain 1.96.0 --profile minimal
fi
sudo -u tester bash -lc 'source "$HOME/.cargo/env" && rustup default 1.96.0'

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

bash -n \
  "$ROOT_DIR/install.sh" \
  "$ROOT_DIR/backup-configs.sh" \
  "$ROOT_DIR/restore-configs.sh" \
  "$ROOT_DIR/config-ops-runtime.sh" \
  "$ROOT_DIR/backupctl"
shellcheck \
  "$ROOT_DIR/install.sh" \
  "$ROOT_DIR/backup-configs.sh" \
  "$ROOT_DIR/restore-configs.sh" \
  "$ROOT_DIR/config-ops-runtime.sh" \
  "$ROOT_DIR/backupctl"

cd -- "$ROOT_DIR"
sudo -u tester bash -lc 'source "$HOME/.cargo/env" && cargo test --workspace'

printf '\nVM tests completed successfully.\n'
