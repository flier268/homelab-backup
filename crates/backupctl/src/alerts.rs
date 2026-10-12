//! Failure notifications and the `<state_root>/_alerts.json` ledger.
//! Alerting never takes the global backup lock. Lock, read, and write problems
//! warn and still attempt delivery.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
#[cfg(test)]
use std::os::fd::AsFd;
use std::os::fd::OwnedFd;
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

use chrono::{DateTime, FixedOffset, Local, SecondsFormat};
use homelab_backup_domain::duration::parse_duration;
use homelab_backup_domain::names::valid_service_name;
use homelab_backup_domain::paths::lexical_absolute;
use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{FlockOperation, Mode, OFlags, fchmod, flock, fstat, openat};
use rustix::io::Errno;
use serde_json::{Map, Value};

use crate::catalog::select_services;
use crate::documents::load_global_config;
use crate::files::CommandError;
use crate::state::load_backup_state;
use crate::unix::atomic::atomic_write_json;
use crate::unix::control::{
    ensure_control_directory, file_name, is_reg, mode_of, open_directory, parent_path,
    validate_control_directory,
};

pub const BACKUP_UNIT: &str = "homelab-backup.service";
pub const MAINTENANCE_UNIT: &str = "homelab-backup-maintenance.service";
pub const CHECK_UNIT: &str = "homelab-backup-check.service";
pub const STATUS_UNIT: &str = "homelab-backup-status.service";
pub const ALERT_STATE_NAME: &str = "_alerts.json";
pub const ALERT_LOCK_NAME: &str = "_alerts.lock";

const LOCK_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_RETRY: Duration = Duration::from_millis(100);
const DEFAULT_RENOTIFY: Duration = Duration::from_secs(24 * 60 * 60);
const STATUS_HANDLED: Duration = Duration::from_secs(20 * 60);
const RUN_DUE_HANDOFF: Duration = Duration::from_secs(20 * 60);
const CRASH_QUIET: Duration = Duration::from_secs(60 * 60);
const ERROR_LINE_LIMIT: usize = 200;
const NOTIFY_BODY_LIMIT: usize = 3900;
const NOTIFY_HTTP_LIMIT: usize = 10_000;
const HTTP_TIMEOUT: Duration = Duration::from_secs(10);

const NOT_DELIVERED: &str = "WARNING: failure notification was not delivered (alert.notify_url_file not configured or unreachable)";

type JsonMap = Map<String, Value>;

thread_local! {
    static STDERR_CAPTURE: RefCell<Option<Vec<String>>> = const { RefCell::new(None) };
}

fn emit(line: &str) {
    let captured = STDERR_CAPTURE.with(|slot| {
        if let Some(lines) = slot.borrow_mut().as_mut() {
            lines.push(line.to_owned());
            true
        } else {
            false
        }
    });
    if !captured {
        eprintln!("{line}");
    }
}

struct LockPace<'a> {
    timeout: Duration,
    retry: Duration,
    sleep: Box<dyn FnMut(Duration) + 'a>,
}

fn standard_pace() -> LockPace<'static> {
    LockPace {
        timeout: LOCK_TIMEOUT,
        retry: LOCK_RETRY,
        sleep: Box::new(std::thread::sleep),
    }
}

trait AlertHooks {
    fn env(&self, key: &str) -> Option<String>;
    fn journal(&mut self, unit: &str) -> Result<String, String>;
    fn deliver(&mut self, title: &str, message: &str) -> bool;
    fn running(&mut self) -> Vec<String>;
}

struct LiveHooks<'a> {
    config: &'a BTreeMap<String, Yaml>,
}

impl AlertHooks for LiveHooks<'_> {
    fn env(&self, key: &str) -> Option<String> {
        std::env::var(key).ok().filter(|value| !value.is_empty())
    }

    fn journal(&mut self, unit: &str) -> Result<String, String> {
        let output = Command::new("journalctl")
            .args([
                "--unit",
                unit,
                "--lines",
                "30",
                "--no-pager",
                "--output",
                "cat",
            ])
            .output()
            .map_err(|error| error.to_string())?;
        String::from_utf8(output.stdout).map_err(|error| error.to_string())
    }

    fn deliver(&mut self, title: &str, message: &str) -> bool {
        notify(self.config, title, message, false, false, &mut ureq_post)
    }

    fn running(&mut self) -> Vec<String> {
        running_services(self.config)
    }
}

/// Print the journal ALERT line, then notify. Does not take the global backup lock.
/// The ALERT line is printed before config loading can fail.
pub fn run_notify_failure(config_path: &str, unit: &str) -> Result<(), CommandError> {
    emit(&format!("ALERT: systemd unit failed: {unit}"));
    let config =
        load_global_config(config_path).map_err(|error| CommandError::Text(error.to_string()))?;
    let mut hooks = LiveHooks { config: &config };
    let mut pace = standard_pace();
    notify_failure(
        &config,
        unit,
        Local::now().fixed_offset(),
        false,
        &mut hooks,
        &mut pace,
    );
    Ok(())
}

/// Per-service bookkeeping at the end of a completed run-due.
///
/// `invocation_id` is systemd's `INVOCATION_ID` for this run, when one is set.
/// `failures` are `(service, error text)` pairs; only the first line is kept.
/// `known_services` is `None` when a manifest failed to load.
pub fn record_run_due(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    invocation_id: Option<&str>,
    attempted: &[String],
    failures: &[(String, String)],
    known_services: Option<&BTreeSet<String>>,
) {
    let mut pace = standard_pace();
    record_run_due_paced(
        config,
        now,
        invocation_id,
        attempted,
        failures,
        known_services,
        &mut pace,
    );
}

/// A successful run of `unit` clears its throttle so the next failure is pushed.
pub fn clear_unit_alert(config: &BTreeMap<String, Yaml>, unit: &str) {
    if unit_record(&load_alert_state(config), unit).is_empty() {
        return;
    }
    let mut pace = standard_pace();
    with_alert_state(config, &mut pace, |state| {
        if let Some(Value::Object(units)) = state.get_mut("units") {
            units.remove(unit);
        }
    });
}

fn notify_failure(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    now: DateTime<FixedOffset>,
    print_alert: bool,
    hooks: &mut impl AlertHooks,
    pace: &mut LockPace<'_>,
) {
    if print_alert {
        emit(&format!("ALERT: systemd unit failed: {unit}"));
    }
    if unit == BACKUP_UNIT {
        notify_run_due(config, now, hooks, pace);
        return;
    }
    let state = load_alert_state(config);
    let record = unit_record(&state, unit);
    let renotify = renotify_after(config);
    let mut delivered = false;
    if unit == STATUS_UNIT && status_handled(&state, &record, now, hooks) {
        emit("status already handled this problem; not pushing it again");
    } else if notified_within(&Value::Object(record.clone()), renotify, now) {
        let at = record
            .get("notified_at")
            .and_then(Value::as_str)
            .unwrap_or("");
        emit(&format!(
            "{unit} failure already notified at {at}; not pushing again before alert.renotify_after"
        ));
    } else {
        delivered = push_unit_failure(config, unit, &format!("{unit} failed"), "", hooks);
    }
    let stamp = isoformat(now);
    update_unit(config, unit, pace, |current| {
        current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
        if delivered {
            current.insert("notified_at".to_owned(), Value::String(stamp.clone()));
        }
    });
}

fn notify_run_due(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    hooks: &mut impl AlertHooks,
    pace: &mut LockPace<'_>,
) {
    let state = load_alert_state(config);
    let record = unit_record(&state, BACKUP_UNIT);
    let renotify = renotify_after(config);
    if let Some(run) = trusted_run(&record, now, hooks) {
        notify_recorded_failures(config, now, &record, &run, renotify, hooks, pace);
        return;
    }
    let (result, details) = monitor_result(hooks);
    let key = format!("crash:{result}");
    let crashes = object_field(&record, "crashes");
    let existing = crashes.get(&key).cloned().unwrap_or(Value::Null);
    let delivered = if notified_within(&existing, renotify, now) {
        let at = existing
            .get("notified_at")
            .and_then(Value::as_str)
            .unwrap_or("");
        emit(&format!(
            "{BACKUP_UNIT} {result} failure already notified at {at}; not pushing again before alert.renotify_after"
        ));
        false
    } else {
        push_unit_failure(
            config,
            BACKUP_UNIT,
            &format!("{BACKUP_UNIT} failed ({result})"),
            &format!(
                "run-due ended without recording which services failed ({details}); it crashed, timed out, was killed or could not start. Check the journal."
            ),
            hooks,
        )
    };
    let running = hooks.running();
    let stamp = isoformat(now);
    update_unit(config, BACKUP_UNIT, pace, |current| {
        current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
        let mut stored = object_field(current, "crashes");
        let mut entry = object_field(&stored, &key);
        entry.insert("last_seen_at".to_owned(), Value::String(stamp.clone()));
        entry.insert("details".to_owned(), Value::String(details.clone()));
        let mut merged = BTreeSet::new();
        if let Some(previous) = entry.get("running").and_then(Value::as_array) {
            for name in previous {
                if let Some(name) = name.as_str() {
                    merged.insert(name.to_owned());
                }
            }
        }
        merged.extend(running.iter().cloned());
        if !merged.is_empty() {
            entry.insert(
                "running".to_owned(),
                Value::Array(merged.into_iter().map(Value::String).collect()),
            );
        }
        if delivered {
            entry.insert("notified_at".to_owned(), Value::String(stamp.clone()));
        }
        stored.insert(key.clone(), Value::Object(entry));
        current.insert("crashes".to_owned(), Value::Object(stored));
    });
}

fn notify_recorded_failures(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    record: &JsonMap,
    run: &JsonMap,
    renotify: Duration,
    hooks: &mut impl AlertHooks,
    pace: &mut LockPace<'_>,
) {
    let mut failed = BTreeMap::new();
    if let Some(Value::Object(entries)) = run.get("failures") {
        for (name, error) in entries {
            failed.insert(name.clone(), json_text(error));
        }
    }
    let services = object_field(record, "services");
    let mut pending: Vec<String> = failed
        .keys()
        .filter(|name| {
            !services
                .get(*name)
                .is_some_and(|entry| notified_within(entry, renotify, now))
        })
        .cloned()
        .collect();
    pending.sort();
    let mut known: Vec<String> = failed
        .keys()
        .filter(|name| !pending.iter().any(|pending| pending == *name))
        .cloned()
        .collect();
    known.sort();
    let delivered = if pending.is_empty() {
        emit(&format!(
            "failed services already notified ({}); not pushing again before alert.renotify_after",
            known.join(", ")
        ));
        false
    } else {
        let mut lines = vec![format!("failed: {}", pending.join(", "))];
        if !known.is_empty() {
            lines.push(format!(
                "still failing (already notified): {}",
                known.join(", ")
            ));
        }
        for name in &pending {
            lines.push(format!("- {name}: {}", failed[name]));
        }
        push_unit_failure(
            config,
            BACKUP_UNIT,
            &format!("{BACKUP_UNIT} failed"),
            &lines.join("\n"),
            hooks,
        )
    };
    let stamp = isoformat(now);
    update_unit(config, BACKUP_UNIT, pace, |current| {
        current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
        if delivered {
            let mut stored = object_field(current, "services");
            for name in &pending {
                let mut entry = JsonMap::new();
                entry.insert("notified_at".to_owned(), Value::String(stamp.clone()));
                stored.insert(name.clone(), Value::Object(entry));
            }
            current.insert("services".to_owned(), Value::Object(stored));
        }
    });
}

