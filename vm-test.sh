#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${HOMELAB_VM_STATE_DIR:-$ROOT_DIR/.vm-test}"
IMAGE_NAME="noble-server-cloudimg-amd64.img"
IMAGE_URL="${HOMELAB_VM_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/$IMAGE_NAME}"
CHECKSUM_URL="${HOMELAB_VM_CHECKSUM_URL:-https://cloud-images.ubuntu.com/noble/current/SHA256SUMS}"
BASE_IMAGE="$STATE_DIR/$IMAGE_NAME"
ROOT_DISK="$STATE_DIR/root.qcow2"
BTRFS_DISK="$STATE_DIR/btrfs.qcow2"
SEED_ISO="$STATE_DIR/seed.iso"
SSH_KEY="$STATE_DIR/id_ed25519"
PID_FILE="$STATE_DIR/qemu.pid"
MONITOR_SOCKET="$STATE_DIR/qemu-monitor.sock"
SERIAL_LOG="$STATE_DIR/serial.log"
SSH_PORT="${HOMELAB_VM_SSH_PORT:-22222}"
VM_MEMORY_MB="${HOMELAB_VM_MEMORY_MB:-4096}"
VM_CPUS="${HOMELAB_VM_CPUS:-2}"

usage() {
  cat <<'EOF'
Usage: ./vm-test.sh COMMAND

Commands:
  prepare   Download and verify the cloud image; create VM disks and cloud-init seed.
  start     Start the VM and wait for SSH plus cloud-init.
  test      Recreate a clean VM, run all tests, then cleanly stop it.
  all       Alias for test (default).
  ssh       Open an interactive SSH session.
  status    Show whether the VM is running.
  stop      Ask the guest to power off; use the QEMU monitor as fallback.
  reset     Stop the VM and remove only its writable disks and generated seed.

Environment overrides:
  HOMELAB_VM_SSH_PORT      Host loopback SSH port (default: 22222)
  HOMELAB_VM_MEMORY_MB     Guest RAM in MiB (default: 4096)
  HOMELAB_VM_CPUS          Guest vCPU count (default: 2)
  HOMELAB_VM_STATE_DIR     VM state directory (default: .vm-test)
  HOMELAB_VM_IMAGE_URL     Ubuntu cloud image URL
  HOMELAB_VM_CHECKSUM_URL  SHA256SUMS URL matching the image
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'ERROR: missing command: %s\n' "$1" >&2
    exit 1
  }
}

