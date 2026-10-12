//! Validate, documentation, and explicit snapshot deletion.

use std::collections::BTreeMap;
use std::io::{BufRead, IsTerminal, Write};
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::process::Command;

use homelab_backup_domain::manifest::{requires_docker, service_label, validate_manifest};
use homelab_backup_domain::names::valid_service_name;
use homelab_backup_domain::yaml::Yaml;
use rustix::process::geteuid;

use crate::actions::{action_entries, action_executable};
use crate::catalog::select_every_manifest;
use crate::documents::load_global_config;
use crate::files::{CommandError, flat_strings, run_restic};
use crate::state::invalidate_snapshot_fingerprint;
use crate::storage::{parse_snapshots, validate_path_source_policy};
use crate::unix::account::account;
use crate::unix::control::clear_control_leaf;
use crate::unix::control::validate_control_file;
use crate::unix::data::open_data_path;
use crate::unix::error::SecurityError;
use crate::unix::lock::GlobalLock;
use crate::unix::mount::validate_control_root;
use crate::unix::restic::restic_env;

const ACTION_OPERATIONS: [&str; 2] = ["backup", "restore"];
const ACTION_PHASES: [&str; 5] = ["before", "after", "finally", "on_success", "on_failure"];

pub fn run_validate(config_path: &str) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let (manifests, load_errors) = select_every_manifest(&config).map_err(text)?;
    let mut errors = Vec::new();
    for failure in load_errors {
        eprintln!("{}", failure.line);
        errors.push(failure.summary);
    }
    let records = crate::unix::mount::current_mount_records().map_err(text)?;
    for key in ["staging_root", "restore_root"] {
        if let Some(path) = flat.get(key)
            && let Err(error) = validate_control_root(path, false, &records)
        {
            errors.push(format!("{key} is unsupported: {error}"));
        }
    }
    let mut loaded = Vec::new();
    let mut schema_valid = Vec::new();
    let mut runtime_ready = Vec::new();
    for service in manifests {
        let manifest = service.manifest;
        loaded.push(manifest.clone());
        let path = manifest.get("_path").and_then(Yaml::as_str).unwrap_or("");
        let label = format!("{} ({path})", service_label(&manifest));
        println!("\n== Validating {label} ==");
        match prepare_manifest(&manifest) {
            Ok(()) => {
                schema_valid.push(manifest.clone());
                runtime_ready.push((manifest, label));
            }
            Err(error) => {
                let message = format!("{label}: {error}");
                eprintln!("ERROR: {message}");
                errors.push(message);
            }
        }
    }
    let needs_docker = schema_valid.iter().any(requires_docker);
    let mut required = vec!["restic", "rclone", "rsync"];
    if needs_docker {
        required.insert(0, "docker");
    }
    for command in required {
        if !command_exists(command) {
            errors.push(format!("missing command: {command}"));
        }
    }
    if needs_docker && command_exists("docker") {
        let compose = Command::new("docker").args(["compose", "version"]).status();
        if compose.map(|status| status.success()).unwrap_or(false) {
            // Compose is present. Image rebuild stays with the installer.
        } else {
            eprintln!("ERROR: Docker Compose is unavailable");
            errors.push("docker compose version failed".to_owned());
        }
    }
    for key in ["password_file", "rclone_config"] {
        if let Some(path) = flat.get(key)
            && let Err(error) = validate_control_file(path)
        {
            errors.push(format!("unsafe config file {path}: {error}"));
        }
    }
    let services_root = flat.get("services_root").map(String::as_str).unwrap_or("");
    if !loaded
        .iter()
        .any(|manifest| manifest.get("enabled").and_then(Yaml::as_bool) != Some(false))
    {
        errors.push(format!(
            "no enabled backup.yaml manifests found under {services_root}/"
        ));
    }
    for (manifest, label) in runtime_ready {
        match check_runtime_sources(&flat, &manifest) {
            Ok(()) => println!("OK: {label}"),
            Err(error) => {
                let message = format!("{label}: {error}");
                eprintln!("ERROR: {message}");
                errors.push(message);
            }
        }
    }
    if errors.is_empty() {
        println!("\nOK: {} service manifest(s) validated", schema_valid.len());
        return Ok(());
    }
    eprintln!("\nVALIDATION FAILED");
    for error in errors {
        eprintln!("  - {error}");
    }
    Err(CommandError::Reported)
}

