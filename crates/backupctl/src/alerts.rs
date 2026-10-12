//! Failure notifications for systemd OnFailure units.
//! The alert ledger is `<state_root>/_alerts.json` and is never the global lock.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::time::{Duration, Instant};

use chrono::{DateTime, FixedOffset, Local};
use homelab_backup_domain::duration::parse_duration;
use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{FlockOperation, Mode, OFlags, flock, fstat, openat};
use serde_json::{Map, Value};

use crate::documents::load_global_config;
use crate::files::CommandError;
use crate::unix::atomic::atomic_write_json;
use crate::unix::control::{ensure_control_directory, is_reg, mode_of, validate_control_file};
use crate::unix::error::SecurityError;

pub const BACKUP_UNIT: &str = "homelab-backup.service";
pub const STATUS_UNIT: &str = "homelab-backup-status.service";
const ALERT_STATE: &str = "_alerts.json";
const ALERT_LOCK: &str = "_alerts.lock";
const LOCK_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_RETRY: Duration = Duration::from_millis(100);
const STATUS_HANDLED: Duration = Duration::from_secs(20 * 60);
const RUN_DUE_HANDOFF: Duration = Duration::from_secs(20 * 60);
const CRASH_QUIET: Duration = Duration::from_secs(60 * 60);
const ERROR_LINE_LIMIT: usize = 200;
const NOTIFY_BODY_LIMIT: usize = 3900;

#[derive(Debug, Clone, Default)]
pub struct MonitorEnv {
    pub invocation_id: Option<String>,
    pub monitor_invocation_id: Option<String>,
    pub service_result: Option<String>,
    pub exit_code: Option<String>,
    pub exit_status: Option<String>,
}

pub struct AlertIo<'a> {
    pub journal: &'a mut dyn FnMut(&str) -> String,
    pub deliver: &'a mut dyn FnMut(&str, &str, bool) -> bool,
}

pub fn run_notify_failure(config_path: &str, unit: &str) -> Result<(), CommandError> {
    eprintln!("ALERT: systemd unit failed: {unit}");
    let config =
        load_global_config(config_path).map_err(|error| CommandError::Text(error.message))?;
    let now = Local::now().fixed_offset();
    let env = MonitorEnv::from_process();
    let mut journal = |unit: &str| read_journal(unit);
    let mut deliver = |title: &str, body: &str, ok: bool| deliver_http(&config, title, body, ok);
    let mut io = AlertIo {
        journal: &mut journal,
        deliver: &mut deliver,
    };
    notify_unit(&config, unit, now, &env, &mut io).map_err(CommandError::Text)
}

impl MonitorEnv {
    fn from_process() -> Self {
        Self {
            invocation_id: std::env::var("INVOCATION_ID").ok(),
            monitor_invocation_id: std::env::var("MONITOR_INVOCATION_ID").ok(),
            service_result: std::env::var("MONITOR_SERVICE_RESULT").ok(),
            exit_code: std::env::var("MONITOR_EXIT_CODE").ok(),
            exit_status: std::env::var("MONITOR_EXIT_STATUS").ok(),
        }
    }
}

pub fn notify_unit(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    now: DateTime<FixedOffset>,
    env: &MonitorEnv,
    io: &mut AlertIo<'_>,
) -> Result<(), String> {
    if unit == BACKUP_UNIT {
        notify_run_due(config, now, env, io)?;
        return Ok(());
    }
    let state = load_alert_state(config);
    let record = unit_record(&state, unit);
    let mut delivered = false;
    if unit == STATUS_UNIT && status_handled(&state, record, now, env) {
        eprintln!("status already handled this problem; not pushing it again");
    } else if notified_within(&Value::Object(record.clone()), config, now) {
        eprintln!(
            "{} failure already notified at {}; not pushing again before alert.renotify_after",
            unit,
            record
                .get("notified_at")
                .and_then(Value::as_str)
                .unwrap_or("")
        );
    } else {
        delivered = push_unit(config, unit, &format!("{unit} failed"), "", now, env, io);
    }
    let stamp = iso(now);
    update_unit(config, unit, |current| {
        current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
        if delivered {
            current.insert("notified_at".to_owned(), Value::String(stamp.clone()));
        }
    });
    Ok(())
}

