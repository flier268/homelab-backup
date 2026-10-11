//! List, status, schedule, retention, and repository commands.
//! `notify-failure` and the run-due alert ledger are still separate.

use std::collections::BTreeMap;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::time::Duration;

use chrono::{DateTime, FixedOffset, Local, Utc};
use homelab_backup_domain::duration::parse_duration;
use homelab_backup_domain::manifest::{service_label, validate_manifest};
use homelab_backup_domain::names::valid_service_name;
use homelab_backup_domain::yaml::Yaml;

use crate::catalog::select_services;
use crate::documents::load_global_config;
use crate::due::{due_status, success_staleness};
use crate::files::{CommandError, backup_loaded, flat_strings, run_restic};
use crate::state::{load_backup_state, save_backup_state};
use crate::storage::retention_command;
use crate::unix::control::validate_control_file;
use crate::unix::error::SecurityError;
use crate::unix::lock::{GlobalLock, global_lock_holder};
use crate::unix::restic::restic_env;

struct Failure {
    operation: String,
    summary: String,
    line: String,
}

pub fn run_list(config_path: &str) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let state_root = flat.get("state_root").cloned().unwrap_or_default();
    let (ready, load_errors) = select_services(&config, &[]).map_err(text)?;
    let mut failures = load_errors
        .into_iter()
        .map(|failure| Failure {
            operation: failure.operation,
            summary: failure.summary,
            line: failure.line,
        })
        .collect::<Vec<_>>();
    let now = Local::now().fixed_offset();
    for service in ready {
        let manifest = service.manifest;
        let name = manifest
            .get("service")
            .and_then(Yaml::as_str)
            .unwrap_or("<unnamed>")
            .to_owned();
        let state = match load_backup_state(&state_root, &name) {
            Ok(state) => state,
            Err(error) => {
                let line = format!("ERROR: list failed for {name}: {error}");
                failures.push(Failure {
                    operation: name,
                    summary: format!("listing failed: {error}"),
                    line,
                });
                continue;
            }
        };
        match list_line(&manifest, &state, now) {
            Ok(line) => println!("{line}"),
            Err(error) => {
                let line = format!("ERROR: list failed for {name}: {error}");
                failures.push(Failure {
                    operation: name,
                    summary: format!("listing failed: {error}"),
                    line,
                });
            }
        }
    }
    finish(failures, "LIST FAILURES")
}

pub fn run_init(config_path: &str) -> Result<(), CommandError> {
    let (flat, environment) = environment_for(config_path)?;
    let _ = flat;
    run_restic("restic", &["init".to_owned()], None, &environment, false).map_err(text)?;
    Ok(())
}

pub fn run_snapshots(config_path: &str, service: Option<&str>) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let environment = restic_env(&flat).map_err(text)?;
    let host = flat.get("host_id").cloned().unwrap_or_default();
    let mut args = vec!["snapshots".to_owned(), "--host".to_owned(), host.clone()];
    if let Some(service) = service {
        args.push("--tag".to_owned());
        args.push(format!("service:{service}"));
    }
    run_restic("restic", &args, None, &environment, false).map_err(text)?;
    let Some(service) = service else {
        println!("\nSpecify a service to include retention reasons: backupctl snapshots SERVICE");
        return Ok(());
    };
    let (ready, _) = select_services(&config, &[service.to_owned()]).map_err(text)?;
    let Some(found) = ready.into_iter().next() else {
        eprintln!(
            "WARNING: retention preview unavailable for {service}: unknown or disabled service: {service}"
        );
        return Ok(());
    };
    if let Err(error) = validate_manifest(&Yaml::Map(found.manifest.clone())) {
        eprintln!("WARNING: retention preview unavailable for {service}: {error}");
        return Ok(());
    }
    println!("\n== Retention preview: {service} ==");
    let retention = found
        .manifest
        .get("retention")
        .and_then(Yaml::as_map)
        .cloned()
        .unwrap_or_default();
    let command = retention_command(&host, service, &retention, false, true);
    run_restic("restic", &command[1..], None, &environment, false).map_err(text)?;
    Ok(())
}

