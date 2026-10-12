//! Publish a reviewed restore into the live service tree.

#![allow(dead_code)]

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::os::fd::OwnedFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use homelab_backup_domain::manifest::{requires_docker, source_path};
use homelab_backup_domain::names::validate_docker_volume_name;
use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{
    AtFlags, Mode, OFlags, fchmod, fchown, fstat, fsync, mkdirat, openat, renameat, statat,
    unlinkat,
};
use rustix::io::Errno;
use rustix::process::{Gid, Uid};
use serde_json::Value;

use crate::actions::{action_entries, failure_environment, run_phase};
use crate::restore_inventory::{
    InventoryAncestor, restored_path_details, restored_path_details_raw,
};
use crate::restore_plan::{
    RestorePlan, compose_file_paths, mount_conflicts, plan_compose_model, plan_running_services,
    prepare_restore_plan, project_containers, run_checked, validate_data_parent,
    validate_data_path, volume_exists,
};
use crate::storage::build_path_filter_args;
use crate::unix::atomic::atomic_copy_file;
use crate::unix::control::{
    clear_control_leaf, ensure_control_directory, file_name, is_dir, is_lnk, is_reg, mode_of,
    parent_path,
};
use crate::unix::data::{current_euid, validate_payload};
use crate::unix::error::SecurityError;
use crate::unix::mount::data_mount_snapshot;

#[derive(Clone, Debug, PartialEq, Eq)]
struct ObjectState {
    dev: u64,
    ino: u64,
    kind: u32,
    uid: u32,
    gid: u32,
    perm: u32,
    size: u64,
    mtime: i64,
    ctime: i64,
    children: Vec<(String, ObjectState)>,
}

struct VolumeClaim {
    name: String,
    operation_id: String,
    owned: bool,
}

struct AncestorClaim {
    path: String,
    dev: u64,
    ino: u64,
}

struct DataClaim {
    path: String,
    dev: u64,
    ino: u64,
    state: Option<ObjectState>,
    owned: bool,
}

struct ControlClaim {
    path: String,
    dev: u64,
    ino: u64,
    owned: bool,
}

enum Claim {
    Volume(VolumeClaim),
    Ancestor(AncestorClaim),
    Data(DataClaim),
    Control(ControlClaim),
}

struct RollbackLedger {
    claims: Vec<Claim>,
}

struct RestoreChanges {
    rollback: RollbackLedger,
    live_mutations: Vec<String>,
}

pub enum RestoreMode {
    Direct,
    Existing,
    Rebuild,
}

pub fn apply_one(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    start_services: bool,
) -> Result<(), SecurityError> {
    let plan = prepare_restore_plan(config, manifest, root)?;
    print_plan(&plan);
    if start_services && !requires_docker(&plan.manifest) {
        return Err(SecurityError::invalid(
            "--start is not supported for files mode".to_owned(),
        ));
    }
    let targets = plan.running_services.clone();
    if let Err(error) = stop_running_services(&plan) {
        cleanup(
            || {
                run_restore_actions(&plan, root, "on_failure", Some(&error), Some("stop"))
                    .map(|_| ())
            },
            "restore.on_failure actions",
        );
        cleanup(
            || stop_hook_started_services(&plan),
            "stop services after failed restore stop",
        );
        if !targets.is_empty() {
            let recovery = compose_args(
                &plan.manifest,
                &["up".to_owned(), "-d".to_owned(), "--no-deps".to_owned()],
                &targets,
            );
            cleanup(
                || recovery.and_then(|argv| run_checked(&argv).map(|_| ())),
                "service recovery after failed Compose stop",
            );
        }
        return Err(error);
    }
    let mut optional = Vec::new();
    let result = apply_stopped(
        config,
        manifest,
        root,
        &plan,
        &targets,
        start_services,
        &mut optional,
    );
    warn_optional(&optional);
    result
}

pub fn restore_path_source(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    source: &BTreeMap<String, Yaml>,
    inventory: &Value,
    mode: RestoreMode,
) -> Result<(), SecurityError> {
    let details = restored_path_details_raw(manifest, root, source, inventory)?;
    restore_details(manifest, source, inventory, &details, mode, None)
}

fn restore_details(
    manifest: &BTreeMap<String, Yaml>,
    source: &BTreeMap<String, Yaml>,
    inventory: &Value,
    details: &crate::restore_inventory::PathArtifact,
    mode: RestoreMode,
    mut on_publish: Option<&mut dyn FnMut((u64, u64), ObjectState)>,
) -> Result<(), SecurityError> {
    if details.source_type == "missing" {
        if source
            .get("required")
            .and_then(Yaml::as_bool)
            .unwrap_or(true)
        {
            return Err(SecurityError::runtime(format!(
                "restored path source is missing: {}",
                details.restored.display()
            )));
        }
        return Ok(());
    }
    if details.source_type == "file" && !is_real_file(&details.restored) {
        return Err(SecurityError::runtime(format!(
            "restored file artifact is missing: {}",
            details.restored.display()
        )));
    }
    if details.source_type == "symlink" && !is_real_link(&details.restored) {
        return Err(SecurityError::runtime(format!(
            "restored symlink artifact is missing: {}",
            details.restored.display()
        )));
    }
    if details.source_type == "directory" && !is_real_dir(&details.restored) {
        return Err(SecurityError::runtime(format!(
            "restored directory artifact is missing: {}",
            details.restored.display()
        )));
    }
    match &mode {
        RestoreMode::Direct => {
            if let Err(error) = validate_data_path(&details.target)
                && !error.is_missing_path()
            {
                return Err(error);
            }
        }
        RestoreMode::Rebuild => validate_data_parent(&details.target, true)?,
        RestoreMode::Existing => validate_data_path(&details.target)?,
    }
    let mount_state = data_mount_snapshot(&details.target).ok();
    validate_payload(&details.restored, None, current_euid(), None)?;
    let ancestors = ancestor_metadata(source, inventory);
    let rebuild = matches!(mode, RestoreMode::Rebuild);
    let (parent_fd, name) = open_data_parent(
        &details.target,
        if rebuild { Some(&ancestors) } else { None },
        &mut None,
    )?;
    let published = (|| {
        normalize_restore_target(&parent_fd, &name, &details.source_type)?;
        if details.source_type == "file" || details.source_type == "symlink" {
            restore_non_directory(&details.restored, &parent_fd, &name, &mut on_publish)
        } else if rebuild {
            restore_rebuild_directory(&details.restored, &parent_fd, &name, &mut on_publish)
        } else {
            restore_existing_directory(
                source,
                &details.restored,
                &parent_fd,
                &name,
                &mut on_publish,
            )
        }
    })();
    let _ = fsync(&parent_fd);
    published?;
    if let Some(before) = mount_state
        && data_mount_snapshot(&details.target).ok().as_ref() != Some(&before)
    {
        return Err(SecurityError::runtime(format!(
            "mount topology changed during restore: {}",
            details.target
        )));
    }
    let _ = manifest;
    Ok(())
}