pid_is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(sed -n '1p' "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

ssh_options=(
  -i "$SSH_KEY"
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
)

guest_ssh() {
  # Arguments intentionally become the remote command assembled by ssh.
  # shellcheck disable=SC2029
  ssh "${ssh_options[@]}" tester@127.0.0.1 "$@"
}

download_base_image() {
  [[ -f "$BASE_IMAGE" ]] && return
  printf 'Downloading Ubuntu 24.04 cloud image...\n'
  local partial checksums expected
  partial="$BASE_IMAGE.partial"
  checksums="$STATE_DIR/SHA256SUMS"
  curl --fail --location --retry 3 --output "$partial" "$IMAGE_URL"
  curl --fail --location --retry 3 --output "$checksums" "$CHECKSUM_URL"
  expected="$(awk -v name="$IMAGE_NAME" '$2 == name || $2 == "*" name {print $1; exit}' "$checksums")"
  [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || {
    printf 'ERROR: no checksum for %s in %s\n' "$IMAGE_NAME" "$CHECKSUM_URL" >&2
    exit 1
  }
  printf '%s  %s\n' "$expected" "$partial" | sha256sum --check -
  mv -- "$partial" "$BASE_IMAGE"
  chmod 0444 "$BASE_IMAGE"
}

create_seed() {
  [[ -f "$SSH_KEY" ]] || ssh-keygen -q -t ed25519 -N '' -f "$SSH_KEY"
  local public_key
  public_key="$(<"$SSH_KEY.pub")"
  cat >"$STATE_DIR/meta-data" <<'EOF'
instance-id: homelab-backup-test-v1
local-hostname: homelab-backup-test
EOF
  cat >"$STATE_DIR/user-data" <<EOF
#cloud-config
users:
  - default
  - name: tester
    gecos: Homelab Backup Tester
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $public_key
ssh_pwauth: false
disable_root: true
package_update: true
packages:
  - acl
  - attr
  - btrfs-progs
  - ca-certificates
  - curl
  - docker.io
  - python3-venv
  - rclone
  - restic
  - rsync
  - shellcheck
runcmd:
  - [systemctl, enable, --now, docker]
  - [usermod, -aG, docker, tester]
final_message: homelab-backup test VM is ready
EOF
  genisoimage -quiet -output "$SEED_ISO" -volid cidata -joliet -rock \
    "$STATE_DIR/user-data" "$STATE_DIR/meta-data"
}

prepare_vm() {
  for command in curl genisoimage qemu-img qemu-system-x86_64 sha256sum ssh ssh-keygen; do
    require_command "$command"
  done
  mkdir -p -- "$STATE_DIR"
  chmod 0700 "$STATE_DIR"
  download_base_image
  if [[ ! -f "$ROOT_DISK" ]]; then
    qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$ROOT_DISK" 32G
  fi
  if [[ ! -f "$BTRFS_DISK" ]]; then
    qemu-img create -q -f qcow2 "$BTRFS_DISK" 8G
  fi
  create_seed
  printf 'VM prepared in %s\n' "$STATE_DIR"
}

start_vm() {
  prepare_vm
  if pid_is_running; then
    printf 'VM is already running.\n'
    return
  fi
  rm -f -- "$PID_FILE" "$MONITOR_SOCKET" "$SERIAL_LOG"
  local acceleration=()
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    acceleration=(-enable-kvm -cpu host)
    printf 'Starting VM with KVM acceleration.\n'
  else
    acceleration=(-accel 'tcg,thread=multi' -cpu max)
    printf 'WARNING: /dev/kvm is unavailable; using slower TCG emulation.\n' >&2
  fi
  qemu-system-x86_64 \
    "${acceleration[@]}" \
    -machine q35 \
    -smp "$VM_CPUS" \
    -m "$VM_MEMORY_MB" \
    -drive "if=none,id=rootdisk,format=qcow2,file=$ROOT_DISK" \
    -device virtio-blk-pci,drive=rootdisk,serial=homelab-root,bootindex=1 \
    -drive "if=none,id=btrfs,format=qcow2,file=$BTRFS_DISK" \
    -device virtio-blk-pci,drive=btrfs,serial=homelab-btrfs \
    -drive "if=none,id=seed,format=raw,readonly=on,file=$SEED_ISO" \
    -device virtio-blk-pci,drive=seed,serial=homelab-seed \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
    -device virtio-net-pci,netdev=net0 \
    -display none \
    -serial "file:$SERIAL_LOG" \
    -monitor "unix:$MONITOR_SOCKET,server=on,wait=off" \
    -pidfile "$PID_FILE" \
    -daemonize

  printf 'Waiting for SSH on 127.0.0.1:%s...\n' "$SSH_PORT"
  local _attempt
  for _attempt in $(seq 1 240); do
    if guest_ssh true 2>/dev/null; then
      guest_ssh sudo cloud-init status --wait
      printf 'VM is ready.\n'
      return
    fi
    if ! pid_is_running; then
      printf 'ERROR: QEMU exited during boot; inspect %s\n' "$SERIAL_LOG" >&2
      exit 1
    fi
    sleep 5
  done
  printf 'ERROR: VM did not become ready; inspect %s\n' "$SERIAL_LOG" >&2
  exit 1
}

copy_project() {
  require_command rsync
  guest_ssh \
    'sudo install -d -o tester -g tester -m 0755 /home/tester/homelab-backup && sudo find /home/tester/homelab-backup -mindepth 1 -delete' \
    || return
  rsync -a --delete \
    --exclude .codegraph/ \
    --exclude .git/ \
    --exclude .pytest_cache/ \
    --exclude .venv/ \
    --exclude .vm-test/ \
    --exclude __pycache__/ \
    -e "ssh -i $SSH_KEY -p $SSH_PORT -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR" \
    "$ROOT_DIR/" tester@127.0.0.1:/home/tester/homelab-backup/ \
    || return
}

run_tests_in_vm() {
  pid_is_running || start_vm || return
  copy_project || return
  guest_ssh 'cd /home/tester/homelab-backup && sudo ./vm-guest-test.sh' || return
}

stop_vm() {
  if ! pid_is_running; then
    printf 'VM is not running.\n'
    rm -f -- "$PID_FILE" "$MONITOR_SOCKET"
    return
  fi
  guest_ssh sudo poweroff >/dev/null 2>&1 || true
  local _attempt
  for _attempt in $(seq 1 30); do
    pid_is_running || {
      rm -f -- "$PID_FILE" "$MONITOR_SOCKET"
      printf 'VM stopped.\n'
      return
    }
    sleep 2
  done
  if [[ -S "$MONITOR_SOCKET" ]] && command -v socat >/dev/null 2>&1; then
    printf 'system_powerdown\n' | socat - UNIX-CONNECT:"$MONITOR_SOCKET" || true
    sleep 5
  fi
  if pid_is_running; then
    printf 'ERROR: VM did not stop cleanly; QEMU PID is %s\n' "$(sed -n '1p' "$PID_FILE")" >&2
    return 1
  fi
  rm -f -- "$PID_FILE" "$MONITOR_SOCKET"
}

reset_vm() {
  stop_vm
  rm -f -- \
    "$ROOT_DISK" "$BTRFS_DISK" "$SEED_ISO" \
    "$STATE_DIR/meta-data" "$STATE_DIR/user-data" \
    "$PID_FILE" "$MONITOR_SOCKET" "$SERIAL_LOG"
  printf 'Writable VM state removed; the verified base image and SSH key were retained.\n'
}

run_clean_tests() {
  reset_vm || return
  start_vm || return
  local test_status=0 stop_status=0
  run_tests_in_vm || test_status=$?
  stop_vm || stop_status=$?
  if ((test_status != 0)); then
    return "$test_status"
  fi
  return "$stop_status"
}

command_name="${1:-all}"
case "$command_name" in
  prepare) prepare_vm ;;
  start) start_vm ;;
  test|all) run_clean_tests ;;
  ssh)
    pid_is_running || start_vm
    exec ssh "${ssh_options[@]}" tester@127.0.0.1
    ;;
  status)
    if pid_is_running; then
      printf 'running pid=%s ssh=127.0.0.1:%s\n' "$(sed -n '1p' "$PID_FILE")" "$SSH_PORT"
    else
      printf 'stopped\n'
    fi
    ;;
  stop) stop_vm ;;
  reset) reset_vm ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