pub fn record_run_due(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    attempted: &[String],
    failures: &[(String, String)],
    known_services: Option<&BTreeSet<String>>,
) {
    let mut failed = BTreeMap::new();
    for (operation, error) in failures {
        failed
            .entry(operation.clone())
            .or_insert_with(|| first_line(error));
    }
    let succeeded = attempted
        .iter()
        .filter(|name| !failed.contains_key(*name))
        .cloned()
        .collect::<BTreeSet<_>>();
    let state = load_alert_state(config);
    if failed.is_empty() && unit_record(&state, BACKUP_UNIT).is_empty() {
        return;
    }
    let idle = attempted.is_empty() && failed.is_empty();
    let env = MonitorEnv::from_process();
    update_unit(config, BACKUP_UNIT, |record| {
        let mut crashes = record_dict(record, "crashes");
        let keys = crashes.keys().cloned().collect::<Vec<_>>();
        for key in keys {
            let entry = crashes.get(&key).cloned().unwrap_or(Value::Null);
            match crash_after_round(config, &entry, now, &succeeded, known_services) {
                Some(entry) => {
                    crashes.insert(key, entry);
                }
                None => {
                    crashes.remove(&key);
                }
            }
        }
        if crashes.is_empty() {
            record.remove("crashes");
        } else {
            record.insert("crashes".to_owned(), Value::Object(crashes));
        }
        if idle {
            return;
        }
        for legacy in ["failure_signature", "notified_signature", "notified_at"] {
            record.remove(legacy);
        }
        let mut services = record_dict(record, "services");
        let names = services.keys().cloned().collect::<Vec<_>>();
        for name in names {
            let succeeded_now = succeeded.contains(&name);
            let removed = known_services.is_some_and(|known| !known.contains(&name))
                && !failed.contains_key(&name);
            if succeeded_now || removed {
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
            let mut body = Map::new();
            if let Some(id) = &env.invocation_id {
                body.insert("invocation_id".to_owned(), Value::String(id.clone()));
            } else {
                body.insert("invocation_id".to_owned(), Value::Null);
            }
            body.insert("completed_at".to_owned(), Value::String(iso(now)));
            body.insert(
                "failures".to_owned(),
                Value::Object(
                    failed
                        .iter()
                        .map(|(key, value)| (key.clone(), Value::String(value.clone())))
                        .collect(),
                ),
            );
            record.insert("run".to_owned(), Value::Object(body));
        }
    });
}

pub fn clear_unit_alert(config: &BTreeMap<String, Yaml>, unit: &str) {
    let state = load_alert_state(config);
    if unit_record(&state, unit).is_empty() {
        return;
    }
    let Some(root) = state_root(config) else {
        return;
    };
    let _lock = acquire_lock(&root);
    let mut state = load_alert_state(config);
    if let Some(units) = state.get_mut("units").and_then(Value::as_object_mut) {
        units.remove(unit);
        save_alert_state(config, &state);
    }
}

fn notify_run_due(
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
    env: &MonitorEnv,
    io: &mut AlertIo<'_>,
) -> Result<(), String> {
    let state = load_alert_state(config);
    let record = unit_record(&state, BACKUP_UNIT);
    if let Some(run_record) = trusted_run(record, now, env) {
        let failed = run_record
            .get("failures")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let services = record_dict(record, "services");
        let pending = failed
            .keys()
            .filter(|name| {
                !notified_within(services.get(*name).unwrap_or(&Value::Null), config, now)
            })
            .cloned()
            .collect::<BTreeSet<_>>();
        let known = failed
            .keys()
            .filter(|name| !pending.contains(*name))
            .cloned()
            .collect::<Vec<_>>();
        let mut delivered = false;
        if !pending.is_empty() {
            let mut lines = vec![format!(
                "failed: {}",
                pending.iter().cloned().collect::<Vec<_>>().join(", ")
            )];
            if !known.is_empty() {
                lines.push(format!(
                    "still failing (already notified): {}",
                    known.join(", ")
                ));
            }
            for name in &pending {
                let error = failed.get(name).and_then(Value::as_str).unwrap_or("");
                lines.push(format!("- {name}: {error}"));
            }
            delivered = push_unit(
                config,
                BACKUP_UNIT,
                &format!("{BACKUP_UNIT} failed"),
                &lines.join("\n"),
                now,
                env,
                io,
            );
        } else {
            eprintln!(
                "failed services already notified ({}); not pushing again before alert.renotify_after",
                known.join(", ")
            );
        }
        let stamp = iso(now);
        let pending = pending.clone();
        update_unit(config, BACKUP_UNIT, move |current| {
            current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
            if delivered {
                let mut stored = record_dict(current, "services");
                for name in &pending {
                    let mut entry = Map::new();
                    entry.insert("notified_at".to_owned(), Value::String(stamp.clone()));
                    stored.insert(name.clone(), Value::Object(entry));
                }
                current.insert("services".to_owned(), Value::Object(stored));
            }
        });
        return Ok(());
    }
    let (result, details) = monitor_result(env);
    let key = format!("crash:{result}");
    let crashes = record_dict(record, "crashes");
    let mut delivered = false;
    if notified_within(crashes.get(&key).unwrap_or(&Value::Null), config, now) {
        let when = crashes
            .get(&key)
            .and_then(Value::as_object)
            .and_then(|entry| entry.get("notified_at"))
            .and_then(Value::as_str)
            .unwrap_or("");
        eprintln!(
            "{BACKUP_UNIT} {result} failure already notified at {when}; not pushing again before alert.renotify_after"
        );
    } else {
        delivered = push_unit(
            config,
            BACKUP_UNIT,
            &format!("{BACKUP_UNIT} failed ({result})"),
            &format!(
                "run-due ended without recording which services failed ({details}); it crashed, timed out, was killed or could not start. Check the journal."
            ),
            now,
            env,
            io,
        );
    }
    let running = running_services(config);
    let stamp = iso(now);
    update_unit(config, BACKUP_UNIT, |current| {
        current.insert("last_failure_at".to_owned(), Value::String(stamp.clone()));
        let mut stored = record_dict(current, "crashes");
        let mut entry = record_dict_value(stored.get(&key));
        entry.insert("last_seen_at".to_owned(), Value::String(stamp.clone()));
        entry.insert("details".to_owned(), Value::String(details.clone()));
        let mut merged = running.iter().cloned().collect::<BTreeSet<_>>();
        if let Some(previous) = entry.get("running").and_then(Value::as_array) {
            merged.extend(previous.iter().filter_map(Value::as_str).map(str::to_owned));
        }
        if !merged.is_empty() {
            entry.insert(
                "running".to_owned(),
                Value::Array(merged.into_iter().map(Value::String).collect()),
            );
        }
        if delivered {
            entry.insert("notified_at".to_owned(), Value::String(stamp.clone()));
        }
        stored.insert(key, Value::Object(entry));
        current.insert("crashes".to_owned(), Value::Object(stored));
    });
    Ok(())
}

fn push_unit(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    title: &str,
    summary: &str,
    _now: DateTime<FixedOffset>,
    env: &MonitorEnv,
    io: &mut AlertIo<'_>,
) -> bool {
    let journal = (io.journal)(unit);
    let (result, details) = monitor_result(env);
    let mut title = title.to_owned();
    if unit != BACKUP_UNIT && env.service_result.is_some() && result != "exit-code" {
        title = format!("{title} ({result})");
    }
    let summary = if summary.is_empty() && env.service_result.is_some() {
        details
    } else {
        summary.to_owned()
    };
    let host = config.get("host_id").and_then(Yaml::as_str).unwrap_or("?");
    let title = format!("homelab-backup on {host}: {title}");
    let body = notification_body(&title, &summary, &journal);
    let body = if body.is_empty() {
        "see journalctl for details".to_owned()
    } else {
        body
    };
    let delivered = (io.deliver)(&title, &body, false);
    if !delivered {
        eprintln!(
            "WARNING: failure notification was not delivered (alert.notify_url_file not configured or unreachable)"
        );
    }
    delivered
}

fn notification_body(title: &str, summary: &str, journal: &str) -> String {
    let mut budget = NOTIFY_BODY_LIMIT.saturating_sub(utf8_len(title) + 2);
    let summary = if summary.is_empty() {
        String::new()
    } else {
        utf8_head(summary, budget)
    };
    budget = budget.saturating_sub(utf8_len(&summary) + if summary.is_empty() { 0 } else { 2 });
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

fn running_services(config: &BTreeMap<String, Yaml>) -> Vec<String> {
    let Ok((manifests, _)) = crate::catalog::select_every_manifest(config) else {
        return Vec::new();
    };
    let root = state_root(config).unwrap_or_default();
    let mut running = Vec::new();
    for manifest in manifests {
        let Some(service) = manifest.manifest.get("service").and_then(Yaml::as_str) else {
            continue;
        };
        let Ok(state) = crate::state::load_backup_state(&root, service) else {
            continue;
        };
        if state.get("last_result").and_then(Value::as_str) == Some("running") {
            running.push(service.to_owned());
        }
    }
    running.sort();
    running
}

fn trusted_run<'a>(
    record: &'a Map<String, Value>,
    now: DateTime<FixedOffset>,
    env: &MonitorEnv,
) -> Option<&'a Map<String, Value>> {
    let run_record = record.get("run")?.as_object()?;
    let failures = run_record.get("failures")?.as_object()?;
    if failures.is_empty() {
        return None;
    }
    if let Some(failed_id) = &env.monitor_invocation_id {
        return (run_record.get("invocation_id").and_then(Value::as_str)
            == Some(failed_id.as_str()))
        .then_some(run_record);
    }
    let completed = parse_timestamp(run_record.get("completed_at"))?;
    if completed > now || now.signed_duration_since(completed).to_std().ok()? > RUN_DUE_HANDOFF {
        return None;
    }
    if let Some(last_failure) = parse_timestamp(record.get("last_failure_at"))
        && last_failure >= completed
    {
        return None;
    }
    Some(run_record)
}