fn apply_stopped(
    _config: &BTreeMap<String, Yaml>,
    requested: &BTreeMap<String, Yaml>,
    root: &Path,
    plan: &RestorePlan,
    targets: &[String],
    start_services: bool,
    optional: &mut Vec<crate::actions::ActionFailure>,
) -> Result<(), SecurityError> {
    let mut changes = RestoreChanges {
        rollback: RollbackLedger { claims: Vec::new() },
        live_mutations: Vec::new(),
    };
    let mut primary: Option<(SecurityError, String)> = None;
    let mut active = "preflight";
    let mut mutation_started = false;
    if let Err(error) = (|| {
        dynamic_preflight(plan)?;
        active = "before";
        optional.extend(run_restore_actions(plan, root, "before", None, None)?);
        mutation_started = true;
        let operation_id = token_hex(16);
        active = "restore";
        if plan.mode == "rebuild" {
            publish_controls(requested, plan, &mut changes)?;
            restore_data(plan, &mut changes, &operation_id)?;
        } else {
            restore_data(plan, &mut changes, &operation_id)?;
            publish_controls(requested, plan, &mut changes)?;
        }
        active = "after";
        optional.extend(run_restore_actions(plan, root, "after", None, None)?);
        Ok(())
    })() {
        primary = Some((error, active.to_owned()));
    }
    if let Err(error) = run_restore_actions(plan, root, "finally", None, None) {
        match &mut primary {
            None => primary = Some((error, "finally".to_owned())),
            Some((existing, _)) => {
                existing.message = format!(
                    "{}; restore.finally also failed: RuntimeError: {error}",
                    existing.message
                );
            }
        }
    } else if let Ok(failures) = run_restore_actions(plan, root, "finally", None, None) {
        optional.extend(failures);
    }
    if let Some((error, phase)) = primary {
        cleanup(
            || {
                run_restore_actions(plan, root, "on_failure", Some(&error), Some(&phase))
                    .map(|_| ())
            },
            "restore.on_failure actions",
        );
        cleanup(
            || stop_hook_started_services(plan),
            "stop services started by restore actions",
        );
        if !mutation_started && !targets.is_empty() {
            let recovery = compose_args(
                &plan.manifest,
                &["up".to_owned(), "-d".to_owned(), "--no-deps".to_owned()],
                targets,
            );
            cleanup(
                || recovery.and_then(|argv| run_checked(&argv).map(|_| ())),
                "service recovery after restore preflight",
            );
        } else if mutation_started && plan.mode == "rebuild" {
            cleanup(
                || rollback(&mut changes.rollback),
                "rebuild restore rollback",
            );
            eprintln!("ERROR: rebuild restore failed; rollback of new targets was attempted");
        } else if mutation_started {
            let mut message = "ERROR: restore failed after live mutation".to_owned();
            if requires_docker(&plan.manifest) {
                message.push_str("; services remain stopped");
            }
            eprintln!("{message}");
            let mut seen = Vec::new();
            for target in &changes.live_mutations {
                if !seen.contains(target) {
                    eprintln!("  - possibly modified: {target}");
                    seen.push(target.clone());
                }
            }
        }
        return Err(error);
    }
    if let Err(error) = restart_services(plan, targets, start_services) {
        cleanup(
            || {
                run_restore_actions(plan, root, "on_failure", Some(&error), Some("restart"))
                    .map(|_| ())
            },
            "restore.on_failure actions",
        );
        cleanup(
            || stop_hook_started_services(plan),
            "stop services after failed restore restart",
        );
        return Err(error);
    }
    if let Err(error) = run_restore_actions(plan, root, "on_success", None, None) {
        cleanup(
            || {
                run_restore_actions(plan, root, "on_failure", Some(&error), Some("on_success"))
                    .map(|_| ())
            },
            "restore.on_failure actions",
        );
        cleanup(
            || restart_services(plan, targets, start_services),
            "service reconciliation after failed restore.on_success",
        );
        return Err(error);
    }
    Ok(())
}

fn restore_data(
    plan: &RestorePlan,
    changes: &mut RestoreChanges,
    operation_id: &str,
) -> Result<(), SecurityError> {
    let mut resolved = plan.volumes.clone();
    if plan.mode == "rebuild" {
        if plan.volumes.iter().any(|(source, _)| {
            source
                .get("compose_volume")
                .and_then(Yaml::as_str)
                .is_some_and(|value| !value.is_empty())
        }) {
            let model = plan_compose_model(&plan.manifest)?;
            resolved = resolved_rebuild_volumes(plan, &model)?;
            create_rebuild_compose_volumes(plan, &model, &resolved, changes, operation_id)?;
        }
        for (source, name) in &resolved {
            if source
                .get("compose_volume")
                .and_then(Yaml::as_str)
                .is_some_and(|value| !value.is_empty())
            {
                continue;
            }
            changes.rollback.claims.push(Claim::Volume(VolumeClaim {
                name: name.clone(),
                operation_id: operation_id.to_owned(),
                owned: false,
            }));
            let index = changes.rollback.claims.len() - 1;
            create_restore_volume(
                plan,
                source,
                name,
                operation_id,
                None,
                &mut changes.rollback,
                index,
            )?;
        }
    }
    let flat = config_strings();
    for source in path_source_maps(&plan.manifest) {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        if plan.deferred_sources.iter().any(|item| item == source_id) {
            continue;
        }
        let target = source_path(&plan.manifest, source).map_err(invalid)?;
        let mut claim_index = None;
        if plan.mode == "rebuild" {
            let details =
                restored_path_details(&plan.manifest, &plan.root, source, &plan.inventory)?;
            if details.source_type != "missing" {
                let entry = plan
                    .inventory
                    .paths_by_id()
                    .get(source_id)
                    .copied()
                    .ok_or_else(|| SecurityError::runtime("missing path entry".to_owned()))?;
                claim_rebuild_path(
                    &target,
                    &details.source_type,
                    &entry.ancestors,
                    &mut changes.rollback,
                )?;
                claim_index = Some(changes.rollback.claims.len() - 1);
            }
        } else {
            changes.live_mutations.push(target.clone());
        }
        let raw = plan.inventory.to_snapshot_dict();
        let details = restored_path_details_raw(&plan.manifest, &plan.root, source, &raw)?;
        let mode = if plan.mode == "rebuild" {
            RestoreMode::Rebuild
        } else {
            RestoreMode::Existing
        };
        let mut publish = |identity: (u64, u64), state: ObjectState| {
            if let Some(index) = claim_index
                && let Claim::Data(claim) = &mut changes.rollback.claims[index]
            {
                claim.dev = identity.0;
                claim.ino = identity.1;
                claim.state = Some(state);
            }
        };
        let callback = claim_index.map(|_| &mut publish as &mut dyn FnMut((u64, u64), ObjectState));
        restore_details(&plan.manifest, source, &raw, &details, mode, callback)?;
    }
    if plan.mode != "rebuild" {
        for (_, name) in &plan.volumes {
            changes.live_mutations.push(format!("volume:{name}"));
        }
    }
    if requires_docker(&plan.manifest) {
        restore_volumes(&flat, &resolved, &plan.root)?;
    }
    Ok(())
}

fn restore_volumes(
    config: &BTreeMap<String, String>,
    resolved: &[(BTreeMap<String, Yaml>, String)],
    root: &Path,
) -> Result<(), SecurityError> {
    let image = config
        .get("volume_helper_image")
        .cloned()
        .unwrap_or_default();
    for (source, name) in resolved {
        validate_docker_volume_name(name, "Docker volume name").map_err(invalid)?;
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        let staged = root.join("volumes").join(source_id);
        let volume_root = root.join("volumes");
        if !is_real_dir(&volume_root) || !is_real_dir(&staged) {
            return Err(SecurityError::runtime(format!(
                "restored volume artifact is missing or not a real directory: {}",
                staged.display()
            )));
        }
        let mut argv = vec![
            "docker".to_owned(),
            "run".to_owned(),
            "--rm".to_owned(),
            "--network".to_owned(),
            "none".to_owned(),
            "--mount".to_owned(),
            format!("type=volume,src={name},dst=/dst"),
            "--mount".to_owned(),
            format!("type=bind,src={},dst=/src,readonly", staged.display()),
            image.clone(),
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--delete".to_owned(),
        ];
        if let Some(items) = source.get("exclude").and_then(Yaml::as_seq) {
            for item in items.iter().filter_map(Yaml::as_str) {
                argv.push("--exclude".to_owned());
                argv.push(item.to_owned());
            }
        }
        argv.push("/src/".to_owned());
        argv.push("/dst/".to_owned());
        run_checked(&argv)?;
    }
    Ok(())
}