pub fn run_check(config_path: &str, no_wait: bool) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let Some(_guard) = acquire(
        flat.get("lock_file").map(String::as_str).unwrap_or(""),
        no_wait,
        0,
    )?
    else {
        println!("SKIP: backup or maintenance is still running");
        return Ok(());
    };
    let environment = restic_env(&flat).map_err(text)?;
    if let Err(error) = run_restic("restic", &["unlock".to_owned()], None, &environment, false) {
        eprintln!("WARNING: could not clear stale restic locks: RuntimeError: {error}");
    }
    let mut args = vec!["check".to_owned()];
    if let Some(subset) = config
        .get("check")
        .and_then(Yaml::as_map)
        .and_then(|check| check.get("read_data_subset"))
        .and_then(Yaml::as_str)
    {
        args.push("--read-data-subset".to_owned());
        args.push(subset.to_owned());
    }
    run_restic("restic", &args, None, &environment, false).map_err(text)?;
    Ok(())
}

pub fn run_unlock(config_path: &str) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let Some(_guard) = acquire(
        flat.get("lock_file").map(String::as_str).unwrap_or(""),
        true,
        0,
    )?
    else {
        return Err(CommandError::Text(
            "another backupctl process is running".to_owned(),
        ));
    };
    let environment = restic_env(&flat).map_err(text)?;
    run_restic("restic", &["unlock".to_owned()], None, &environment, false).map_err(text)?;
    Ok(())
}

pub fn run_maintenance(
    config_path: &str,
    dry_run: bool,
    no_wait: bool,
) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let Some(_guard) = acquire(
        flat.get("lock_file").map(String::as_str).unwrap_or(""),
        no_wait,
        0,
    )?
    else {
        println!("SKIP: backup or maintenance is still running");
        return Ok(());
    };
    let environment = restic_env(&flat).map_err(text)?;
    if let Err(error) = run_restic("restic", &["unlock".to_owned()], None, &environment, false) {
        eprintln!("WARNING: could not clear stale restic locks: RuntimeError: {error}");
    }
    let mut failures = Vec::new();
    if !dry_run {
        match snapshot_services(flat.get("state_root").map(String::as_str).unwrap_or("")) {
            Ok(services) if !services.is_empty() => failures.push(Failure {
                operation: "Btrfs snapshot recovery".to_owned(),
                summary: "Btrfs snapshot recovery is not ported yet".to_owned(),
                line: "ERROR: Btrfs snapshot journal scan failed: Btrfs snapshot recovery is not ported yet".to_owned(),
            }),
            Ok(_) => {}
            Err(error) => failures.push(Failure {
                operation: "Btrfs snapshot recovery".to_owned(),
                summary: format!("Btrfs snapshot recovery failed: {error}"),
                line: format!("ERROR: Btrfs snapshot journal scan failed: {error}"),
            }),
        }
    }
    let host = flat.get("host_id").cloned().unwrap_or_default();
    let (ready, load_errors) = select_services(&config, &[]).map_err(text)?;
    for failure in load_errors {
        failures.push(Failure {
            operation: failure.operation,
            summary: failure.summary,
            line: failure.line,
        });
    }
    for service in ready {
        let manifest = service.manifest;
        let label = manifest
            .get("service")
            .and_then(Yaml::as_str)
            .unwrap_or("<unnamed>")
            .to_owned();
        println!("\n== Retention: {} ==", service_label(&manifest));
        if !valid_service_name(&label) {
            failures.push(Failure {
                operation: label.clone(),
                summary: format!("listing failed: invalid manifest service name: '{label}'"),
                line: format!(
                    "ERROR: retention failed for {label}: invalid manifest service name: '{label}'"
                ),
            });
            continue;
        }
        let retention = manifest
            .get("retention")
            .and_then(Yaml::as_map)
            .cloned()
            .unwrap_or_default();
        let command = retention_command(&host, &label, &retention, false, dry_run);
        if let Err(error) = run_restic("restic", &command[1..], None, &environment, false) {
            failures.push(Failure {
                operation: label.clone(),
                summary: error.message.clone(),
                line: format!("ERROR: retention failed for {label}: {error}"),
            });
            continue;
        }
        if dry_run {
            continue;
        }
        let state_root = flat.get("state_root").map(String::as_str).unwrap_or("");
        match load_backup_state(state_root, &label) {
            Ok(mut state)
                if !state
                    .get("last_retention_error")
                    .is_some_and(|value| value.is_null())
                    && state.contains_key("last_retention_error") =>
            {
                state.insert("last_retention_error".to_owned(), serde_json::Value::Null);
                if let Err(error) = save_backup_state(state_root, &label, &state) {
                    failures.push(Failure {
                        operation: label.clone(),
                        summary: format!("retention state update failed: {error}"),
                        line: format!("ERROR: retention state update failed for {label}: {error}"),
                    });
                }
            }
            Ok(_) => {}
            Err(error) => failures.push(Failure {
                operation: label.clone(),
                summary: format!("retention state update failed: {error}"),
                line: format!("ERROR: retention state update failed for {label}: {error}"),
            }),
        }
    }
    if !dry_run
        && let Err(error) = run_restic("restic", &["prune".to_owned()], None, &environment, false)
    {
        failures.push(Failure {
            operation: "repository prune".to_owned(),
            summary: error.message.clone(),
            line: format!("ERROR: repository prune failed: {error}"),
        });
    }
    finish(failures, "MAINTENANCE FAILURES")
}