fn record_run_due_paced(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    invocation_id: Option<&str>,
    attempted: &[String],
    failures: &[(String, String)],
    known_services: Option<&BTreeSet<String>>,
    pace: &mut LockPace<'_>,
) {
    let mut failed = BTreeMap::new();
    for (operation, error) in failures {
        failed
            .entry(operation.clone())
            .or_insert_with(|| first_line(error));
    }
    if failed.is_empty() && unit_record(&load_alert_state(config), BACKUP_UNIT).is_empty() {
        return;
    }
    let succeeded: BTreeSet<String> = attempted
        .iter()
        .filter(|name| !failed.contains_key(name.as_str()))
        .cloned()
        .collect();
    let idle = attempted.is_empty() && failed.is_empty();
    let renotify = renotify_after(config);
    let stamp = isoformat(now);
    let invocation = match invocation_id {
        Some(id) => Value::String(id.to_owned()),
        None => Value::Null,
    };
    update_unit(config, BACKUP_UNIT, pace, |record| {
        let mut crashes = object_field(record, "crashes");
        let mut crashes_changed = false;
        for key in crashes.keys().cloned().collect::<Vec<_>>() {
            let entry = crashes.get(&key).cloned().unwrap_or(Value::Null);
            match crash_after(&entry, now, &succeeded, known_services, renotify) {
                None => {
                    crashes.remove(&key);
                    crashes_changed = true;
                }
                Some(updated) if crashes.get(&key) != Some(&updated) => {
                    crashes.insert(key, updated);
                    crashes_changed = true;
                }
                Some(_) => {}
            }
        }
        if crashes_changed {
            if crashes.is_empty() {
                record.remove("crashes");
            } else {
                record.insert("crashes".to_owned(), Value::Object(crashes));
            }
        }
        if idle {
            return;
        }
        for legacy in ["failure_signature", "notified_signature", "notified_at"] {
            record.remove(legacy);
        }
        let mut services = object_field(record, "services");
        let names = services.keys().cloned().collect::<Vec<_>>();
        for name in names {
            let drop_removed = known_services
                .is_some_and(|known| !known.contains(&name) && !failed.contains_key(name.as_str()));
            if succeeded.contains(&name) || drop_removed {
                services.remove(&name);
            }
        }
        if services.is_empty() {
            record.remove("services");
        } else {
            record.insert("services".to_owned(), Value::Object(services));
        }
        if failed.is_empty() {
            record.remove("run");
            record.remove("last_failure_at");
        } else {
            let mut stored = JsonMap::new();
            for (name, error) in &failed {
                stored.insert(name.clone(), Value::String(error.clone()));
            }
            let mut run = JsonMap::new();
            run.insert("invocation_id".to_owned(), invocation.clone());
            run.insert("completed_at".to_owned(), Value::String(stamp.clone()));
            run.insert("failures".to_owned(), Value::Object(stored));
            record.insert("run".to_owned(), Value::Object(run));
        }
    });
}

fn crash_after(
    entry: &Value,
    now: DateTime<FixedOffset>,
    succeeded: &BTreeSet<String>,
    known: Option<&BTreeSet<String>>,
    renotify: Duration,
) -> Option<Value> {
    let object = entry.as_object()?;
    if let Some(running) = object.get("running").and_then(Value::as_array)
        && !running.is_empty()
    {
        let left: Vec<Value> = running
            .iter()
            .filter(|name| {
                let Some(name) = name.as_str() else {
                    return false;
                };
                if succeeded.contains(name) {
                    return false;
                }
                known.is_none_or(|known| known.contains(name))
            })
            .cloned()
            .collect();
        if crash_quiet(entry, now, renotify) {
            return None;
        }
        if !left.is_empty() {
            let mut updated = object.clone();
            updated.insert("running".to_owned(), Value::Array(left));
            return Some(Value::Object(updated));
        }
        let mut updated = object.clone();
        updated.remove("running");
        let stripped = Value::Object(updated);
        return if crash_quiet(&stripped, now, CRASH_QUIET) {
            None
        } else {
            Some(stripped)
        };
    }
    if crash_quiet(entry, now, CRASH_QUIET) {
        None
    } else {
        Some(entry.clone())
    }
}

fn crash_quiet(entry: &Value, now: DateTime<FixedOffset>, period: Duration) -> bool {
    let Some(object) = entry.as_object() else {
        return true;
    };
    let Some(last_seen) = parse_timestamp(
        object
            .get("last_seen_at")
            .or_else(|| object.get("notified_at")),
    ) else {
        return true;
    };
    last_seen <= now && elapsed_at_least(now, last_seen, period)
}

fn status_handled(
    state: &JsonMap,
    record: &JsonMap,
    now: DateTime<FixedOffset>,
    hooks: &impl AlertHooks,
) -> bool {
    let Some(status) = state.get("status").and_then(Value::as_object) else {
        return false;
    };
    let Some(handled_at) = parse_timestamp(status.get("handled_at")) else {
        return false;
    };
    if handled_at > now {
        return false;
    }
    if let Some(failed_id) = hooks.env("MONITOR_INVOCATION_ID") {
        return status.get("invocation_id").and_then(Value::as_str) == Some(failed_id.as_str());
    }
    if !elapsed_within(now, handled_at, STATUS_HANDLED) {
        return false;
    }
    match parse_timestamp(record.get("last_failure_at")) {
        None => true,
        Some(last_failure) => handled_at > last_failure,
    }
}

fn trusted_run(
    record: &JsonMap,
    now: DateTime<FixedOffset>,
    hooks: &impl AlertHooks,
) -> Option<JsonMap> {
    let run = record.get("run")?.as_object()?;
    let failures = run.get("failures")?.as_object()?;
    if failures.is_empty() {
        return None;
    }
    if let Some(failed_id) = hooks.env("MONITOR_INVOCATION_ID") {
        return (run.get("invocation_id").and_then(Value::as_str) == Some(failed_id.as_str()))
            .then(|| run.clone());
    }
    let completed = parse_timestamp(run.get("completed_at"))?;
    if completed > now || !elapsed_within(now, completed, RUN_DUE_HANDOFF) {
        return None;
    }
    if let Some(last_failure) = parse_timestamp(record.get("last_failure_at"))
        && last_failure >= completed
    {
        return None;
    }
    Some(run.clone())
}

fn monitor_result(hooks: &impl AlertHooks) -> (String, String) {
    let result = hooks
        .env("MONITOR_SERVICE_RESULT")
        .unwrap_or_else(|| "unknown".to_owned());
    let mut details = vec![format!("result={result}")];
    if let Some(exit_code) = hooks.env("MONITOR_EXIT_CODE") {
        details.push(format!("exit_code={exit_code}"));
    }
    if let Some(exit_status) = hooks.env("MONITOR_EXIT_STATUS") {
        details.push(format!("exit_status={exit_status}"));
    }
    (result, details.join(", "))
}

fn push_unit_failure(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    title: &str,
    summary: &str,
    hooks: &mut impl AlertHooks,
) -> bool {
    let journal = match hooks.journal(unit) {
        Ok(text) => text,
        Err(error) => format!("(journal unavailable: {error})"),
    };
    let (result, details) = monitor_result(hooks);
    let monitor_set = hooks.env("MONITOR_SERVICE_RESULT").is_some();
    let mut title = title.to_owned();
    if unit != BACKUP_UNIT && monitor_set && result != "exit-code" {
        title.push_str(&format!(" ({result})"));
    }
    let summary = if summary.is_empty() && monitor_set {
        details
    } else {
        summary.to_owned()
    };
    let title = format!("homelab-backup on {}: {title}", host_id(config));
    let body = notification_body(&title, &summary, &journal);
    let message = if body.is_empty() {
        "see journalctl for details".to_owned()
    } else {
        body
    };
    let delivered = hooks.deliver(&title, &message);
    if !delivered {
        emit(NOT_DELIVERED);
    }
    delivered
}

fn notification_body(title: &str, summary: &str, journal: &str) -> String {
    let mut budget = NOTIFY_BODY_LIMIT.saturating_sub(title.len() + 2);
    let summary = if summary.is_empty() {
        String::new()
    } else {
        utf8_head(summary, budget)
    };
    budget = budget.saturating_sub(summary.len());
    if !summary.is_empty() {
        budget = budget.saturating_sub(2);
    }
    let journal = if journal.is_empty() || budget == 0 {
        String::new()
    } else {
        utf8_tail(journal, budget)
    };
    [summary, journal]
        .into_iter()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn utf8_head(text: &str, limit: usize) -> String {
    let data = text.as_bytes();
    if data.len() <= limit {
        return text.to_owned();
    }
    let marker = "…";
    if limit < marker.len() {
        return String::new();
    }
    let cut = valid_prefix(&data[..limit - marker.len()]);
    let mut out = String::from_utf8(cut.to_vec()).unwrap_or_default();
    out.push_str(marker);
    out
}

fn utf8_tail(text: &str, limit: usize) -> String {
    let data = text.as_bytes();
    if data.len() <= limit {
        return text.to_owned();
    }
    let marker = "…";
    if limit < marker.len() {
        return String::new();
    }
    let start = data.len() - (limit - marker.len());
    let cut = valid_suffix(&data[start..]);
    let mut out = String::from(marker);
    out.push_str(std::str::from_utf8(cut).unwrap_or(""));
    out
}

fn valid_prefix(bytes: &[u8]) -> &[u8] {
    match std::str::from_utf8(bytes) {
        Ok(_) => bytes,
        Err(error) => &bytes[..error.valid_up_to()],
    }
}

fn valid_suffix(bytes: &[u8]) -> &[u8] {
    let mut start = 0;
    while start < bytes.len() {
        match std::str::from_utf8(&bytes[start..]) {
            Ok(_) => break,
            Err(error) => start += error.valid_up_to().max(1),
        }
    }
    &bytes[start.min(bytes.len())..]
}

fn first_line(error: &str) -> String {
    let line = error
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("-");
    let mut chars = line.chars();
    let mut cut = String::new();
    for _ in 0..ERROR_LINE_LIMIT {
        let Some(ch) = chars.next() else {
            return line.to_owned();
        };
        cut.push(ch);
    }
    if chars.next().is_none() {
        return cut;
    }
    cut.pop();
    cut.push('…');
    cut
}

type HttpSend<'a> = dyn FnMut(&str, &[u8], &[(&str, &str)]) -> Result<(), String> + 'a;

fn notify(
    config: &BTreeMap<String, Yaml>,
    title: &str,
    message: &str,
    ok: bool,
    recovered: bool,
    opener: &mut HttpSend<'_>,
) -> bool {
    let Some(url) = notification_url(config) else {
        return false;
    };
    let body = capped_body(title, message);
    if healthchecks(&url) {
        let base = url.trim_end_matches('/');
        let target = if ok {
            base.to_owned()
        } else {
            format!("{base}/fail")
        };
        return deliver(
            &target,
            &body,
            &[("Content-Type", "text/plain; charset=utf-8")],
            opener,
        );
    }
    if ok && !recovered {
        return false;
    }
    let ascii = ascii_title(title);
    let priority = if ok { "default" } else { "high" };
    let tags = if ok { "white_check_mark" } else { "warning" };
    deliver(
        &url,
        &body,
        &[
            ("Title", ascii.as_str()),
            ("Priority", priority),
            ("Tags", tags),
            ("Content-Type", "text/plain; charset=utf-8"),
        ],
        opener,
    )
}

fn deliver(target: &str, body: &[u8], headers: &[(&str, &str)], opener: &mut HttpSend<'_>) -> bool {
    match opener(target, body, headers) {
        Ok(()) => true,
        Err(name) => {
            emit(&format!("WARNING: notification delivery failed: {name}"));
            false
        }
    }
}

fn capped_body(title: &str, message: &str) -> Vec<u8> {
    let text = format!("{title}\n\n{message}");
    let end = text.len().min(NOTIFY_HTTP_LIMIT);
    valid_prefix(&text.as_bytes()[..end]).to_vec()
}

fn ascii_title(title: &str) -> String {
    title
        .chars()
        .map(|ch| if ch.is_ascii() { ch } else { '?' })
        .collect()
}

fn healthchecks(url: &str) -> bool {
    url.contains("hc-ping.com") || url.contains("/ping/")
}

fn ureq_post(target: &str, body: &[u8], headers: &[(&str, &str)]) -> Result<(), String> {
    let agent = ureq::AgentBuilder::new().timeout(HTTP_TIMEOUT).build();
    let mut request = agent.post(target);
    for (name, value) in headers {
        request = request.set(name, value);
    }
    request
        .send_bytes(body)
        .map(|_| ())
        .map_err(|error| http_error_name(&error).to_owned())
}

fn http_error_name(error: &ureq::Error) -> &'static str {
    match error {
        ureq::Error::Status(_, _) => "HTTPError",
        ureq::Error::Transport(_) => "URLError",
    }
}

