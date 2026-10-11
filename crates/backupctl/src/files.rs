//! Files-mode backup: copy path sources, write the inventory, then snapshot.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use chrono::Local;
use homelab_backup_domain::manifest::{service_label, source_path, validate_manifest};
use homelab_backup_domain::yaml::Yaml;

use crate::actions::{failure_environment, legacy_messages, run_phase};
use crate::backup::{BackupCommand, BackupDeps, execute_backup};
use crate::catalog::{SelectionFailure, select_services};
use crate::documents::load_global_config;
use crate::fingerprint::stage_fingerprint;
use crate::state::{load_backup_state, save_backup_state};
use crate::storage::{PathInventory, SyncOptions, check_backup_space, prepare_stage, sync_paths};
use crate::unix::atomic::{atomic_copy_file, atomic_write_json};
use crate::unix::control::{clear_control_leaf, ensure_private_directory};
use crate::unix::error::SecurityError;
use crate::unix::lock::GlobalLock;
use crate::unix::restic::restic_env;

#[derive(Debug)]
pub enum CommandError {
    Text(String),
    Reported,
}

impl std::fmt::Display for CommandError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Text(message) => formatter.write_str(message),
            Self::Reported => Ok(()),
        }
    }
}

pub fn run_backup(
    config_path: &str,
    services: &[String],
    allow_low_space: bool,
    restic_program: &str,
) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let (ready, failures) = select_services(&config, services).map_err(text)?;
    let mut failures = failures;
    let flat = flat_strings(&config);
    let environment = if ready.is_empty() {
        BTreeMap::new()
    } else {
        restic_env(&flat).map_err(text)?
    };
    let lock_file = flat
        .get("lock_file")
        .cloned()
        .ok_or_else(|| CommandError::Text("missing lock_file".to_owned()))?;
    let mut lock = GlobalLock::new(lock_file, false, 0);
    if !lock.enter(&mut || {}).map_err(text)? {
        return Err(CommandError::Text(
            "another backupctl process is running".to_owned(),
        ));
    }
    if !ready.is_empty()
        && let Err(error) = run_restic(
            restic_program,
            &["unlock".to_owned()],
            None,
            &environment,
            false,
        )
    {
        eprintln!("WARNING: could not clear stale restic locks: RuntimeError: {error}");
    }
    for service in ready {
        let manifest = service.manifest;
        let label = service_label(&manifest);
        if let Err(error) = backup_loaded(
            &flat,
            &manifest,
            allow_low_space,
            restic_program,
            &environment,
        ) {
            failures.push(SelectionFailure {
                operation: label.clone(),
                summary: error.clone(),
                line: format!("ERROR: backup failed for {label}: {error}"),
            });
        }
    }
    if failures.is_empty() {
        return Ok(());
    }
    for failure in &failures {
        eprintln!("{}", failure.line);
    }
    eprintln!("\nBACKUP FAILURES");
    for failure in failures {
        eprintln!("  - {}: {}", failure.operation, failure.summary);
    }
    Err(CommandError::Reported)
}