pub fn run_due(config_path: &str) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let Some(_guard) = acquire(
        flat.get("lock_file").map(String::as_str).unwrap_or(""),
        true,
        3,
    )?
    else {
        println!("SKIP: another backup or maintenance process is still running");
        return Ok(());
    };
    let now = Local::now().fixed_offset();
    let (ready, load_errors) = select_services(&config, &[]).map_err(text)?;
    let mut failures = load_errors
        .into_iter()
        .map(|failure| Failure {
            operation: failure.operation,
            summary: failure.summary,
            line: failure.line,
        })
        .collect::<Vec<_>>();
    let mut due = Vec::new();
    let state_root = flat.get("state_root").cloned().unwrap_or_default();
    for service in ready {
        let manifest = service.manifest;
        let name = manifest
            .get("service")
            .and_then(Yaml::as_str)
            .unwrap_or("<unnamed>")
            .to_owned();
        let state = load_backup_state(&state_root, &name).unwrap_or_default();
        match due_status(&manifest, &state, now) {
            Ok(status) => {
                println!(
                    "{}: {} ({})",
                    service_label(&manifest),
                    if status.due { "DUE" } else { "skip" },
                    status.reason
                );
                if status.due {
                    due.push(manifest);
                }
            }
            Err(error) => failures.push(Failure {
                operation: name.clone(),
                summary: format!("schedule planning failed: {error}"),
                line: format!("ERROR: schedule planning failed for {name}: {error}"),
            }),
        }
    }
    if due.is_empty() {
        println!("No backups are due.");
    } else {
        let environment = restic_env(&flat).map_err(text)?;
        if let Err(error) = run_restic("restic", &["unlock".to_owned()], None, &environment, false)
        {
            eprintln!("WARNING: could not clear stale restic locks: RuntimeError: {error}");
        }
        for manifest in due {
            println!("\n== Scheduled backup: {} ==", service_label(&manifest));
            if let Err(error) = backup_loaded(&flat, &manifest, false, "restic", &environment) {
                let label = service_label(&manifest);
                failures.push(Failure {
                    operation: manifest
                        .get("service")
                        .and_then(Yaml::as_str)
                        .unwrap_or(&label)
                        .to_owned(),
                    summary: error.clone(),
                    line: format!("ERROR: scheduled backup failed for {label}: {error}"),
                });
            }
        }
    }
    finish(failures, "SCHEDULED BACKUP FAILURES")
}