fn publish_controls(
    requested: &BTreeMap<String, Yaml>,
    plan: &RestorePlan,
    changes: &mut RestoreChanges,
) -> Result<(), SecurityError> {
    for source in path_source_maps(&plan.manifest) {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        if !plan.deferred_sources.iter().any(|item| item == source_id) {
            continue;
        }
        let details = restored_path_details(&plan.manifest, &plan.root, source, &plan.inventory)?;
        if details.source_type != "file" {
            return Err(SecurityError::runtime(format!(
                "Compose source must be a regular file: {}",
                details.target
            )));
        }
        if plan.mode == "rebuild" {
            ensure_control_directory(&parent_path(&details.target), 0o755)?;
            changes.rollback.claims.push(Claim::Control(ControlClaim {
                path: details.target.clone(),
                dev: 0,
                ino: 0,
                owned: false,
            }));
            let index = changes.rollback.claims.len() - 1;
            let mut published = |identity: (u64, u64)| {
                if let Claim::Control(claim) = &mut changes.rollback.claims[index] {
                    claim.dev = identity.0;
                    claim.ino = identity.1;
                    claim.owned = true;
                }
            };
            atomic_copy_file(
                &details.restored,
                Path::new(&details.target),
                true,
                Some(&mut published),
            )?;
        } else {
            changes.live_mutations.push(details.target.clone());
            atomic_copy_file(&details.restored, Path::new(&details.target), false, None)?;
        }
    }
    if requested
        .get("_restore_manifest_requested")
        .and_then(Yaml::as_bool)
        == Some(true)
    {
        let target = requested
            .get("_path")
            .and_then(Yaml::as_str)
            .unwrap_or("")
            .to_owned();
        let source = requested
            .get("_snapshot_manifest")
            .and_then(Yaml::as_str)
            .unwrap_or("");
        if plan.mode == "rebuild" {
            ensure_control_directory(&parent_path(&target), 0o755)?;
            changes.rollback.claims.push(Claim::Control(ControlClaim {
                path: target.clone(),
                dev: 0,
                ino: 0,
                owned: false,
            }));
            let index = changes.rollback.claims.len() - 1;
            let mut published = |identity: (u64, u64)| {
                if let Claim::Control(claim) = &mut changes.rollback.claims[index] {
                    claim.dev = identity.0;
                    claim.ino = identity.1;
                    claim.owned = true;
                }
            };
            atomic_copy_file(
                Path::new(source),
                Path::new(&target),
                true,
                Some(&mut published),
            )?;
        } else {
            changes.live_mutations.push(target.clone());
            atomic_copy_file(Path::new(source), Path::new(&target), false, None)?;
        }
    }
    Ok(())
}

fn dynamic_preflight(plan: &RestorePlan) -> Result<(), SecurityError> {
    let files_mode = !requires_docker(&plan.manifest);
    let path_targets = path_source_maps(&plan.manifest)
        .iter()
        .map(|source| source_path(&plan.manifest, source).map_err(invalid))
        .collect::<Result<Vec<_>, _>>()?;
    if !files_mode {
        let names = if plan.mode == "rebuild" {
            plan.all_volume_names.clone()
        } else {
            plan.volumes.iter().map(|(_, name)| name.clone()).collect()
        };
        let conflicts = mount_conflicts(&path_targets, &names, plan.mode == "rebuild")?;
        if !conflicts.is_empty() {
            return Err(SecurityError::runtime(format!(
                "restore targets are used by containers: {}",
                conflicts.join(", ")
            )));
        }
        let remaining = project_containers(&plan.project_name, plan.mode == "rebuild")?;
        if !remaining.is_empty() {
            return Err(SecurityError::runtime(format!(
                "Compose project still has running containers after stop: {}",
                remaining.join(", ")
            )));
        }
    }
    for source in path_source_maps(&plan.manifest) {
        let target = source_path(&plan.manifest, source).map_err(invalid)?;
        if plan.mode == "rebuild" {
            validate_data_parent(&target, true)?;
        } else {
            validate_data_path(&target)?;
        }
        let _ = data_mount_snapshot(&target)?;
        let entry = plan
            .inventory
            .paths_by_id()
            .get(source.get("id").and_then(Yaml::as_str).unwrap_or(""))
            .copied();
        let present = entry.is_some_and(|entry| entry.present);
        let exists = exists_or_link(Path::new(&target));
        if plan.mode == "rebuild" && exists {
            return Err(SecurityError::runtime(format!(
                "rebuild target appeared during preflight: {target}"
            )));
        }
        if plan.mode != "rebuild" && present && !exists {
            return Err(SecurityError::runtime(format!(
                "existing target disappeared during preflight: {target}"
            )));
        }
    }
    if plan.mode == "rebuild" && !files_mode {
        let compose_names = plan
            .volumes
            .iter()
            .filter(|(source, _)| {
                source
                    .get("compose_volume")
                    .and_then(Yaml::as_str)
                    .is_some_and(|value| !value.is_empty())
            })
            .map(|(_, name)| name.as_str())
            .collect::<Vec<_>>();
        for name in &plan.all_volume_names {
            if compose_names.contains(&name.as_str()) {
                continue;
            }
            if volume_exists(name)? {
                return Err(SecurityError::runtime(format!(
                    "rebuild volume appeared during preflight: {name}"
                )));
            }
        }
    }
    for (source, name) in &plan.volumes {
        if plan.mode == "existing" {
            if !volume_exists(name)? {
                return Err(SecurityError::runtime(format!(
                    "existing volume disappeared during preflight: {name}"
                )));
            }
            let _ = source;
        }
    }
    if plan.mode == "rebuild" {
        let manifest_target = plan
            .manifest
            .get("_path")
            .and_then(Yaml::as_str)
            .map(str::to_owned)
            .or_else(|| {
                plan.manifest
                    .get("_dir")
                    .and_then(Yaml::as_str)
                    .map(|dir| Path::new(dir).join("backup.yaml").display().to_string())
            });
        let mut controls = if files_mode {
            Vec::new()
        } else {
            compose_file_paths(&plan.manifest)?
        };
        if plan
            .manifest
            .get("_restore_manifest_requested")
            .and_then(Yaml::as_bool)
            == Some(true)
            && let Some(target) = manifest_target
        {
            controls.push(target);
        }
        if controls.iter().any(|path| exists_or_link(Path::new(path))) {
            return Err(SecurityError::runtime(
                "rebuild control target appeared during preflight".to_owned(),
            ));
        }
    }
    Ok(())
}

fn stop_running_services(plan: &RestorePlan) -> Result<(), SecurityError> {
    if plan.running_services.is_empty() {
        return Ok(());
    }
    let timeout = plan
        .manifest
        .get("consistency")
        .and_then(Yaml::as_map)
        .and_then(|map| map.get("timeout"))
        .and_then(Yaml::as_int)
        .unwrap_or(120);
    let argv = compose_args(
        &plan.manifest,
        &["stop".to_owned(), "-t".to_owned(), timeout.to_string()],
        &plan.running_services,
    )?;
    run_checked(&argv).map(|_| ())
}

