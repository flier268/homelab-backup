//! Backup and restore actions. Inline scripts are never written to the log.

use std::collections::BTreeMap;
use std::os::unix::fs::MetadataExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use homelab_backup_domain::yaml::Yaml;

use crate::unix::account::{account, drop_privileges, fixed_environment};
use crate::unix::control::{is_dir, is_lnk, is_reg};
use crate::unix::error::SecurityError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionFailure {
    pub phase: String,
    pub name: String,
    pub result: String,
}

pub fn legacy_messages(manifest: &BTreeMap<String, Yaml>) -> Vec<String> {
    let mut messages = Vec::new();
    let actions = manifest.get("actions").and_then(Yaml::as_map);
    if actions.is_some_and(|actions| !actions.is_empty() && style(actions) == "legacy") {
        messages.push("legacy flat actions are deprecated; migrate to actions.backup".to_owned());
    }
    let consistency = manifest.get("consistency").and_then(Yaml::as_map);
    if consistency.is_some_and(|map| map.contains_key("before") || map.contains_key("after")) {
        messages.push(
            "consistency.before/after are deprecated; migrate to actions.backup.before/finally scripts"
                .to_owned(),
        );
    }
    messages
}

pub fn failure_environment(service: &str, phase: &str, error: &str) -> Vec<(String, String)> {
    let reason = error
        .replace('\0', "")
        .lines()
        .collect::<Vec<_>>()
        .join(" ");
    let reason = reason.chars().take(4096).collect::<String>();
    vec![
        ("BACKUPCTL_FAILURE_PHASE".to_owned(), phase.to_owned()),
        (
            "BACKUPCTL_FAILURE_TYPE".to_owned(),
            "RuntimeError".to_owned(),
        ),
        ("BACKUPCTL_FAILURE_REASON".to_owned(), reason),
        ("BACKUPCTL_FAILURE_SERVICE".to_owned(), service.to_owned()),
        ("BACKUPCTL_FAILURE_SECONDARY".to_owned(), String::new()),
    ]
}

pub fn action_entries<'a>(
    manifest: &'a BTreeMap<String, Yaml>,
    operation: &str,
    phase: &str,
) -> Vec<&'a BTreeMap<String, Yaml>> {
    let Some(actions) = manifest.get("actions").and_then(Yaml::as_map) else {
        return Vec::new();
    };
    let entries = if style(actions) == "nested" {
        actions
            .get(operation)
            .and_then(Yaml::as_map)
            .and_then(|section| section.get(phase))
    } else if operation == "backup"
        && matches!(phase, "before" | "finally" | "on_success" | "on_failure")
    {
        actions.get(phase)
    } else {
        None
    };
    entries
        .and_then(Yaml::as_seq)
        .map(|items| items.iter().filter_map(Yaml::as_map).collect())
        .unwrap_or_default()
}

pub fn run_phase(
    manifest: &BTreeMap<String, Yaml>,
    operation: &str,
    phase: &str,
    extra_env: &[(&str, &str)],
    containers: &mut dyn FnMut(&str) -> Option<bool>,
) -> Result<Vec<ActionFailure>, SecurityError> {
    let mut failures = Vec::new();
    for action in action_entries(manifest, operation, phase) {
        let name = action.get("name").and_then(Yaml::as_str).unwrap_or("");
        let stopped = stopped_containers(action, containers);
        if !stopped.is_empty() {
            eprintln!(
                "WARNING: skipping {phase} action {name:?} for {}: container not running: {}",
                manifest.get("service").and_then(Yaml::as_str).unwrap_or(""),
                stopped.join(", ")
            );
            failures.push(ActionFailure {
                phase: phase.to_owned(),
                name: name.to_owned(),
                result: "failed".to_owned(),
            });
            continue;
        }
        if let Err(error) = run_action(action, manifest, operation, phase, extra_env) {
            let required = action
                .get("required")
                .and_then(Yaml::as_bool)
                .unwrap_or(true);
            if required {
                return Err(error);
            }
            let result = if error.message.contains("timed out") {
                "timeout"
            } else {
                "failed"
            };
            eprintln!("WARNING: optional {phase} action {name:?} {result}: {error}");
            failures.push(ActionFailure {
                phase: phase.to_owned(),
                name: name.to_owned(),
                result: result.to_owned(),
            });
        }
    }
    Ok(failures)
}