pub fn run_cleanup_restores(
    config_path: &str,
    targets: &[String],
    all: bool,
    yes: bool,
) -> Result<(), CommandError> {
    if !yes {
        return Err(CommandError::Text(
            "cleanup-restores requires --yes".to_owned(),
        ));
    }
    if all != targets.is_empty() {
        return Err(CommandError::Text(
            "specify restore targets or --all, but not both".to_owned(),
        ));
    }
    let mut requested = Vec::new();
    if !all {
        for target in targets {
            let parsed = parse_restore_target(target)?;
            if !requested.contains(&parsed) {
                requested.push(parsed);
            }
        }
    }
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let restore_root = flat
        .get("restore_root")
        .cloned()
        .ok_or_else(|| CommandError::Text("missing restore_root".to_owned()))?;
    let records = crate::unix::mount::current_mount_records().map_err(text)?;
    let selected = if all {
        all_restore_targets(&restore_root, &records)?
    } else {
        requested
    };
    let Some(_guard) = acquire(flat.get("lock_file").map(String::as_str).unwrap_or(""))? else {
        return Err(CommandError::Text(
            "another backupctl process is running".to_owned(),
        ));
    };
    let mut failures = Vec::new();
    let mut removed = 0usize;
    for (service, restore_id) in selected {
        let label = format!("{service}/{restore_id}");
        let target = format!("{restore_root}/{service}/{restore_id}");
        match validate_control_root(&target, false, &records)
            .and_then(|_| clear_control_leaf(&target))
        {
            Ok(()) => {
                removed += 1;
                println!("Removed restore: {label}");
            }
            Err(error) => {
                eprintln!("ERROR: cannot remove restore {label}: {error}");
                failures.push((label, error.message));
            }
        }
    }
    println!("Removed {removed} restore artifact(s).");
    if failures.is_empty() {
        return Ok(());
    }
    eprintln!("\nRESTORE CLEANUP FAILURES");
    for (label, error) in failures {
        eprintln!("  - {label}: {error}");
    }
    Err(CommandError::Reported)
}

fn parse_restore_target(value: &str) -> Result<(String, String), CommandError> {
    let path = Path::new(value);
    let parts: Vec<_> = path.components().collect();
    let invalid = || {
        CommandError::Text(format!(
            "invalid restore target '{value}'; expected SERVICE/RESTORE_ID"
        ))
    };
    if path.is_absolute() || parts.len() != 2 {
        return Err(invalid());
    }
    let (
        Some(std::path::Component::Normal(service)),
        Some(std::path::Component::Normal(restore_id)),
    ) = (parts.first(), parts.get(1))
    else {
        return Err(invalid());
    };
    let Some(service) = service.to_str() else {
        return Err(invalid());
    };
    let Some(restore_id) = restore_id.to_str() else {
        return Err(invalid());
    };
    if !valid_service_name(service) || !is_restore_id(restore_id) {
        return Err(invalid());
    }
    Ok((service.to_owned(), restore_id.to_owned()))
}

fn is_restore_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 8 + 1 + 6 + 1 + 9
        && bytes[8] == b'-'
        && bytes[15] == b'-'
        && bytes[..8].iter().all(u8::is_ascii_digit)
        && bytes[9..15].iter().all(u8::is_ascii_digit)
        && bytes[16..].iter().all(u8::is_ascii_digit)
}

