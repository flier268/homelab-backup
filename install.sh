#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT_DIR"
apt-get update
apt-get install -y python3 python3-venv rsync restic rclone age ca-certificates openssl btrfs-progs
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo 'ERROR: homelab-backup requires Python 3.10 or newer.' >&2
  exit 1
fi
LIB_ROOT=/usr/local/lib/homelab-backup
APP_ROOT="$LIB_ROOT/app"
VENV_ROOT="$LIB_ROOT/venv"
install -d -m 0755 "$LIB_ROOT"
if [[ -x "$VENV_ROOT/bin/python" ]]; then
  python3 -m venv --upgrade "$VENV_ROOT"
else
  python3 -m venv "$VENV_ROOT"
fi
"$VENV_ROOT/bin/python" -m pip install \
  --require-hashes --no-deps --only-binary=:all: --upgrade \
  -r requirements.txt

# Build a complete application tree off to the side, then publish it as one
# unit. Removed modules therefore cannot survive an upgrade.
APP_NEXT="$(mktemp -d "$LIB_ROOT/.app.next.XXXXXX")"
APP_PREVIOUS="$(mktemp -d "$LIB_ROOT/.app.previous.XXXXXX")"
rmdir -- "$APP_PREVIOUS"
cleanup_app_install() {
  if [[ -n "${APP_NEXT:-}" && -e "$APP_NEXT" ]]; then
    rm -rf -- "$APP_NEXT"
  fi
  if [[ -n "${APP_PREVIOUS:-}" && -e "$APP_PREVIOUS" && ! -e "$APP_ROOT" ]]; then
    mv -- "$APP_PREVIOUS" "$APP_ROOT"
  fi
}
trap cleanup_app_install EXIT
chmod 0755 "$APP_NEXT"
install -d -m 0755 "$APP_NEXT/homelab_backup"
install -m 0644 homelab_backup/*.py "$APP_NEXT/homelab_backup/"
if [[ -e "$APP_ROOT" ]]; then
  mv -- "$APP_ROOT" "$APP_PREVIOUS"
fi
mv -- "$APP_NEXT" "$APP_ROOT"
APP_NEXT=
if [[ -e "$APP_PREVIOUS" ]]; then
  rm -rf -- "$APP_PREVIOUS"
fi
APP_PREVIOUS=
trap - EXIT
install -m 0755 backupctl /usr/local/sbin/backupctl
install -d -o root -g root -m 0700 \
  /etc/homelab-backup /etc/homelab-backup/rclone
install -d -m 0700 /var/lib/homelab-backup/{staging,restores,state}
install -d -o root -g root -m 0700 /run/homelab-backup
install -d -o root -g root -m 0755 /var/cache/homelab-backup/restic /srv/stacks /srv/data
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