pub fn action_executable(command: &[String], run_as: &str) -> Option<PathBuf> {
    let executable = command.first()?;
    let record = account(run_as).ok()?;
    let candidate = if executable.contains('/') {
        let path = PathBuf::from(executable);
        if !path.is_absolute() {
            return None;
        }
        path
    } else {
        let env = fixed_environment(run_as).ok()?;
        let path = env.iter().find(|(key, _)| key == "PATH")?.1.as_str();
        which(executable, path)?
    };
    if record.uid == 0 {
        let candidate = if candidate == Path::new("/bin/bash") {
            std::fs::canonicalize(&candidate).ok()?
        } else {
            candidate
        };
        return validate_root_executable(&candidate).map(PathBuf::from);
    }
    let meta = std::fs::symlink_metadata(&candidate).ok()?;
    if is_lnk(meta.mode()) || !is_reg(meta.mode()) {
        return None;
    }
    Some(candidate)
}

pub fn validate_root_executable(path: &Path) -> Option<String> {
    let mut current = path.to_path_buf();
    loop {
        let metadata = std::fs::symlink_metadata(&current).ok()?;
        let mode = metadata.mode();
        let directory = is_dir(mode);
        let file = is_reg(mode);
        if is_lnk(mode) || !(directory || file) || metadata.uid() != 0 || mode & 0o022 != 0 {
            return None;
        }
        if current == Path::new("/") {
            break;
        }
        current = current.parent()?.to_path_buf();
    }
    Some(path.display().to_string())
}

fn run_action(
    action: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    operation: &str,
    phase: &str,
    extra_env: &[(&str, &str)],
) -> Result<(), SecurityError> {
    let name = action.get("name").and_then(Yaml::as_str).unwrap_or("");
    let script = action.get("script").and_then(Yaml::as_str);
    let (mut command, display) = if let Some(script) = script {
        (
            vec![
                "/bin/bash".to_owned(),
                "-euo".to_owned(),
                "pipefail".to_owned(),
                "-c".to_owned(),
                script.to_owned(),
            ],
            vec![
                "/bin/bash".to_owned(),
                "-euo".to_owned(),
                "pipefail".to_owned(),
                "-c".to_owned(),
                "<inline script>".to_owned(),
            ],
        )
    } else {
        let command: Vec<String> = action
            .get("command")
            .and_then(Yaml::as_seq)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Yaml::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        (command.clone(), command)
    };
    let run_as = action.get("run_as").and_then(Yaml::as_str).unwrap_or("");
    let record = account(run_as)?;
    let executable = action_executable(&command, run_as).ok_or_else(|| {
        SecurityError::runtime(format!(
            "action {name:?} executable was not found: {}",
            command.first().map(String::as_str).unwrap_or("")
        ))
    })?;
    command[0] = executable.display().to_string();
    let timeout = action
        .get("timeout")
        .and_then(Yaml::as_int)
        .unwrap_or(30)
        .max(0) as u64;
    println!(
        "+ [{operation}.{phase} {name:?} as {}:{}] {}",
        record.uid,
        record.gid,
        display
            .iter()
            .map(|item| quote(item))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let mut process = Command::new(&command[0]);
    process
        .args(&command[1..])
        .current_dir(manifest.get("_dir").and_then(Yaml::as_str).unwrap_or("/"))
        .env_clear()
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    for (key, value) in fixed_environment(run_as)? {
        process.env(key, value);
    }
    process.env("BACKUPCTL_OPERATION", operation);
    process.env("BACKUPCTL_ACTION_PHASE", phase);
    process.env(
        "BACKUPCTL_SERVICE",
        manifest.get("service").and_then(Yaml::as_str).unwrap_or(""),
    );
    for (key, value) in extra_env {
        process.env(*key, *value);
    }
    if let Some((uid, gid)) = drop_privileges(run_as)? {
        process.uid(uid).gid(gid);
        // std sets uid/gid after pre_exec and does not clear supplementary groups.
        // Drop them here, while the child can still call setgroups.
        unsafe {
            process.pre_exec(|| {
                rustix::thread::set_thread_groups(&[]).map_err(|error| {
                    std::io::Error::from_raw_os_error(error.raw_os_error())
                })
            });
        }
    }
    let child = process
        .spawn()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let pid = child.id() as i32;
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = sender.send(child.wait_with_output());
    });
    let output = match receiver.recv_timeout(Duration::from_secs(timeout)) {
        Ok(Ok(output)) => output,
        Ok(Err(error)) => return Err(SecurityError::runtime(error.to_string())),
        Err(_) => {
            unsafe { kill(-pid, 15) };
            if let Ok(Ok(output)) = receiver.recv_timeout(Duration::from_secs(2)) {
                print_output(&output);
            } else {
                unsafe { kill(-pid, 9) };
                if let Ok(Ok(output)) = receiver.recv() {
                    print_output(&output);
                }
            }
            return Err(SecurityError::runtime(format!(
                "action {name:?} timed out after {timeout} seconds"
            )));
        }
    };
    print_output(&output);
    if !output.status.success() {
        return Err(SecurityError::runtime(format!(
            "command failed with exit code {}: {}",
            output.status.code().unwrap_or(1),
            display.join(" ")
        )));
    }
    Ok(())
}