pub fn run_status(
    config_path: &str,
    max_age: Option<&str>,
    notify: bool,
) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let max_age_seconds = match max_age {
        Some(value) => Some(
            parse_duration(value, "--max-age")
                .map_err(|error| CommandError::Text(error.to_string()))?
                .as_secs() as i64,
        ),
        None => None,
    };
    let (ready, load_errors) = select_services(&config, &[]).map_err(text)?;
    let mut failures = load_errors
        .into_iter()
        .map(|failure| Failure {
            operation: failure.operation,
            summary: failure.summary,
            line: failure.line,
        })
        .collect::<Vec<_>>();
    let now = Local::now().fixed_offset();
    let state_root = flat.get("state_root").cloned().unwrap_or_default();
    let mut stale = Vec::new();
    for service in ready {
        let manifest = service.manifest;
        let name = manifest
            .get("service")
            .and_then(Yaml::as_str)
            .unwrap_or("<unnamed>")
            .to_owned();
        let state = match load_backup_state(&state_root, &name) {
            Ok(state) => state,
            Err(error) => {
                failures.push(Failure {
                    operation: name.clone(),
                    summary: format!("state loading failed: {error}"),
                    line: format!("ERROR: state loading failed for {name}: {error}"),
                });
                continue;
            }
        };
        let status = match due_status(&manifest, &state, now) {
            Ok(status) => status,
            Err(error) => {
                let line = format!("ERROR: status schedule failed for {name}: {error}");
                failures.push(Failure {
                    operation: name,
                    summary: format!("schedule status failed: {error}"),
                    line,
                });
                continue;
            }
        };
        let staleness = match success_staleness(&config, &manifest, &state, now, max_age_seconds) {
            Ok(staleness) => staleness,
            Err(error) => {
                failures.push(Failure {
                    operation: name.clone(),
                    summary: format!("state loading failed: {error}"),
                    line: format!("ERROR: state loading failed for {name}: {error}"),
                });
                continue;
            }
        };
        println!(
            "{}",
            status_json(&name, status.due, &status.reason, &state, &staleness)
        );
        if staleness.stale {
            eprintln!(
                "ALERT: {name}: no successful backup within {}s (last success: {}; last error: {})",
                staleness.max_success_age_seconds.unwrap_or(0),
                state
                    .get("last_success_at")
                    .and_then(|value| value.as_str())
                    .unwrap_or("never"),
                state
                    .get("last_error")
                    .and_then(|value| value.as_str())
                    .unwrap_or("-")
            );
            stale.push(name);
        }
    }
    let (lock_alert, _holder) =
        stalled_lock(flat.get("lock_file").map(String::as_str), &config, now);
    if let Some(alert) = &lock_alert {
        eprintln!("ALERT: {alert}");
    }
    if notify {
        send_status_notification(&config, &flat, &stale, lock_alert.as_deref(), &failures);
    }
    if !failures.is_empty() {
        return finish(failures, "STATUS FAILURES");
    }
    if !stale.is_empty() || lock_alert.is_some() {
        return Err(CommandError::Exit(2));
    }
    Ok(())
}

fn list_line(
    manifest: &BTreeMap<String, Yaml>,
    state: &BTreeMap<String, serde_json::Value>,
    now: DateTime<FixedOffset>,
) -> Result<String, String> {
    let service = manifest.get("service").and_then(Yaml::as_str).unwrap_or("");
    if !valid_service_name(service) {
        return Err(format!("invalid manifest service name: '{service}'"));
    }
    let due = due_status(manifest, state, now)?;
    let cron = manifest
        .get("schedule")
        .and_then(Yaml::as_map)
        .and_then(|schedule| schedule.get("cron"))
        .and_then(Yaml::as_str)
        .unwrap_or("");
    let path = manifest.get("_path").and_then(Yaml::as_str).unwrap_or("");
    Ok(format!(
        "{}: cron {cron}; {}; {path}",
        service_label(manifest),
        if due.due {
            "DUE".to_owned()
        } else {
            due.reason
        }
    ))
}

fn status_json(
    service: &str,
    due: bool,
    reason: &str,
    state: &BTreeMap<String, serde_json::Value>,
    staleness: &crate::due::Staleness,
) -> String {
    let value = serde_json::json!({
        "service": service,
        "due": due,
        "schedule_status": reason,
        "last_result": state.get("last_result").cloned().unwrap_or(serde_json::Value::Null),
        "last_success_at": state.get("last_success_at").cloned().unwrap_or(serde_json::Value::Null),
        "last_duration_seconds": state.get("last_duration_seconds").cloned().unwrap_or(serde_json::Value::Null),
        "last_error": state.get("last_error").cloned().unwrap_or(serde_json::Value::Null),
        "last_retention_error": state.get("last_retention_error").cloned().unwrap_or(serde_json::Value::Null),
        "stale": staleness.stale,
        "last_success_age_seconds": staleness.last_success_age_seconds,
        "max_success_age_seconds": staleness.max_success_age_seconds,
    });
    serde_json::to_string(&value).unwrap_or_else(|_| "{}".to_owned())
}