fn all_restore_targets(
    restore_root: &str,
    records: &[crate::unix::mount::MountRecord],
) -> Result<Vec<(String, String)>, CommandError> {
    let root = Path::new(restore_root);
    if !root.exists() && std::fs::symlink_metadata(root).is_err() {
        return Ok(Vec::new());
    }
    validate_control_root(restore_root, false, records).map_err(text)?;
    let mut services = std::fs::read_dir(root)
        .map_err(|error| CommandError::Text(error.to_string()))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| CommandError::Text(error.to_string()))?;
    services.sort_by_key(|entry| entry.file_name());
    let mut targets = Vec::new();
    for service in services {
        let name = service.file_name();
        let Some(service_name) = name.to_str() else {
            return Err(CommandError::Text(format!(
                "invalid service directory in restore root: {}",
                service.path().display()
            )));
        };
        if !valid_service_name(service_name) {
            return Err(CommandError::Text(format!(
                "invalid service directory in restore root: {}",
                service.path().display()
            )));
        }
        validate_control_root(&service.path().to_string_lossy(), false, records).map_err(text)?;
        let mut restores = std::fs::read_dir(service.path())
            .map_err(|error| CommandError::Text(error.to_string()))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| CommandError::Text(error.to_string()))?;
        restores.sort_by_key(|entry| entry.file_name());
        for restore in restores {
            let restore_name = restore.file_name();
            let Some(restore_id) = restore_name.to_str().filter(|name| is_restore_id(name)) else {
                return Err(CommandError::Text(format!(
                    "invalid restore directory: {}",
                    restore.path().display()
                )));
            };
            validate_control_root(&restore.path().to_string_lossy(), false, records)
                .map_err(text)?;
            targets.push((service_name.to_owned(), restore_id.to_owned()));
        }
    }
    Ok(targets)
}

fn acquire(path: &str) -> Result<Option<GlobalLock>, CommandError> {
    let mut lock = GlobalLock::new(path, false, 0);
    if lock.enter(&mut || {}).map_err(text)? {
        Ok(Some(lock))
    } else {
        Ok(None)
    }
}

pub fn run_inventory() -> Result<(), CommandError> {
    let output_name = "homelab-backup-inventory.jsonl";
    let temporary_name = format!(".homelab-backup-inventory.{}.tmp", std::process::id());
    let mut lines = vec![
        serde_json::json!({
            "format": "homelab-backup-inventory",
            "version": 1,
            "root": "."
        })
        .to_string(),
    ];
    walk_inventory(
        std::path::Path::new("."),
        output_name,
        &temporary_name,
        &mut lines,
    )
    .map_err(CommandError::Text)?;
    let body = lines.join("\n") + "\n";
    let temporary = std::path::Path::new(&temporary_name);
    std::fs::write(temporary, body).map_err(|error| CommandError::Text(error.to_string()))?;
    std::fs::rename(temporary, output_name).map_err(|error| {
        let _ = std::fs::remove_file(temporary);
        CommandError::Text(error.to_string())
    })
}

fn walk_inventory(
    directory: &std::path::Path,
    output_name: &str,
    temporary_name: &str,
    lines: &mut Vec<String>,
) -> Result<(), String> {
    let mut names = Vec::new();
    for entry in std::fs::read_dir(directory).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        names.push(entry.file_name());
    }
    names.sort();
    for name in names {
        let path = directory.join(&name);
        let relative = path.strip_prefix(".").unwrap_or(&path);
        let relative = relative.to_string_lossy().replace('\\', "/");
        if relative == output_name || relative == temporary_name {
            continue;
        }
        let metadata = std::fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
        let kind = if metadata.file_type().is_symlink() {
            "symlink"
        } else if metadata.file_type().is_dir() {
            "directory"
        } else if metadata.file_type().is_file() {
            "file"
        } else {
            continue;
        };
        lines.push(
            serde_json::json!({
                "path": relative,
                "type": kind,
            })
            .to_string(),
        );
        if kind == "directory" {
            walk_inventory(&path, output_name, temporary_name, lines)?;
        }
    }
    Ok(())
}

pub fn run_docs() -> Result<(), CommandError> {
    let guide = installed_guide()?;
    let uri = format!("file://{}", guide.display());
    match Command::new("xdg-open").arg(&uri).status() {
        Ok(status) if status.success() => Ok(()),
        _ => Err(CommandError::Text(format!(
            "could not find a browser; open this file manually: {}",
            guide.display()
        ))),
    }
}