fn backup_loaded(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    allow_low_space: bool,
    restic_program: &str,
    environment: &BTreeMap<String, String>,
) -> Result<(), String> {
    validate_manifest(&Yaml::Map(manifest.clone())).map_err(|error| error.message)?;
    let mode = manifest
        .get("consistency")
        .and_then(Yaml::as_map)
        .and_then(|consistency| consistency.get("mode"))
        .and_then(Yaml::as_str)
        .unwrap_or("stop");
    if mode != "files" {
        return Err(format!("consistency mode {mode} is not ported yet"));
    }
    for message in legacy_messages(manifest) {
        eprintln!("WARNING: {message}");
    }
    let service = manifest
        .get("service")
        .and_then(Yaml::as_str)
        .unwrap_or("")
        .to_owned();
    let state_root = config
        .get("state_root")
        .cloned()
        .ok_or_else(|| "missing state_root".to_owned())?;
    let initial = load_backup_state(&state_root, &service).map_err(|error| error.message)?;
    let mut initial = Some(initial);
    let manifest_for_stage = manifest.clone();
    let config_for_stage = config.clone();
    let mut stage = move || stage_files(&config_for_stage, &manifest_for_stage, allow_low_space);
    let mut fingerprint = |path: &PathBuf| stage_fingerprint(path);
    let mut repository = {
        let environment = environment.clone();
        let program = restic_program.to_owned();
        move || repository_id(&program, &environment)
    };
    let mut run = {
        let environment = environment.clone();
        let program = restic_program.to_owned();
        move |command: BackupCommand| {
            let mut argv = command.argv;
            if argv.first().is_some_and(|name| name == "restic") {
                argv[0] = program.clone();
            }
            run_restic(
                &argv[0],
                &argv[1..],
                command.cwd.as_deref(),
                &environment,
                false,
            )
            .map(|_| ())
        }
    };
    let mut remove = |path: &PathBuf| {
        clear_control_leaf(&path.to_string_lossy()).or_else(|error| {
            if error.is_missing_path() {
                Ok(())
            } else {
                Err(error)
            }
        })
    };
    let mut success = {
        let manifest = manifest.clone();
        move || run_phase(&manifest, "backup", "on_success", &[], &mut |_| None).map(|_| ())
    };
    let mut failure = {
        let manifest = manifest.clone();
        let service = service.clone();
        move |error: &SecurityError, phase: &str| {
            let owned = failure_environment(&service, phase, &error.message);
            let pairs = owned
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()))
                .collect::<Vec<_>>();
            let _ = run_phase(&manifest, "backup", "on_failure", &pairs, &mut |_| None);
        }
    };
    let mut now = || Local::now().fixed_offset();
    let mut load = || initial.take().unwrap_or_default();
    let mut save = {
        let state_root = state_root.clone();
        let service = service.clone();
        move |state: &BTreeMap<String, serde_json::Value>| {
            save_backup_state(&state_root, &service, state)
        }
    };
    let mut deps = BackupDeps {
        now: &mut now,
        load_state: &mut load,
        save_state: &mut save,
        stage: &mut stage,
        fingerprint: &mut fingerprint,
        repository_id: &mut repository,
        run: &mut run,
        remove_stage: &mut remove,
        on_success: &mut success,
        on_failure: &mut failure,
    };
    execute_backup(config, manifest, allow_low_space, &mut deps)
        .map(|_| ())
        .map_err(|error| error.message)
}

fn stage_files(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    allow_low_space: bool,
) -> Result<PathBuf, SecurityError> {
    let service = manifest.get("service").and_then(Yaml::as_str).unwrap_or("");
    let label = service_label(manifest);
    let staging_root = config.get("staging_root").map(String::as_str).unwrap_or("");
    let mut protected = vec![
        manifest
            .get("_dir")
            .and_then(Yaml::as_str)
            .unwrap_or("")
            .to_owned(),
    ];
    if let Some(sources) = manifest.get("sources").and_then(Yaml::as_map)
        && let Some(Yaml::Seq(paths)) = sources.get("paths")
    {
        for source in paths {
            let Some(source) = source.as_map() else {
                continue;
            };
            protected.push(
                source_path(manifest, source)
                    .map_err(|error| SecurityError::invalid(error.message))?,
            );
        }
    }
    let stage = prepare_stage(staging_root, service, &protected)?;
    ensure_space(&label, &stage, 0, allow_low_space)?;
    let captured = (|| {
        let mut before = run_phase(manifest, "backup", "before", &[], &mut |_| None)?;
        let mut check = || ensure_space(&label, &stage, 0, allow_low_space);
        let mut options = SyncOptions {
            capture_method: "best-effort".to_owned(),
            before_copy: Some(&mut check),
            ..SyncOptions::default()
        };
        let paths = sync_paths(config, manifest, Path::new(&stage), &mut options)?;
        ensure_space(&label, &stage, 0, allow_low_space)?;
        before.extend(run_phase(manifest, "backup", "after", &[], &mut |_| None)?);
        Ok::<_, SecurityError>((paths, before))
    })();
    let finally = run_phase(manifest, "backup", "finally", &[], &mut |_| None);
    let (paths, mut failures) = match captured {
        Ok(value) => value,
        Err(error) => {
            if let Err(cleanup) = finally {
                eprintln!("ERROR: finally actions cleanup also failed: {cleanup}");
            }
            return Err(error);
        }
    };
    failures.extend(finally?);
    write_inventory(manifest, Path::new(&stage), &paths, &failures)?;
    Ok(PathBuf::from(stage))
}