fn stalled_lock(
    lock_file: Option<&str>,
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
) -> (Option<String>, Option<crate::unix::lock::LockHolder>) {
    let Some(lock_file) = lock_file.filter(|path| !path.is_empty()) else {
        return (None, None);
    };
    let holder = match global_lock_holder(Path::new(lock_file)) {
        Ok(Some(holder)) => holder,
        Ok(None) => return (None, None),
        Err(error) => {
            eprintln!("WARNING: could not inspect global lock {lock_file}: {error}");
            return (None, None);
        }
    };
    let Some(started) = holder
        .started
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
    else {
        eprintln!(
            "WARNING: global lock held by pid={} with unknown start time ({}); cannot tell if it is stalled",
            holder
                .pid
                .map(|pid| pid.to_string())
                .unwrap_or_else(|| "None".to_owned()),
            holder
                .started
                .as_deref()
                .map(|value| format!("'{value}'"))
                .unwrap_or_else(|| "None".to_owned())
        );
        return (None, None);
    };
    let held_seconds = (to_utc(now) - started.with_timezone(&Utc))
        .num_seconds()
        .max(0);
    eprintln!(
        "{}",
        serde_json::json!({
            "lock_holder": {
                "pid": holder.pid,
                "started": holder.started,
                "held_seconds": held_seconds,
            }
        })
    );
    let max_age = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("max_lock_age"))
        .and_then(Yaml::as_str)
        .unwrap_or("13h");
    let Ok(max_age_seconds) = parse_duration(max_age, "alert.max_lock_age") else {
        return (None, Some(holder));
    };
    if held_seconds <= max_age_seconds.as_secs() as i64 {
        return (None, Some(holder));
    }
    (
        Some(format!(
            "global lock held by pid={} since {} ({held_seconds}s > {}s); backup or check may be stalled",
            holder
                .pid
                .map(|pid| pid.to_string())
                .unwrap_or_else(|| "None".to_owned()),
            holder.started.as_deref().unwrap_or(""),
            max_age_seconds.as_secs()
        )),
        Some(holder),
    )
}

fn send_status_notification(
    config: &BTreeMap<String, Yaml>,
    flat: &BTreeMap<String, String>,
    stale: &[String],
    lock_alert: Option<&str>,
    failures: &[Failure],
) {
    let host = flat.get("host_id").map(String::as_str).unwrap_or("?");
    let problem = !stale.is_empty() || lock_alert.is_some() || !failures.is_empty();
    if !problem {
        let _ = notify(
            config,
            &format!("homelab-backup on {host}: OK"),
            "all scheduled backups are fresh",
            true,
            false,
        );
        return;
    }
    let mut lines = Vec::new();
    for service in stale {
        lines.push(format!("{service}: last success never; last error -"));
    }
    if let Some(alert) = lock_alert {
        lines.push(alert.to_owned());
    }
    for failure in failures {
        lines.push(format!(
            "status error: {}: {}",
            failure.operation, failure.summary
        ));
    }
    let mut title_parts = Vec::new();
    if !stale.is_empty() {
        title_parts.push("backups are stale");
    }
    if lock_alert.is_some() {
        title_parts.push("backup appears stalled");
    }
    if !failures.is_empty() {
        title_parts.push("status check failed");
    }
    let title = format!("homelab-backup on {host}: {}", title_parts.join("; "));
    let _ = notify(config, &title, &lines.join("\n"), false, false);
}

fn notify(
    config: &BTreeMap<String, Yaml>,
    title: &str,
    message: &str,
    ok: bool,
    recovered: bool,
) -> bool {
    let Some(url) = notification_url(config) else {
        return false;
    };
    let kind = if url.contains("hc-ping.com") || url.contains("/ping/") {
        "healthchecks"
    } else {
        "ntfy"
    };
    let body = format!("{title}\n\n{message}");
    let bytes = truncate_utf8(body.as_bytes(), 10000);
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(10))
        .build();
    let result = if kind == "healthchecks" {
        let target = if ok {
            url.trim_end_matches('/').to_owned()
        } else {
            format!("{}/fail", url.trim_end_matches('/'))
        };
        agent
            .post(&target)
            .set("Content-Type", "text/plain; charset=utf-8")
            .send_bytes(bytes)
    } else {
        if ok && !recovered {
            return false;
        }
        let ascii: String = title
            .chars()
            .map(|ch| if ch.is_ascii() { ch } else { '?' })
            .collect();
        agent
            .post(&url)
            .set("Title", &ascii)
            .set("Priority", if ok { "default" } else { "high" })
            .set("Tags", if ok { "white_check_mark" } else { "warning" })
            .set("Content-Type", "text/plain; charset=utf-8")
            .send_bytes(bytes)
    };
    if let Err(error) = result {
        eprintln!(
            "WARNING: notification delivery failed: {}",
            error_type_name(&error)
        );
        return false;
    }
    true
}