fn notification_url(config: &BTreeMap<String, Yaml>) -> Option<String> {
    let path = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("notify_url_file"))
        .and_then(Yaml::as_str)
        .filter(|path| !path.is_empty())?;
    let text = match read_private_url(path) {
        Ok(text) => text,
        Err(UrlRead::Missing) => {
            emit(&format!(
                "WARNING: notification URL file is missing: {path}"
            ));
            return None;
        }
        Err(UrlRead::Unusable(error)) => {
            emit(&format!(
                "WARNING: notification URL file is unusable: {path}: {error}"
            ));
            return None;
        }
    };
    let url = text
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("");
    if !is_https_url(url) {
        emit(&format!(
            "WARNING: notification URL file does not contain an https URL: {path}"
        ));
        return None;
    }
    Some(url.to_owned())
}

enum UrlRead {
    Missing,
    Unusable(String),
}

fn read_private_url(path: &str) -> Result<String, UrlRead> {
    let absolute = lexical_absolute(path).map_err(|error| UrlRead::Unusable(error.message))?;
    let parent = parent_path(&absolute);
    validate_control_directory(&parent, false, None)
        .map_err(|error| classify_open(error.to_string()))?;
    let parent_fd = open_directory(&parent).map_err(|error| classify_open(error.to_string()))?;
    let name = file_name(&absolute);
    let fd = match openat(
        &parent_fd,
        name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Err(UrlRead::Missing),
        Err(Errno::LOOP) => {
            return Err(UrlRead::Unusable(format!(
                "control file must be a regular file: {absolute}"
            )));
        }
        Err(error) => {
            return Err(UrlRead::Unusable(format!(
                "control file could not be opened: {absolute}: {error}"
            )));
        }
    };
    let metadata = fstat(&fd).map_err(|error| UrlRead::Unusable(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(UrlRead::Unusable(format!(
            "control file must be a regular file: {absolute}"
        )));
    }
    let uid = metadata.st_uid;
    let effective = rustix::process::geteuid().as_raw();
    if uid != 0 && uid != effective {
        return Err(UrlRead::Unusable(format!(
            "control file is not owned by root: {absolute}"
        )));
    }
    let mode = mode_of(&metadata);
    if mode & 0o077 != 0 {
        return Err(UrlRead::Unusable(format!(
            "control file must not be accessible by group/others (mode {:04o}): {absolute}",
            mode & 0o777
        )));
    }
    let mut file = File::from(fd);
    let mut text = String::new();
    file.read_to_string(&mut text)
        .map_err(|error| UrlRead::Unusable(error.to_string()))?;
    Ok(text)
}

fn classify_open(message: String) -> UrlRead {
    if message.contains("No such file") || message.contains("does not exist") {
        UrlRead::Missing
    } else {
        UrlRead::Unusable(message)
    }
}

fn is_https_url(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("https://") else {
        return false;
    };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    !host.is_empty()
}

fn running_services(config: &BTreeMap<String, Yaml>) -> Vec<String> {
    let Ok((services, _)) = select_services(config, &[]) else {
        return Vec::new();
    };
    let Some(root) = state_root(config) else {
        return Vec::new();
    };
    let mut running = Vec::new();
    for service in services {
        let Some(name) = service.manifest.get("service").and_then(Yaml::as_str) else {
            continue;
        };
        if !valid_service_name(name) {
            continue;
        }
        let Ok(state) = load_backup_state(root, name) else {
            continue;
        };
        if state.get("last_result").and_then(Value::as_str) == Some("running") {
            running.push(name.to_owned());
        }
    }
    running.sort();
    running
}

fn renotify_after(config: &BTreeMap<String, Yaml>) -> Duration {
    let Some(value) = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("renotify_after"))
        .and_then(Yaml::as_str)
    else {
        return DEFAULT_RENOTIFY;
    };
    match parse_duration(value, "alert.renotify_after") {
        Ok(duration) => duration,
        Err(error) => {
            emit(&format!("WARNING: {error}"));
            DEFAULT_RENOTIFY
        }
    }
}

fn host_id(config: &BTreeMap<String, Yaml>) -> &str {
    config.get("host_id").and_then(Yaml::as_str).unwrap_or("?")
}

fn state_root(config: &BTreeMap<String, Yaml>) -> Option<&str> {
    config
        .get("state_root")
        .and_then(Yaml::as_str)
        .filter(|root| !root.is_empty())
}

fn with_alert_state(
    config: &BTreeMap<String, Yaml>,
    pace: &mut LockPace<'_>,
    mutate: impl FnOnce(&mut JsonMap),
) {
    let Some(root) = state_root(config) else {
        let mut ignored = JsonMap::new();
        mutate(&mut ignored);
        return;
    };
    let _lock = acquire_alert_lock(root, pace);
    let mut state = load_alert_state(config);
    let original = state.clone();
    mutate(&mut state);
    if state != original {
        save_alert_state(config, &state);
    }
}

fn update_unit(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    pace: &mut LockPace<'_>,
    update: impl FnOnce(&mut JsonMap),
) {
    with_alert_state(config, pace, |state| {
        let had_units = state.contains_key("units");
        let mut units = state
            .get("units")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let mut record = units
            .get(unit)
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        update(&mut record);
        if record.is_empty() {
            units.remove(unit);
        } else {
            units.insert(unit.to_owned(), Value::Object(record));
        }
        if !units.is_empty() || had_units {
            state.insert("units".to_owned(), Value::Object(units));
        }
    });
}

fn acquire_alert_lock(root: &str, pace: &mut LockPace<'_>) -> Option<OwnedFd> {
    let root = match lexical_absolute(root) {
        Ok(root) => root,
        Err(error) => {
            warn_lock(root, error.message);
            return None;
        }
    };
    let path = join_alert(&root, ALERT_LOCK_NAME);
    if let Err(error) = ensure_control_directory(&root, 0o700) {
        warn_lock(&path, error.to_string());
        return None;
    }
    let parent = parent_path(&path);
    let parent_fd = match open_directory(&parent) {
        Ok(fd) => fd,
        Err(error) => {
            warn_lock(&path, error.to_string());
            return None;
        }
    };
    let fd = match openat(
        &parent_fd,
        ALERT_LOCK_NAME,
        OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    ) {
        Ok(fd) => fd,
        Err(error) => {
            warn_lock(&path, error.to_string());
            return None;
        }
    };
    let metadata = match fstat(&fd) {
        Ok(metadata) => metadata,
        Err(error) => {
            warn_lock(&path, error.to_string());
            return None;
        }
    };
    if !is_reg(mode_of(&metadata)) {
        warn_lock(&path, "not a regular file");
        return None;
    }
    if let Err(error) = fchmod(&fd, Mode::from_bits_truncate(0o600)) {
        warn_lock(&path, error.to_string());
        return None;
    }
    let timeout = pace.timeout;
    let retry = pace.retry;
    let started = Instant::now();
    loop {
        match flock(&fd, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => return Some(fd),
            Err(Errno::AGAIN) => {
                if started.elapsed() >= timeout {
                    warn_lock(
                        &path,
                        format!("still held after {}", timeout_label(timeout)),
                    );
                    return None;
                }
                (pace.sleep)(retry);
            }
            Err(error) => {
                warn_lock(&path, error.to_string());
                return None;
            }
        }
    }
}

fn timeout_label(timeout: Duration) -> String {
    let secs = timeout.as_secs_f64();
    if secs.fract() == 0.0 {
        format!("{}s", secs as u64)
    } else {
        format!("{secs}s")
    }
}

fn warn_lock(path: &str, err: impl std::fmt::Display) {
    emit(&format!(
        "WARNING: could not lock alert state {path}: {err}; continuing without the lock"
    ));
}

fn load_alert_state(config: &BTreeMap<String, Yaml>) -> JsonMap {
    let Some(root) = state_root(config) else {
        return JsonMap::new();
    };
    let Ok(root) = lexical_absolute(root) else {
        return JsonMap::new();
    };
    let path = join_alert(&root, ALERT_STATE_NAME);
    match read_alert_file(&path) {
        Ok(state) => state,
        Err(AlertRead::Missing) => JsonMap::new(),
        Err(AlertRead::Open(error)) => {
            emit(&format!(
                "WARNING: could not open alert state {path}: {error}"
            ));
            JsonMap::new()
        }
        Err(AlertRead::Unreadable(error)) => {
            emit(&format!(
                "WARNING: ignoring unreadable alert state {path}: {error}"
            ));
            JsonMap::new()
        }
    }
}

enum AlertRead {
    Missing,
    Open(String),
    Unreadable(String),
}