pub fn run_delete_snapshot(
    config_path: &str,
    service: &str,
    selector: &str,
    yes: bool,
    prune: bool,
) -> Result<(), CommandError> {
    if !valid_service_name(service) {
        return Err(CommandError::Text(format!(
            "invalid service name: '{service}'"
        )));
    }
    if !is_snapshot_selector(selector) {
        return Err(CommandError::Text(
            "snapshot ID must be an 8-64 character lowercase hexadecimal ID".to_owned(),
        ));
    }
    let config = load_global_config(config_path).map_err(text)?;
    let flat = flat_strings(&config);
    let environment = restic_env(&flat).map_err(text)?;
    let host = flat.get("host_id").cloned().unwrap_or_default();
    let output = run_restic(
        "restic",
        &[
            "snapshots".to_owned(),
            "--json".to_owned(),
            "--host".to_owned(),
            host.clone(),
            "--tag".to_owned(),
            format!("service:{service}"),
        ],
        None,
        &environment,
        true,
    )
    .map_err(text)?;
    let snapshots = parse_snapshots(&output).map_err(text)?;
    let snapshot = resolve_snapshot(&snapshots, &host, service, selector)?;
    if !yes {
        if !std::io::stdin().is_terminal() {
            return Err(CommandError::Text(
                "non-interactive snapshot deletion requires --yes".to_owned(),
            ));
        }
        if !prompt_yes(&format!(
            "Delete snapshot {snapshot} for service {service}?"
        ))? {
            println!("Snapshot deletion cancelled.");
            return Ok(());
        }
    }
    if let Some(state_root) = flat.get("state_root") {
        invalidate_snapshot_fingerprint(state_root, service).map_err(text)?;
    }
    run_restic(
        "restic",
        &["forget".to_owned(), snapshot.clone()],
        None,
        &environment,
        false,
    )
    .map_err(text)?;
    println!("Deleted snapshot {snapshot} for {service}.");
    if !prune {
        return Ok(());
    }
    if let Err(error) = run_restic("restic", &["prune".to_owned()], None, &environment, false) {
        let context =
            format!("Snapshot {snapshot} for {service} was deleted, but repository prune failed");
        eprintln!("ERROR: {context}: {error}");
        return Err(CommandError::Text(context));
    }
    println!("Reclaimed unreferenced repository data.");
    Ok(())
}

fn prepare_manifest(manifest: &BTreeMap<String, Yaml>) -> Result<(), String> {
    validate_manifest(&Yaml::Map(manifest.clone())).map_err(|error| error.message)?;
    for message in crate::actions::legacy_messages(manifest) {
        eprintln!("WARNING: {message}");
    }
    let nested = manifest
        .get("actions")
        .and_then(Yaml::as_map)
        .is_some_and(|actions| actions.contains_key("backup") || actions.contains_key("restore"));
    let operations: &[&str] = if nested {
        &ACTION_OPERATIONS
    } else {
        &["backup"]
    };
    for operation in operations {
        for phase in ACTION_PHASES {
            for action in action_entries(manifest, operation, phase) {
                let name = action.get("name").and_then(Yaml::as_str).unwrap_or("");
                let run_as = action.get("run_as").and_then(Yaml::as_str).unwrap_or("");
                account(run_as).map_err(|error| error.message)?;
                let command = action
                    .get("command")
                    .and_then(Yaml::as_seq)
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(Yaml::as_str)
                            .map(str::to_owned)
                            .collect::<Vec<_>>()
                    })
                    .filter(|items| !items.is_empty())
                    .unwrap_or_else(|| vec!["/bin/bash".to_owned()]);
                if action_executable(&command, run_as).is_none() {
                    return Err(format!(
                        "action '{name}' executable is missing: {}",
                        command.first().map(String::as_str).unwrap_or("")
                    ));
                }
            }
        }
    }
    if manifest
        .get("consistency")
        .and_then(Yaml::as_map)
        .and_then(|consistency| consistency.get("mode"))
        .and_then(Yaml::as_str)
        == Some("snapshot")
    {
        for executable in ["btrfs", "findmnt"] {
            if !command_exists(executable) {
                return Err(format!("snapshot mode requires command: {executable}"));
            }
        }
    }
    Ok(())
}

