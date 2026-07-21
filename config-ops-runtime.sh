#!/usr/bin/env bash

CONFIG_OPS_PYTHON=""
CONFIG_OPS_MODULE=""
CONFIG_OPS_RELEASE_FDS=()

resolve_config_ops_runtime() {
  local release lease_fd
  [[ -z "$CONFIG_OPS_PYTHON" ]] || return 0
  if [[ -x /usr/local/lib/homelab-backup/current/venv/bin/python &&
        -d /usr/local/lib/homelab-backup/current/app ]]; then
    release="$(readlink -f -- /usr/local/lib/homelab-backup/current)"
    [[ "$release" == /usr/local/lib/homelab-backup/releases/release.* &&
       -x "$release/venv/bin/python" && -d "$release/app" ]] || {
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
    CONFIG_OPS_PYTHON="$release/venv/bin/python"
    CONFIG_OPS_MODULE="$release/app"
  elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    CONFIG_OPS_PYTHON="$ROOT_DIR/.venv/bin/python"
    CONFIG_OPS_MODULE="$ROOT_DIR"
  else
    CONFIG_OPS_PYTHON=python3
    CONFIG_OPS_MODULE="$ROOT_DIR"
  fi
}

run_config_ops() {
  resolve_config_ops_runtime
  PYTHONPATH="$CONFIG_OPS_MODULE" \
    "$CONFIG_OPS_PYTHON" -m homelab_backup.config_ops "$@"
}

run_config_ops_as_user() {
  local user=$1 uid=$2
  shift 2
  resolve_config_ops_runtime
  if ((EUID == uid)); then
    PYTHONPATH="$CONFIG_OPS_MODULE" \
      "$CONFIG_OPS_PYTHON" -m homelab_backup.config_ops "$@"
  else
    runuser --user "$user" -- env PYTHONPATH="$CONFIG_OPS_MODULE" \
      "$CONFIG_OPS_PYTHON" -m homelab_backup.config_ops "$@"
  fi
}