fn restart_services(
    plan: &RestorePlan,
    targets: &[String],
    start_services: bool,
) -> Result<(), SecurityError> {
    if !requires_docker(&plan.manifest) {
        if start_services {
            return Err(SecurityError::invalid(
                "--start is not supported for files mode".to_owned(),
            ));
        }
        return Ok(());
    }
    if start_services {
        if !compose_files_exist(&plan.manifest) {
            return Err(SecurityError::runtime(format!(
                "cannot start {}: Compose files were not restored",
                plan.manifest
                    .get("service")
                    .and_then(Yaml::as_str)
                    .unwrap_or("")
            )));
        }
        let argv = compose_args(&plan.manifest, &["up".to_owned(), "-d".to_owned()], &[])?;
        return run_checked(&argv).map(|_| ());
    }
    if has_restore_actions(&plan.manifest) {
        if plan.mode == "rebuild" {
            let argv = compose_args(
                &plan.manifest,
                &["down".to_owned(), "--remove-orphans".to_owned()],
                &[],
            )?;
            run_checked(&argv)?;
        } else {
            let current = plan_running_services(&plan.manifest)?;
            let extras = current
                .into_iter()
                .filter(|service| !targets.contains(service))
                .collect::<Vec<_>>();
            if !extras.is_empty() {
                let argv = compose_args(&plan.manifest, &["stop".to_owned()], &extras)?;
                run_checked(&argv)?;
            }
        }
    }
    if !targets.is_empty() {
        let argv = compose_args(
            &plan.manifest,
            &["up".to_owned(), "-d".to_owned(), "--no-deps".to_owned()],
            targets,
        )?;
        run_checked(&argv)?;
    }
    Ok(())
}

fn stop_hook_started_services(plan: &RestorePlan) -> Result<(), SecurityError> {
    if !requires_docker(&plan.manifest) || !has_restore_actions(&plan.manifest) {
        return Ok(());
    }
    if plan.mode == "rebuild" {
        let argv = compose_args(
            &plan.manifest,
            &["down".to_owned(), "--remove-orphans".to_owned()],
            &[],
        )?;
        return run_checked(&argv).map(|_| ());
    }
    let current = plan_running_services(&plan.manifest)?;
    if current.is_empty() {
        return Ok(());
    }
    let argv = compose_args(&plan.manifest, &["stop".to_owned()], &current)?;
    run_checked(&argv).map(|_| ())
}

fn run_restore_actions(
    plan: &RestorePlan,
    root: &Path,
    phase: &str,
    error: Option<&SecurityError>,
    failure_phase: Option<&str>,
) -> Result<Vec<crate::actions::ActionFailure>, SecurityError> {
    if action_entries(&plan.manifest, "restore", phase).is_empty() {
        return Ok(Vec::new());
    }
    let service = plan
        .manifest
        .get("service")
        .and_then(Yaml::as_str)
        .unwrap_or("");
    let mut owned = vec![
        (
            "BACKUPCTL_RESTORE_ROOT".to_owned(),
            root.display().to_string(),
        ),
        ("BACKUPCTL_RESTORE_MODE".to_owned(), plan.mode.clone()),
    ];
    if let Some(error) = error {
        owned.extend(failure_environment(
            service,
            failure_phase.unwrap_or(phase),
            &error.message,
        ));
    }
    let extra = owned
        .iter()
        .map(|(key, value)| (key.as_str(), value.as_str()))
        .collect::<Vec<_>>();
    run_phase(&plan.manifest, "restore", phase, &extra, &mut |_| None)
}

fn has_restore_actions(manifest: &BTreeMap<String, Yaml>) -> bool {
    ["before", "after", "finally", "on_success", "on_failure"]
        .iter()
        .any(|phase| !action_entries(manifest, "restore", phase).is_empty())
}

fn warn_optional(failures: &[crate::actions::ActionFailure]) {
    if failures.is_empty() {
        return;
    }
    eprintln!(
        "WARNING: {} optional restore action(s) did not succeed:",
        failures.len()
    );
    for failure in failures {
        eprintln!(
            "  - restore.{} {}: {}",
            failure.phase,
            homelab_backup_domain::yaml::python_str(&failure.name),
            failure.result
        );
    }
}

fn print_plan(plan: &RestorePlan) {
    println!("RESTORE PLAN ({})", plan.mode);
    for source in path_source_maps(&plan.manifest) {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        let present = plan
            .inventory
            .paths_by_id()
            .get(source_id)
            .is_some_and(|entry| entry.present);
        let target = source_path(&plan.manifest, source).unwrap_or_else(|_| source_id.to_owned());
        println!(
            "  path {source_id}: {target} ({})",
            if present {
                "restore"
            } else {
                "snapshot absent"
            }
        );
    }
    for entry in &plan.inventory.volumes {
        println!(
            "  volume {}: {} ({})",
            entry.id,
            entry.actual_name,
            if entry.present {
                "restore"
            } else {
                "snapshot absent"
            }
        );
    }
}

fn resolved_rebuild_volumes(
    plan: &RestorePlan,
    model: &Value,
) -> Result<Vec<crate::restore_plan::VolumeSource>, SecurityError> {
    let model_name = model.get("name").and_then(Value::as_str).unwrap_or("");
    if model_name != plan.project_name {
        return Err(SecurityError::runtime(format!(
            "Compose project identity changed: expected {}, got {}",
            homelab_backup_domain::yaml::python_str(&plan.project_name),
            homelab_backup_domain::yaml::python_str(model_name),
        )));
    }
    let resolved = crate::volumes::resolved_volumes(&plan.manifest, model)?;
    let mut seen = BTreeMap::<String, String>::new();
    let mut values = Vec::new();
    for (source, snapshot_name) in &plan.volumes {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        let Some((_, _, name)) = resolved.iter().find(|(id, _, _)| id == source_id) else {
            return Err(SecurityError::runtime(format!(
                "restored volume target differs from the reviewed Compose model for {}: missing",
                homelab_backup_domain::yaml::python_str(source_id)
            )));
        };
        if name != snapshot_name {
            return Err(SecurityError::runtime(format!(
                "restored volume target differs from the reviewed Compose model for {}: snapshot={}, compose={}",
                homelab_backup_domain::yaml::python_str(source_id),
                homelab_backup_domain::yaml::python_str(snapshot_name),
                homelab_backup_domain::yaml::python_str(name),
            )));
        }
        if let Some(logical) = source.get("compose_volume").and_then(Yaml::as_str)
            && model
                .get("volumes")
                .and_then(|volumes| volumes.get(logical))
                .and_then(|volume| volume.get("external"))
                .and_then(Value::as_bool)
                == Some(true)
        {
            return Err(SecurityError::runtime(format!(
                "rebuild cannot restore external Compose volume {}; exclude it from rebuild and restore it separately after reviewing its rollback requirements",
                homelab_backup_domain::yaml::python_str(logical)
            )));
        }
        if let Some(previous) = seen.get(name) {
            return Err(SecurityError::runtime(format!(
                "duplicate rebuild volume target {name} for {previous} and {source_id}"
            )));
        }
        seen.insert(name.clone(), source_id.to_owned());
        values.push((source.clone(), name.clone()));
    }
    Ok(values)
}