fn write_inventory(
    manifest: &BTreeMap<String, Yaml>,
    stage: &Path,
    paths: &[PathInventory],
    failures: &[crate::actions::ActionFailure],
) -> Result<(), SecurityError> {
    let meta = stage.join("_meta");
    ensure_private_directory(&meta.to_string_lossy(), false)?;
    if let Some(source) = manifest.get("_path").and_then(Yaml::as_str) {
        atomic_copy_file(Path::new(source), &meta.join("backup.yaml"), false, None)?;
    }
    atomic_write_json(
        &meta.join("inventory.json"),
        &inventory_value(manifest, paths, failures),
    )
}

fn inventory_value(
    manifest: &BTreeMap<String, Yaml>,
    paths: &[PathInventory],
    failures: &[crate::actions::ActionFailure],
) -> serde_json::Value {
    let path_items = paths
        .iter()
        .map(|item| {
            let mut entry = serde_json::Map::new();
            entry.insert("id".to_owned(), serde_json::Value::String(item.id.clone()));
            entry.insert(
                "path".to_owned(),
                serde_json::Value::String(item.path.clone()),
            );
            if let Some(kind) = &item.kind {
                entry.insert("type".to_owned(), serde_json::Value::String(kind.clone()));
            }
            entry.insert("present".to_owned(), serde_json::Value::Bool(item.present));
            if !item.ancestors.is_empty() {
                entry.insert(
                    "ancestors".to_owned(),
                    serde_json::Value::Array(
                        item.ancestors
                            .iter()
                            .map(|ancestor| {
                                serde_json::json!({
                                    "path": ancestor.path,
                                    "uid": ancestor.uid,
                                    "gid": ancestor.gid,
                                    "mode": ancestor.mode,
                                })
                            })
                            .collect(),
                    ),
                );
            }
            entry.insert(
                "capture_method".to_owned(),
                serde_json::Value::String(item.capture_method.clone()),
            );
            serde_json::Value::Object(entry)
        })
        .collect::<Vec<_>>();
    let action_failures = failures
        .iter()
        .map(|failure| {
            serde_json::json!({
                "phase": failure.phase,
                "name": failure.name,
                "result": failure.result,
            })
        })
        .collect::<Vec<_>>();
    serde_json::json!({
        "version": 1,
        "service": manifest.get("service").and_then(Yaml::as_str).unwrap_or(""),
        "service_directory": manifest.get("_dir").and_then(Yaml::as_str).unwrap_or(""),
        "service_relative_directory": manifest.get("_relative_dir").and_then(Yaml::as_str).unwrap_or(""),
        "paths": path_items,
        "volumes": [],
        "consistency": {
            "mode": "files",
            "guarantee": "best-effort",
            "optional_action_failures": action_failures,
            "writers": [],
        }
    })
}

fn ensure_space(
    label: &str,
    stage: &str,
    estimated: u64,
    allow_low_space: bool,
) -> Result<(), SecurityError> {
    let free = free_bytes(stage)?;
    match check_backup_space(label, stage, estimated, free, allow_low_space) {
        Ok(warnings) => {
            for warning in warnings {
                eprintln!("{warning}");
            }
            Ok(())
        }
        Err((warnings, message)) => {
            for warning in warnings {
                eprintln!("{warning}");
            }
            Err(SecurityError::runtime(message))
        }
    }
}