fn status_handled(
    state: &Map<String, Value>,
    record: &Map<String, Value>,
    now: DateTime<FixedOffset>,
    env: &MonitorEnv,
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
    if let Some(failed_id) = &env.monitor_invocation_id {
        return status.get("invocation_id").and_then(Value::as_str) == Some(failed_id.as_str());
    }
    if now
        .signed_duration_since(handled_at)
        .to_std()
        .ok()
        .unwrap_or(STATUS_HANDLED)
        > STATUS_HANDLED
    {
        return false;
    }
    match parse_timestamp(record.get("last_failure_at")) {
        None => true,
        Some(last_failure) => handled_at > last_failure,
    }
}

fn crash_after_round(
    config: &BTreeMap<String, Yaml>,
    entry: &Value,
    now: DateTime<FixedOffset>,
    succeeded: &BTreeSet<String>,
    known_services: Option<&BTreeSet<String>>,
) -> Option<Value> {
    let mut entry_map = entry.as_object().cloned().unwrap_or_default();
    if let Some(running) = entry.get("running").and_then(Value::as_array)
        && !running.is_empty()
    {
        let left = running
            .iter()
            .filter_map(Value::as_str)
            .filter(|name| !succeeded.contains(*name))
            .filter(|name| known_services.is_none_or(|known| known.contains(*name)))
            .map(str::to_owned)
            .collect::<Vec<_>>();
        let period = renotify_after(config);
        if crash_quiet(&entry_map, now, period) {
            return None;
        }
        if !left.is_empty() {
            entry_map.insert(
                "running".to_owned(),
                Value::Array(left.into_iter().map(Value::String).collect()),
            );
            return Some(Value::Object(entry_map));
        }
        entry_map.remove("running");
    }
    if crash_quiet(&entry_map, now, CRASH_QUIET) {
        None
    } else {
        Some(Value::Object(entry_map))
    }
}