fn notification_url(config: &BTreeMap<String, Yaml>) -> Option<String> {
    let path = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("notify_url_file"))
        .and_then(Yaml::as_str)?;
    let text = (|| {
        let checked = validate_control_file(path)?;
        let metadata = std::fs::metadata(&checked)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        if MetadataExt::mode(&metadata) & 0o077 != 0 {
            return Err(SecurityError::invalid(format!(
                "control file is readable by group or others: {path}"
            )));
        }
        std::fs::read_to_string(&checked).map_err(|error| SecurityError::runtime(error.to_string()))
    })();
    let text = match text {
        Ok(text) => text,
        Err(error) if error.is_missing_path() || error.message.contains("does not exist") => {
            eprintln!("WARNING: notification URL file is missing: {path}");
            return None;
        }
        Err(error) => {
            eprintln!("WARNING: notification URL file is unusable: {path}: {error}");
            return None;
        }
    };
    let url = text
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("");
    let host = url
        .strip_prefix("https://")
        .unwrap_or("")
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("");
    if host.is_empty() {
        eprintln!("WARNING: notification URL file does not contain an https URL: {path}");
        return None;
    }
    Some(url.to_owned())
}

fn truncate_utf8(bytes: &[u8], limit: usize) -> &[u8] {
    let mut end = bytes.len().min(limit);
    while end > 0 && (bytes[end - 1] & 0b1100_0000) == 0b1000_0000 {
        end -= 1;
    }
    &bytes[..end]
}

fn error_type_name(error: &ureq::Error) -> &'static str {
    match error {
        ureq::Error::Status(_, _) => "HTTPError",
        ureq::Error::Transport(_) => "URLError",
    }
}

fn snapshot_services(state_root: &str) -> Result<Vec<String>, SecurityError> {
    if state_root.is_empty() {
        return Ok(Vec::new());
    }
    let root = homelab_backup_domain::paths::lexical_absolute(state_root)
        .map_err(|error| SecurityError::invalid(error.message))?;
    let entries = match std::fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    crate::unix::control::validate_control_directory(&root, false, None)?;
    let mut services = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| SecurityError::runtime(error.to_string()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Some(service) = snapshot_service_name(&name) {
            services.push(service);
        }
    }
    services.sort();
    Ok(services)
}

fn snapshot_service_name(file_name: &str) -> Option<String> {
    let rest = file_name
        .strip_prefix('.')?
        .strip_suffix(".btrfs-snapshots.json")?;
    if rest.chars().next()?.is_ascii_alphanumeric()
        && rest
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-'))
    {
        Some(rest.to_owned())
    } else {
        None
    }
}

type StringMap = BTreeMap<String, String>;

fn environment_for(config_path: &str) -> Result<(StringMap, StringMap), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let environment = restic_env(&flat).map_err(text)?;
    Ok((flat, environment))
}

fn acquire(
    path: &str,
    nonblocking: bool,
    retries: u32,
) -> Result<Option<GlobalLock>, CommandError> {
    let mut lock = GlobalLock::new(path, nonblocking, retries);
    if lock
        .enter(&mut || std::thread::sleep(Duration::from_millis(100)))
        .map_err(text)?
    {
        Ok(Some(lock))
    } else {
        Ok(None)
    }
}

fn finish(failures: Vec<Failure>, title: &str) -> Result<(), CommandError> {
    if failures.is_empty() {
        return Ok(());
    }
    for failure in &failures {
        eprintln!("{}", failure.line);
    }
    eprintln!("\n{title}");
    for failure in failures {
        eprintln!("  - {}: {}", failure.operation, failure.summary);
    }
    Err(CommandError::Reported)
}

fn text(error: SecurityError) -> CommandError {
    CommandError::Text(error.message)
}

fn to_utc(value: DateTime<FixedOffset>) -> DateTime<Utc> {
    value.with_timezone(&Utc)
}