fn check_runtime_sources(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
) -> Result<(), String> {
    let mode = manifest
        .get("consistency")
        .and_then(Yaml::as_map)
        .and_then(|consistency| consistency.get("mode"))
        .and_then(Yaml::as_str)
        .unwrap_or("stop");
    let sources = manifest.get("sources").and_then(Yaml::as_map);
    if sources
        .and_then(|sources| sources.get("volumes"))
        .and_then(Yaml::as_seq)
        .is_some_and(|volumes| !volumes.is_empty())
    {
        crate::volumes::prepare_docker(config, manifest).map_err(|error| error.message)?;
    }
    let Some(paths) = sources
        .and_then(|sources| sources.get("paths"))
        .and_then(Yaml::as_seq)
    else {
        return Ok(());
    };
    for source in paths.iter().filter_map(Yaml::as_map) {
        let resolved =
            validate_path_source_policy(config, manifest, source).map_err(|error| error.message)?;
        let required = source
            .get("required")
            .and_then(Yaml::as_bool)
            .unwrap_or(true);
        match open_data_path(Path::new(&resolved)) {
            Ok(_) => {}
            Err(error) if error.is_missing_path() && (mode == "hooks" || !required) => {}
            Err(error) if error.is_missing_path() => {
                return Err(format!("missing required source: {resolved}"));
            }
            Err(error) => return Err(error.message),
        }
    }
    Ok(())
}

fn resolve_snapshot(
    snapshots: &[serde_json::Map<String, serde_json::Value>],
    host: &str,
    service: &str,
    selector: &str,
) -> Result<String, CommandError> {
    let expected = format!("service:{service}");
    let mut matches = Vec::new();
    for snapshot in snapshots {
        let Some(id) = snapshot.get("id").and_then(|value| value.as_str()) else {
            continue;
        };
        if !id.starts_with(selector) {
            continue;
        }
        if snapshot.get("hostname").and_then(|value| value.as_str()) != Some(host) {
            continue;
        }
        let tagged = snapshot
            .get("tags")
            .and_then(|value| value.as_array())
            .is_some_and(|tags| {
                tags.iter()
                    .any(|tag| tag.as_str() == Some(expected.as_str()))
            });
        if tagged {
            matches.push(id.to_owned());
        }
    }
    match matches.as_slice() {
        [one] => Ok(one.clone()),
        [] => Err(CommandError::Text(format!(
            "snapshot '{selector}' does not belong to service '{service}' on this host"
        ))),
        _ => Err(CommandError::Text(format!(
            "snapshot prefix '{selector}' is ambiguous for service '{service}'"
        ))),
    }
}

fn is_snapshot_selector(value: &str) -> bool {
    (8..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn prompt_yes(question: &str) -> Result<bool, CommandError> {
    let mut input = std::io::stdin().lock();
    let mut line = String::new();
    loop {
        print!("{question} [y/N] ");
        let _ = std::io::stdout().flush();
        line.clear();
        if input
            .read_line(&mut line)
            .map_err(|error| CommandError::Text(error.to_string()))?
            == 0
        {
            return Ok(false);
        }
        match line.trim().to_ascii_lowercase().as_str() {
            "" | "n" | "no" => return Ok(false),
            "y" | "yes" => return Ok(true),
            _ => println!("Please answer y or n."),
        }
    }
}

fn installed_guide() -> Result<std::path::PathBuf, CommandError> {
    let raw = std::env::var("HOMELAB_BACKUP_RELEASE_ROOT").map_err(|_| {
        CommandError::Text(
            "docs is only available from the installed /usr/local/sbin/backupctl".to_owned(),
        )
    })?;
    let release = Path::new(&raw);
    let releases = Path::new("/usr/local/lib/homelab-backup/releases");
    let name = release
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let parent_ok = release.parent() == Some(releases);
    let name_ok = name.starts_with("release.")
        && name.len() > "release.".len()
        && name["release.".len()..]
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric());
    if !parent_ok || !name_ok {
        return Err(CommandError::Text(
            "installed release layout is invalid".to_owned(),
        ));
    }
    let guide = release.join("GUIDE.html");
    let metadata = std::fs::symlink_metadata(&guide).map_err(|error| {
        CommandError::Text(format!("installed documentation is unavailable: {error}"))
    })?;
    let owner_ok = metadata.uid() == 0 || metadata.uid() == geteuid().as_raw();
    if !metadata.file_type().is_file() || !owner_ok || metadata.mode() & 0o022 != 0 {
        return Err(CommandError::Text(
            "installed documentation is not a protected regular file".to_owned(),
        ));
    }
    Ok(guide)
}

fn command_exists(name: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(name).is_file()))
}