fn create_rebuild_compose_volumes(
    plan: &RestorePlan,
    model: &Value,
    resolved: &[(BTreeMap<String, Yaml>, String)],
    changes: &mut RestoreChanges,
    operation_id: &str,
) -> Result<(), SecurityError> {
    for (source, name) in resolved {
        let Some(logical) = source.get("compose_volume").and_then(Yaml::as_str) else {
            continue;
        };
        if volume_exists(name)? {
            return Err(SecurityError::runtime(format!(
                "rebuild Compose volume already exists: {name}"
            )));
        }
        let volume = model
            .get("volumes")
            .and_then(|volumes| volumes.get(logical))
            .cloned()
            .unwrap_or(Value::Null);
        changes.rollback.claims.push(Claim::Volume(VolumeClaim {
            name: name.clone(),
            operation_id: operation_id.to_owned(),
            owned: false,
        }));
        let index = changes.rollback.claims.len() - 1;
        create_restore_volume(
            plan,
            source,
            name,
            operation_id,
            Some(&volume),
            &mut changes.rollback,
            index,
        )?;
    }
    Ok(())
}

fn create_restore_volume(
    plan: &RestorePlan,
    source: &BTreeMap<String, Yaml>,
    name: &str,
    operation_id: &str,
    volume: Option<&Value>,
    ledger: &mut RollbackLedger,
    index: usize,
) -> Result<(), SecurityError> {
    validate_docker_volume_name(name, "Docker volume name").map_err(invalid)?;
    let mut labels = Vec::new();
    if let Some(volume) = volume
        && let Some(object) = volume.get("labels").and_then(Value::as_object)
    {
        for (key, value) in object {
            let text = value.as_str().unwrap_or("");
            labels.push((key.clone(), text.to_owned()));
        }
    }
    let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
    labels.push((
        "io.homelab-backup.service".to_owned(),
        plan.manifest
            .get("service")
            .and_then(Yaml::as_str)
            .unwrap_or("")
            .to_owned(),
    ));
    labels.push(("io.homelab-backup.source".to_owned(), source_id.to_owned()));
    labels.push((
        "io.homelab-backup.operation".to_owned(),
        operation_id.to_owned(),
    ));
    if let Some(logical) = source.get("compose_volume").and_then(Yaml::as_str) {
        labels.push((
            "com.docker.compose.project".to_owned(),
            plan.project_name.clone(),
        ));
        labels.push(("com.docker.compose.volume".to_owned(), logical.to_owned()));
    }
    let mut argv = vec![
        "docker".to_owned(),
        "volume".to_owned(),
        "create".to_owned(),
    ];
    if let Some(driver) = volume
        .and_then(|volume| volume.get("driver"))
        .and_then(Value::as_str)
    {
        argv.push("--driver".to_owned());
        argv.push(driver.to_owned());
    }
    if let Some(options) = volume
        .and_then(|volume| volume.get("driver_opts"))
        .and_then(Value::as_object)
    {
        let mut pairs = options
            .iter()
            .filter_map(|(key, value)| value.as_str().map(|value| (key.clone(), value.to_owned())))
            .collect::<Vec<_>>();
        pairs.sort();
        for (key, value) in pairs {
            argv.push("--opt".to_owned());
            argv.push(format!("{key}={value}"));
        }
    }
    for (key, value) in &labels {
        argv.push("--label".to_owned());
        argv.push(format!("{key}={value}"));
    }
    argv.push(name.to_owned());
    let output = run_checked(&argv)?;
    if let Claim::Volume(claim) = &mut ledger.claims[index] {
        claim.owned = true;
    }
    if output.stdout.trim() != name {
        let _ = run_checked(&[
            "docker".to_owned(),
            "volume".to_owned(),
            "rm".to_owned(),
            name.to_owned(),
        ]);
        return Err(SecurityError::runtime(format!(
            "Docker created an unexpected volume: {}",
            output.stdout.trim()
        )));
    }
    Ok(())
}

fn claim_rebuild_path(
    target: &str,
    source_type: &str,
    ancestors: &[InventoryAncestor],
    ledger: &mut RollbackLedger,
) -> Result<(), SecurityError> {
    let metadata = ancestors
        .iter()
        .map(|ancestor| crate::unix::data::Ancestor {
            path: ancestor.path.clone(),
            uid: ancestor.uid,
            gid: ancestor.gid,
            mode: ancestor.mode,
        })
        .collect::<Vec<_>>();
    let mut created = Vec::new();
    let (parent, name) = open_data_parent(target, Some(&metadata), &mut Some(&mut created))?;
    for (path, dev, ino) in created {
        ledger
            .claims
            .push(Claim::Ancestor(AncestorClaim { path, dev, ino }));
    }
    let result = if source_type == "directory" {
        mkdirat(&parent, name.as_str(), Mode::from_bits_truncate(0o700))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        openat(
            &parent,
            name.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))
    } else {
        openat(
            &parent,
            name.as_str(),
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_bits_truncate(0o600),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))
    };
    let descriptor = result?;
    let stat = fstat(&descriptor).map_err(|error| SecurityError::runtime(error.to_string()))?;
    let state = object_state_fd(&descriptor, target)?;
    ledger.claims.push(Claim::Data(DataClaim {
        path: target.to_owned(),
        dev: stat.st_dev,
        ino: stat.st_ino,
        state: Some(state),
        owned: true,
    }));
    fsync(&parent).map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(())
}

fn rollback(ledger: &mut RollbackLedger) -> Result<(), SecurityError> {
    while let Some(claim) = ledger.claims.pop() {
        let label = claim_label(&claim);
        if let Err(error) = rollback_claim(claim) {
            eprintln!("WARNING: remove rebuild target {label}: {error}");
        }
    }
    Ok(())
}

fn rollback_claim(claim: Claim) -> Result<(), SecurityError> {
    match claim {
        Claim::Volume(claim) => {
            if !claim.owned {
                return Ok(());
            }
            if volume_owned(&claim.name, &claim.operation_id)? {
                run_checked(&[
                    "docker".to_owned(),
                    "volume".to_owned(),
                    "rm".to_owned(),
                    claim.name,
                ])?;
            } else {
                eprintln!(
                    "WARNING: preserving rebuild volume whose ownership changed: {}",
                    claim.name
                );
            }
        }
        Claim::Ancestor(claim) => {
            let parent = parent_path(&claim.path);
            let name = file_name(&claim.path);
            let Ok(parent_fd) = open_parent(&parent) else {
                return Ok(());
            };
            let Ok(metadata) = statat(&parent_fd, name.as_str(), AtFlags::SYMLINK_NOFOLLOW) else {
                return Ok(());
            };
            if metadata.st_dev != claim.dev || metadata.st_ino != claim.ino {
                eprintln!(
                    "WARNING: preserving rebuild ancestor whose ownership changed: {}",
                    claim.path
                );
                return Ok(());
            }
            match unlinkat(&parent_fd, name.as_str(), AtFlags::REMOVEDIR) {
                Ok(()) => {
                    let _ = fsync(&parent_fd);
                }
                Err(Errno::NOTEMPTY) | Err(Errno::EXIST) => {
                    eprintln!(
                        "WARNING: preserving non-empty rebuild ancestor: {}",
                        claim.path
                    );
                }
                Err(error) => return Err(SecurityError::runtime(error.to_string())),
            }
        }
        Claim::Data(claim) => {
            if !claim.owned {
                return Ok(());
            }
            let parent = parent_path(&claim.path);
            let name = file_name(&claim.path);
            let Ok(parent_fd) = open_parent(&parent) else {
                return Ok(());
            };
            let Ok(metadata) = statat(&parent_fd, name.as_str(), AtFlags::SYMLINK_NOFOLLOW) else {
                return Ok(());
            };
            if metadata.st_dev != claim.dev || metadata.st_ino != claim.ino {
                eprintln!(
                    "WARNING: preserving rebuild target whose ownership changed: {}",
                    claim.path
                );
                return Ok(());
            }
            if let Some(expected) = &claim.state {
                let current = object_state_path(&claim.path)?;
                if &current != expected {
                    eprintln!(
                        "WARNING: preserving rebuild target modified after publication: {}",
                        claim.path
                    );
                    return Ok(());
                }
            }
            remove_entry(&parent_fd, &name)?;
        }
        Claim::Control(claim) => {
            if !claim.owned {
                return Ok(());
            }
            let Ok(metadata) = std::fs::symlink_metadata(&claim.path) else {
                return Ok(());
            };
            if metadata.dev() != claim.dev || metadata.ino() != claim.ino {
                eprintln!(
                    "WARNING: preserving rebuild target whose ownership changed: {}",
                    claim.path
                );
                return Ok(());
            }
            clear_control_leaf(&claim.path)?;
        }
    }
    Ok(())
}

