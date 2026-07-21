#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT_DIR"
apt-get update
apt-get install -y python3 python3-venv rsync restic rclone age ca-certificates openssl btrfs-progs util-linux
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo 'ERROR: homelab-backup requires Python 3.10 or newer.' >&2
  exit 1
fi
LIB_ROOT=/usr/local/lib/homelab-backup
RELEASES_ROOT="$LIB_ROOT/releases"
CURRENT_ROOT="$LIB_ROOT/current"
PREVIOUS_ROOT="$LIB_ROOT/previous"
install -d -m 0755 "$LIB_ROOT" "$RELEASES_ROOT"
exec 9>"$LIB_ROOT/install.lock"
if ! flock -n 9; then
  echo "ERROR: another homelab-backup installation is already running." >&2
  exit 1
fi

# Build the application and its dependencies in one immutable release. The
# launcher sees both only after every runtime artifact is ready and the current
# symlink is atomically replaced.
OLD_RELEASE=
if [[ -e "$CURRENT_ROOT" || -L "$CURRENT_ROOT" ]]; then
  if [[ ! -L "$CURRENT_ROOT" ]]; then
    echo "ERROR: current release is not a symbolic link: $CURRENT_ROOT" >&2
    exit 1
  fi
  OLD_RELEASE="$(readlink -f -- "$CURRENT_ROOT")"
  if [[ "$(dirname -- "$OLD_RELEASE")" != "$RELEASES_ROOT" ||
        "$(basename -- "$OLD_RELEASE")" != release.* ]]; then
    echo "ERROR: current release points outside $RELEASES_ROOT" >&2
    exit 1
  fi
  if [[ ! -d "$OLD_RELEASE" ]]; then
    echo "ERROR: current release does not exist: $OLD_RELEASE" >&2
    exit 1
  fi
fi
RELEASE_NEXT="$(mktemp -d "$RELEASES_ROOT/release.XXXXXX")"
RELEASE_NAME="$(basename -- "$RELEASE_NEXT")"
HELPER_IMAGE="homelab/volume-rsync:$RELEASE_NAME"
CURRENT_NEXT="$LIB_ROOT/.current.next.$$"
PREVIOUS_NEXT="$LIB_ROOT/.previous.next.$$"
LAUNCHER_NEXT="/usr/local/sbin/.backupctl.next.$$"
INSTALL_BACKUP="$(mktemp -d "$LIB_ROOT/.install-backup.XXXXXX")"
UNIT_NAMES=(
  homelab-backup.service
  homelab-backup.timer
  homelab-backup-maintenance.service
  homelab-backup-maintenance.timer
)
UNIT_NEXT_PATHS=()
CURRENT_PUBLISHED=0
PREVIOUS_PUBLISHED=0
LAUNCHER_PUBLISHED=0
UNITS_PUBLISHED=0
HAD_PREVIOUS=0
HAD_LAUNCHER=0
restore_install_path() {
  source_path=$1
  destination_path=$2
  description=$3
  rollback_path="${destination_path}.rollback.$$"
  rm -f -- "$rollback_path"
  if cp -a -- "$source_path" "$rollback_path" &&
     mv -Tf -- "$rollback_path" "$destination_path"; then
    return 0
  fi
  rm -f -- "$rollback_path"
  echo "WARNING: could not roll back $description" >&2
  return 1
}
cleanup_release_install() {
  status=$?
  trap - EXIT
  set +e
  if (( CURRENT_PUBLISHED )); then
    rollback_current="$LIB_ROOT/.current.rollback.$$"
    if [[ -n "$OLD_RELEASE" ]]; then
      if ln -s -- "releases/$(basename -- "$OLD_RELEASE")" "$rollback_current" &&
         mv -Tf -- "$rollback_current" "$CURRENT_ROOT"; then
        CURRENT_PUBLISHED=0
      else
        echo "WARNING: could not roll back current release; retaining new release" >&2
      fi
    elif rm -f -- "$CURRENT_ROOT"; then
      CURRENT_PUBLISHED=0
    else
      echo "WARNING: could not remove newly published current release" >&2
    fi
  fi
  if (( LAUNCHER_PUBLISHED )); then
    if (( HAD_LAUNCHER )); then
      restore_install_path \
        "$INSTALL_BACKUP/backupctl" /usr/local/sbin/backupctl launcher
    else
      rm -f -- /usr/local/sbin/backupctl ||
        echo "WARNING: could not remove newly published launcher" >&2
    fi
  fi
  if (( PREVIOUS_PUBLISHED )); then
    if (( HAD_PREVIOUS )); then
      restore_install_path \
        "$INSTALL_BACKUP/previous" "$PREVIOUS_ROOT" "previous release link"
    else
      rm -f -- "$PREVIOUS_ROOT" ||
        echo "WARNING: could not remove newly published previous release link" >&2
    fi
  fi
  if (( UNITS_PUBLISHED )); then
    for unit in "${UNIT_NAMES[@]}"; do
      unit_path="/etc/systemd/system/$unit"
      if [[ -e "$INSTALL_BACKUP/$unit" || -L "$INSTALL_BACKUP/$unit" ]]; then
        restore_install_path \
          "$INSTALL_BACKUP/$unit" "$unit_path" "systemd unit: $unit"
      else
        rm -f -- "$unit_path" ||
          echo "WARNING: could not remove newly published systemd unit: $unit" >&2
      fi
    done
    systemctl daemon-reload ||
      echo "WARNING: systemd daemon-reload failed during rollback" >&2
  fi
  if [[ -n "${CURRENT_NEXT:-}" && ( -e "$CURRENT_NEXT" || -L "$CURRENT_NEXT" ) ]]; then
    rm -f -- "$CURRENT_NEXT"
  fi
  if [[ -n "${PREVIOUS_NEXT:-}" && ( -e "$PREVIOUS_NEXT" || -L "$PREVIOUS_NEXT" ) ]]; then
    rm -f -- "$PREVIOUS_NEXT"
  fi
  if [[ -n "${LAUNCHER_NEXT:-}" && ( -e "$LAUNCHER_NEXT" || -L "$LAUNCHER_NEXT" ) ]]; then
    rm -f -- "$LAUNCHER_NEXT"
  fi
  for unit_next in "${UNIT_NEXT_PATHS[@]}"; do
    rm -f -- "$unit_next"
  done
  if [[ -n "${RELEASE_NEXT:-}" && -e "$RELEASE_NEXT" ]] &&
     (( ! CURRENT_PUBLISHED )); then
    rm -rf -- "$RELEASE_NEXT"
  fi
  if [[ -n "${HELPER_IMAGE:-}" ]] && (( ! CURRENT_PUBLISHED )); then
    docker image rm "$HELPER_IMAGE" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$INSTALL_BACKUP"
  exit "$status"
}
trap cleanup_release_install EXIT
chmod 0755 "$RELEASE_NEXT"
install -m 0644 /dev/null "$RELEASE_NEXT/.lease"
python3 -m venv "$RELEASE_NEXT/venv"
"$RELEASE_NEXT/venv/bin/python" -m pip install \
  --require-hashes --no-deps --only-binary=:all: --upgrade \
  -r requirements.txt