fn text(error: SecurityError) -> CommandError {
    CommandError::Text(error.message)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    #[test]
    fn validate_reports_a_missing_config() {
        let error = run_validate("/tmp/homelab-missing-config.yaml").unwrap_err();
        match error {
            CommandError::Text(message) => assert!(message.contains("missing"), "{message}"),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn docs_require_an_installed_release() {
        let previous = std::env::var("HOMELAB_BACKUP_RELEASE_ROOT").ok();
        unsafe { std::env::remove_var("HOMELAB_BACKUP_RELEASE_ROOT") };
        let error = run_docs().unwrap_err();
        if let Some(previous) = previous {
            unsafe { std::env::set_var("HOMELAB_BACKUP_RELEASE_ROOT", previous) };
        }
        match error {
            CommandError::Text(message) => {
                assert!(message.contains("installed"), "{message}");
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn cleanup_requires_confirmation_and_one_selector() {
        let missing = run_cleanup_restores("/unused", &[], false, false).unwrap_err();
        assert!(matches!(missing, CommandError::Text(message) if message.contains("--yes")));
        let both = run_cleanup_restores(
            "/unused",
            &["demo/20261012-120000-000000001".to_owned()],
            true,
            true,
        )
        .unwrap_err();
        assert!(matches!(both, CommandError::Text(message) if message.contains("not both")));
        let invalid = run_cleanup_restores("/unused", &["demo/not-an-id".to_owned()], false, true)
            .unwrap_err();
        match invalid {
            CommandError::Text(message) => {
                assert!(message.contains("SERVICE/RESTORE_ID"), "{message}")
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn cleanup_removes_a_downloaded_restore_directory() {
        let root = std::env::temp_dir().join(format!(
            "homelab-cleanup-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        for name in ["staging", "restore", "cache", "state", "services"] {
            let path = root.join(name);
            std::fs::create_dir_all(&path).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
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
                root.join("services").display(),
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
        let target = root
            .join("restore")
            .join("demo")
            .join("20261012-120000-000000001");
        std::fs::create_dir_all(&target).unwrap();
        std::fs::write(target.join("payload.txt"), "keep").unwrap();
        let result = run_cleanup_restores(
            &config.to_string_lossy(),
            &["demo/20261012-120000-000000001".to_owned()],
            false,
            true,
        );
        match result {
            Ok(()) => assert!(!target.exists()),
            Err(CommandError::Reported) => assert!(
                target.exists(),
                "a refused restore directory must stay in place"
            ),
            Err(error) => panic!("unexpected {error:?}"),
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn snapshot_prefix_must_match_one_snapshot() {
        let snapshot = serde_json::json!({
            "id": "abcdef1234",
            "hostname": "host",
            "tags": ["service:demo"]
        });
        let map = snapshot.as_object().cloned().unwrap();
        assert_eq!(
            resolve_snapshot(std::slice::from_ref(&map), "host", "demo", "abcdef12").unwrap(),
            "abcdef1234"
        );
        let error = resolve_snapshot(&[map], "host", "other", "abcdef12").unwrap_err();
        match error {
            CommandError::Text(message) => {
                assert!(message.contains("does not belong"), "{message}")
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