fn crash_quiet(entry: &Map<String, Value>, now: DateTime<FixedOffset>, period: Duration) -> bool {
    let Some(last_seen) = parse_timestamp(
        entry
            .get("last_seen_at")
            .or_else(|| entry.get("notified_at")),
    ) else {
        return true;
    };
    if last_seen > now {
        return false;
    }
    now.signed_duration_since(last_seen)
        .to_std()
        .ok()
        .is_some_and(|elapsed| elapsed >= period)
}

fn notified_within(
    entry: &Value,
    config: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
) -> bool {
    let Some(entry) = entry.as_object() else {
        return false;
    };
    let Some(notified_at) = parse_timestamp(entry.get("notified_at")) else {
        return false;
    };
    if notified_at > now {
        return false;
    }
    now.signed_duration_since(notified_at)
        .to_std()
        .ok()
        .is_some_and(|elapsed| elapsed < renotify_after(config))
}

fn renotify_after(config: &BTreeMap<String, Yaml>) -> Duration {
    let value = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("renotify_after"))
        .and_then(Yaml::as_str)
        .unwrap_or("24h");
    parse_duration(value, "alert.renotify_after")
        .map(|duration| Duration::from_secs(duration.as_secs()))
        .unwrap_or(Duration::from_secs(24 * 60 * 60))
}

