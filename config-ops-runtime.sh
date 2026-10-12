#!/usr/bin/env bash

CONFIG_OPS_BIN=""
CONFIG_OPS_RELEASE_FDS=()

resolve_config_ops_runtime() {
  local release lease_fd candidate
  [[ -z "$CONFIG_OPS_BIN" ]] || return 0
  candidate=/usr/local/lib/homelab-backup/current/bin/backupctl
  if [[ -x "$candidate" && ! -L "$candidate" ]]; then
    release="$(readlink -f -- /usr/local/lib/homelab-backup/current)"
    [[ "$release" == /usr/local/lib/homelab-backup/releases/release.* &&
       -x "$release/bin/backupctl" && ! -L "$release/bin/backupctl" ]] || {
      printf 'ERROR: installed config helper release is invalid: %s\n' "$release" >&2
      return 1
    }
    [[ -f "$release/.lease" && ! -L "$release/.lease" ]] || {
      printf 'ERROR: installed config helper release lease is invalid: %s\n' "$release/.lease" >&2
      return 1
    }
    exec {lease_fd}<"$release/.lease"
    flock -s "$lease_fd"
    CONFIG_OPS_RELEASE_FDS+=("$lease_fd")
    CONFIG_OPS_BIN="$release/bin/backupctl"
    return 0
  fi
  if [[ -x "$ROOT_DIR/target/release/backupctl" && ! -L "$ROOT_DIR/target/release/backupctl" ]]; then
    CONFIG_OPS_BIN="$ROOT_DIR/target/release/backupctl"
    return 0
  fi
  if [[ -x "$ROOT_DIR/target/debug/backupctl" && ! -L "$ROOT_DIR/target/debug/backupctl" ]]; then
    CONFIG_OPS_BIN="$ROOT_DIR/target/debug/backupctl"
    return 0
  fi
  printf 'ERROR: backupctl is not built; run cargo build --release -p backupctl or install.sh\n' >&2
  return 1
}

run_config_ops() {
  resolve_config_ops_runtime
  "$CONFIG_OPS_BIN" config-ops "$@"
}

run_config_ops_as_user() {
  local user=$1 uid=$2
  shift 2
  resolve_config_ops_runtime
  if ((EUID == uid)); then
    "$CONFIG_OPS_BIN" config-ops "$@"
  else
    runuser --user "$user" -- "$CONFIG_OPS_BIN" config-ops "$@"
  fi
}
