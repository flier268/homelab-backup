//! Restic stays an external program. This module only builds and parses its output.

use std::collections::BTreeMap;

use homelab_backup_domain::manifest::RETENTION_FLAGS;
use homelab_backup_domain::yaml::Yaml;

use crate::unix::error::SecurityError;

pub fn snapshots_command(host_id: &str, filters: &[String]) -> Vec<String> {
    let mut command = vec![
        "restic".to_owned(),
        "snapshots".to_owned(),
        "--json".to_owned(),
        "--host".to_owned(),
        host_id.to_owned(),
    ];
    command.extend(filters.iter().cloned());
    command
}

pub fn retention_command(
    host_id: &str,
    service: &str,
    retention: &BTreeMap<String, Yaml>,
    prune: bool,
    dry_run: bool,
) -> Vec<String> {
    let mut command = vec![
        "restic".to_owned(),
        "forget".to_owned(),
        "--host".to_owned(),
        host_id.to_owned(),
        "--tag".to_owned(),
        format!("service:{service}"),
        "--group-by".to_owned(),
        "host,tags".to_owned(),
    ];
    for (key, flag) in RETENTION_FLAGS {
        if let Some(value) = retention.get(*key).and_then(Yaml::as_int) {
            command.push((*flag).to_owned());
            command.push(value.to_string());
        }
    }
    if prune {
        command.push("--prune".to_owned());
    }
    if dry_run {
        command.push("--dry-run".to_owned());
    }
    command
}

pub fn parse_repository_id(stdout: &str) -> Result<String, SecurityError> {
    let value: serde_json::Value = serde_json::from_str(stdout)
        .map_err(|_| SecurityError::runtime("Restic returned an invalid repository config"))?;
    let id = value
        .get("id")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if id.len() == 64 && id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        Ok(id.to_ascii_lowercase())
    } else {
        Err(SecurityError::runtime(
            "Restic repository config has an invalid repository ID",
        ))
    }
}

pub fn parse_snapshots(
    stdout: &str,
) -> Result<Vec<serde_json::Map<String, serde_json::Value>>, SecurityError> {
    let value: serde_json::Value =
        serde_json::from_str(if stdout.is_empty() { "[]" } else { stdout }).map_err(|error| {
            SecurityError::runtime(format!("invalid JSON from restic snapshots: {error}"))
        })?;
    let Some(items) = value.as_array() else {
        return Err(SecurityError::runtime(
            "invalid JSON structure from restic snapshots",
        ));
    };
    items
        .iter()
        .map(|item| {
            item.as_object().cloned().ok_or_else(|| {
                SecurityError::runtime("invalid JSON structure from restic snapshots")
            })
        })
        .collect()
}
