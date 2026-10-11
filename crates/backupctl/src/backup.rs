//! One service backup is one snapshot transaction.

use std::path::PathBuf;

use chrono::{DateTime, FixedOffset, SecondsFormat};
use homelab_backup_domain::manifest::service_label;
use homelab_backup_domain::yaml::Yaml;

use crate::fingerprint::snapshot_context_fingerprint;
use crate::unix::error::SecurityError;

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackupCommand {
    pub argv: Vec<String>,
    pub cwd: Option<PathBuf>,
}

type SaveState<'a> =
    &'a mut dyn FnMut(&BTreeMap<String, serde_json::Value>) -> Result<(), SecurityError>;

pub struct BackupDeps<'a> {
    pub now: &'a mut dyn FnMut() -> DateTime<FixedOffset>,
    pub load_state: &'a mut dyn FnMut() -> BTreeMap<String, serde_json::Value>,
    pub save_state: SaveState<'a>,
    pub stage: &'a mut dyn FnMut() -> Result<PathBuf, SecurityError>,
    pub fingerprint: &'a mut dyn FnMut(&PathBuf) -> Result<String, SecurityError>,
    pub repository_id: &'a mut dyn FnMut() -> Result<String, SecurityError>,
    pub run: &'a mut dyn FnMut(BackupCommand) -> Result<(), SecurityError>,
    pub remove_stage: &'a mut dyn FnMut(&PathBuf) -> Result<(), SecurityError>,
    pub on_success: &'a mut dyn FnMut() -> Result<(), SecurityError>,
    pub on_failure: &'a mut dyn FnMut(&SecurityError, &str),
}

pub fn execute_backup(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    allow_low_space: bool,
    deps: &mut BackupDeps<'_>,
) -> Result<bool, SecurityError> {
    let service = manifest.get("service").and_then(Yaml::as_str).unwrap_or("");
    let label = service_label(manifest);
    let started = (deps.now)();
    let mut state = (deps.load_state)();
    state.insert(
        "service".to_owned(),
        serde_json::Value::String(service.to_owned()),
    );
    state.insert("last_attempt_at".to_owned(), json_time(started));
    state.insert(
        "last_result".to_owned(),
        serde_json::Value::String("running".to_owned()),
    );
    state.insert("last_error".to_owned(), serde_json::Value::Null);
    state
        .entry("first_seen_at".to_owned())
        .or_insert_with(|| json_time(started));
    if let Err(error) = (deps.save_state)(&state) {
        (deps.on_failure)(&error, "state");
        return Err(error);
    }
    let _ = allow_low_space;

    let stage = match (deps.stage)() {
        Ok(stage) => stage,
        Err(error) => {
            record_failure(
                config, service, started, &mut state, &error, "staging", deps,
            );
            return Err(error);
        }
    };
    let fingerprint = match (deps.fingerprint)(&stage) {
        Ok(fingerprint) => fingerprint,
        Err(error) => {
            record_failure(
                config, service, started, &mut state, &error, "staging", deps,
            );
            return Err(error);
        }
    };
    let repository_id = match (deps.repository_id)() {
        Ok(repository_id) => repository_id,
        Err(error) => {
            record_failure(config, service, started, &mut state, &error, "restic", deps);
            return Err(error);
        }
    };
    let host_id = config.get("host_id").map(String::as_str).unwrap_or("");
    let snapshot_context = snapshot_context_fingerprint(host_id, &repository_id);
    let unchanged = state
        .get("last_snapshot_fingerprint")
        .and_then(serde_json::Value::as_str)
        == Some(fingerprint.as_str())
        && state
            .get("last_snapshot_context")
            .and_then(serde_json::Value::as_str)
            == Some(snapshot_context.as_str());
    let force = manifest
        .get("create_snapshot_if_unchanged")
        .and_then(Yaml::as_bool)
        .unwrap_or(false);
    let committed = if unchanged && !force {
        println!("SKIP: Backup content is unchanged for {label}");
        false
    } else {
        let command = BackupCommand {
            argv: vec![
                "restic".to_owned(),
                "backup".to_owned(),
                ".".to_owned(),
                "--host".to_owned(),
                host_id.to_owned(),
                "--tag".to_owned(),
                format!("service:{service}"),
            ],
            cwd: Some(stage.clone()),
        };
        if let Err(error) = (deps.run)(command) {
            record_failure(config, service, started, &mut state, &error, "restic", deps);
            return Err(error);
        }
        state.insert(
            "last_snapshot_fingerprint".to_owned(),
            serde_json::Value::String(fingerprint),
        );
        state.insert(
            "last_snapshot_context".to_owned(),
            serde_json::Value::String(snapshot_context),
        );
        true
    };
    let mut postcommit = None;
    if let Err(error) = (deps.remove_stage)(&stage) {
        eprintln!(
            "ERROR: Restic backup for {label} completed, but staging cleanup failed: RuntimeError: {error}"
        );
        (deps.on_failure)(&error, "cleanup");
        postcommit = Some(error);
    }
    if let Err(error) = (deps.on_success)() {
        eprintln!(
            "ERROR: Restic backup for {label} completed, but on_success actions failed: RuntimeError: {error}"
        );
        (deps.on_failure)(&error, "on_success");
        if postcommit.is_none() {
            postcommit = Some(error);
        }
    }
    let finished = (deps.now)();
    state.insert("last_success_at".to_owned(), json_time(finished));
    state.insert("last_finished_at".to_owned(), json_time(finished));
    state.insert(
        "last_result".to_owned(),
        serde_json::Value::String("success".to_owned()),
    );
    state.insert("last_error".to_owned(), serde_json::Value::Null);
    state.insert(
        "last_duration_seconds".to_owned(),
        serde_json::json!((finished - started).num_milliseconds() as f64 / 1000.0),
    );
    if let Err(error) = (deps.save_state)(&state) {
        eprintln!(
            "ERROR: Restic backup for {label} completed, but state update failed: RuntimeError: {error}"
        );
        (deps.on_failure)(&error, "state");
        return Err(postcommit.unwrap_or(error));
    }
    if let Some(error) = postcommit {
        return Err(error);
    }
    Ok(committed)
}