fn print_output(output: &std::process::Output) {
    if !output.stdout.is_empty() {
        print!("{}", String::from_utf8_lossy(&output.stdout));
    }
    if !output.stderr.is_empty() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
    }
}

fn stopped_containers(
    action: &BTreeMap<String, Yaml>,
    containers: &mut dyn FnMut(&str) -> Option<bool>,
) -> Vec<String> {
    let names = match action.get("skip_if_not_running") {
        Some(Yaml::String(name)) => vec![name.clone()],
        Some(Yaml::Seq(items)) => items
            .iter()
            .filter_map(Yaml::as_str)
            .map(str::to_owned)
            .collect(),
        _ => return Vec::new(),
    };
    let mut stopped = Vec::new();
    let action_name = action.get("name").and_then(Yaml::as_str).unwrap_or("");
    for name in names {
        match containers(&name) {
            None => eprintln!(
                "WARNING: could not determine whether container {name:?} is running; running action {action_name:?} anyway"
            ),
            Some(false) => stopped.push(name),
            Some(true) => {}
        }
    }
    stopped
}

fn style(actions: &BTreeMap<String, Yaml>) -> &'static str {
    if actions.contains_key("backup") || actions.contains_key("restore") {
        "nested"
    } else {
        "legacy"
    }
}

fn which(name: &str, path: &str) -> Option<PathBuf> {
    for directory in path.split(':') {
        let candidate = Path::new(directory).join(name);
        let Ok(metadata) = std::fs::symlink_metadata(&candidate) else {
            continue;
        };
        if is_reg(metadata.mode()) && metadata.mode() & 0o111 != 0 {
            return Some(candidate);
        }
    }
    None
}

fn quote(value: &str) -> String {
    if value.is_empty()
        || value.bytes().any(|byte| {
            !byte.is_ascii_alphanumeric() && !matches!(byte, b'-' | b'_' | b'.' | b'/' | b'=')
        })
    {
        format!("'{}'", value.replace('\'', "'\\''"))
    } else {
        value.to_owned()
    }
}