fn free_bytes(path: &str) -> Result<u64, SecurityError> {
    let stat =
        rustix::fs::statvfs(path).map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(stat.f_bavail.saturating_mul(stat.f_frsize))
}

fn repository_id(
    program: &str,
    environment: &BTreeMap<String, String>,
) -> Result<String, SecurityError> {
    let output = run_restic(
        program,
        &["cat".to_owned(), "config".to_owned()],
        None,
        environment,
        true,
    )?;
    crate::storage::parse_repository_id(&output)
}

fn run_restic(
    program: &str,
    args: &[String],
    cwd: Option<&Path>,
    environment: &BTreeMap<String, String>,
    capture: bool,
) -> Result<String, SecurityError> {
    let mut display = vec![program.to_owned()];
    display.extend(args.iter().cloned());
    println!(
        "+ {}",
        display
            .iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let mut command = Command::new(program);
    command.args(args).env_clear().envs(environment);
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    let output = command
        .output()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    if !capture {
        print!("{stdout}");
        eprint!("{stderr}");
    }
    if output.status.success() {
        Ok(stdout)
    } else {
        Err(SecurityError::runtime(format!(
            "command failed with exit code {}",
            output.status.code().unwrap_or(1)
        )))
    }
}

fn flat_strings(config: &BTreeMap<String, Yaml>) -> BTreeMap<String, String> {
    let mut flat = BTreeMap::new();
    for key in [
        "host_id",
        "services_root",
        "repository",
        "password_file",
        "rclone_config",
        "staging_root",
        "restore_root",
        "cache_root",
        "volume_helper_image",
        "state_root",
        "lock_file",
    ] {
        if let Some(value) = config.get(key).and_then(Yaml::as_str) {
            flat.insert(key.to_owned(), value.to_owned());
        }
    }
    if let Some(limit) = config
        .get("rclone")
        .and_then(Yaml::as_map)
        .and_then(|rclone| rclone.get("bwlimit"))
        .and_then(Yaml::as_str)
    {
        flat.insert("bwlimit".to_owned(), limit.to_owned());
    }
    flat
}

fn shell_quote(value: &str) -> String {
    if value.chars().all(|ch| {
        ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | '/' | ':' | '=' | ',')
    }) && !value.is_empty()
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

fn text(error: SecurityError) -> CommandError {
    CommandError::Text(error.message)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static TEMP: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn files_backup_copies_the_source_and_records_success() {
        let root = temp_root("files");
        let services = root.join("services");
        let demo = services.join("demo");
        let data = demo.join("data");
        std::fs::create_dir_all(&data).unwrap();
        for path in [&services, &demo, &data] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        std::fs::write(data.join("payload.txt"), "snapshot").unwrap();
        let manifest = demo.join("backup.yaml");
        std::fs::write(
            &manifest,
            "version: 1\nservice: demo\nschedule:\n  cron: '0 0 * * *'\nretention:\n  keep_last: 1\nconsistency:\n  mode: files\nsources:\n  paths:\n    - id: data\n      path: data\n",
        )
        .unwrap();
        std::fs::set_permissions(&manifest, std::fs::Permissions::from_mode(0o600)).unwrap();
        let config_path = write_config(&root, &services);
        let log = root.join("restic.log");
        let inventory = root.join("inventory.json");
        let program = fake_restic(&root, &log, &inventory);
        run_backup(
            &config_path.to_string_lossy(),
            &[],
            false,
            &program.to_string_lossy(),
        )
        .unwrap();
        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(calls.contains("unlock"), "{calls}");
        assert!(calls.contains("cat config"), "{calls}");
        assert!(calls.contains("backup ."), "{calls}");
        let saved: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&inventory).unwrap()).unwrap();
        assert_eq!(saved["consistency"]["mode"], "files");
        assert_eq!(saved["consistency"]["guarantee"], "best-effort");
        assert_eq!(saved["paths"][0]["capture_method"], "best-effort");
        assert_eq!(saved["paths"][0]["type"], "directory");
        assert!(
            !saved["paths"][0]["ancestors"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        let state: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(root.join("state").join("demo.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(state["last_result"], "success");
        assert!(state["last_error"].is_null());
        assert!(!root.join("staging").join("demo").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn other_consistency_modes_fail_before_restic_backup() {
        let root = temp_root("stop");
        let services = root.join("services");
        let demo = services.join("demo");
        std::fs::create_dir_all(&demo).unwrap();
        std::fs::set_permissions(&services, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::set_permissions(&demo, std::fs::Permissions::from_mode(0o755)).unwrap();
        let manifest = demo.join("backup.yaml");
        std::fs::write(
            &manifest,
            "version: 1\nservice: demo\nschedule:\n  cron: '0 0 * * *'\nretention:\n  keep_last: 1\nconsistency:\n  mode: stop\nsources:\n  paths: []\n  volumes: []\n",
        )
        .unwrap();
        std::fs::set_permissions(&manifest, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::write(demo.join("compose.yaml"), "services: {}\n").unwrap();
        std::fs::set_permissions(
            demo.join("compose.yaml"),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let config_path = write_config(&root, &services);
        let log = root.join("restic.log");
        let program = fake_restic(&root, &log, &root.join("inventory.json"));
        let error = run_backup(
            &config_path.to_string_lossy(),
            &["demo".to_owned()],
            false,
            &program.to_string_lossy(),
        )
        .unwrap_err();
        assert!(matches!(error, CommandError::Reported));
        let calls = std::fs::read_to_string(&log).unwrap_or_default();
        assert!(!calls.contains("backup"), "{calls}");
        let _ = std::fs::remove_dir_all(&root);
    }

    fn temp_root(label: &str) -> PathBuf {
        let id = TEMP.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("homelab-{label}-{}-{id}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        root
    }

    fn write_config(root: &Path, services: &Path) -> PathBuf {
        for name in ["staging", "restore", "cache", "state"] {
            let path = root.join(name);
            std::fs::create_dir_all(&path).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        std::fs::set_permissions(services, std::fs::Permissions::from_mode(0o755)).unwrap();
        for name in ["password", "rclone.conf"] {
            let path = root.join(name);
            std::fs::write(&path, "secret").unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let config = root.join("config.yaml");
        std::fs::write(
            &config,
            format!(
                "version: 1\nhost_id: host\nservices_root: {}\nrepository: rclone:demo:repo\npassword_file: {}\nrclone_config: {}\nstaging_root: {}\nrestore_root: {}\ncache_root: {}\nvolume_helper_image: helper:latest\nstate_root: {}\nlock_file: {}\n",
                services.display(),
                root.join("password").display(),
                root.join("rclone.conf").display(),
                root.join("staging").display(),
                root.join("restore").display(),
                root.join("cache").display(),
                root.join("state").display(),
                root.join("lock").display(),
            ),
        )
        .unwrap();
        std::fs::set_permissions(&config, std::fs::Permissions::from_mode(0o600)).unwrap();
        config
    }

    fn fake_restic(root: &Path, log: &Path, inventory: &Path) -> PathBuf {
        let program = root.join("restic");
        std::fs::write(
            &program,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nif [ \"$1\" = cat ]; then\n  printf '%s\\n' '{{\"id\":\"{}\"}}'\n  exit 0\nfi\nif [ \"$1\" = backup ]; then\n  cp _meta/inventory.json '{}'\n  test -f paths/data/payload.txt\nfi\nexit 0\n",
                log.display(),
                "a".repeat(64),
                inventory.display(),
            ),
        )
        .unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
        program
    }
}
