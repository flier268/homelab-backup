//! Per-service backup state stored beside the repository cache.

use std::collections::BTreeMap;

use std::io::Read;

use homelab_backup_domain::names::valid_service_name;
use homelab_backup_domain::paths::{lexical_absolute, path_contains};
use rustix::fs::{CWD, Mode, OFlags, fstat, openat};
use rustix::io::Errno;

use crate::unix::atomic::atomic_write_json;
use crate::unix::control::{ensure_control_directory, is_reg, mode_of};
use crate::unix::error::SecurityError;

pub fn load_backup_state(
    state_root: &str,
    service: &str,
) -> Result<BTreeMap<String, serde_json::Value>, SecurityError> {
    let path = state_file(state_root, service)?;
    let fd = match openat(
        CWD,
        path.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Ok(BTreeMap::new()),
        Err(Errno::LOOP) => {
            return Err(SecurityError::invalid(format!(
                "{path}: backup state must be a regular file"
            )));
        }
        Err(error) => {
            return Err(SecurityError::invalid(format!(
                "{path}: could not open backup state: {error}"
            )));
        }
    };
    let metadata = fstat(&fd).map_err(|error| {
        SecurityError::invalid(format!("{path}: could not open backup state: {error}"))
    })?;
    if !is_reg(mode_of(&metadata)) {
        return Err(SecurityError::invalid(format!(
            "{path}: backup state must be a regular file"
        )));
    }
    let mut file = std::fs::File::from(fd);
    let mut text = String::new();
    file.read_to_string(&mut text).map_err(|error| {
        SecurityError::invalid(format!("{path}: could not load backup state: {error}"))
    })?;
    let value: serde_json::Value = serde_json::from_str(&text).map_err(|error| {
        SecurityError::invalid(format!("{path}: could not load backup state: {error}"))
    })?;
    validate_state(value, service, &path)
}

pub fn invalidate_snapshot_fingerprint(
    state_root: &str,
    service: &str,
) -> Result<(), SecurityError> {
    let mut state = load_backup_state(state_root, service)?;
    let mut changed = false;
    for field in ["last_snapshot_fingerprint", "last_snapshot_context"] {
        if state.get(field).is_some_and(|value| !value.is_null()) {
            state.remove(field);
            changed = true;
        }
    }
    if changed {
        save_backup_state(state_root, service, &state)?;
    }
    Ok(())
}

pub fn save_backup_state(
    state_root: &str,
    service: &str,
    state: &BTreeMap<String, serde_json::Value>,
) -> Result<(), SecurityError> {
    let path = state_file(state_root, service)?;
    let value = serde_json::Value::Object(state.clone().into_iter().collect());
    validate_state(value.clone(), service, &path)?;
    ensure_control_directory(state_root, 0o700)?;
    atomic_write_json(std::path::Path::new(&path), &value)
}

fn state_file(state_root: &str, service: &str) -> Result<String, SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "invalid service name for backup state: '{service}'"
        )));
    }
    let root =
        lexical_absolute(state_root).map_err(|error| SecurityError::invalid(error.message))?;
    let path = if root == "/" {
        format!("/{service}.json")
    } else {
        format!("{root}/{service}.json")
    };
    if !path_contains(&root, &path).unwrap_or(false) {
        return Err(SecurityError::invalid(format!(
            "backup state path escapes state_root: {path}"
        )));
    }
    Ok(path)
}

fn validate_state(
    value: serde_json::Value,
    service: &str,
    source: &str,
) -> Result<BTreeMap<String, serde_json::Value>, SecurityError> {
    let serde_json::Value::Object(map) = value else {
        return Err(SecurityError::invalid(format!(
            "{source}: backup state must be a JSON mapping"
        )));
    };
    let state: BTreeMap<_, _> = map.into_iter().collect();
    let allowed = [
        "service",
        "first_seen_at",
        "last_result",
        "last_success_at",
        "last_attempt_at",
        "last_finished_at",
        "last_duration_seconds",
        "last_error",
        "last_retention_error",
        "last_snapshot_fingerprint",
        "last_snapshot_context",
    ];
    let unknown = state
        .keys()
        .filter(|key| !allowed.contains(&key.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        return Err(SecurityError::invalid(format!(
            "{source}: unsupported backup state fields: {}",
            python_list(&unknown)
        )));
    }
    if let Some(found) = state.get("service").and_then(serde_json::Value::as_str)
        && found != service
    {
        return Err(SecurityError::invalid(format!(
            "{source}: service does not match '{service}'"
        )));
    }
    if let Some(result) = state.get("last_result")
        && result
            .as_str()
            .is_none_or(|result| !matches!(result, "running" | "success" | "failed"))
    {
        return Err(SecurityError::invalid(format!(
            "{source}: last_result is invalid"
        )));
    }
    for field in [
        "first_seen_at",
        "last_success_at",
        "last_attempt_at",
        "last_finished_at",
    ] {
        if state.contains_key(field) && timestamp(state.get(field)).is_none() {
            return Err(SecurityError::invalid(format!(
                "{source}: {field} must be a timezone-aware timestamp"
            )));
        }
    }
    if let Some(duration) = state.get("last_duration_seconds") {
        let valid = duration
            .as_f64()
            .is_some_and(|value| value.is_finite() && value >= 0.0);
        if !valid {
            return Err(SecurityError::invalid(format!(
                "{source}: last_duration_seconds must be non-negative"
            )));
        }
    }
    for field in [
        "last_error",
        "last_retention_error",
        "last_snapshot_fingerprint",
        "last_snapshot_context",
    ] {
        if let Some(value) = state.get(field)
            && !value.is_null()
            && !value.is_string()
        {
            return Err(SecurityError::invalid(format!(
                "{source}: {field} must be a string or null"
            )));
        }
    }
    for field in ["last_snapshot_fingerprint", "last_snapshot_context"] {
        if let Some(value) = state.get(field).and_then(serde_json::Value::as_str)
            && !valid_fingerprint(value)
        {
            return Err(SecurityError::invalid(format!(
                "{source}: {field} must be a SHA-256 fingerprint"
            )));
        }
    }
    Ok(state)
}

fn timestamp(value: Option<&serde_json::Value>) -> Option<()> {
    let text = value?.as_str()?;
    chrono::DateTime::parse_from_rfc3339(text).ok().map(|_| ())
}

fn valid_fingerprint(value: &str) -> bool {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return false;
    };
    hex.len() == 64
        && hex
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn python_list(items: &[String]) -> String {
    let body = items
        .iter()
        .map(|item| format!("'{item}'"))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{body}]")
}