install -d -m 0755 "$RELEASE_NEXT/app/homelab_backup"
install -m 0644 homelab_backup/*.py "$RELEASE_NEXT/app/homelab_backup/"
(
  cd -- "$RELEASE_NEXT"
  PYTHONPATH="$RELEASE_NEXT/app" \
    "$RELEASE_NEXT/venv/bin/python" -c 'import homelab_backup.cli'
)

# Prepare every shared runtime dependency before publishing the new Python
# release. A failure here leaves the existing current release active.
if [[ -e "$LAUNCHER_NEXT" || -L "$LAUNCHER_NEXT" ]]; then
  echo "ERROR: temporary launcher already exists: $LAUNCHER_NEXT" >&2
  exit 1
fi
install -m 0755 backupctl "$LAUNCHER_NEXT"
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
docker build -t "$HELPER_IMAGE" -f Dockerfile.volume-rsync .
docker run --rm --network none "$HELPER_IMAGE" rsync --version >/dev/null
install -m 0644 /dev/null "$RELEASE_NEXT/volume-helper-image"
printf '%s\n' "$HELPER_IMAGE" > "$RELEASE_NEXT/volume-helper-image"

if [[ -e "$CURRENT_NEXT" || -L "$CURRENT_NEXT" ]]; then
  echo "ERROR: temporary release link already exists: $CURRENT_NEXT" >&2
  exit 1
fi
if [[ -e "$PREVIOUS_NEXT" || -L "$PREVIOUS_NEXT" ]]; then
  echo "ERROR: temporary previous link already exists: $PREVIOUS_NEXT" >&2
  exit 1
fi
if [[ -e "$PREVIOUS_ROOT" || -L "$PREVIOUS_ROOT" ]]; then
  HAD_PREVIOUS=1
  cp -a -- "$PREVIOUS_ROOT" "$INSTALL_BACKUP/previous"
fi
if [[ -e /usr/local/sbin/backupctl || -L /usr/local/sbin/backupctl ]]; then
  HAD_LAUNCHER=1
  cp -a -- /usr/local/sbin/backupctl "$INSTALL_BACKUP/backupctl"
fi
for unit in "${UNIT_NAMES[@]}"; do
  unit_path="/etc/systemd/system/$unit"
  unit_next="$unit_path.next.$$"
  if [[ -e "$unit_next" || -L "$unit_next" ]]; then
    echo "ERROR: temporary systemd unit already exists: $unit_next" >&2
    exit 1
  fi
  if [[ -e "$unit_path" || -L "$unit_path" ]]; then
    cp -a -- "$unit_path" "$INSTALL_BACKUP/$unit"
  fi
  install -m 0644 "systemd/$unit" "$unit_next"
  UNIT_NEXT_PATHS+=("$unit_next")
done

UNITS_PUBLISHED=1
for index in "${!UNIT_NAMES[@]}"; do
  mv -Tf -- "${UNIT_NEXT_PATHS[$index]}" \
    "/etc/systemd/system/${UNIT_NAMES[$index]}"
done
UNIT_NEXT_PATHS=()
systemctl daemon-reload

LAUNCHER_PUBLISHED=1
mv -Tf -- "$LAUNCHER_NEXT" /usr/local/sbin/backupctl
LAUNCHER_NEXT=

if [[ -n "$OLD_RELEASE" ]]; then
  ln -s -- "releases/$(basename -- "$OLD_RELEASE")" "$PREVIOUS_NEXT"
  PREVIOUS_PUBLISHED=1
  mv -Tf -- "$PREVIOUS_NEXT" "$PREVIOUS_ROOT"
  PREVIOUS_NEXT=
fi
ln -s -- "releases/$(basename -- "$RELEASE_NEXT")" "$CURRENT_NEXT"
CURRENT_PUBLISHED=1
mv -Tf -- "$CURRENT_NEXT" "$CURRENT_ROOT"
CURRENT_NEXT=
NEW_RELEASE="$RELEASE_NEXT"
RELEASE_NEXT=
HELPER_IMAGE=
trap - EXIT
rm -rf -- "$INSTALL_BACKUP" ||
  echo "WARNING: could not remove installer rollback data: $INSTALL_BACKUP" >&2

# Publication is complete. Cleanup is deliberately best-effort so a pruning
# problem cannot turn a successful activation into a reported failed install.
for release in "$RELEASES_ROOT"/release.*; do
  [[ -e "$release" ]] || continue
  if [[ "$release" != "$NEW_RELEASE" && "$release" != "$OLD_RELEASE" ]]; then
    if [[ ! -f "$release/.lease" || -L "$release/.lease" ]]; then
      echo "WARNING: release lease is missing or unsafe; retaining release: $release" >&2
      continue
    fi
    exec {release_lease_fd}<"$release/.lease"
    if ! flock -n -x "$release_lease_fd"; then
      echo "WARNING: active process still uses release; retaining it: $release" >&2
      exec {release_lease_fd}<&-
      continue
    fi
    obsolete_helper=
    if [[ -f "$release/volume-helper-image" ]]; then
      obsolete_helper="$(sed -n '1p' "$release/volume-helper-image")"
    fi
    if [[ -n "$obsolete_helper" &&
          ! "$obsolete_helper" =~ ^homelab/volume-rsync:release\.[A-Za-z0-9]+$ ]]; then
      echo "WARNING: invalid obsolete helper image metadata; retaining release for inspection: $release" >&2
      exec {release_lease_fd}<&-
      continue
    fi
    if [[ -n "$obsolete_helper" ]]; then
      obsolete_image_id=
      if ! obsolete_image_id="$(
        docker image ls --quiet --no-trunc "$obsolete_helper"
      )"; then
        echo "WARNING: could not inspect obsolete helper image; retaining release metadata for retry: $obsolete_helper" >&2
        exec {release_lease_fd}<&-
        continue
      fi
      if [[ -n "$obsolete_image_id" ]] &&
         ! docker image rm "$obsolete_helper" >/dev/null 2>&1; then
        echo "WARNING: could not remove obsolete helper image; retaining release metadata for retry: $obsolete_helper" >&2
        exec {release_lease_fd}<&-
        continue
      fi
    fi
    if ! rm -rf -- "$release"; then
      echo "WARNING: could not remove obsolete release: $release" >&2
    fi
    exec {release_lease_fd}<&-
  fi
done
echo 'Installed. Timers are not enabled yet.'
