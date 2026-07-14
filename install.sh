#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT_DIR"
apt-get update
apt-get install -y python3 python3-yaml rsync restic rclone ca-certificates openssl
LIB_ROOT=/usr/local/lib/homelab-backup
install -d -m 0755 "$LIB_ROOT/homelab_backup"
install -m 0644 homelab_backup/*.py "$LIB_ROOT/homelab_backup/"
install -m 0755 backupctl /usr/local/sbin/backupctl
install -d -m 0700 /etc/homelab-backup/rclone
install -d -m 0700 /var/lib/homelab-backup/{staging,restores,state}
install -d -m 0755 /var/cache/homelab-backup/restic /srv/stacks
if [[ ! -f /etc/homelab-backup/restic-password ]]; then
  umask 077; openssl rand -base64 48 > /etc/homelab-backup/restic-password
fi
if [[ ! -f /etc/homelab-backup/config.yaml ]]; then
  install -m 0600 config.yaml.example /etc/homelab-backup/config.yaml
fi
docker build -t homelab/volume-rsync:1 -f Dockerfile.volume-rsync .
install -m 0644 systemd/homelab-backup.service /etc/systemd/system/
install -m 0644 systemd/homelab-backup.timer /etc/systemd/system/
install -m 0644 systemd/homelab-backup-maintenance.service /etc/systemd/system/
install -m 0644 systemd/homelab-backup-maintenance.timer /etc/systemd/system/
systemctl daemon-reload
echo 'Installed. Timers are not enabled yet.'