unsafe extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    fn action(name: &str, command: &[&str]) -> Yaml {
        Yaml::map([
            ("name", Yaml::string(name)),
            (
                "command",
                Yaml::seq(command.iter().copied().map(Yaml::string)),
            ),
            (
                "run_as",
                Yaml::string(format!(
                    "{}:{}",
                    rustix::process::geteuid().as_raw(),
                    rustix::process::getegid().as_raw()
                )),
            ),
        ])
    }

    #[test]
    fn legacy_hooks_are_deprecated_and_failure_text_is_sanitized() {
        let manifest = BTreeMap::from([
            ("service".to_owned(), Yaml::string("demo")),
            (
                "consistency".to_owned(),
                Yaml::map([("before", Yaml::seq([])), ("after", Yaml::seq([]))]),
            ),
        ]);
        let messages = legacy_messages(&manifest);
        assert_eq!(messages.len(), 1);
        assert!(messages[0].contains("consistency.before/after"));
        let env = failure_environment("demo", "staging", "disk\0 full");
        assert_eq!(
            env.iter()
                .find(|(key, _)| key == "BACKUPCTL_FAILURE_REASON")
                .unwrap()
                .1,
            "disk full"
        );
        assert_eq!(
            env.iter()
                .find(|(key, _)| key == "BACKUPCTL_FAILURE_PHASE")
                .unwrap()
                .1,
            "staging"
        );
    }

    #[test]
    fn relative_and_writable_executables_are_rejected() {
        assert!(action_executable(&["./scripts/save".to_owned()], "0:0").is_none());
        let root = std::env::temp_dir().join(format!("homelab-action-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let tool = root.join("tool");
        std::fs::write(&tool, "#!/bin/sh\n").unwrap();
        std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o777)).unwrap();
        let link = root.join("link");
        std::os::unix::fs::symlink(&tool, &link).unwrap();
        assert!(validate_root_executable(&tool).is_none());
        assert!(validate_root_executable(&link).is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn inline_script_failure_does_not_report_the_script_body() {
        let manifest = BTreeMap::from([
            ("service".to_owned(), Yaml::string("demo")),
            ("_dir".to_owned(), Yaml::string("/tmp")),
            (
                "actions".to_owned(),
                Yaml::map([(
                    "before",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("dump")),
                        ("script", Yaml::string("token=secret-value; false")),
                        (
                            "run_as",
                            Yaml::string(format!(
                                "{}:{}",
                                rustix::process::geteuid().as_raw(),
                                rustix::process::getegid().as_raw()
                            )),
                        ),
                    ])]),
                )]),
            ),
        ]);
        let error = run_phase(&manifest, "backup", "before", &[], &mut |_| Some(true)).unwrap_err();
        assert!(!error.message.contains("secret-value"), "{}", error.message);
        assert!(
            error.message.contains("<inline script>"),
            "{}",
            error.message
        );
    }

    #[test]
    fn optional_failure_continues_and_timeout_stops_the_group() {
        let identity = format!(
            "{}:{}",
            rustix::process::geteuid().as_raw(),
            rustix::process::getegid().as_raw()
        );
        let truth = std::fs::canonicalize("/bin/true").unwrap();
        let failure = std::fs::canonicalize("/bin/false").unwrap();
        let sleep = std::fs::canonicalize("/bin/sleep").unwrap();
        let manifest = BTreeMap::from([
            ("service".to_owned(), Yaml::string("demo")),
            ("_dir".to_owned(), Yaml::string("/tmp")),
            (
                "actions".to_owned(),
                Yaml::map([(
                    "before",
                    Yaml::seq([
                        Yaml::map([
                            ("name", Yaml::string("optional")),
                            (
                                "command",
                                Yaml::seq([Yaml::string(failure.to_str().unwrap())]),
                            ),
                            ("required", Yaml::Bool(false)),
                            ("run_as", Yaml::string(&identity)),
                        ]),
                        Yaml::map([
                            ("name", Yaml::string("later")),
                            (
                                "command",
                                Yaml::seq([Yaml::string(truth.to_str().unwrap())]),
                            ),
                            ("run_as", Yaml::string(&identity)),
                        ]),
                    ]),
                )]),
            ),
        ]);
        let failures = run_phase(&manifest, "backup", "before", &[], &mut |_| Some(true)).unwrap();
        assert_eq!(failures[0].name, "optional");
        assert_eq!(failures[0].result, "failed");
        let slow = BTreeMap::from([
            ("service".to_owned(), Yaml::string("demo")),
            ("_dir".to_owned(), Yaml::string("/tmp")),
            (
                "actions".to_owned(),
                Yaml::map([(
                    "before",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("save")),
                        (
                            "command",
                            Yaml::seq([Yaml::string(sleep.to_str().unwrap()), Yaml::string("30")]),
                        ),
                        ("timeout", Yaml::Int(1)),
                        ("run_as", Yaml::string(&identity)),
                    ])]),
                )]),
            ),
        ]);
        let error = run_phase(&slow, "backup", "before", &[], &mut |_| Some(true)).unwrap_err();
        assert!(error.message.contains("timed out"), "{}", error.message);
    }
}