fn volume_owned(name: &str, operation_id: &str) -> Result<bool, SecurityError> {
    let output = run_checked(&[
        "docker".to_owned(),
        "volume".to_owned(),
        "inspect".to_owned(),
        name.to_owned(),
    ])?;
    let values: Vec<Value> = serde_json::from_str(&output.stdout).unwrap_or_default();
    let label = values
        .first()
        .and_then(|value| value.get("Labels"))
        .and_then(|labels| labels.get("io.homelab-backup.operation"))
        .and_then(Value::as_str);
    Ok(label == Some(operation_id))
}

fn claim_label(claim: &Claim) -> String {
    match claim {
        Claim::Volume(claim) => claim.name.clone(),
        Claim::Ancestor(claim) => claim.path.clone(),
        Claim::Data(claim) => claim.path.clone(),
        Claim::Control(claim) => claim.path.clone(),
    }
}

fn ancestor_metadata(
    source: &BTreeMap<String, Yaml>,
    inventory: &Value,
) -> Vec<crate::unix::data::Ancestor> {
    let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
    inventory
        .get("paths")
        .and_then(Value::as_array)
        .and_then(|items| {
            items
                .iter()
                .find(|item| item.get("id").and_then(Value::as_str) == Some(source_id))
        })
        .and_then(|entry| entry.get("ancestors"))
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    Some(crate::unix::data::Ancestor {
                        path: item.get("path")?.as_str()?.to_owned(),
                        uid: item.get("uid")?.as_u64()? as u32,
                        gid: item.get("gid")?.as_u64()? as u32,
                        mode: item.get("mode")?.as_u64()? as u32,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn open_data_parent(
    path: &str,
    create_metadata: Option<&[crate::unix::data::Ancestor]>,
    created: &mut Option<&mut Vec<(String, u64, u64)>>,
) -> Result<(OwnedFd, String), SecurityError> {
    let absolute = homelab_backup_domain::paths::lexical_absolute(path).map_err(invalid)?;
    if absolute == "/" {
        return Err(SecurityError::invalid(
            "data path must not be the filesystem root: /".to_owned(),
        ));
    }
    let parts = absolute
        .split('/')
        .filter(|part| !part.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut current = openat(
        rustix::fs::CWD,
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    for index in 0..parts.len().saturating_sub(1) {
        let relative = parts[..=index].join("/");
        let component = &parts[index];
        match statat(&current, component.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
            Ok(metadata) => {
                if !is_dir(mode_of(&metadata)) {
                    return Err(SecurityError::invalid(format!(
                        "data path component is not a real directory: /{relative}"
                    )));
                }
                let next = openat(
                    &current,
                    component.as_str(),
                    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                    Mode::empty(),
                )
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
                let opened =
                    fstat(&next).map_err(|error| SecurityError::runtime(error.to_string()))?;
                if metadata.st_dev != opened.st_dev || metadata.st_ino != opened.st_ino {
                    return Err(SecurityError::runtime(format!(
                        "data parent changed while being opened: {absolute}"
                    )));
                }
                current = next;
            }
            Err(Errno::NOENT) => {
                let record = expected_ancestor(create_metadata.unwrap_or(&[]), &relative);
                let Some(record) = record else {
                    return Err(SecurityError::runtime(format!(
                        "snapshot lacks metadata for missing data ancestor: /{relative}"
                    )));
                };
                mkdirat(
                    &current,
                    component.as_str(),
                    Mode::from_bits_truncate(0o700),
                )
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
                let next = openat(
                    &current,
                    component.as_str(),
                    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                    Mode::empty(),
                )
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
                fchown(
                    &next,
                    Some(Uid::from_raw(record.uid)),
                    Some(Gid::from_raw(record.gid)),
                )
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
                fchmod(&next, Mode::from_bits_truncate(record.mode))
                    .map_err(|error| SecurityError::runtime(error.to_string()))?;
                fsync(&next).map_err(|error| SecurityError::runtime(error.to_string()))?;
                fsync(&current).map_err(|error| SecurityError::runtime(error.to_string()))?;
                let opened =
                    fstat(&next).map_err(|error| SecurityError::runtime(error.to_string()))?;
                if let Some(created) = created.as_mut() {
                    created.push((format!("/{relative}"), opened.st_dev, opened.st_ino));
                }
                current = next;
            }
            Err(error) => return Err(SecurityError::runtime(error.to_string())),
        }
    }
    let name = parts.last().cloned().unwrap_or_default();
    Ok((current, name))
}

fn expected_ancestor<'a>(
    metadata: &'a [crate::unix::data::Ancestor],
    relative: &str,
) -> Option<&'a crate::unix::data::Ancestor> {
    if let Some(exact) = metadata
        .iter()
        .find(|item| item.path == relative || item.path == format!("/{relative}"))
    {
        return Some(exact);
    }
    let relative_parts = relative
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    metadata
        .iter()
        .filter(|item| {
            let parts = item
                .path
                .trim_start_matches('/')
                .split('/')
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>();
            parts.len() <= relative_parts.len() && relative_parts.ends_with(parts.as_slice())
        })
        .max_by_key(|item| item.path.len())
}

fn normalize_restore_target(
    parent: &OwnedFd,
    name: &str,
    source_type: &str,
) -> Result<(), SecurityError> {
    let metadata = match statat(parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(metadata) => metadata,
        Err(Errno::NOENT) => return Ok(()),
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    let mode = mode_of(&metadata);
    let compatible = (source_type == "directory" && is_dir(mode))
        || (source_type == "file" && is_reg(mode))
        || (source_type == "symlink" && is_lnk(mode));
    if !compatible {
        remove_entry(parent, name)?;
    }
    Ok(())
}

fn restore_non_directory(
    restored: &Path,
    parent: &OwnedFd,
    name: &str,
    on_publish: &mut Option<&mut dyn FnMut((u64, u64), ObjectState)>,
) -> Result<(), SecurityError> {
    let temporary = reserve_directory(parent)?;
    let copied = (|| {
        let argv = vec![
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            restored.display().to_string(),
            format!("{}/payload", temporary.1),
        ];
        run_checked(&argv)?;
        let payload = format!("{}/payload", temporary.1);
        let metadata = std::fs::symlink_metadata(&payload)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        renameat(parent, file_name(&payload).as_str(), parent, name)
            .or_else(|_| {
                // The payload lives inside the temporary directory, not the parent.
                renameat(&temporary.0, "payload", parent, name)
            })
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let identity = (metadata.dev(), metadata.ino());
        if let Some(callback) = on_publish.as_mut() {
            let state = object_state_path(&format!("{}/{}", parent_display(parent), name))
                .unwrap_or_else(|_| file_state(&metadata));
            callback(identity, state);
        }
        let _ = fsync(parent);
        Ok(())
    })();
    let _ = unlinkat(&temporary.0, "payload", AtFlags::empty());
    let _ = unlinkat(parent, file_name(&temporary.1).as_str(), AtFlags::REMOVEDIR);
    copied
}

fn restore_rebuild_directory(
    restored: &Path,
    parent: &OwnedFd,
    name: &str,
    on_publish: &mut Option<&mut dyn FnMut((u64, u64), ObjectState)>,
) -> Result<(), SecurityError> {
    let temporary = reserve_directory(parent)?;
    let copied = (|| {
        let argv = vec![
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--delete".to_owned(),
            format!("{}/", restored.display()),
            format!("{}/", temporary.1),
        ];
        run_checked(&argv)?;
        let metadata =
            fstat(&temporary.0).map_err(|error| SecurityError::runtime(error.to_string()))?;
        renameat(parent, file_name(&temporary.1).as_str(), parent, name)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        if let Some(callback) = on_publish.as_mut() {
            let state = object_state_fd(&temporary.0, name)?;
            callback((metadata.st_dev, metadata.st_ino), state);
        }
        let _ = fsync(parent);
        Ok(())
    })();
    if copied.is_err() {
        let _ = remove_entry(parent, &file_name(&temporary.1));
    }
    copied
}

fn restore_existing_directory(
    source: &BTreeMap<String, Yaml>,
    restored: &Path,
    parent: &OwnedFd,
    name: &str,
    on_publish: &mut Option<&mut dyn FnMut((u64, u64), ObjectState)>,
) -> Result<(), SecurityError> {
    if let Err(Errno::EXIST) = mkdirat(parent, name, Mode::from_bits_truncate(0o700)) {
    } else if false {
        return Err(SecurityError::runtime(
            "cannot create restore directory".to_owned(),
        ));
    }
    let _ = mkdirat(parent, name, Mode::from_bits_truncate(0o700));
    let target = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let include = string_list(source, "include");
    let exclude = string_list(source, "exclude");
    let mut argv = vec![
        "rsync".to_owned(),
        "-aHAX".to_owned(),
        "--numeric-ids".to_owned(),
        "--delete".to_owned(),
        "--one-file-system".to_owned(),
    ];
    argv.extend(build_path_filter_args(&exclude, &include, true));
    let destination = directory_path(&target, name);
    argv.push(format!("{}/", restored.display()));
    argv.push(format!("{destination}/"));
    run_checked(&argv)?;
    let metadata = fstat(&target).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if let Some(callback) = on_publish.as_mut() {
        callback(
            (metadata.st_dev, metadata.st_ino),
            object_state_fd(&target, name)?,
        );
    }
    Ok(())
}

fn reserve_directory(parent: &OwnedFd) -> Result<(OwnedFd, String), SecurityError> {
    for _ in 0..16 {
        let candidate = format!(".backupctl-restore-{}", token_hex(8));
        match mkdirat(parent, candidate.as_str(), Mode::from_bits_truncate(0o700)) {
            Ok(()) => {}
            Err(Errno::EXIST) => continue,
            Err(error) => return Err(SecurityError::runtime(error.to_string())),
        }
        let initial = statat(parent, candidate.as_str(), AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let fd = openat(
            parent,
            candidate.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let opened = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
        if initial.st_dev != opened.st_dev || initial.st_ino != opened.st_ino {
            return Err(SecurityError::runtime(
                "restore publication directory changed while being opened".to_owned(),
            ));
        }
        let path = directory_path(&fd, &candidate);
        return Ok((fd, path));
    }
    Err(SecurityError::runtime(
        "cannot reserve a restore publication directory".to_owned(),
    ))
}

fn directory_path(fd: &OwnedFd, fallback: &str) -> String {
    std::fs::read_link(format!("/proc/self/fd/{}", rustix_raw(fd)))
        .unwrap_or_else(|_| PathBuf::from(fallback))
        .display()
        .to_string()
}

fn rustix_raw(fd: &OwnedFd) -> i32 {
    use std::os::fd::AsRawFd;
    fd.as_raw_fd()
}

fn parent_display(_parent: &OwnedFd) -> String {
    String::new()
}

fn object_state_fd(fd: &OwnedFd, display: &str) -> Result<ObjectState, SecurityError> {
    let metadata = fstat(fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    let mut state = stat_state(&metadata);
    if is_dir(mode_of(&metadata)) {
        let path = directory_path(fd, display);
        if let Ok(entries) = std::fs::read_dir(&path) {
            let mut names = entries
                .filter_map(|entry| entry.ok())
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
                .collect::<Vec<_>>();
            names.sort();
            for name in names {
                state
                    .children
                    .push((name.clone(), object_state_path(&format!("{path}/{name}"))?));
            }
        }
    }
    Ok(state)
}

fn object_state_path(path: &str) -> Result<ObjectState, SecurityError> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| SecurityError::runtime(format!("{path}: {error}")))?;
    let mut state = file_state(&metadata);
    if metadata.is_dir() {
        let mut names = std::fs::read_dir(path)
            .map_err(|error| SecurityError::runtime(error.to_string()))?
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        names.sort();
        for name in names {
            state
                .children
                .push((name.clone(), object_state_path(&format!("{path}/{name}"))?));
        }
    }
    Ok(state)
}

fn file_state(metadata: &std::fs::Metadata) -> ObjectState {
    ObjectState {
        dev: metadata.dev(),
        ino: metadata.ino(),
        kind: metadata.mode() & 0o170000,
        uid: metadata.uid(),
        gid: metadata.gid(),
        perm: metadata.mode() & 0o7777,
        size: metadata.size(),
        mtime: metadata.mtime(),
        ctime: metadata.ctime(),
        children: Vec::new(),
    }
}

fn stat_state(metadata: &rustix::fs::Stat) -> ObjectState {
    ObjectState {
        dev: metadata.st_dev,
        ino: metadata.st_ino,
        kind: mode_of(metadata) & 0o170000,
        uid: metadata.st_uid,
        gid: metadata.st_gid,
        perm: mode_of(metadata) & 0o7777,
        size: metadata.st_size as u64,
        mtime: metadata.st_mtime,
        ctime: metadata.st_ctime,
        children: Vec::new(),
    }
}

fn remove_entry(parent: &OwnedFd, name: &str) -> Result<(), SecurityError> {
    let metadata = match statat(parent, name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(metadata) => metadata,
        Err(Errno::NOENT) => return Ok(()),
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    if is_dir(mode_of(&metadata)) {
        let child = openat(
            parent,
            name,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let path = directory_path(&child, name);
        if let Ok(entries) = std::fs::read_dir(&path) {
            for entry in entries.filter_map(|entry| entry.ok()) {
                let child_name = entry.file_name().to_string_lossy().into_owned();
                remove_entry(&child, &child_name)?;
            }
        }
        unlinkat(parent, name, AtFlags::REMOVEDIR)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
    } else {
        unlinkat(parent, name, AtFlags::empty())
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
    }
    let _ = fsync(parent);
    Ok(())
}

fn open_parent(path: &str) -> Result<OwnedFd, SecurityError> {
    openat(
        rustix::fs::CWD,
        path,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))
}

fn compose_args(
    manifest: &BTreeMap<String, Yaml>,
    command: &[String],
    targets: &[String],
) -> Result<Vec<String>, SecurityError> {
    let mut argv = crate::volumes::compose_argv(manifest)?;
    argv.extend(command.iter().cloned());
    argv.extend(targets.iter().cloned());
    Ok(argv)
}

fn compose_files_exist(manifest: &BTreeMap<String, Yaml>) -> bool {
    let directory = manifest.get("_dir").and_then(Yaml::as_str).unwrap_or("");
    crate::restore_inventory::compose_file_names(manifest)
        .iter()
        .all(|file| exists_or_link(&Path::new(directory).join(file)))
}

fn path_source_maps(manifest: &BTreeMap<String, Yaml>) -> Vec<&BTreeMap<String, Yaml>> {
    manifest
        .get("sources")
        .and_then(Yaml::as_map)
        .and_then(|sources| sources.get("paths"))
        .and_then(Yaml::as_seq)
        .map(|items| items.iter().filter_map(Yaml::as_map).collect())
        .unwrap_or_default()
}

fn string_list(source: &BTreeMap<String, Yaml>, key: &str) -> Vec<String> {
    source
        .get(key)
        .and_then(Yaml::as_seq)
        .map(|items| {
            items
                .iter()
                .filter_map(Yaml::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn config_strings() -> BTreeMap<String, String> {
    BTreeMap::new()
}

fn cleanup(action: impl FnOnce() -> Result<(), SecurityError>, context: &str) {
    if let Err(error) = action() {
        eprintln!("WARNING: {context}: {error}");
    }
}

fn token_hex(bytes: usize) -> String {
    let mut buffer = vec![0_u8; bytes];
    if let Ok(mut file) = File::open("/dev/urandom") {
        let _ = file.read_exact(&mut buffer);
    }
    buffer.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn exists_or_link(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

fn is_real_file(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|metadata| metadata.is_file())
        .unwrap_or(false)
}

fn is_real_dir(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|metadata| metadata.is_dir())
        .unwrap_or(false)
}

fn is_real_link(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|metadata| metadata.file_type().is_symlink())
        .unwrap_or(false)
}

fn invalid(error: impl ToString) -> SecurityError {
    SecurityError::invalid(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::restore_plan::{RuntimeHooks, install_runtime};

    fn temp_base(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "homelab-apply-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn absent_optional_path_does_not_run_a_command() {
        let root = temp_base("absent");
        let restore = root.join("restore");
        std::fs::create_dir_all(&restore).unwrap();
        let target = root.join("target");
        let mut source = BTreeMap::new();
        source.insert("id".to_owned(), Yaml::string("data"));
        source.insert(
            "path".to_owned(),
            Yaml::string(target.display().to_string()),
        );
        source.insert("required".to_owned(), Yaml::Bool(false));
        let mut manifest = BTreeMap::new();
        manifest.insert("_dir".to_owned(), Yaml::string(root.display().to_string()));
        let inventory = serde_json::json!({
            "paths": [{
                "id": "data",
                "path": target.display().to_string(),
                "type": "file",
                "present": false,
            }]
        });
        let _guard = install_runtime(RuntimeHooks {
            volume_exists: None,
            project_containers: None,
            mount_conflicts: None,
            running_services: None,
            compose_model: None,
            local_manifest: None,
            run_command: Some(Box::new(|_| {
                panic!("command should not run");
            })),
        });
        restore_path_source(
            &manifest,
            &restore,
            &source,
            &inventory,
            RestoreMode::Direct,
        )
        .unwrap();
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn rebuild_recreates_missing_ancestors_and_refuses_to_guess() {
        let base = temp_base("ancestors");
        let restored = base.join("restore");
        let staged = restored.join("paths").join("saved");
        std::fs::create_dir_all(&staged).unwrap();
        std::fs::write(staged.join("world.sav"), "snapshot").unwrap();
        let target = base.join("palworld").join("Pal").join("Saved");
        let mut source = BTreeMap::new();
        source.insert("id".to_owned(), Yaml::string("saved"));
        source.insert(
            "path".to_owned(),
            Yaml::string(target.display().to_string()),
        );
        let mut manifest = BTreeMap::new();
        manifest.insert("_dir".to_owned(), Yaml::string(base.display().to_string()));
        let uid = rustix::process::geteuid().as_raw();
        let gid = rustix::process::getegid().as_raw();
        let inventory = serde_json::json!({
            "paths": [{
                "id": "saved",
                "path": target.display().to_string(),
                "type": "directory",
                "present": true,
                "ancestors": [
                    {"path": format!("{}/palworld", base.display()), "mode": 0o750, "uid": uid, "gid": gid},
                    {"path": format!("{}/palworld/Pal", base.display()), "mode": 0o770, "uid": uid, "gid": gid}
                ]
            }]
        });
        let _guard = install_runtime(RuntimeHooks {
            volume_exists: None,
            project_containers: None,
            mount_conflicts: None,
            running_services: None,
            compose_model: None,
            local_manifest: None,
            run_command: Some(Box::new({
                let staged = staged.clone();
                move |argv| {
                    assert_eq!(argv[0], "rsync");
                    let destination = argv.last().unwrap().trim_end_matches('/');
                    copy_dir(&staged, Path::new(destination))?;
                    Ok(crate::restore_plan::CommandOutput {
                        code: 0,
                        stdout: String::new(),
                        stderr: String::new(),
                    })
                }
            })),
        });
        restore_path_source(
            &manifest,
            &restored,
            &source,
            &inventory,
            RestoreMode::Rebuild,
        )
        .unwrap();
        let palworld = std::fs::symlink_metadata(base.join("palworld")).unwrap();
        let pal = std::fs::symlink_metadata(base.join("palworld").join("Pal")).unwrap();
        assert_eq!(palworld.mode() & 0o777, 0o750);
        assert_eq!(pal.mode() & 0o777, 0o770);
        assert_eq!(
            std::fs::read_to_string(target.join("world.sav")).unwrap(),
            "snapshot"
        );
        let bare = serde_json::json!({
            "paths": [{
                "id": "saved",
                "path": target.display().to_string(),
                "type": "directory",
                "present": true
            }]
        });
        let missing = base.join("other").join("Saved");
        source.insert(
            "path".to_owned(),
            Yaml::string(missing.display().to_string()),
        );
        let error = restore_path_source(&manifest, &restored, &source, &bare, RestoreMode::Rebuild)
            .unwrap_err();
        assert!(error.message.contains("lacks metadata"), "{error}");
        let _ = std::fs::remove_dir_all(&base);
    }

    fn copy_dir(source: &Path, destination: &Path) -> Result<(), SecurityError> {
        std::fs::create_dir_all(destination)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        for entry in
            std::fs::read_dir(source).map_err(|error| SecurityError::runtime(error.to_string()))?
        {
            let entry = entry.map_err(|error| SecurityError::runtime(error.to_string()))?;
            let target = destination.join(entry.file_name());
            if entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false) {
                copy_dir(&entry.path(), &target)?;
            } else {
                std::fs::copy(entry.path(), target)
                    .map_err(|error| SecurityError::runtime(error.to_string()))?;
            }
        }
        Ok(())
    }
}