fn read_alert_file(path: &str) -> Result<JsonMap, AlertRead> {
    let parent = parent_path(path);
    let parent_fd = match open_directory(&parent) {
        Ok(fd) => fd,
        Err(error) if error.to_string().contains("does not exist") => {
            return Err(AlertRead::Missing);
        }
        Err(error) => return Err(AlertRead::Open(error.to_string())),
    };
    let fd = match openat(
        &parent_fd,
        ALERT_STATE_NAME,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Err(AlertRead::Missing),
        Err(error) => return Err(AlertRead::Open(error.to_string())),
    };
    let metadata = fstat(&fd).map_err(|error| AlertRead::Open(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(AlertRead::Unreadable("not a regular file".to_owned()));
    }
    let mut file = File::from(fd);
    let mut text = String::new();
    file.read_to_string(&mut text)
        .map_err(|error| AlertRead::Unreadable(error.to_string()))?;
    let value: Value =
        serde_json::from_str(&text).map_err(|error| AlertRead::Unreadable(error.to_string()))?;
    value
        .as_object()
        .cloned()
        .ok_or_else(|| AlertRead::Unreadable("not a JSON mapping".to_owned()))
}

fn save_alert_state(config: &BTreeMap<String, Yaml>, state: &JsonMap) {
    let Some(root) = state_root(config) else {
        return;
    };
    let root = match lexical_absolute(root) {
        Ok(root) => root,
        Err(error) => {
            emit(&format!("WARNING: could not save alert state: {error}"));
            return;
        }
    };
    if let Err(error) = ensure_control_directory(&root, 0o700) {
        emit(&format!("WARNING: could not save alert state: {error}"));
        return;
    }
    let path = join_alert(&root, ALERT_STATE_NAME);
    if let Err(error) = atomic_write_json(Path::new(&path), &Value::Object(state.clone())) {
        emit(&format!("WARNING: could not save alert state: {error}"));
    }
}

fn join_alert(root: &str, name: &str) -> String {
    if root == "/" {
        format!("/{name}")
    } else {
        format!("{root}/{name}")
    }
}

fn unit_record(state: &JsonMap, unit: &str) -> JsonMap {
    state
        .get("units")
        .and_then(Value::as_object)
        .and_then(|units| units.get(unit))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn object_field(record: &JsonMap, key: &str) -> JsonMap {
    record
        .get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn json_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn parse_timestamp(value: Option<&Value>) -> Option<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value?.as_str()?).ok()
}

fn isoformat(now: DateTime<FixedOffset>) -> String {
    now.to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn notified_within(entry: &Value, renotify: Duration, now: DateTime<FixedOffset>) -> bool {
    let Some(notified_at) = parse_timestamp(entry.get("notified_at")) else {
        return false;
    };
    notified_at <= now && elapsed_below(now, notified_at, renotify)
}

fn elapsed_below(now: DateTime<FixedOffset>, then: DateTime<FixedOffset>, limit: Duration) -> bool {
    match now.signed_duration_since(then).to_std() {
        Ok(elapsed) => elapsed < limit,
        Err(_) => false,
    }
}

fn elapsed_within(
    now: DateTime<FixedOffset>,
    then: DateTime<FixedOffset>,
    limit: Duration,
) -> bool {
    // `now - then > limit` is outside the window; equality is still inside.
    match now.signed_duration_since(then).to_std() {
        Ok(elapsed) => elapsed <= limit,
        Err(_) => false,
    }
}

fn elapsed_at_least(
    now: DateTime<FixedOffset>,
    then: DateTime<FixedOffset>,
    limit: Duration,
) -> bool {
    match now.signed_duration_since(then).to_std() {
        Ok(elapsed) => elapsed >= limit,
        Err(_) => false,
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::fs::{self, OpenOptions};
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use clap::CommandFactory;
    use homelab_backup_domain::names::valid_service_name;
    use rustix::fs::{FlockOperation, flock};
    use serde_json::json;

    use super::*;
    use crate::cli::Cli;
    use crate::state::save_backup_state;

    static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

    struct Tmp {
        path: PathBuf,
    }

    impl Tmp {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "backupctl-alerts-{}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos(),
                TMP_SEQ.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self { path }
        }

        fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = fs::set_permissions(&self.path, fs::Permissions::from_mode(0o700));
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    struct ClearCapture;
    impl Drop for ClearCapture {
        fn drop(&mut self) {
            STDERR_CAPTURE.with(|slot| {
                slot.borrow_mut().take();
            });
        }
    }

    fn captured(body: impl FnOnce()) -> String {
        STDERR_CAPTURE.with(|slot| *slot.borrow_mut() = Some(Vec::new()));
        let _guard = ClearCapture;
        body();
        STDERR_CAPTURE.with(|slot| slot.borrow_mut().take().unwrap_or_default().join("\n"))
    }

    fn sample_now() -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339("2026-10-11T12:00:00+08:00").unwrap()
    }

    fn later(now: DateTime<FixedOffset>, minutes: i64) -> DateTime<FixedOffset> {
        now + chrono::Duration::minutes(minutes)
    }

    fn config_with(state_root: &str, renotify: Option<&str>) -> BTreeMap<String, Yaml> {
        let mut config = BTreeMap::new();
        config.insert("host_id".to_owned(), Yaml::string("h"));
        config.insert("state_root".to_owned(), Yaml::string(state_root));
        if let Some(renotify) = renotify {
            config.insert(
                "alert".to_owned(),
                Yaml::map([("renotify_after".to_owned(), Yaml::string(renotify))]),
            );
        }
        config
    }

    struct TestHooks {
        env: BTreeMap<String, String>,
        journal: Result<String, String>,
        running: Vec<String>,
        pushes: Vec<(String, String)>,
        deliver_ok: bool,
    }

    impl AlertHooks for TestHooks {
        fn env(&self, key: &str) -> Option<String> {
            self.env.get(key).cloned().filter(|value| !value.is_empty())
        }

        fn journal(&mut self, _unit: &str) -> Result<String, String> {
            self.journal.clone()
        }

        fn deliver(&mut self, title: &str, message: &str) -> bool {
            self.pushes.push((title.to_owned(), message.to_owned()));
            self.deliver_ok
        }

        fn running(&mut self) -> Vec<String> {
            self.running.clone()
        }
    }

    fn hooks(journal: &str) -> TestHooks {
        TestHooks {
            env: BTreeMap::new(),
            journal: Ok(journal.to_owned()),
            running: Vec::new(),
            pushes: Vec::new(),
            deliver_ok: true,
        }
    }

    fn notify_at(
        config: &BTreeMap<String, Yaml>,
        unit: &str,
        now: DateTime<FixedOffset>,
        hook: &mut TestHooks,
    ) -> String {
        captured(|| {
            let mut pace = standard_pace();
            notify_failure(config, unit, now, true, hook, &mut pace);
        })
    }

    fn read_state(config: &BTreeMap<String, Yaml>) -> Value {
        let root = config.get("state_root").unwrap().as_str().unwrap();
        let path = join_alert(&lexical_absolute(root).unwrap(), ALERT_STATE_NAME);
        match fs::read_to_string(path) {
            Ok(text) => serde_json::from_str(&text).unwrap_or(Value::Null),
            Err(_) => json!({}),
        }
    }

    fn unit_value(config: &BTreeMap<String, Yaml>, unit: &str) -> Value {
        read_state(config)
            .get("units")
            .and_then(|units| units.get(unit))
            .cloned()
            .unwrap_or_else(|| json!({}))
    }

    fn seed(config: &BTreeMap<String, Yaml>, value: Value) {
        let root = config.get("state_root").unwrap().as_str().unwrap();
        fs::create_dir_all(root).unwrap();
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
        let path = join_alert(&lexical_absolute(root).unwrap(), ALERT_STATE_NAME);
        fs::write(path, serde_json::to_vec_pretty(&value).unwrap()).unwrap();
    }

    #[test]
    fn isoformat_matches_python() {
        assert_eq!(isoformat(sample_now()), "2026-10-11T12:00:00+08:00");
    }

    #[test]
    fn renotify_after_defaults_to_24h_and_accepts_12h() {
        assert_eq!(
            renotify_after(&BTreeMap::new()),
            Duration::from_secs(24 * 3600)
        );
        let mut config = BTreeMap::new();
        config.insert(
            "alert".to_owned(),
            Yaml::map([("renotify_after".to_owned(), Yaml::string("12h"))]),
        );
        assert_eq!(renotify_after(&config), Duration::from_secs(12 * 3600));
    }

    #[test]
    fn alert_state_name_cannot_be_a_service() {
        assert!(ALERT_STATE_NAME.ends_with(".json"));
        assert!(!valid_service_name(
            ALERT_STATE_NAME.trim_end_matches(".json")
        ));
    }

    #[test]
    fn help_mentions_journal_lines() {
        let mut command = Cli::command();
        let help = command
            .find_subcommand_mut("notify-failure")
            .unwrap()
            .render_help()
            .to_string();
        assert!(help.contains("last 30 journal lines"), "{help}");
    }

    #[test]
    fn alert_line_is_printed_before_config_load_fails() {
        let path = std::env::temp_dir().join(format!(
            "missing-backupctl-config-{}-{}.yaml",
            std::process::id(),
            TMP_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let text = captured(|| {
            let error = run_notify_failure(path.to_str().unwrap(), BACKUP_UNIT);
            assert!(error.is_err());
        });
        assert_eq!(
            text.matches("ALERT: systemd unit failed").count(),
            1,
            "{text}"
        );
        assert!(text.contains("ALERT: systemd unit failed: homelab-backup.service"));
    }

    #[test]
    fn notify_failure_does_not_create_the_global_lock() {
        let tmp = Tmp::new();
        let root = tmp.path().join("state");
        let lock = tmp.path().join("backupctl.lock");
        let mut config = config_with(root.to_str().unwrap(), None);
        config.insert(
            "lock_file".to_owned(),
            Yaml::string(lock.display().to_string()),
        );
        let mut hook = hooks("tail\n");
        let err = notify_at(&config, CHECK_UNIT, sample_now(), &mut hook);
        assert!(err.contains("ALERT: systemd unit failed: homelab-backup-check.service"));
        assert!(!lock.exists());
        assert!(root.join(ALERT_LOCK_NAME).exists());
        assert_eq!(hook.pushes.len(), 1);
    }

    #[test]
    fn ntfy_problem_recovery_and_ok_skip() {
        let sent = RefCell::new(Vec::new());
        let mut opener = |target: &str, body: &[u8], headers: &[(&str, &str)]| {
            sent.borrow_mut().push((
                target.to_owned(),
                body.to_vec(),
                headers
                    .iter()
                    .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
                    .collect::<Vec<_>>(),
            ));
            Ok(())
        };
        let mut config = BTreeMap::new();
        assert!(!notify(
            &config,
            "Title",
            "body",
            false,
            false,
            &mut |_target, _body, _headers| Ok(())
        ));
        let tmp = Tmp::new();
        let url_path = tmp.path().join("notify-url");
        fs::write(&url_path, "\nhttps://ntfy.sh/secret-topic\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        config.insert(
            "alert".to_owned(),
            Yaml::map([(
                "notify_url_file".to_owned(),
                Yaml::string(url_path.display().to_string()),
            )]),
        );
        sent.borrow_mut().clear();
        assert!(notify(&config, "Title", "body", false, false, &mut opener));
        {
            let sent = sent.borrow();
            assert_eq!(sent[0].0, "https://ntfy.sh/secret-topic");
            assert!(sent[0].1.windows(4).any(|item| item == b"body"));
            assert!(
                sent[0]
                    .2
                    .iter()
                    .any(|(key, value)| key == "Title" && value == "Title")
            );
            assert!(
                sent[0]
                    .2
                    .iter()
                    .any(|(key, value)| key == "Priority" && value == "high")
            );
        }
        sent.borrow_mut().clear();
        assert!(!notify(&config, "Title", "body", true, false, &mut opener));
        assert!(sent.borrow().is_empty());
        assert!(notify(&config, "Title", "body", true, true, &mut opener));
        {
            let sent = sent.borrow();
            assert!(
                sent[0]
                    .2
                    .iter()
                    .any(|(key, value)| key == "Priority" && value == "default")
            );
            assert!(
                sent[0]
                    .2
                    .iter()
                    .any(|(key, value)| key == "Tags" && value == "white_check_mark")
            );
        }
    }

    #[test]
    fn body_cap_does_not_split_a_character() {
        let tmp = Tmp::new();
        let url_path = tmp.path().join("notify-url");
        fs::write(&url_path, "https://ntfy.sh/t\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut config = BTreeMap::new();
        config.insert(
            "alert".to_owned(),
            Yaml::map([(
                "notify_url_file".to_owned(),
                Yaml::string(url_path.display().to_string()),
            )]),
        );
        let mut body = Vec::new();
        notify(
            &config,
            "T",
            &"備".repeat(5000),
            false,
            false,
            &mut |_target, bytes, _headers| {
                body = bytes.to_vec();
                Ok(())
            },
        );
        assert!(body.len() <= 10_000);
        let text = String::from_utf8(body).unwrap();
        assert!(text.ends_with('備'));
    }

    #[test]
    fn healthchecks_fail_and_success_endpoints() {
        let tmp = Tmp::new();
        let url_path = tmp.path().join("notify-url");
        fs::write(&url_path, "https://hc-ping.com/uuid\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut config = BTreeMap::new();
        config.insert(
            "alert".to_owned(),
            Yaml::map([(
                "notify_url_file".to_owned(),
                Yaml::string(url_path.display().to_string()),
            )]),
        );
        let mut targets = Vec::new();
        let mut opener = |target: &str, _body: &[u8], _headers: &[(&str, &str)]| {
            targets.push(target.to_owned());
            Ok(())
        };
        assert!(notify(&config, "T", "b", false, false, &mut opener));
        assert!(notify(&config, "T", "b", true, false, &mut opener));
        assert_eq!(
            targets,
            vec![
                "https://hc-ping.com/uuid/fail".to_owned(),
                "https://hc-ping.com/uuid".to_owned()
            ]
        );
        assert!(healthchecks("https://hc.example.org/ping/abc"));
    }

    #[test]
    fn delivery_failure_names_only_the_type() {
        let tmp = Tmp::new();
        let url_path = tmp.path().join("notify-url");
        fs::write(&url_path, "https://ntfy.sh/secret-topic\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        let mut config = BTreeMap::new();
        config.insert(
            "alert".to_owned(),
            Yaml::map([(
                "notify_url_file".to_owned(),
                Yaml::string(url_path.display().to_string()),
            )]),
        );
        for name in ["URLError", "RuntimeError", "KeyError"] {
            let err = captured(|| {
                let delivered = notify(&config, "T", "b", false, false, &mut |_t, _b, _h| {
                    Err(name.to_owned())
                });
                assert!(!delivered);
            });
            assert_eq!(
                err.trim(),
                format!("WARNING: notification delivery failed: {name}")
            );
            assert!(!err.contains("secret-topic"), "{err}");
        }
    }

    #[test]
    fn private_url_file_rules() {
        let tmp = Tmp::new();
        let url_path = tmp.path().join("notify-url");
        fs::write(&url_path, "https://ntfy.sh/secret-topic\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        let config = |path: &Path| {
            let mut config = BTreeMap::new();
            config.insert(
                "alert".to_owned(),
                Yaml::map([(
                    "notify_url_file".to_owned(),
                    Yaml::string(path.display().to_string()),
                )]),
            );
            config
        };
        assert_eq!(
            notification_url(&config(&url_path)).as_deref(),
            Some("https://ntfy.sh/secret-topic")
        );
        for mode in [0o640, 0o644, 0o604] {
            fs::set_permissions(&url_path, fs::Permissions::from_mode(mode)).unwrap();
            let err = captured(|| assert!(notification_url(&config(&url_path)).is_none()));
            assert!(
                err.contains("WARNING: notification URL file is unusable"),
                "{err}"
            );
            assert!(err.contains("group/others"), "{err}");
            assert!(!err.contains("secret-topic"), "{err}");
        }
        fs::write(&url_path, "http://ntfy.sh/topic\n").unwrap();
        fs::set_permissions(&url_path, fs::Permissions::from_mode(0o600)).unwrap();
        let err = captured(|| assert!(notification_url(&config(&url_path)).is_none()));
        assert!(err.contains("https"), "{err}");
        assert!(!err.contains("ntfy.sh/topic"), "{err}");
        let missing = tmp.path().join("absent");
        let err = captured(|| assert!(notification_url(&config(&missing)).is_none()));
        assert!(
            err.contains("WARNING: notification URL file is missing"),
            "{err}"
        );
        assert!(notification_url(&BTreeMap::new()).is_none());
    }

    #[test]
    fn utf8_cuts_and_notification_budget() {
        let text = "a備份é😀".repeat(50);
        for limit in 0..40 {
            for cut in [
                utf8_head(text.as_str(), limit),
                utf8_tail(text.as_str(), limit),
            ] {
                assert!(cut.len() <= limit, "{limit} {cut}");
                assert!(text.contains(cut.trim_matches('…')), "{limit} {cut}");
            }
        }
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let journal = (0..400)
            .map(|index| format!("第{index}行：備份失敗，無法連線到儲存庫\n"))
            .collect::<String>();
        let mut hook = hooks(&journal);
        let stderr = captured(|| {
            push_unit_failure(
                &config,
                CHECK_UNIT,
                "check failed",
                "- svc-a: 錯誤",
                &mut hook,
            );
        });
        assert!(!stderr.contains('�'));
        let (title, body) = &hook.pushes[0];
        let packed = format!("{title}\n\n{body}");
        assert!(packed.len() <= NOTIFY_BODY_LIMIT);
        assert!(packed.len() > NOTIFY_BODY_LIMIT - 4, "{}", packed.len());
        let (summary, tail) = body.split_once("\n\n").unwrap();
        assert_eq!(summary, "- svc-a: 錯誤");
        assert!(tail.starts_with('…'));
        assert!(journal.ends_with(tail.strip_prefix('…').unwrap()));
        assert!(tail.contains("第399行"));

        hook.pushes.clear();
        push_unit_failure(
            &config,
            CHECK_UNIT,
            "check failed",
            &"服務失敗".repeat(2000),
            &mut hook,
        );
        let body = &hook.pushes[0].1;
        assert!(body.ends_with('…'));
        assert!(
            "服務失敗"
                .repeat(2000)
                .starts_with(body.trim_end_matches('…'))
        );
        assert!(!body.contains("journal"));

        hook.journal = Ok("line1\nline2\n".to_owned());
        hook.pushes.clear();
        push_unit_failure(&config, CHECK_UNIT, "check failed", "- svc-a: x", &mut hook);
        assert_eq!(hook.pushes[0].1, "- svc-a: x\n\nline1\nline2\n");
    }

    #[test]
    fn unit_failure_is_throttled_per_unit_and_records_delivery() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let mut hook = hooks("tail");
        let err = notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert!(err.contains("ALERT: systemd unit failed: homelab-backup.service"));
        assert_eq!(hook.pushes.len(), 1);
        assert!(
            hook.pushes[0]
                .0
                .contains("homelab-backup.service failed (unknown)")
        );
        assert!(hook.pushes[0].1.contains("tail"));
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 60), &mut hook);
        assert!(hook.pushes.is_empty());
        notify_at(&config, CHECK_UNIT, later(now, 60), &mut hook);
        assert_eq!(hook.pushes.len(), 1);
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 25 * 60), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        let mut missed = hooks("tail");
        missed.deliver_ok = false;
        let err = notify_at(&config, MAINTENANCE_UNIT, later(now, 120), &mut missed);
        assert!(err.contains(NOT_DELIVERED), "{err}");
        assert_eq!(missed.pushes.len(), 1);
        missed.deliver_ok = true;
        missed.pushes.clear();
        notify_at(&config, MAINTENANCE_UNIT, later(now, 125), &mut missed);
        assert_eq!(missed.pushes.len(), 1);
    }

    #[test]
    fn non_exit_code_monitor_is_added_for_other_units() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let mut hook = hooks("");
        hook.env
            .insert("MONITOR_SERVICE_RESULT".to_owned(), "timeout".to_owned());
        hook.env
            .insert("MONITOR_EXIT_CODE".to_owned(), "killed".to_owned());
        hook.env
            .insert("MONITOR_EXIT_STATUS".to_owned(), "TERM".to_owned());
        notify_at(&config, CHECK_UNIT, sample_now(), &mut hook);
        let (title, body) = &hook.pushes[0];
        assert!(title.contains("homelab-backup-check.service failed (timeout)"));
        assert!(body.contains("result=timeout, exit_code=killed, exit_status=TERM"));
    }

    #[test]
    fn corrupted_and_unwritable_state_still_notifies() {
        let tmp = Tmp::new();
        let root = tmp.path().join("state");
        fs::create_dir_all(&root).unwrap();
        let config = config_with(root.to_str().unwrap(), None);
        fs::write(root.join(ALERT_STATE_NAME), "{not json").unwrap();
        let err = captured(|| assert!(load_alert_state(&config).is_empty()));
        assert!(err.contains("WARNING"), "{err}");
        let mut hook = hooks("tail");
        notify_at(&config, CHECK_UNIT, sample_now(), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        fs::set_permissions(&root, fs::Permissions::from_mode(0o500)).unwrap();
        let mut hook = hooks("tail");
        let err = notify_at(&config, MAINTENANCE_UNIT, sample_now(), &mut hook);
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(hook.pushes.len(), 1, "{err}");
    }

    #[test]
    fn status_handoff_uses_invocation_or_recent_unconsumed_record() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        seed(
            &config,
            json!({
                "status": {
                    "handled_at": isoformat(now),
                    "invocation_id": "run-1",
                    "signature": "stale",
                    "notified_at": isoformat(now)
                }
            }),
        );
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-1".to_owned());
        notify_at(&config, STATUS_UNIT, later(now, 1), &mut hook);
        assert!(hook.pushes.is_empty());
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-2".to_owned());
        notify_at(&config, STATUS_UNIT, later(now, 5), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        seed(
            &config,
            json!({"status": {"handled_at": isoformat(now), "notified_at": isoformat(now)}}),
        );
        hook.pushes.clear();
        hook.env.remove("MONITOR_INVOCATION_ID");
        notify_at(&config, STATUS_UNIT, later(now, 1), &mut hook);
        assert!(hook.pushes.is_empty());
        notify_at(&config, STATUS_UNIT, later(now, 3), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        seed(&config, json!({"status": {"handled_at": isoformat(now)}}));
        hook.pushes.clear();
        notify_at(&config, STATUS_UNIT, later(now, 21), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        seed(&config, json!({"status": {"handled_at": isoformat(now)}}));
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-9".to_owned());
        notify_at(&config, STATUS_UNIT, later(now, 1), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        seed(
            &config,
            json!({
                "status": {
                    "handled_at": isoformat(later(now, 120)),
                    "invocation_id": "run-1"
                }
            }),
        );
        hook.pushes.clear();
        hook.env.remove("MONITOR_INVOCATION_ID");
        notify_at(&config, STATUS_UNIT, later(now, 121), &mut hook);
        assert!(hook.pushes.is_empty());

        seed(
            &config,
            json!({"status": {"handled_at": isoformat(now), "invocation_id": "run-1"}}),
        );
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-1".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 1), &mut hook);
        assert_eq!(hook.pushes.len(), 1);
    }

    #[test]
    fn clear_unit_alert_resets_only_that_unit() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let mut hook = hooks("tail");
        notify_at(&config, STATUS_UNIT, now, &mut hook);
        notify_at(&config, CHECK_UNIT, now, &mut hook);
        hook.pushes.clear();
        notify_at(&config, STATUS_UNIT, later(now, 5), &mut hook);
        assert!(hook.pushes.is_empty());
        clear_unit_alert(&config, STATUS_UNIT);
        assert_eq!(unit_value(&config, STATUS_UNIT), json!({}));
        assert!(unit_value(&config, CHECK_UNIT).get("notified_at").is_some());
        notify_at(&config, STATUS_UNIT, later(now, 120), &mut hook);
        assert_eq!(hook.pushes.len(), 1);
        clear_unit_alert(&config, "missing.service");
        assert!(!tmp.path().join("state").join("backupctl.lock").exists());
    }

    #[test]
    fn transaction_saves_private_lock_and_skips_unchanged_or_panic() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let mut pace = standard_pace();
        with_alert_state(&config, &mut pace, |state| {
            state.insert("x".to_owned(), json!(1));
        });
        assert_eq!(read_state(&config), json!({"x": 1}));
        let lock = tmp.path().join("state").join(ALERT_LOCK_NAME);
        let mode = fs::symlink_metadata(&lock).unwrap().mode();
        assert_eq!(mode & 0o170000, 0o100000);
        assert_eq!(mode & 0o777, 0o600);

        let before = fs::read(tmp.path().join("state").join(ALERT_STATE_NAME)).unwrap();
        std::thread::sleep(Duration::from_millis(20));
        with_alert_state(&config, &mut pace, |_| {});
        assert_eq!(
            fs::read(tmp.path().join("state").join(ALERT_STATE_NAME)).unwrap(),
            before
        );

        let panicked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut pace = standard_pace();
            with_alert_state(&config, &mut pace, |state| {
                state.insert("y".to_owned(), json!(2));
                panic!("boom");
            });
        }));
        assert!(panicked.is_err());
        assert_eq!(read_state(&config), json!({"x": 1}));
        with_alert_state(&config, &mut pace, |state| {
            state.insert("y".to_owned(), json!(2));
        });
        assert_eq!(read_state(&config)["y"], json!(2));

        with_alert_state(&BTreeMap::new(), &mut pace, |state| {
            state.insert("x".to_owned(), json!(1));
        });
    }

    #[test]
    fn waits_for_a_briefly_held_lock_and_fails_open_when_stuck() {
        let tmp = Tmp::new();
        let root = tmp.path().join("state");
        fs::create_dir_all(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let config = config_with(root.to_str().unwrap(), None);
        let lock_path = root.join(ALERT_LOCK_NAME);
        let holder = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&lock_path)
            .unwrap();
        flock(holder.as_fd(), FlockOperation::LockExclusive).unwrap();
        let sleeps = std::cell::RefCell::new(Vec::new());
        let err = captured(|| {
            let mut pace = LockPace {
                timeout: LOCK_TIMEOUT,
                retry: LOCK_RETRY,
                sleep: Box::new(|delay| {
                    sleeps.borrow_mut().push(delay);
                    let _ = flock(holder.as_fd(), FlockOperation::Unlock);
                }),
            };
            with_alert_state(&config, &mut pace, |state| {
                state.insert("x".to_owned(), json!(1));
            });
        });
        assert_eq!(sleeps.borrow().as_slice(), &[LOCK_RETRY]);
        assert_eq!(err, "");
        assert_eq!(read_state(&config), json!({"x": 1}));

        flock(holder.as_fd(), FlockOperation::LockExclusive).unwrap();
        let err = captured(|| {
            let mut pace = LockPace {
                timeout: Duration::ZERO,
                retry: LOCK_RETRY,
                sleep: Box::new(|_| {}),
            };
            with_alert_state(&config, &mut pace, |state| {
                state.insert("z".to_owned(), json!(3));
            });
        });
        assert!(err.contains("WARNING: could not lock alert state"), "{err}");
        assert!(err.contains("still held after 0s"), "{err}");
        assert_eq!(read_state(&config)["z"], json!(3));
        let _ = flock(holder.as_fd(), FlockOperation::Unlock);

        let target = tmp.path().join("elsewhere");
        fs::remove_file(&lock_path).unwrap();
        symlink(&target, &lock_path).unwrap();
        let err = captured(|| {
            let mut pace = standard_pace();
            with_alert_state(&config, &mut pace, |state| {
                state.insert("s".to_owned(), json!(4));
            });
        });
        assert!(err.contains("WARNING: could not lock alert state"), "{err}");
        assert!(!target.exists());
        assert_eq!(read_state(&config)["s"], json!(4));
    }

    #[test]
    fn notification_goes_out_while_the_alert_lock_is_stuck() {
        let tmp = Tmp::new();
        let root = tmp.path().join("state");
        fs::create_dir_all(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let config = config_with(root.to_str().unwrap(), None);
        let holder = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(root.join(ALERT_LOCK_NAME))
            .unwrap();
        flock(holder.as_fd(), FlockOperation::LockExclusive).unwrap();
        let mut hook = hooks("tail");
        let err = captured(|| {
            let mut pace = LockPace {
                timeout: Duration::ZERO,
                retry: LOCK_RETRY,
                sleep: Box::new(|_| {}),
            };
            notify_failure(
                &config,
                BACKUP_UNIT,
                sample_now(),
                true,
                &mut hook,
                &mut pace,
            );
        });
        assert_eq!(hook.pushes.len(), 1, "{err}");
        let _ = flock(holder.as_fd(), FlockOperation::Unlock);
    }

    #[test]
    fn concurrent_threads_do_not_lose_updates() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let mut threads = Vec::new();
        for _ in 0..4 {
            let config = config.clone();
            threads.push(std::thread::spawn(move || {
                for _ in 0..25 {
                    let mut pace = standard_pace();
                    with_alert_state(&config, &mut pace, |state| {
                        let count = state.get("count").and_then(Value::as_i64).unwrap_or(0);
                        state.insert("count".to_owned(), json!(count + 1));
                    });
                }
            }));
        }
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(read_state(&config), json!({"count": 100}));
    }

    #[test]
    fn run_due_records_failures_and_throttles_per_service() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let long = format!("\n  {}\nsecond", "e".repeat(500));
        record_run_due(
            &config,
            now,
            Some("run-1"),
            &["a".to_owned()],
            &[("a".to_owned(), long)],
            Some(&BTreeSet::from(["a".to_owned()])),
        );
        let error = unit_value(&config, BACKUP_UNIT)["run"]["failures"]["a"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(error.chars().count(), ERROR_LINE_LIMIT);
        assert!(error.starts_with("eee"));
        assert!(error.ends_with('…'));

        let root = tmp.path().join("fresh");
        let config = config_with(root.to_str().unwrap(), None);
        let known = BTreeSet::from(["a".to_owned(), "b".to_owned(), "c".to_owned()]);
        record_run_due(
            &config,
            now,
            Some("run-1"),
            &["b".to_owned(), "a".to_owned(), "c".to_owned()],
            &[
                ("b".to_owned(), "x".to_owned()),
                ("a".to_owned(), "y\nmore".to_owned()),
            ],
            Some(&known),
        );
        assert_eq!(
            unit_value(&config, BACKUP_UNIT)["run"],
            json!({
                "invocation_id": "run-1",
                "completed_at": isoformat(now),
                "failures": {"a": "y", "b": "x"}
            })
        );
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-1".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert!(hook.pushes[0].1.contains("- a: y"));
        assert!(hook.pushes[0].1.contains("- b: x"));
        record_run_due(
            &config,
            later(now, 5),
            Some("run-5"),
            &["a".to_owned()],
            &[("a".to_owned(), "other text, same service".to_owned())],
            Some(&known),
        );
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-5".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        assert!(hook.pushes.is_empty());
        assert_eq!(
            unit_value(&config, BACKUP_UNIT)["services"],
            json!({
                "a": {"notified_at": isoformat(now)},
                "b": {"notified_at": isoformat(now)}
            })
        );
    }

    #[test]
    fn newly_failing_service_is_pushed_without_repeating_others() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let known = BTreeSet::from(["a".to_owned(), "b".to_owned()]);
        let mut hook = hooks("journal tail\n");
        let round =
            |hook: &mut TestHooks, minutes: i64, failures: &[(&str, &str)], due: &[&str]| {
                let attempted = due
                    .iter()
                    .map(|name| (*name).to_owned())
                    .collect::<Vec<_>>();
                let failures = failures
                    .iter()
                    .map(|(name, error)| ((*name).to_owned(), (*error).to_owned()))
                    .collect::<Vec<_>>();
                record_run_due(
                    &config,
                    later(now, minutes),
                    Some(&format!("run-{minutes}")),
                    &attempted,
                    &failures,
                    Some(&known),
                );
                hook.env
                    .insert("MONITOR_INVOCATION_ID".to_owned(), format!("run-{minutes}"));
                hook.pushes.clear();
                if !failures.is_empty() {
                    notify_at(&config, BACKUP_UNIT, later(now, minutes), hook);
                }
            };
        round(&mut hook, 0, &[("a", "x")], &["a", "b"]);
        assert_eq!(hook.pushes.len(), 1);
        round(&mut hook, 5, &[("a", "x"), ("b", "y")], &["a", "b"]);
        assert_eq!(hook.pushes.len(), 1);
        let body = &hook.pushes[0].1;
        assert!(
            body.contains("failed: b\nstill failing (already notified): a\n- b: y"),
            "{body}"
        );
        assert!(!body.contains("- a:"));
        round(&mut hook, 10, &[("a", "x"), ("b", "y")], &["a", "b"]);
        assert!(hook.pushes.is_empty());
    }

    #[test]
    fn success_prunes_removed_services_and_legacy_fields() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let both = BTreeSet::from(["a".to_owned(), "b".to_owned()]);
        record_run_due(
            &config,
            now,
            Some("run-0"),
            &["a".to_owned(), "b".to_owned()],
            &[
                ("a".to_owned(), "x".to_owned()),
                ("b".to_owned(), "y".to_owned()),
            ],
            Some(&both),
        );
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        record_run_due(
            &config,
            later(now, 5),
            Some("run-5"),
            &["a".to_owned(), "b".to_owned()],
            &[("b".to_owned(), "y".to_owned())],
            Some(&both),
        );
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-5".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        assert!(hook.pushes.is_empty());
        let services = unit_value(&config, BACKUP_UNIT)["services"].clone();
        assert_eq!(
            services.as_object().unwrap().keys().collect::<Vec<_>>(),
            vec!["b"]
        );

        record_run_due(
            &config,
            later(now, 6),
            Some("run-6"),
            &["a".to_owned()],
            &[],
            Some(&BTreeSet::from(["a".to_owned(), "b".to_owned()])),
        );
        // a succeeded, b was not attempted, so b stays.
        assert!(
            unit_value(&config, BACKUP_UNIT)["services"]
                .get("b")
                .is_some()
        );
        assert!(unit_value(&config, BACKUP_UNIT).get("run").is_none());

        record_run_due(
            &config,
            later(now, 7),
            Some("run-7"),
            &["b".to_owned()],
            &[],
            Some(&BTreeSet::from(["b".to_owned()])),
        );
        assert_eq!(unit_value(&config, BACKUP_UNIT), json!({}));

        seed(
            &config,
            json!({
                "units": {
                    "homelab-backup.service": {
                        "notified_at": isoformat(now),
                        "failure_signature": ["a"],
                        "notified_signature": ["a"]
                    }
                }
            }),
        );
        record_run_due(
            &config,
            later(now, 8),
            Some("run-8"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&BTreeSet::from(["a".to_owned()])),
        );
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-8".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 8), &mut hook);
        assert_eq!(hook.pushes.len(), 1);
        let record = unit_value(&config, BACKUP_UNIT);
        for legacy in ["notified_at", "failure_signature", "notified_signature"] {
            assert!(record.get(legacy).is_none(), "{legacy} still in {record}");
        }
    }

    #[test]
    fn handoff_without_monitor_id_and_stale_run_becomes_a_crash() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        record_run_due(
            &config,
            now,
            None,
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&BTreeSet::from(["a".to_owned()])),
        );
        let mut hook = hooks("tail");
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert!(hook.pushes[0].1.contains("- a: x"));
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        assert!(hook.pushes[0].0.contains("failed (unknown)"));

        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-1".to_owned());
        hook.pushes.clear();
        // The consumed record has no invocation id, so a monitor id is a crash.
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        record_run_due(
            &config,
            now,
            None,
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&BTreeSet::from(["a".to_owned()])),
        );
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert!(hook.pushes[0].0.contains("failed (unknown)"));

        record_run_due(
            &config,
            now,
            Some("run-0"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&BTreeSet::from(["a".to_owned()])),
        );
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-crashed".to_owned());
        hook.env
            .insert("MONITOR_SERVICE_RESULT".to_owned(), "timeout".to_owned());
        hook.env
            .insert("MONITOR_EXIT_CODE".to_owned(), "killed".to_owned());
        hook.env
            .insert("MONITOR_EXIT_STATUS".to_owned(), "TERM".to_owned());
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        let (title, body) = &hook.pushes[0];
        assert!(
            title.contains("homelab-backup.service failed (timeout)"),
            "{title}"
        );
        assert!(body.contains("result=timeout, exit_code=killed, exit_status=TERM"));
        assert!(!body.split("\n\n").next().unwrap().contains("- a:"));
        assert!(
            unit_value(&config, BACKUP_UNIT)["crashes"]
                .get("crash:timeout")
                .is_some()
        );
    }

    #[test]
    fn crashes_record_running_services_and_clear_once_quiet() {
        let tmp = Tmp::new();
        let state_root = tmp.path().join("state");
        let services = tmp.path().join("services");
        fs::create_dir_all(&services).unwrap();
        fs::set_permissions(&services, fs::Permissions::from_mode(0o755)).unwrap();
        for name in ["a", "b", "c"] {
            let dir = services.join(name);
            fs::create_dir_all(&dir).unwrap();
            fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
            let manifest = dir.join("backup.yaml");
            fs::write(&manifest, format!("service: {name}\n")).unwrap();
            fs::set_permissions(&manifest, fs::Permissions::from_mode(0o644)).unwrap();
        }
        let mut config = config_with(state_root.to_str().unwrap(), None);
        config.insert(
            "services_root".to_owned(),
            Yaml::string(services.display().to_string()),
        );
        let now = sample_now();
        let stamp = isoformat(now);
        let running = |name: &str, result: &str| {
            let mut state = BTreeMap::new();
            state.insert("service".to_owned(), json!(name));
            state.insert("last_result".to_owned(), json!(result));
            state.insert("last_attempt_at".to_owned(), json!(stamp));
            save_backup_state(state_root.to_str().unwrap(), name, &state).unwrap();
        };
        running("a", "running");
        fs::write(state_root.join("b.json"), "{not json").unwrap();
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        hook.running = running_services(&config);
        assert_eq!(hook.running, vec!["a".to_owned()]);
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert_eq!(
            unit_value(&config, BACKUP_UNIT)["crashes"]["crash:unknown"]["running"],
            json!(["a"])
        );
        running("a", "success");
        running("c", "running");
        hook.running = running_services(&config);
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-5".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        assert_eq!(
            unit_value(&config, BACKUP_UNIT)["crashes"]["crash:unknown"]["running"],
            json!(["a", "c"])
        );

        let bare = Tmp::new();
        let config = config_with(bare.path().join("state").to_str().unwrap(), None);
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert!(
            unit_value(&config, BACKUP_UNIT)["crashes"]["crash:unknown"]
                .get("running")
                .is_none()
        );
        hook.deliver_ok = false;
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-x".to_owned());
        let fresh = Tmp::new();
        let config = config_with(fresh.path().join("state").to_str().unwrap(), None);
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        assert_eq!(hook.pushes.len(), 1);
        let entry = unit_value(&config, BACKUP_UNIT)["crashes"]["crash:unknown"].clone();
        assert_eq!(entry["last_seen_at"], json!(isoformat(now)));
        assert!(entry.get("notified_at").is_none());
        hook.deliver_ok = true;
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        assert_eq!(hook.pushes.len(), 1);

        hook.deliver_ok = true;
        hook.pushes.clear();
        let quiet = Tmp::new();
        let config = config_with(quiet.path().join("state").to_str().unwrap(), None);
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-x".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        hook.pushes.clear();
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-y".to_owned());
        notify_at(&config, BACKUP_UNIT, later(now, 30), &mut hook);
        assert!(hook.pushes.is_empty());
        let entry = unit_value(&config, BACKUP_UNIT)["crashes"]["crash:unknown"].clone();
        assert_eq!(entry["notified_at"], json!(isoformat(now)));
        assert_eq!(entry["last_seen_at"], json!(isoformat(later(now, 30))));

        let cleared = Tmp::new();
        let config = config_with(cleared.path().join("state").to_str().unwrap(), None);
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-x".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        let known = BTreeSet::from(["a".to_owned()]);
        record_run_due(&config, later(now, 5), Some("idle"), &[], &[], Some(&known));
        record_run_due(
            &config,
            later(now, 10),
            Some("ok"),
            &["a".to_owned()],
            &[],
            Some(&known),
        );
        record_run_due(
            &config,
            later(now, 59),
            Some("fail"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&known),
        );
        assert!(unit_value(&config, BACKUP_UNIT).get("crashes").is_some());
        record_run_due(
            &config,
            later(now, 60),
            Some("fail2"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&known),
        );
        assert!(unit_value(&config, BACKUP_UNIT).get("crashes").is_none());
    }

    #[test]
    fn nothing_due_clears_only_a_quiet_crash_and_skips_pointless_writes() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let known = BTreeSet::from(["a".to_owned()]);
        record_run_due(&config, now, Some("none"), &[], &[], Some(&known));
        assert!(!tmp.path().join("state").join(ALERT_STATE_NAME).exists());
        assert!(!tmp.path().join("state").join(ALERT_LOCK_NAME).exists());

        record_run_due(
            &config,
            now,
            Some("run-0"),
            &["a".to_owned(), "b".to_owned()],
            &[
                ("a".to_owned(), "x".to_owned()),
                ("b".to_owned(), "y".to_owned()),
            ],
            Some(&BTreeSet::from(["a".to_owned(), "b".to_owned()])),
        );
        let mut hook = hooks("tail");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-x".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        let before = unit_value(&config, BACKUP_UNIT);
        assert!(before.get("crashes").is_some());
        record_run_due(
            &config,
            later(now, 60),
            Some("idle"),
            &[],
            &[],
            Some(&BTreeSet::from(["b".to_owned()])),
        );
        let after = unit_value(&config, BACKUP_UNIT);
        assert!(after.get("crashes").is_none());
        let mut expected = before;
        expected.as_object_mut().unwrap().remove("crashes");
        assert_eq!(after, expected);

        let bytes = fs::read(tmp.path().join("state").join(ALERT_STATE_NAME)).unwrap();
        record_run_due(
            &config,
            later(now, 61),
            Some("idle2"),
            &[],
            &[],
            Some(&known),
        );
        // 61 minutes after the crash is already quiet, so the previous call
        // cleared it. Another idle round must not rewrite the file.
        assert_eq!(
            fs::read(tmp.path().join("state").join(ALERT_STATE_NAME)).unwrap(),
            bytes
        );

        notify_at(&config, CHECK_UNIT, now, &mut hook);
        record_run_due(
            &config,
            later(now, 62),
            Some("other"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&known),
        );
        record_run_due(
            &config,
            later(now, 63),
            Some("done"),
            &["a".to_owned()],
            &[],
            Some(&known),
        );
        assert!(unit_value(&config, CHECK_UNIT).get("notified_at").is_some());

        let empty = Tmp::new();
        let config = config_with(empty.path().join("state").to_str().unwrap(), None);
        record_run_due(
            &config,
            now,
            Some("ok"),
            &["a".to_owned()],
            &[],
            Some(&known),
        );
        assert!(!empty.path().join("state").join(ALERT_STATE_NAME).exists());
        assert!(!empty.path().join("state").join(ALERT_LOCK_NAME).exists());
    }

    #[test]
    fn still_failing_line_survives_an_oversized_summary() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let now = sample_now();
        let mut names = vec!["a".to_owned()];
        names.extend((0..40).map(|index| format!("svc-{index:02}")));
        let known = names.iter().cloned().collect::<BTreeSet<_>>();
        record_run_due(
            &config,
            now,
            Some("run-0"),
            &["a".to_owned()],
            &[("a".to_owned(), "x".to_owned())],
            Some(&known),
        );
        let mut hook = hooks("journal line\n");
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-0".to_owned());
        notify_at(&config, BACKUP_UNIT, now, &mut hook);
        let attempted = names.clone();
        let mut failures = vec![("a".to_owned(), "x".to_owned())];
        failures.extend((0..40).map(|index| (format!("svc-{index:02}"), "錯誤".repeat(200))));
        record_run_due(
            &config,
            later(now, 5),
            Some("run-5"),
            &attempted,
            &failures,
            Some(&known),
        );
        hook.env
            .insert("MONITOR_INVOCATION_ID".to_owned(), "run-5".to_owned());
        hook.pushes.clear();
        notify_at(&config, BACKUP_UNIT, later(now, 5), &mut hook);
        let (title, body) = &hook.pushes[0];
        let packed = format!("{title}\n\n{body}");
        assert!(packed.len() <= NOTIFY_BODY_LIMIT);
        assert!(body.ends_with('…'));
        assert!(
            body.contains("\nstill failing (already notified): a\n- svc-00: "),
            "{body}"
        );
        let _ = attempted;
    }

    struct Schedule {
        name: String,
        period: i64,
        offset: i64,
    }

    struct Simulation {
        _tmp: Tmp,
        config: BTreeMap<String, Yaml>,
        schedules: Vec<Schedule>,
        round: i64,
        states: BTreeMap<String, String>,
        pushes: Vec<(i64, String, String)>,
        hooks: TestHooks,
    }

    impl Simulation {
        fn new(schedules: &[(&str, i64, i64)], renotify: &str) -> Self {
            let tmp = Tmp::new();
            let config = config_with(tmp.path().join("state").to_str().unwrap(), Some(renotify));
            Self {
                _tmp: tmp,
                config,
                schedules: schedules
                    .iter()
                    .map(|(name, period, offset)| Schedule {
                        name: (*name).to_owned(),
                        period: *period,
                        offset: *offset,
                    })
                    .collect(),
                round: 0,
                states: BTreeMap::new(),
                pushes: Vec::new(),
                hooks: hooks("journal tail\n"),
            }
        }

        fn now(&self) -> DateTime<FixedOffset> {
            DateTime::parse_from_rfc3339("2026-01-05T00:00:00+00:00").unwrap()
                + chrono::Duration::minutes(5 * self.round)
        }

        fn due(&self) -> Vec<String> {
            let minute = 5 * self.round;
            self.schedules
                .iter()
                .filter(|schedule| {
                    minute >= schedule.offset && (minute - schedule.offset) % schedule.period == 0
                })
                .map(|schedule| schedule.name.clone())
                .collect()
        }

        fn execute(&mut self, failing: &BTreeSet<String>, crash: Option<&str>) -> Option<String> {
            if crash == Some("exit-code") {
                return Some("exit-code".to_owned());
            }
            let due = self.due();
            let mut failures = Vec::new();
            for name in &due {
                self.states.insert(name.clone(), "running".to_owned());
                if matches!(crash, Some("timeout" | "oom-kill")) {
                    return Some(crash.unwrap().to_owned());
                }
                if failing.contains(name) {
                    self.states.insert(name.clone(), "failed".to_owned());
                    failures.push((name.clone(), format!("{name} broke\nlong detail")));
                } else {
                    self.states.insert(name.clone(), "success".to_owned());
                }
            }
            let known = self
                .schedules
                .iter()
                .map(|schedule| schedule.name.clone())
                .collect::<BTreeSet<_>>();
            record_run_due(
                &self.config,
                self.now(),
                Some(&format!("run-due-{}", self.round)),
                &due,
                &failures,
                Some(&known),
            );
            if failures.is_empty() {
                None
            } else {
                Some("exit-code".to_owned())
            }
        }

        fn push_failure(&mut self, result: &str) {
            self.hooks.env.clear();
            self.hooks.env.insert(
                "MONITOR_INVOCATION_ID".to_owned(),
                format!("run-due-{}", self.round),
            );
            let (code, status) = match result {
                "exit-code" => ("exited", "1"),
                "timeout" => ("killed", "TERM"),
                "oom-kill" => ("killed", "KILL"),
                _ => ("", ""),
            };
            self.hooks
                .env
                .insert("MONITOR_SERVICE_RESULT".to_owned(), result.to_owned());
            self.hooks
                .env
                .insert("MONITOR_EXIT_CODE".to_owned(), code.to_owned());
            self.hooks
                .env
                .insert("MONITOR_EXIT_STATUS".to_owned(), status.to_owned());
            self.hooks.running = if result == "exit-code" {
                Vec::new()
            } else {
                let mut names = self
                    .states
                    .iter()
                    .filter(|(_, result)| result.as_str() == "running")
                    .map(|(name, _)| name.clone())
                    .collect::<Vec<_>>();
                names.sort();
                names
            };
            self.hooks.pushes.clear();
            let mut pace = standard_pace();
            notify_failure(
                &self.config,
                BACKUP_UNIT,
                self.now(),
                false,
                &mut self.hooks,
                &mut pace,
            );
            for (title, message) in self.hooks.pushes.drain(..) {
                self.pushes.push((self.round, title, message));
            }
        }

        fn step(&mut self, failing: &[&str], crash: Option<&str>) {
            let failing = failing.iter().map(|name| (*name).to_owned()).collect();
            if let Some(result) = self.execute(&failing, crash) {
                self.push_failure(&result);
            }
            self.round += 1;
        }

        fn run(&mut self, rounds: i64, failing: &[&str], crash: Option<&str>) {
            for _ in 0..rounds {
                self.step(failing, crash);
            }
        }

        fn run_intermittent(&mut self, rounds: i64, every: i64, crash: &str) {
            for index in 0..rounds {
                let crash = if index % every == 0 {
                    Some(crash)
                } else {
                    None
                };
                self.step(&[], crash);
            }
        }

        fn pushed_services(&self) -> BTreeMap<String, Vec<i64>> {
            let mut found = BTreeMap::new();
            for (round, _, body) in &self.pushes {
                for line in body.lines() {
                    if let Some(rest) = line.strip_prefix("- ")
                        && let Some((name, _)) = rest.split_once(':')
                    {
                        found
                            .entry(name.to_owned())
                            .or_insert_with(Vec::new)
                            .push(*round);
                    }
                }
            }
            found
        }

        fn crash_pushes(&self) -> Vec<(i64, String)> {
            self.pushes
                .iter()
                .filter_map(|(round, title, _)| {
                    let start = title.rfind("failed (")?;
                    let inside = title[start + "failed (".len()..].strip_suffix(')')?;
                    Some((*round, inside.to_owned()))
                })
                .collect()
        }

        fn backup_record(&self) -> Value {
            unit_value(&self.config, BACKUP_UNIT)
        }
    }

    const DAY: i64 = 24 * 60 / 5;
    const HOUR: i64 = 60 / 5;

    #[test]
    fn scenario_one_failing_service_next_to_hourly_success() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(DAY, &["svc-a"], None);
        assert_eq!(sim.pushes.len(), 1);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0])])
        );
        assert!(sim.pushes[0].2.contains("- svc-a: svc-a broke"));
        assert!(!sim.pushes[0].2.contains("long detail"));
    }

    #[test]
    fn scenario_two_and_three_failing_services() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 30)], "24h");
        sim.run(DAY, &["svc-a", "svc-b"], None);
        assert_eq!(sim.pushes.len(), 2);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0]), ("svc-b".to_owned(), vec![6])])
        );

        let mut sim = Simulation::new(
            &[("svc-a", 5, 0), ("svc-b", 60, 30), ("svc-c", 120, 90)],
            "24h",
        );
        sim.run(DAY, &["svc-a", "svc-b", "svc-c"], None);
        assert_eq!(sim.pushes.len(), 3);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([
                ("svc-a".to_owned(), vec![0]),
                ("svc-b".to_owned(), vec![6]),
                ("svc-c".to_owned(), vec![18])
            ])
        );

        let mut sim = Simulation::new(
            &[("svc-a", 5, 0), ("svc-b", 60, 0), ("svc-c", 120, 0)],
            "24h",
        );
        sim.run(DAY, &["svc-a", "svc-b", "svc-c"], None);
        assert_eq!(sim.pushes.len(), 1);
        assert!(sim.pushes[0].2.contains("failed: svc-a, svc-b, svc-c"));
    }

    #[test]
    fn scenario_recovery_and_daily_renotify() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(12, &["svc-a"], None);
        sim.run(1, &[], None);
        sim.run(DAY - 13, &["svc-a"], None);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0, 13])])
        );
        assert_eq!(sim.pushes.len(), 2);

        let mut sim = Simulation::new(&[("svc-b", 60, 0)], "24h");
        sim.run(36, &["svc-b"], None);
        sim.run(12, &[], None);
        sim.run(DAY - 48, &["svc-b"], None);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-b".to_owned(), vec![0, 48])])
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(2 * DAY, &["svc-a"], None);
        assert_eq!(sim.pushes.len(), 2);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0, DAY])])
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0)], "6h");
        sim.run(DAY, &["svc-a"], None);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0, 72, 144, 216])])
        );
    }

    #[test]
    fn scenario_crash_after_a_normal_failure_is_not_the_stale_set() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(1, &["svc-a"], None);
        sim.run(20 * 12, &[], Some("exit-code"));
        assert_eq!(sim.pushes.len(), 2);
        let (round, title, body) = &sim.pushes[1];
        assert_eq!(*round, 1);
        assert!(title.ends_with("homelab-backup.service failed (exit-code)"));
        assert!(body.contains("result=exit-code, exit_code=exited, exit_status=1"));
        assert!(!body.split("\n\n").next().unwrap().contains("svc-a"));
        sim.run(12, &[], Some("timeout"));
        assert_eq!(
            sim.crash_pushes(),
            vec![(1, "exit-code".to_owned()), (241, "timeout".to_owned())]
        );
        sim.run(1, &["svc-a"], None);
        assert_eq!(
            sim.backup_record()["crashes"]
                .as_object()
                .unwrap()
                .keys()
                .collect::<Vec<_>>(),
            vec!["crash:timeout"]
        );
        sim.run(1, &[], None);
        assert_eq!(
            sim.backup_record()["crashes"]
                .as_object()
                .unwrap()
                .keys()
                .collect::<Vec<_>>(),
            vec!["crash:timeout"]
        );
        assert_eq!(sim.pushes.len(), 3);
        sim.run(1, &[], Some("timeout"));
        assert_eq!(sim.pushes.len(), 3);
        sim.run(HOUR - 1, &[], None);
        assert!(sim.backup_record().get("crashes").is_some());
        sim.run(1, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("timeout"));
        assert_eq!(
            sim.crash_pushes().last().unwrap(),
            &(268, "timeout".to_owned())
        );
        assert_eq!(sim.pushes.len(), 4);
    }

    #[test]
    fn scenario_intermittent_crashes_stay_throttled_until_quiet() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run_intermittent(DAY, HOUR, "oom-kill");
        assert_eq!(sim.crash_pushes(), vec![(0, "oom-kill".to_owned())]);
        assert_eq!(sim.pushes.len(), 1);
        assert_eq!(
            sim.backup_record()["crashes"]["crash:oom-kill"]["details"],
            json!("result=oom-kill, exit_code=killed, exit_status=KILL")
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0)], "24h");
        sim.run_intermittent(DAY, HOUR / 2, "timeout");
        assert_eq!(sim.crash_pushes(), vec![(0, "timeout".to_owned())]);

        let mut sim = Simulation::new(&[("svc-a", 5, 0)], "24h");
        sim.run(1, &[], Some("oom-kill"));
        sim.run(3 * HOUR, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("oom-kill"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "oom-kill".to_owned()),
                (3 * HOUR + 1, "oom-kill".to_owned())
            ]
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0)], "24h");
        sim.run(DAY + 12, &[], Some("exit-code"));
        assert_eq!(
            sim.crash_pushes(),
            vec![(0, "exit-code".to_owned()), (DAY, "exit-code".to_owned())]
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0)], "24h");
        sim.run(3, &[], Some("exit-code"));
        sim.run(3, &[], Some("timeout"));
        sim.run(3, &[], Some("exit-code"));
        assert_eq!(
            sim.crash_pushes(),
            vec![(0, "exit-code".to_owned()), (3, "timeout".to_owned())]
        );
    }

    #[test]
    fn scenario_interleaved_crashes_and_failures() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(6, &[], Some("timeout"));
        sim.run(94, &["svc-a"], None);
        sim.run(12, &[], Some("exit-code"));
        sim.run(88, &["svc-a"], None);
        sim.run(6, &[], Some("exit-code"));
        sim.run(DAY - 206, &["svc-a"], None);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![6])])
        );
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "timeout".to_owned()),
                (100, "exit-code".to_owned()),
                (200, "exit-code".to_owned())
            ]
        );
        assert_eq!(sim.pushes.len(), 4);
    }

    #[test]
    fn scenario_same_crash_after_an_hour_and_while_a_service_keeps_failing() {
        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(1, &[], Some("oom-kill"));
        sim.run(HOUR - 1, &[], None);
        assert!(sim.backup_record().get("crashes").is_some());
        sim.run(1, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("oom-kill"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "oom-kill".to_owned()),
                (HOUR + 1, "oom-kill".to_owned())
            ]
        );

        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(6, &["svc-a"], None);
        sim.run(1, &[], Some("timeout"));
        sim.run(HOUR, &["svc-a"], None);
        assert_eq!(
            sim.backup_record()["crashes"]["crash:timeout"]["running"],
            json!(["svc-a"])
        );
        sim.run(1, &[], Some("timeout"));
        sim.run(DAY - HOUR - 8, &["svc-a"], None);
        assert_eq!(
            sim.pushed_services(),
            BTreeMap::from([("svc-a".to_owned(), vec![0])])
        );
        assert_eq!(sim.crash_pushes(), vec![(6, "timeout".to_owned())]);
        assert_eq!(sim.pushes.len(), 2);

        let mut sim = Simulation::new(&[("svc-a", 5, 0), ("svc-b", 60, 0)], "24h");
        sim.run(6, &["svc-a"], None);
        sim.run(1, &[], Some("exit-code"));
        sim.run(HOUR, &["svc-a"], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("exit-code"));
        sim.run(DAY - HOUR - 8, &["svc-a"], None);
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (6, "exit-code".to_owned()),
                (HOUR + 7, "exit-code".to_owned())
            ]
        );
        assert_eq!(sim.pushes.len(), 3);
    }

    #[test]
    fn scenario_daily_service_crash_between_nothing_due_rounds() {
        let mut sim = Simulation::new(&[("svc-d", 24 * 60, 0)], "24h");
        sim.run(1, &[], None);
        sim.run(1, &[], Some("exit-code"));
        sim.run(HOUR - 1, &[], None);
        assert!(sim.backup_record().get("crashes").is_some());
        sim.run(1, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("exit-code"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (1, "exit-code".to_owned()),
                (HOUR + 2, "exit-code".to_owned())
            ]
        );
        assert_eq!(sim.pushes.len(), 2);

        let mut sim = Simulation::new(&[("svc-d", 24 * 60, 0)], "24h");
        sim.run(1, &[], None);
        sim.run_intermittent(DAY - 1, HOUR, "exit-code");
        assert_eq!(sim.crash_pushes(), vec![(1, "exit-code".to_owned())]);
        assert_eq!(sim.pushes.len(), 1);
    }

    #[test]
    fn scenario_oom_during_backup_pins_the_running_service() {
        let mut sim = Simulation::new(&[("svc-d", 24 * 60, 0)], "24h");
        sim.run(DAY, &[], Some("oom-kill"));
        assert_eq!(sim.crash_pushes(), vec![(0, "oom-kill".to_owned())]);
        assert_eq!(
            sim.backup_record()["crashes"]["crash:oom-kill"]["running"],
            json!(["svc-d"])
        );
        sim.run(2 * DAY, &[], Some("oom-kill"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "oom-kill".to_owned()),
                (DAY, "oom-kill".to_owned()),
                (2 * DAY, "oom-kill".to_owned())
            ]
        );
        assert_eq!(sim.pushes.len(), 3);

        for hours in [2, 6] {
            let mut sim = Simulation::new(&[("svc-x", hours * 60, 0)], "24h");
            sim.run(DAY, &[], Some("oom-kill"));
            assert_eq!(sim.crash_pushes(), vec![(0, "oom-kill".to_owned())]);
            assert_eq!(sim.pushes.len(), 1);
        }

        let mut sim = Simulation::new(&[("svc-x", 6 * 60, 0)], "24h");
        sim.run(1, &[], Some("oom-kill"));
        sim.run(6 * HOUR, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(6 * HOUR - 1, &[], None);
        sim.run(1, &[], Some("oom-kill"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "oom-kill".to_owned()),
                (12 * HOUR, "oom-kill".to_owned())
            ]
        );

        let mut sim = Simulation::new(&[("svc-x", 6 * 60 + 5, 0)], "6h");
        sim.run(1, &[], Some("oom-kill"));
        sim.run(6 * HOUR - 1, &[], None);
        assert!(sim.backup_record().get("crashes").is_some());
        sim.run(1, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
        sim.run(1, &[], Some("oom-kill"));
        assert_eq!(
            sim.crash_pushes(),
            vec![
                (0, "oom-kill".to_owned()),
                (6 * HOUR + 1, "oom-kill".to_owned())
            ]
        );

        let mut sim = Simulation::new(&[("svc-x", 24 * 60, 0), ("svc-a", 5, 0)], "24h");
        sim.run(1, &[], Some("oom-kill"));
        sim.schedules.retain(|schedule| schedule.name != "svc-x");
        sim.run(1, &[], None);
        assert!(
            sim.backup_record()["crashes"]["crash:oom-kill"]
                .get("running")
                .is_none()
        );
        sim.run(HOUR - 1, &[], None);
        assert!(sim.backup_record().get("crashes").is_none());
    }

    #[test]
    fn journal_failure_is_included_and_first_line_limit_is_characters() {
        let tmp = Tmp::new();
        let config = config_with(tmp.path().join("state").to_str().unwrap(), None);
        let mut hook = hooks("");
        hook.journal = Err("boom".to_owned());
        notify_at(&config, CHECK_UNIT, sample_now(), &mut hook);
        assert!(hook.pushes[0].1.contains("(journal unavailable: boom)"));
        assert_eq!(first_line(""), "-");
    }
}