fn record_failure(
    config: &BTreeMap<String, String>,
    service: &str,
    started: DateTime<FixedOffset>,
    state: &mut BTreeMap<String, serde_json::Value>,
    error: &SecurityError,
    phase: &str,
    deps: &mut BackupDeps<'_>,
) {
    (deps.on_failure)(error, phase);
    if let Some(root) = config.get("staging_root") {
        let partial = PathBuf::from(root).join(service);
        let _ = (deps.remove_stage)(&partial);
    }
    let finished = (deps.now)();
    state.insert("last_finished_at".to_owned(), json_time(finished));
    state.insert(
        "last_result".to_owned(),
        serde_json::Value::String("failed".to_owned()),
    );
    state.insert(
        "last_error".to_owned(),
        serde_json::Value::String(format!("RuntimeError: {error}")),
    );
    state.insert(
        "last_duration_seconds".to_owned(),
        serde_json::json!((finished - started).num_milliseconds() as f64 / 1000.0),
    );
    let _ = (deps.save_state)(state);
}

fn json_time(value: DateTime<FixedOffset>) -> serde_json::Value {
    serde_json::Value::String(value.to_rfc3339_opts(SecondsFormat::Secs, false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unchanged_backup_skips_the_snapshot() {
        let fingerprint = format!("sha256:{}", "1".repeat(64));
        let repository_id = "a".repeat(64);
        let context = snapshot_context_fingerprint("host", &repository_id);
        let mut state = BTreeMap::from([
            (
                "last_snapshot_fingerprint".to_owned(),
                serde_json::Value::String(fingerprint.clone()),
            ),
            (
                "last_snapshot_context".to_owned(),
                serde_json::Value::String(context),
            ),
        ]);
        let saved = state.clone();
        let mut commands = Vec::new();
        let mut now_calls = 0;
        let mut deps = BackupDeps {
            now: &mut || {
                now_calls += 1;
                DateTime::parse_from_rfc3339(if now_calls == 1 {
                    "2026-01-01T00:00:00Z"
                } else {
                    "2026-01-01T00:00:05Z"
                })
                .unwrap()
            },
            load_state: &mut || saved.clone(),
            save_state: &mut |next| {
                state = next.clone();
                Ok(())
            },
            stage: &mut || Ok(PathBuf::from("/tmp/stage")),
            fingerprint: &mut |_| Ok(fingerprint.clone()),
            repository_id: &mut || Ok(repository_id.clone()),
            run: &mut |command| {
                commands.push(command.argv);
                Ok(())
            },
            remove_stage: &mut |_| Ok(()),
            on_success: &mut || Ok(()),
            on_failure: &mut |_error, _phase| {},
        };
        let manifest = BTreeMap::from([("service".to_owned(), Yaml::string("demo"))]);
        let config = BTreeMap::from([
            ("host_id".to_owned(), "host".to_owned()),
            ("staging_root".to_owned(), "/tmp/stage-root".to_owned()),
        ]);
        assert!(!execute_backup(&config, &manifest, false, &mut deps).unwrap());
        assert!(commands.is_empty());
        assert_eq!(
            state.get("last_result").and_then(|value| value.as_str()),
            Some("success")
        );
    }
}