fn monitor_result(env: &MonitorEnv) -> (String, String) {
    let result = env
        .service_result
        .clone()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".to_owned());
    let mut details = vec![format!("result={result}")];
    if let Some(value) = env.exit_code.as_ref().filter(|value| !value.is_empty()) {
        details.push(format!("exit_code={value}"));
    }
    if let Some(value) = env.exit_status.as_ref().filter(|value| !value.is_empty()) {
        details.push(format!("exit_status={value}"));
    }
    (result, details.join(", "))
}

fn first_line(error: &str) -> String {
    let line = error
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("-");
    if line.chars().count() <= ERROR_LINE_LIMIT {
        line.to_owned()
    } else {
        let mut trimmed = line.chars().take(ERROR_LINE_LIMIT - 1).collect::<String>();
        trimmed.push('…');
        trimmed
    }
}

fn update_unit(
    config: &BTreeMap<String, Yaml>,
    unit: &str,
    update: impl FnOnce(&mut Map<String, Value>),
) {
    let Some(root) = state_root(config) else {
        return;
    };
    let _lock = acquire_lock(&root);
    let mut state = load_alert_state(config);
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
    if !units.is_empty() || state.contains_key("units") {
        state.insert("units".to_owned(), Value::Object(units));
    }
    save_alert_state(config, &state);
}

fn load_alert_state(config: &BTreeMap<String, Yaml>) -> Map<String, Value> {
    let Some(root) = state_root(config) else {
        return Map::new();
    };
    let path = format!("{root}/{ALERT_STATE}");
    let file = match File::open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Map::new(),
        Err(error) => {
            eprintln!("WARNING: could not open alert state {path}: {error}");
            return Map::new();
        }
    };
    let mut text = String::new();
    if file.take(1_048_576).read_to_string(&mut text).is_err() {
        eprintln!("WARNING: ignoring unreadable alert state {path}: read failed");
        return Map::new();
    }
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(map)) => map,
        _ => {
            eprintln!("WARNING: ignoring unreadable alert state {path}: not a JSON mapping");
            Map::new()
        }
    }
}

fn save_alert_state(config: &BTreeMap<String, Yaml>, state: &Map<String, Value>) {
    let Some(root) = state_root(config) else {
        return;
    };
    if let Err(error) = ensure_control_directory(&root, 0o700).and_then(|_| {
        atomic_write_json(
            Path::new(&format!("{root}/{ALERT_STATE}")),
            &Value::Object(state.clone()),
        )
    }) {
        eprintln!("WARNING: could not save alert state: {error}");
    }
}

fn acquire_lock(root: &str) -> Option<File> {
    let path = format!("{root}/{ALERT_LOCK}");
    let opened = (|| {
        ensure_control_directory(root, 0o700)?;
        let parent = crate::unix::control::open_directory(root)?;
        let fd = openat(
            &parent,
            ALERT_LOCK,
            OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_bits_truncate(0o600),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let metadata = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
        if !is_reg(mode_of(&metadata)) {
            return Err(SecurityError::invalid("not a regular file"));
        }
        rustix::fs::fchmod(&fd, Mode::from_bits_truncate(0o600))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let file = File::from(fd);
        let deadline = Instant::now() + LOCK_TIMEOUT;
        loop {
            match flock(&file, FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => return Ok(file),
                Err(rustix::io::Errno::AGAIN) => {
                    if Instant::now() >= deadline {
                        return Err(SecurityError::runtime(format!(
                            "still held after {}s",
                            LOCK_TIMEOUT.as_secs()
                        )));
                    }
                    std::thread::sleep(LOCK_RETRY);
                }
                Err(error) => return Err(SecurityError::runtime(error.to_string())),
            }
        }
    })();
    match opened {
        Ok(file) => Some(file),
        Err(error) => {
            eprintln!(
                "WARNING: could not lock alert state {path}: {error}; continuing without the lock"
            );
            None
        }
    }
}

fn state_root(config: &BTreeMap<String, Yaml>) -> Option<String> {
    config
        .get("state_root")
        .and_then(Yaml::as_str)
        .map(str::to_owned)
}

fn unit_record<'a>(state: &'a Map<String, Value>, unit: &str) -> &'a Map<String, Value> {
    state
        .get("units")
        .and_then(Value::as_object)
        .and_then(|units| units.get(unit))
        .and_then(Value::as_object)
        .unwrap_or(&EMPTY_MAP)
}

fn record_dict(record: &Map<String, Value>, key: &str) -> Map<String, Value> {
    record
        .get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn record_dict_value(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

static EMPTY_MAP: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);

fn parse_timestamp(value: Option<&Value>) -> Option<DateTime<FixedOffset>> {
    let text = value?.as_str()?;
    DateTime::parse_from_rfc3339(text).ok()
}

fn iso(value: DateTime<FixedOffset>) -> String {
    value.to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

fn utf8_len(text: &str) -> usize {
    text.len()
}

fn utf8_head(text: &str, limit: usize) -> String {
    let data = text.as_bytes();
    if data.len() <= limit {
        return text.to_owned();
    }
    let marker = "…".as_bytes();
    if limit < marker.len() {
        return String::new();
    }
    let mut end = limit - marker.len();
    while end > 0 && (data[end] & 0b1100_0000) == 0b1000_0000 {
        end -= 1;
    }
    let mut owned = String::from_utf8_lossy(&data[..end]).into_owned();
    owned.push('…');
    owned
}

fn utf8_tail(text: &str, limit: usize) -> String {
    let data = text.as_bytes();
    if data.len() <= limit {
        return text.to_owned();
    }
    let marker = "…".as_bytes();
    if limit < marker.len() {
        return String::new();
    }
    let mut start = data.len() - (limit - marker.len());
    while start < data.len() && (data[start] & 0b1100_0000) == 0b1000_0000 {
        start += 1;
    }
    let mut owned = String::from("…");
    owned.push_str(&String::from_utf8_lossy(&data[start..]));
    owned
}

fn read_journal(unit: &str) -> String {
    match std::process::Command::new("journalctl")
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
    {
        Ok(output) => String::from_utf8_lossy(&output.stdout).into_owned(),
        Err(error) => format!("(journal unavailable: {error})"),
    }
}

fn deliver_http(config: &BTreeMap<String, Yaml>, title: &str, message: &str, ok: bool) -> bool {
    let Some(url) = notification_url(config) else {
        return false;
    };
    let kind = if url.contains("hc-ping.com") || url.contains("/ping/") {
        "healthchecks"
    } else {
        "ntfy"
    };
    let body = format!("{title}\n\n{message}");
    let bytes = truncate_utf8(body.as_bytes(), 10_000);
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
    if result.is_err() {
        eprintln!("WARNING: notification delivery failed: HTTPError");
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
        std::fs::read_to_string(&checked).map_err(|error| SecurityError::runtime(error.to_string()))
    })();
    let text = match text {
        Ok(text) => text,
        Err(error) if error.is_missing_path() => {
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
    if !url.starts_with("https://") {
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

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    fn config(root: &std::path::Path) -> BTreeMap<String, Yaml> {
        let mut config = BTreeMap::new();
        config.insert("host_id".to_owned(), Yaml::string("host"));
        config.insert(
            "state_root".to_owned(),
            Yaml::string(root.join("state").display().to_string()),
        );
        config.insert(
            "services_root".to_owned(),
            Yaml::string(root.join("services").display().to_string()),
        );
        std::fs::create_dir_all(root.join("state")).unwrap();
        std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::set_permissions(root.join("state"), std::fs::Permissions::from_mode(0o700))
            .unwrap();
        config
    }

    fn now() -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339("2026-10-12T12:00:00+00:00").unwrap()
    }

    #[test]
    fn a_repeated_unit_failure_waits_for_renotify_after() {
        let root = std::env::temp_dir().join(format!("homelab-alert-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let config = config(&root);
        let env = MonitorEnv::default();
        let mut pushes = 0u32;
        let mut journal = |_unit: &str| "journal".to_owned();
        let mut deliver = |_title: &str, _body: &str, _ok: bool| {
            pushes += 1;
            true
        };
        notify_unit(
            &config,
            "homelab-backup-check.service",
            now(),
            &env,
            &mut AlertIo {
                journal: &mut journal,
                deliver: &mut deliver,
            },
        )
        .unwrap();
        notify_unit(
            &config,
            "homelab-backup-check.service",
            now(),
            &env,
            &mut AlertIo {
                journal: &mut journal,
                deliver: &mut deliver,
            },
        )
        .unwrap();
        assert_eq!(pushes, 1);
        let _ = std::fs::remove_dir_all(&root);
    }
}
