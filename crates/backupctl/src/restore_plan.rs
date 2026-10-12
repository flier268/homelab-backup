//! Decide whether a downloaded snapshot may be applied.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::Path;
use std::process::Command;

use homelab_backup_domain::manifest::{requires_docker, source_path, validate_manifest};
use homelab_backup_domain::paths::{join_service_path, lexical_absolute};
use homelab_backup_domain::yaml::{Yaml, python_str};
use serde_json::{Map, Value};

use crate::catalog::select_every_manifest;
use crate::restore_inventory::{
    RestoreInventory, compose_file_names, load_restore_inventory, read_unprotected_text,
    validate_restore_inventory, validate_restore_path_separation, validate_restore_sources,
};
use crate::storage::validate_path_source_policy;
use crate::unix::data::CapturedCommand;
use crate::unix::docker::docker_mount_users;
use crate::unix::error::SecurityError;

#[derive(Debug, Clone)]
pub struct RestorePlan {
    pub root: std::path::PathBuf,
    pub inventory: RestoreInventory,
    pub manifest: BTreeMap<String, Yaml>,
    pub mode: String,
    pub volumes: Vec<(BTreeMap<String, Yaml>, String)>,
    pub all_volume_names: Vec<String>,
    pub running_services: Vec<String>,
    pub deferred_sources: Vec<String>,
    pub project_name: String,
}

pub struct CommandOutput {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub(crate) type BoolHook = Box<dyn FnMut(&str) -> Result<bool, SecurityError>>;
pub(crate) type ContainerHook = Box<dyn FnMut(&str, bool) -> Result<Vec<String>, SecurityError>>;
pub(crate) type ConflictHook =
    Box<dyn FnMut(&[String], &[String], bool) -> Result<Vec<String>, SecurityError>>;
pub(crate) type ManifestStrings =
    Box<dyn FnMut(&BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError>>;
pub(crate) type ManifestModel =
    Box<dyn FnMut(&BTreeMap<String, Yaml>) -> Result<Value, SecurityError>>;
pub(crate) type CommandHook = Box<dyn FnMut(&[String]) -> Result<CommandOutput, SecurityError>>;
pub(crate) type VolumeSource = (BTreeMap<String, Yaml>, String);
type InventoryVolumeRow = (BTreeMap<String, Yaml>, String, bool);
type LoadedCompose = (BTreeMap<String, Yaml>, Value, Vec<String>);

#[derive(Default)]
struct Hooks {
    volume_exists: Option<BoolHook>,
    project_containers: Option<ContainerHook>,
    mount_conflicts: Option<ConflictHook>,
    running_services: Option<ManifestStrings>,
    compose_model: Option<ManifestModel>,
    local_manifest: Option<BTreeMap<String, Yaml>>,
    use_local_manifest: bool,
    run_command: Option<CommandHook>,
}

thread_local! {
    static HOOKS: RefCell<Hooks> = RefCell::new(Hooks::default());
}

pub struct RuntimeGuard;

impl Drop for RuntimeGuard {
    fn drop(&mut self) {
        HOOKS.with(|hooks| *hooks.borrow_mut() = Hooks::default());
    }
}

pub struct RuntimeHooks {
    pub volume_exists: Option<BoolHook>,
    pub project_containers: Option<ContainerHook>,
    pub mount_conflicts: Option<ConflictHook>,
    pub running_services: Option<ManifestStrings>,
    pub compose_model: Option<ManifestModel>,
    pub local_manifest: Option<BTreeMap<String, Yaml>>,
    pub run_command: Option<CommandHook>,
}

pub fn install_runtime(hooks: RuntimeHooks) -> RuntimeGuard {
    HOOKS.with(|slot| {
        *slot.borrow_mut() = Hooks {
            volume_exists: hooks.volume_exists,
            project_containers: hooks.project_containers,
            mount_conflicts: hooks.mount_conflicts,
            running_services: hooks.running_services,
            compose_model: hooks.compose_model,
            use_local_manifest: hooks.local_manifest.is_some(),
            local_manifest: hooks.local_manifest,
            run_command: hooks.run_command,
        };
    });
    RuntimeGuard
}

pub fn prepare_restore_plan(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
) -> Result<RestorePlan, SecurityError> {
    let (root, inventory) = load_and_validate_restore_input(manifest, root)?;
    let authorized = if config_string(config, "services_root").is_some() {
        local_manifest(config, manifest_service(manifest)?)?
    } else {
        manifest.clone()
    };
    validate_manifest(&Yaml::Map(authorized.clone())).map_err(invalid)?;
    validate_restore_inventory(&authorized, &inventory.to_snapshot_dict())?;
    if config_string(config, "services_root").is_some() {
        let snapshot = snapshot_manifest(manifest, Some(&root))?;
        if restore_filter_projection(&snapshot) != restore_filter_projection(&authorized) {
            return Err(SecurityError::runtime(
                "snapshot path filters differ from local authorization".to_owned(),
            ));
        }
    }
    let files_mode = !requires_docker(&authorized);
    let (mode, compose_file_targets) = deployment_mode(
        &authorized,
        &inventory,
        config_string(config, "services_root").is_some(),
    )?;
    let deferred = if files_mode {
        Vec::new()
    } else {
        validate_rebuild_compose_sources(&authorized, &mode, &compose_file_targets)?
    };
    let restored_volumes = inventory_volumes(&authorized, &inventory)?;
    let _targets = validate_path_targets(config, &authorized, &inventory, &mode)?;
    if files_mode {
        let authorized_manifest = if mode == "existing" {
            authorize_existing_files_restore(config, manifest, &inventory)?
        } else {
            authorized
        };
        return Ok(RestorePlan {
            root,
            inventory,
            manifest: authorized_manifest,
            mode,
            volumes: Vec::new(),
            all_volume_names: Vec::new(),
            running_services: Vec::new(),
            deferred_sources: Vec::new(),
            project_name: String::new(),
        });
    }
    let (authorized_manifest, identity, running) = if mode == "existing" {
        authorize_existing_restore(config, manifest, &inventory, &restored_volumes)?
    } else {
        authorize_rebuild_restore(&authorized, &inventory, &restored_volumes)?
    };
    let volumes = restored_volumes
        .iter()
        .filter(|(_, _, present)| *present)
        .map(|(source, name, _)| (source.clone(), name.clone()))
        .collect::<Vec<_>>();
    let all_volume_names = restored_volumes
        .iter()
        .map(|(_, name, _)| name.clone())
        .collect::<Vec<_>>();
    validate_restore_conflicts(&mode, &volumes, &all_volume_names)?;
    Ok(RestorePlan {
        root,
        inventory,
        manifest: authorized_manifest,
        mode,
        volumes,
        all_volume_names,
        running_services: running,
        deferred_sources: deferred,
        project_name: identity_project_name(&identity),
    })
}

pub fn deferred_compose_sources(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<Vec<String>, SecurityError> {
    let targets = compose_control_paths(manifest)?;
    let mut sources = Vec::new();
    for source in path_sources(manifest) {
        let target =
            lexical_absolute(&source_path(manifest, source).map_err(invalid)?).map_err(invalid)?;
        if targets.iter().any(|control| control == &target) {
            sources.push(
                source
                    .get("id")
                    .and_then(Yaml::as_str)
                    .unwrap_or("")
                    .to_owned(),
            );
        }
    }
    Ok(sources)
}

pub fn restore_authorization_projection(manifest: &BTreeMap<String, Yaml>) -> Value {
    let sources = manifest.get("sources").and_then(Yaml::as_map);
    Value::Object(Map::from_iter([
        ("service".to_owned(), yaml_to_json(manifest.get("service"))),
        (
            "compose_files".to_owned(),
            Value::Array(
                compose_file_names(manifest)
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            ),
        ),
        (
            "consistency".to_owned(),
            Value::Object(Map::from_iter([(
                "mode".to_owned(),
                Value::String(
                    manifest
                        .get("consistency")
                        .and_then(Yaml::as_map)
                        .and_then(|map| map.get("mode"))
                        .and_then(Yaml::as_str)
                        .unwrap_or("stop")
                        .to_owned(),
                ),
            )])),
        ),
        (
            "restore_actions".to_owned(),
            restore_actions_projection(manifest),
        ),
        (
            "paths".to_owned(),
            project_sources(
                sources,
                "paths",
                &["id", "path", "required", "include", "exclude"],
            ),
        ),
        (
            "volumes".to_owned(),
            project_sources(
                sources,
                "volumes",
                &["id", "name", "compose_volume", "required", "exclude"],
            ),
        ),
    ]))
}

pub fn restore_filter_projection(manifest: &BTreeMap<String, Yaml>) -> Value {
    let mut map = Map::new();
    for source in path_sources(manifest) {
        let id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        map.insert(
            id.to_owned(),
            Value::Object(Map::from_iter([
                ("include".to_owned(), string_list(source.get("include"))),
                ("exclude".to_owned(), string_list(source.get("exclude"))),
            ])),
        );
    }
    Value::Object(map)
}

pub fn inventory_volumes(
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
) -> Result<Vec<InventoryVolumeRow>, SecurityError> {
    let sources = volume_sources(manifest);
    let mut values = Vec::new();
    for entry in &inventory.volumes {
        let Some(source) = sources
            .iter()
            .find(|source| source.get("id").and_then(Yaml::as_str) == Some(entry.id.as_str()))
        else {
            return Err(SecurityError::runtime(format!(
                "restore inventory has no volume source {}",
                python_str(&entry.id)
            )));
        };
        values.push(((*source).clone(), entry.actual_name.clone(), entry.present));
    }
    Ok(values)
}

pub(crate) fn volume_exists(name: &str) -> Result<bool, SecurityError> {
    let hooked = HOOKS.with(|hooks| hooks.borrow().volume_exists.is_some());
    if hooked {
        return HOOKS.with(|hooks| hooks.borrow_mut().volume_exists.as_mut().expect("hook")(name));
    }
    crate::volumes::docker_volume_exists(name)
}

pub(crate) fn project_containers(
    project: &str,
    include_stopped: bool,
) -> Result<Vec<String>, SecurityError> {
    let hooked = HOOKS.with(|hooks| hooks.borrow().project_containers.is_some());
    if hooked {
        return HOOKS.with(|hooks| {
            hooks
                .borrow_mut()
                .project_containers
                .as_mut()
                .expect("hook")(project, include_stopped)
        });
    }
    let mut argv = vec!["docker".to_owned(), "ps".to_owned()];
    if include_stopped {
        argv.push("-a".to_owned());
    }
    argv.extend([
        "-q".to_owned(),
        "--filter".to_owned(),
        format!("label=com.docker.compose.project={project}"),
    ]);
    let output = run_checked(&argv)?;
    Ok(output
        .stdout
        .split_whitespace()
        .map(str::to_owned)
        .collect())
}

pub(crate) fn mount_conflicts(
    targets: &[String],
    volumes: &[String],
    include_stopped: bool,
) -> Result<Vec<String>, SecurityError> {
    let hooked = HOOKS.with(|hooks| hooks.borrow().mount_conflicts.is_some());
    if hooked {
        return HOOKS.with(|hooks| {
            hooks.borrow_mut().mount_conflicts.as_mut().expect("hook")(
                targets,
                volumes,
                include_stopped,
            )
        });
    }
    let target_refs = targets.iter().map(String::as_str).collect::<Vec<_>>();
    let volume_refs = volumes.iter().map(String::as_str).collect::<Vec<_>>();
    docker_mount_users(
        &target_refs,
        &volume_refs,
        include_stopped,
        false,
        &mut |argv| match run_command(argv) {
            Ok(output) => CapturedCommand {
                code: output.code,
                stdout: output.stdout,
            },
            Err(error) => CapturedCommand {
                code: 1,
                stdout: error.message,
            },
        },
    )
}

pub(crate) fn plan_running_services(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<Vec<String>, SecurityError> {
    let hooked = HOOKS.with(|hooks| hooks.borrow().running_services.is_some());
    if hooked {
        return HOOKS
            .with(|hooks| hooks.borrow_mut().running_services.as_mut().expect("hook")(manifest));
    }
    crate::volumes::running_services(manifest)
}

pub(crate) fn plan_compose_model(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<Value, SecurityError> {
    let hooked = HOOKS.with(|hooks| hooks.borrow().compose_model.is_some());
    if hooked {
        return HOOKS
            .with(|hooks| hooks.borrow_mut().compose_model.as_mut().expect("hook")(manifest));
    }
    crate::volumes::compose_model(manifest)
}

pub(crate) fn run_command(argv: &[String]) -> Result<CommandOutput, SecurityError> {
    println!(
        "+ {}",
        argv.iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let hooked = HOOKS.with(|hooks| hooks.borrow().run_command.is_some());
    if hooked {
        return HOOKS.with(|hooks| hooks.borrow_mut().run_command.as_mut().expect("hook")(argv));
    }
    if argv.is_empty() {
        return Err(SecurityError::runtime("command is empty".to_owned()));
    }
    let output = Command::new(&argv[0])
        .args(&argv[1..])
        .output()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(CommandOutput {
        code: output.status.code().unwrap_or(1),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    })
}

pub(crate) fn run_checked(argv: &[String]) -> Result<CommandOutput, SecurityError> {
    let output = run_command(argv)?;
    if output.code != 0 {
        return Err(SecurityError::runtime(format!(
            "command failed with exit code {}",
            output.code
        )));
    }
    Ok(output)
}

fn load_and_validate_restore_input(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
) -> Result<(std::path::PathBuf, RestoreInventory), SecurityError> {
    validate_manifest(&Yaml::Map(manifest.clone())).map_err(invalid)?;
    if !root.is_dir() {
        return Err(SecurityError::runtime(format!(
            "restore directory does not exist: {}",
            root.display()
        )));
    }
    let service = manifest_service(manifest)?;
    let inventory = load_restore_inventory(root, Some(service))?;
    validate_restore_inventory(manifest, &inventory.to_snapshot_dict())?;
    validate_restore_sources(manifest, root, &inventory.to_snapshot_dict())?;
    validate_restore_path_separation(manifest, root, &inventory)?;
    Ok((root.to_path_buf(), inventory))
}

fn deployment_mode(
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
    require_local_manifest: bool,
) -> Result<(String, Vec<String>), SecurityError> {
    let manifest_target = manifest_target(manifest)?;
    if require_local_manifest && !is_real_file(Path::new(&manifest_target)) {
        return Err(SecurityError::runtime(
            "apply requires a protected local manifest; download the restore, review and install _meta/backup.yaml, run backupctl validate, then run backupctl apply"
                .to_owned(),
        ));
    }
    if !requires_docker(manifest) {
        if !require_local_manifest && !exists_or_link(Path::new(&manifest_target)) {
            return Ok(("rebuild".to_owned(), Vec::new()));
        }
        let sources = path_sources(manifest);
        let mut target_states = Vec::new();
        for entry in &inventory.paths {
            if !entry.present {
                continue;
            }
            let Some(source) = sources
                .iter()
                .find(|source| source.get("id").and_then(Yaml::as_str) == Some(entry.id.as_str()))
            else {
                continue;
            };
            let target = source_path(manifest, source).map_err(invalid)?;
            target_states.push(exists_or_link(Path::new(&target)));
        }
        if target_states.is_empty() || target_states.iter().all(|state| *state) {
            return Ok(("existing".to_owned(), Vec::new()));
        }
        if target_states.iter().all(|state| !*state) {
            return Ok(("rebuild".to_owned(), Vec::new()));
        }
        return Err(SecurityError::runtime(
            "mixed existing/missing path target state is not supported".to_owned(),
        ));
    }
    let compose_file_targets = compose_file_paths(manifest)?;
    let compose_controls = compose_control_paths(manifest)?;
    let manifest_exists = is_real_file(Path::new(&manifest_target));
    let compose_exists = compose_controls
        .iter()
        .map(|path| is_real_file(Path::new(path)))
        .collect::<Vec<_>>();
    if !require_local_manifest {
        if manifest_exists && compose_exists.iter().all(|state| *state) {
            return Ok(("existing".to_owned(), compose_file_targets));
        }
        if !exists_or_link(Path::new(&manifest_target))
            && compose_controls
                .iter()
                .all(|path| !exists_or_link(Path::new(path)))
        {
            return Ok(("rebuild".to_owned(), compose_file_targets));
        }
        return Err(SecurityError::runtime(
            "mixed local manifest/Compose state is not supported".to_owned(),
        ));
    }
    if compose_exists.iter().all(|state| *state) {
        return Ok(("existing".to_owned(), compose_file_targets));
    }
    if compose_file_targets
        .iter()
        .all(|path| !exists_or_link(Path::new(path)))
    {
        return Ok(("rebuild".to_owned(), compose_file_targets));
    }
    Err(SecurityError::runtime(
        "mixed local manifest/Compose state is not supported".to_owned(),
    ))
}

fn validate_rebuild_compose_sources(
    manifest: &BTreeMap<String, Yaml>,
    mode: &str,
    _compose_file_targets: &[String],
) -> Result<Vec<String>, SecurityError> {
    let deferred = deferred_compose_sources(manifest)?;
    let controls = compose_control_paths(manifest)?;
    let source_targets = path_sources(manifest)
        .iter()
        .map(|source| {
            lexical_absolute(&source_path(manifest, source).map_err(invalid)?).map_err(invalid)
        })
        .collect::<Result<Vec<_>, _>>()?;
    if mode == "rebuild"
        && controls
            .iter()
            .any(|target| !source_targets.iter().any(|source| source == target))
    {
        return Err(SecurityError::runtime(
            "every Compose file and env file must be a required snapshot path source".to_owned(),
        ));
    }
    if mode == "rebuild"
        && deferred.iter().any(|item| {
            path_sources(manifest).iter().any(|source| {
                source.get("id").and_then(Yaml::as_str) == Some(item.as_str())
                    && source.get("required").and_then(Yaml::as_bool) == Some(false)
            })
        })
    {
        return Err(SecurityError::runtime(
            "Compose file and env file path sources must be required for rebuild".to_owned(),
        ));
    }
    Ok(deferred)
}

fn validate_path_targets(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
    mode: &str,
) -> Result<Vec<String>, SecurityError> {
    let flat = flat_config(config);
    let mut targets = Vec::new();
    for source in path_sources(manifest) {
        let target = validate_path_source_policy(&flat, manifest, source)?;
        let entry = inventory
            .paths_by_id()
            .get(source.get("id").and_then(Yaml::as_str).unwrap_or(""))
            .copied()
            .ok_or_else(|| {
                SecurityError::runtime(format!(
                    "restore inventory has no path source {}",
                    python_str(source.get("id").and_then(Yaml::as_str).unwrap_or(""))
                ))
            })?;
        if mode == "rebuild" {
            validate_data_parent(&target, true)?;
        } else {
            validate_data_path(&target)?;
        }
        let exists = exists_or_link(Path::new(&target));
        if !entry.present {
            if mode == "rebuild" && exists {
                return Err(SecurityError::runtime(format!(
                    "snapshot-absent path already exists during rebuild: {target}"
                )));
            }
            targets.push(target);
            continue;
        }
        if mode == "existing" && !exists {
            return Err(SecurityError::runtime(format!(
                "existing deployment target is missing: {target}"
            )));
        }
        if mode == "rebuild" && exists {
            return Err(SecurityError::runtime(format!(
                "rebuild target already exists: {target}"
            )));
        }
        targets.push(target);
    }
    Ok(targets)
}

fn authorize_existing_restore(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
    restored_volumes: &[(BTreeMap<String, Yaml>, String, bool)],
) -> Result<LoadedCompose, SecurityError> {
    let local = if config_string(config, "services_root").is_some() {
        local_manifest(config, manifest_service(manifest)?)?
    } else {
        manifest.clone()
    };
    validate_manifest(&Yaml::Map(local.clone())).map_err(invalid)?;
    validate_restore_inventory(&local, &inventory.to_snapshot_dict())?;
    let snapshot = snapshot_manifest(manifest, None)?;
    if restore_filter_projection(&snapshot) != restore_filter_projection(&local) {
        return Err(SecurityError::runtime(
            "snapshot path filters differ from local authorization".to_owned(),
        ));
    }
    if manifest
        .get("_restore_manifest_requested")
        .and_then(Yaml::as_bool)
        == Some(true)
        && restore_authorization_projection(manifest) != restore_authorization_projection(&local)
    {
        return Err(SecurityError::runtime(
            "snapshot manifest security settings differ from local authorization".to_owned(),
        ));
    }
    let (identity, resolved) = if config_string(config, "services_root").is_some() {
        let model = plan_compose_model(&local)?;
        let resolved = crate::volumes::resolved_volumes(&local, &model)?;
        (
            compose_authorization_from_model(&local, &model, &resolved),
            resolved,
        )
    } else if let Some(compose) = &inventory.compose {
        let resolved = restored_volumes
            .iter()
            .map(|(source, name, _)| {
                (
                    source
                        .get("id")
                        .and_then(Yaml::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    source
                        .get("compose_volume")
                        .and_then(Yaml::as_str)
                        .map(str::to_owned),
                    name.clone(),
                )
            })
            .collect::<Vec<_>>();
        (compose_authorization_from_inventory(compose), resolved)
    } else {
        return Err(SecurityError::runtime(
            "restore inventory has no Compose identity".to_owned(),
        ));
    };
    if identity
        != compose_authorization_from_inventory(inventory.compose.as_ref().ok_or_else(|| {
            SecurityError::runtime("restore inventory has no Compose identity".to_owned())
        })?)
    {
        return Err(SecurityError::runtime(
            "snapshot Compose identity does not match local deployment".to_owned(),
        ));
    }
    let expected = resolved
        .iter()
        .map(|(id, _, name)| (id.clone(), name.clone()))
        .collect::<BTreeMap<_, _>>();
    for (source, name, present) in restored_volumes {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        if expected.get(source_id) != Some(name) {
            return Err(SecurityError::runtime(format!(
                "inventory volume differs from local deployment: {source_id}"
            )));
        }
        if *present && !volume_exists(name)? {
            return Err(SecurityError::runtime(format!(
                "existing deployment volume is missing: {name}"
            )));
        }
        if *present {
            let project = identity_project_name(&identity);
            validate_volume_identity(
                name,
                Some(project.as_str()),
                source.get("compose_volume").and_then(Yaml::as_str),
            )?;
        }
    }
    let running = plan_running_services(&local)?;
    Ok((local, identity, running))
}

fn authorize_rebuild_restore(
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
    restored_volumes: &[(BTreeMap<String, Yaml>, String, bool)],
) -> Result<LoadedCompose, SecurityError> {
    let Some(compose) = &inventory.compose else {
        return Err(SecurityError::runtime(
            "restore inventory has no Compose identity".to_owned(),
        ));
    };
    if compose.project_name.is_empty() {
        return Err(SecurityError::runtime(
            "snapshot has no Compose project identity".to_owned(),
        ));
    }
    for (source, name, _present) in restored_volumes {
        if source
            .get("compose_volume")
            .and_then(Yaml::as_str)
            .is_some_and(|value| !value.is_empty())
        {
            continue;
        }
        if volume_exists(name)? {
            return Err(SecurityError::runtime(format!(
                "rebuild volume already exists: {name}"
            )));
        }
    }
    if !project_containers(&compose.project_name, true)?.is_empty() {
        return Err(SecurityError::runtime(
            "rebuild Compose project already has containers".to_owned(),
        ));
    }
    Ok((
        manifest.clone(),
        compose_authorization_from_inventory(compose),
        Vec::new(),
    ))
}

fn authorize_existing_files_restore(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    let local = if config_string(config, "services_root").is_some() {
        local_manifest(config, manifest_service(manifest)?)?
    } else {
        manifest.clone()
    };
    validate_manifest(&Yaml::Map(local.clone())).map_err(invalid)?;
    validate_restore_inventory(&local, &inventory.to_snapshot_dict())?;
    let snapshot = snapshot_manifest(manifest, None)?;
    if restore_filter_projection(&snapshot) != restore_filter_projection(&local) {
        return Err(SecurityError::runtime(
            "snapshot path filters differ from local authorization".to_owned(),
        ));
    }
    if manifest
        .get("_restore_manifest_requested")
        .and_then(Yaml::as_bool)
        == Some(true)
        && restore_authorization_projection(manifest) != restore_authorization_projection(&local)
    {
        return Err(SecurityError::runtime(
            "snapshot manifest security settings differ from local authorization".to_owned(),
        ));
    }
    Ok(local)
}

fn validate_restore_conflicts(
    mode: &str,
    volumes: &[(BTreeMap<String, Yaml>, String)],
    all_volume_names: &[String],
) -> Result<(), SecurityError> {
    let names = if mode == "rebuild" {
        all_volume_names.to_vec()
    } else {
        volumes.iter().map(|(_, name)| name.clone()).collect()
    };
    let conflicts = mount_conflicts(&[], &names, mode == "rebuild")?;
    if !conflicts.is_empty() && mode == "rebuild" {
        return Err(SecurityError::runtime(format!(
            "rebuild targets are referenced by containers: {}",
            conflicts.join(", ")
        )));
    }
    Ok(())
}

fn validate_volume_identity(
    name: &str,
    project_name: Option<&str>,
    logical_name: Option<&str>,
) -> Result<(), SecurityError> {
    let Some(logical_name) = logical_name.filter(|value| !value.is_empty()) else {
        return Ok(());
    };
    let output = run_checked(&[
        "docker".to_owned(),
        "volume".to_owned(),
        "inspect".to_owned(),
        name.to_owned(),
    ])?;
    let values: Vec<Value> = serde_json::from_str(&output.stdout).map_err(|error| {
        SecurityError::runtime(format!(
            "unexpected Docker volume inspection result: {name}: {error}"
        ))
    })?;
    if values.len() != 1 {
        return Err(SecurityError::runtime(format!(
            "unexpected Docker volume inspection result: {name}"
        )));
    }
    let labels = values[0].get("Labels").cloned().unwrap_or(Value::Null);
    let project = labels
        .get("com.docker.compose.project")
        .and_then(Value::as_str);
    let volume = labels
        .get("com.docker.compose.volume")
        .and_then(Value::as_str);
    if project != project_name || volume != Some(logical_name) {
        return Err(SecurityError::runtime(format!(
            "Docker volume labels do not match Compose identity: {name}"
        )));
    }
    Ok(())
}

pub(crate) fn snapshot_manifest(
    manifest: &BTreeMap<String, Yaml>,
    root: Option<&Path>,
) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    let snapshot_path = manifest
        .get("_snapshot_manifest")
        .and_then(Yaml::as_str)
        .map(str::to_owned)
        .or_else(|| root.map(|root| root.join("_meta").join("backup.yaml").display().to_string()));
    let Some(snapshot_path) = snapshot_path else {
        return Ok(manifest.clone());
    };
    let text = match read_unprotected_text(&snapshot_path) {
        Ok(text) => text,
        Err(error) if error.is_missing_path() => {
            return Err(SecurityError::runtime(format!(
                "snapshot manifest is missing: {snapshot_path}"
            )));
        }
        Err(error) => return Err(error),
    };
    let value = parse_yaml_mapping(&snapshot_path, &text)?;
    let mut snapshot = match value {
        Yaml::Map(map) => map,
        Yaml::Null => BTreeMap::new(),
        _ => {
            return Err(SecurityError::runtime(format!(
                "snapshot manifest must be a mapping: {snapshot_path}"
            )));
        }
    };
    snapshot.insert("_path".to_owned(), Yaml::string(&snapshot_path));
    if let Some(directory) = manifest.get("_dir") {
        snapshot.insert("_dir".to_owned(), directory.clone());
    }
    let relative = manifest
        .get("_relative_dir")
        .and_then(Yaml::as_str)
        .map(str::to_owned)
        .or_else(|| {
            manifest.get("_dir").and_then(Yaml::as_str).map(|dir| {
                Path::new(dir)
                    .file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or(dir)
                    .to_owned()
            })
        })
        .unwrap_or_default();
    snapshot.insert("_relative_dir".to_owned(), Yaml::string(relative));
    validate_manifest(&Yaml::Map(snapshot.clone())).map_err(|error| {
        SecurityError::runtime(format!(
            "snapshot manifest is invalid: {snapshot_path}: {}",
            error.message
        ))
    })?;
    if snapshot.get("service").and_then(Yaml::as_str)
        != manifest.get("service").and_then(Yaml::as_str)
    {
        return Err(SecurityError::runtime(format!(
            "snapshot manifest belongs to {}, expected {}",
            python_str(snapshot.get("service").and_then(Yaml::as_str).unwrap_or("")),
            python_str(manifest.get("service").and_then(Yaml::as_str).unwrap_or("")),
        )));
    }
    Ok(snapshot)
}

pub(crate) fn parse_yaml_mapping(path: &str, text: &str) -> Result<Yaml, SecurityError> {
    if text.trim().is_empty() {
        return Ok(Yaml::Map(BTreeMap::new()));
    }
    let value = noyalib::from_str::<noyalib::Value>(text).map_err(|error| {
        SecurityError::runtime(format!("snapshot manifest is invalid: {path}: {error}"))
    })?;
    yaml_from_noya(value).map_err(|error| {
        SecurityError::runtime(format!("snapshot manifest is invalid: {path}: {error}"))
    })
}

fn yaml_from_noya(value: noyalib::Value) -> Result<Yaml, String> {
    match value {
        noyalib::Value::Null => Ok(Yaml::Null),
        noyalib::Value::Bool(value) => Ok(Yaml::Bool(value)),
        noyalib::Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                Ok(Yaml::Int(value))
            } else {
                Ok(Yaml::Float(number.as_f64()))
            }
        }
        noyalib::Value::String(value) => Ok(Yaml::String(value)),
        noyalib::Value::Sequence(items) => items
            .into_iter()
            .map(yaml_from_noya)
            .collect::<Result<Vec<_>, _>>()
            .map(Yaml::Seq),
        noyalib::Value::Mapping(mapping) => {
            let mut object = BTreeMap::new();
            for (key, value) in mapping {
                object.insert(key, yaml_from_noya(value)?);
            }
            Ok(Yaml::Map(object))
        }
        noyalib::Value::Tagged(_) => Err("YAML tags are not supported".to_owned()),
    }
}

fn local_manifest(
    config: &BTreeMap<String, Yaml>,
    service: &str,
) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    let forced = HOOKS.with(|hooks| {
        let hooks = hooks.borrow();
        if hooks.use_local_manifest {
            Some(hooks.local_manifest.clone())
        } else {
            None
        }
    });
    if let Some(manifest) = forced {
        return manifest.ok_or_else(|| {
            SecurityError::invalid(format!("unknown or disabled service: {service}"))
        });
    }
    let (ready, failures) = select_every_manifest(config)?;
    if let Some(failure) = failures.iter().find(|failure| {
        failure
            .summary
            .contains(&format!("duplicate service ID '{service}'"))
    }) {
        let message = failure
            .summary
            .split_once(": ")
            .map(|(_, message)| message)
            .unwrap_or(failure.summary.as_str());
        return Err(SecurityError::invalid(message.to_owned()));
    }
    ready
        .into_iter()
        .map(|service| service.manifest)
        .find(|manifest| manifest.get("service").and_then(Yaml::as_str) == Some(service))
        .ok_or_else(|| SecurityError::invalid(format!("unknown or disabled service: {service}")))
}

fn restore_actions_projection(manifest: &BTreeMap<String, Yaml>) -> Value {
    let section = manifest
        .get("actions")
        .and_then(Yaml::as_map)
        .and_then(|actions| actions.get("restore"))
        .and_then(Yaml::as_map);
    let mut projection = Map::new();
    for phase in ["before", "after", "finally", "on_success", "on_failure"] {
        let mut entries = Vec::new();
        if let Some(actions) = section
            .and_then(|section| section.get(phase))
            .and_then(Yaml::as_seq)
        {
            for action in actions.iter().filter_map(Yaml::as_map) {
                let mut entry = Map::new();
                entry.insert("name".to_owned(), yaml_to_json(action.get("name")));
                entry.insert(
                    "timeout".to_owned(),
                    action
                        .get("timeout")
                        .and_then(Yaml::as_int)
                        .map(Value::from)
                        .unwrap_or(Value::from(30)),
                );
                entry.insert(
                    "required".to_owned(),
                    Value::Bool(
                        action
                            .get("required")
                            .and_then(Yaml::as_bool)
                            .unwrap_or(true),
                    ),
                );
                entry.insert("run_as".to_owned(), yaml_to_json(action.get("run_as")));
                if let Some(command) = action.get("command").and_then(Yaml::as_seq) {
                    entry.insert(
                        "command".to_owned(),
                        Value::Array(
                            command
                                .iter()
                                .map(|item| yaml_to_json(Some(item)))
                                .collect(),
                        ),
                    );
                } else {
                    entry.insert("script".to_owned(), yaml_to_json(action.get("script")));
                }
                entries.push(Value::Object(entry));
            }
        }
        projection.insert(phase.to_owned(), Value::Array(entries));
    }
    Value::Object(projection)
}

fn project_sources(sources: Option<&BTreeMap<String, Yaml>>, key: &str, fields: &[&str]) -> Value {
    let Some(items) = sources
        .and_then(|sources| sources.get(key))
        .and_then(Yaml::as_seq)
    else {
        return Value::Array(Vec::new());
    };
    Value::Array(
        items
            .iter()
            .filter_map(Yaml::as_map)
            .map(|source| {
                Value::Object(Map::from_iter(
                    fields
                        .iter()
                        .map(|field| ((*field).to_owned(), yaml_to_json(source.get(*field)))),
                ))
            })
            .collect(),
    )
}

fn compose_authorization_from_inventory(
    identity: &crate::restore_inventory::ComposeIdentity,
) -> Value {
    Value::Object(Map::from_iter([
        (
            "project_name".to_owned(),
            Value::String(identity.project_name.clone()),
        ),
        (
            "compose_files".to_owned(),
            Value::Array(
                identity
                    .compose_files
                    .iter()
                    .cloned()
                    .map(Value::String)
                    .collect(),
            ),
        ),
        (
            "volumes".to_owned(),
            Value::Array(
                identity
                    .volumes
                    .iter()
                    .map(|volume| {
                        let mut item = Map::new();
                        item.insert("id".to_owned(), Value::String(volume.id.clone()));
                        item.insert(
                            "logical_name".to_owned(),
                            volume
                                .logical_name
                                .clone()
                                .map(Value::String)
                                .unwrap_or(Value::Null),
                        );
                        item.insert(
                            "actual_name".to_owned(),
                            Value::String(volume.actual_name.clone()),
                        );
                        Value::Object(item)
                    })
                    .collect(),
            ),
        ),
    ]))
}

fn compose_authorization_from_model(
    manifest: &BTreeMap<String, Yaml>,
    model: &Value,
    resolved: &[(String, Option<String>, String)],
) -> Value {
    let directory = manifest.get("_dir").and_then(Yaml::as_str).unwrap_or("");
    let fallback = Path::new(directory)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let project_name = model
        .get("name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
        .unwrap_or(fallback);
    Value::Object(Map::from_iter([
        (
            "project_name".to_owned(),
            Value::String(project_name.to_owned()),
        ),
        (
            "compose_files".to_owned(),
            Value::Array(
                compose_file_names(manifest)
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            ),
        ),
        (
            "volumes".to_owned(),
            Value::Array(
                resolved
                    .iter()
                    .map(|(id, logical, actual)| {
                        Value::Object(Map::from_iter([
                            ("id".to_owned(), Value::String(id.clone())),
                            (
                                "logical_name".to_owned(),
                                logical.clone().map(Value::String).unwrap_or(Value::Null),
                            ),
                            ("actual_name".to_owned(), Value::String(actual.clone())),
                        ]))
                    })
                    .collect(),
            ),
        ),
    ]))
}

fn identity_project_name(identity: &Value) -> String {
    identity
        .get("project_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

pub(crate) fn compose_file_paths(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<Vec<String>, SecurityError> {
    compose_file_names(manifest)
        .iter()
        .map(|file| service_control(manifest, file))
        .collect()
}

pub(crate) fn compose_control_paths(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<Vec<String>, SecurityError> {
    let mut paths = compose_file_paths(manifest)?;
    if let Some(env_file) = manifest
        .get("compose")
        .and_then(Yaml::as_map)
        .and_then(|compose| compose.get("env_file"))
        .and_then(Yaml::as_str)
        .filter(|value| !value.is_empty())
    {
        paths.push(service_control(manifest, env_file)?);
    }
    Ok(paths)
}

fn service_control(
    manifest: &BTreeMap<String, Yaml>,
    value: &str,
) -> Result<String, SecurityError> {
    if let Some(stripped) = value.strip_prefix('/') {
        let _ = stripped;
        return lexical_absolute(value).map_err(invalid);
    }
    let Some(directory) = manifest.get("_dir").and_then(Yaml::as_str) else {
        return Err(SecurityError::invalid(
            "manifest is missing _dir".to_owned(),
        ));
    };
    lexical_absolute(&join_service_path(directory, value)).map_err(invalid)
}

fn manifest_target(manifest: &BTreeMap<String, Yaml>) -> Result<String, SecurityError> {
    if let Some(path) = manifest.get("_path").and_then(Yaml::as_str) {
        return lexical_absolute(path).map_err(invalid);
    }
    service_control(manifest, "backup.yaml")
}

fn path_sources(manifest: &BTreeMap<String, Yaml>) -> Vec<&BTreeMap<String, Yaml>> {
    source_list(manifest, "paths")
}

fn volume_sources(manifest: &BTreeMap<String, Yaml>) -> Vec<&BTreeMap<String, Yaml>> {
    source_list(manifest, "volumes")
}

fn source_list<'a>(
    manifest: &'a BTreeMap<String, Yaml>,
    key: &str,
) -> Vec<&'a BTreeMap<String, Yaml>> {
    manifest
        .get("sources")
        .and_then(Yaml::as_map)
        .and_then(|sources| sources.get(key))
        .and_then(Yaml::as_seq)
        .map(|items| items.iter().filter_map(Yaml::as_map).collect())
        .unwrap_or_default()
}

fn manifest_service(manifest: &BTreeMap<String, Yaml>) -> Result<&str, SecurityError> {
    manifest
        .get("service")
        .and_then(Yaml::as_str)
        .ok_or_else(|| SecurityError::invalid("manifest is missing service".to_owned()))
}

fn config_string<'a>(config: &'a BTreeMap<String, Yaml>, key: &str) -> Option<&'a str> {
    config
        .get(key)
        .and_then(Yaml::as_str)
        .filter(|value| !value.is_empty())
}

fn flat_config(config: &BTreeMap<String, Yaml>) -> BTreeMap<String, String> {
    config
        .iter()
        .filter_map(|(key, value)| value.as_str().map(|value| (key.clone(), value.to_owned())))
        .collect()
}

fn yaml_to_json(value: Option<&Yaml>) -> Value {
    match value {
        None => Value::Null,
        Some(Yaml::Null) => Value::Null,
        Some(Yaml::Bool(value)) => Value::Bool(*value),
        Some(Yaml::Int(value)) => Value::from(*value),
        Some(Yaml::Float(value)) => serde_json::Number::from_f64(*value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        Some(Yaml::String(value)) => Value::String(value.clone()),
        Some(Yaml::Seq(items)) => {
            Value::Array(items.iter().map(|item| yaml_to_json(Some(item))).collect())
        }
        Some(Yaml::Map(map)) => Value::Object(Map::from_iter(
            map.iter()
                .map(|(key, value)| (key.clone(), yaml_to_json(Some(value)))),
        )),
    }
}

fn string_list(value: Option<&Yaml>) -> Value {
    match value.and_then(Yaml::as_seq) {
        Some(items) => Value::Array(
            items
                .iter()
                .filter_map(Yaml::as_str)
                .map(|item| Value::String(item.to_owned()))
                .collect(),
        ),
        None => Value::Array(Vec::new()),
    }
}

pub(crate) fn validate_data_parent(path: &str, allow_missing: bool) -> Result<(), SecurityError> {
    let path = lexical_absolute(path).map_err(invalid)?;
    if path == "/" {
        return Err(SecurityError::invalid(
            "data path must not be the filesystem root: /".to_owned(),
        ));
    }
    let parts = path
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    let mut current = String::new();
    for (index, part) in parts.iter().enumerate() {
        if index + 1 == parts.len() {
            break;
        }
        current = if current.is_empty() {
            format!("/{part}")
        } else {
            format!("{current}/{part}")
        };
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => {
                return Err(SecurityError::invalid(format!(
                    "data path component is not a real directory: {current}"
                )));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && allow_missing => {
                return Ok(());
            }
            Err(error) => return Err(SecurityError::runtime(error.to_string())),
        }
    }
    Ok(())
}

pub(crate) fn validate_data_path(path: &str) -> Result<(), SecurityError> {
    crate::unix::data::open_data_path(Path::new(path)).map(|_| ())
}

fn exists_or_link(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

fn is_real_file(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|metadata| metadata.is_file())
        .unwrap_or(false)
}

fn invalid(error: impl ToString) -> SecurityError {
    SecurityError::invalid(error.to_string())
}

pub(crate) fn shell_quote(value: &str) -> String {
    if value.chars().all(|ch| {
        ch.is_ascii_alphanumeric()
            || matches!(
                ch,
                '_' | '-' | '.' | '/' | ':' | '=' | ',' | '{' | '}' | '$'
            )
    }) && !value.is_empty()
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quiet_hooks() -> RuntimeGuard {
        install_runtime(RuntimeHooks {
            volume_exists: Some(Box::new(|_| Ok(false))),
            project_containers: Some(Box::new(|_, _| Ok(Vec::new()))),
            mount_conflicts: Some(Box::new(|_, _, _| Ok(Vec::new()))),
            running_services: Some(Box::new(|_| Ok(Vec::new()))),
            compose_model: Some(Box::new(|_| {
                Ok(serde_json::json!({"name": "demo", "services": {}, "volumes": {}}))
            })),
            local_manifest: None,
            run_command: None,
        })
    }

    fn base_manifest(dir: &Path) -> BTreeMap<String, Yaml> {
        let mut manifest = BTreeMap::new();
        manifest.insert("version".to_owned(), Yaml::Int(1));
        manifest.insert("service".to_owned(), Yaml::string("demo"));
        manifest.insert("name".to_owned(), Yaml::string("demo"));
        manifest.insert(
            "schedule".to_owned(),
            Yaml::map([("cron", Yaml::string("0 0 * * *"))]),
        );
        manifest.insert(
            "retention".to_owned(),
            Yaml::map([("keep_last", Yaml::Int(1))]),
        );
        manifest.insert(
            "consistency".to_owned(),
            Yaml::map([("mode", Yaml::string("stop"))]),
        );
        manifest.insert(
            "compose".to_owned(),
            Yaml::map([("files", Yaml::seq([Yaml::string("compose.yaml")]))]),
        );
        manifest.insert(
            "sources".to_owned(),
            Yaml::map([("paths", Yaml::seq([])), ("volumes", Yaml::seq([]))]),
        );
        manifest.insert("_dir".to_owned(), Yaml::string(dir.display().to_string()));
        manifest.insert(
            "_path".to_owned(),
            Yaml::string(dir.join("backup.yaml").display().to_string()),
        );
        manifest.insert("_relative_dir".to_owned(), Yaml::string("demo"));
        manifest
    }

    #[test]
    fn restore_actions_are_part_of_restore_authorization() {
        let mut local = BTreeMap::new();
        local.insert("service".to_owned(), Yaml::string("demo"));
        local.insert(
            "actions".to_owned(),
            Yaml::map([(
                "restore",
                Yaml::map([(
                    "after",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("import")),
                        ("script", Yaml::string("echo trusted")),
                        ("run_as", Yaml::string("root")),
                    ])]),
                )]),
            )]),
        );
        let mut changed = local.clone();
        changed.insert(
            "actions".to_owned(),
            Yaml::map([(
                "restore",
                Yaml::map([(
                    "after",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("import")),
                        ("script", Yaml::string("echo changed")),
                        ("run_as", Yaml::string("root")),
                    ])]),
                )]),
            )]),
        );
        assert_ne!(
            restore_authorization_projection(&local),
            restore_authorization_projection(&changed)
        );
    }

    #[test]
    fn restore_action_authorization_normalizes_defaults() {
        let mut absent = BTreeMap::new();
        absent.insert("service".to_owned(), Yaml::string("demo"));
        let mut explicit = absent.clone();
        explicit.insert(
            "actions".to_owned(),
            Yaml::map([(
                "restore",
                Yaml::map([
                    ("before", Yaml::seq([])),
                    ("after", Yaml::seq([])),
                    ("finally", Yaml::seq([])),
                    ("on_success", Yaml::seq([])),
                    ("on_failure", Yaml::seq([])),
                ]),
            )]),
        );
        assert_eq!(
            restore_authorization_projection(&absent),
            restore_authorization_projection(&explicit)
        );
        let mut implicit = absent.clone();
        implicit.insert(
            "actions".to_owned(),
            Yaml::map([(
                "restore",
                Yaml::map([(
                    "after",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("import")),
                        ("script", Yaml::string("echo trusted")),
                        ("run_as", Yaml::string("root")),
                    ])]),
                )]),
            )]),
        );
        let mut normalized = implicit.clone();
        normalized.insert(
            "actions".to_owned(),
            Yaml::map([(
                "restore",
                Yaml::map([(
                    "after",
                    Yaml::seq([Yaml::map([
                        ("name", Yaml::string("import")),
                        ("script", Yaml::string("echo trusted")),
                        ("timeout", Yaml::Int(30)),
                        ("required", Yaml::Bool(true)),
                        ("run_as", Yaml::string("root")),
                    ])]),
                )]),
            )]),
        );
        assert_eq!(
            restore_authorization_projection(&implicit),
            restore_authorization_projection(&normalized)
        );
    }

    #[test]
    fn deferred_env_file_and_rebuild_rules() {
        let root = std::env::temp_dir().join(format!("homelab-plan-{}", std::process::id()));
        let service = root.join("services").join("demo");
        let mut manifest = base_manifest(&service);
        manifest.insert(
            "sources".to_owned(),
            Yaml::map([
                (
                    "paths",
                    Yaml::seq([
                        Yaml::map([
                            ("id", Yaml::string("compose")),
                            ("path", Yaml::string("compose.yaml")),
                            ("required", Yaml::Bool(true)),
                        ]),
                        Yaml::map([
                            ("id", Yaml::string("env")),
                            ("path", Yaml::string("compose.env")),
                            ("required", Yaml::Bool(true)),
                        ]),
                    ]),
                ),
                ("volumes", Yaml::seq([])),
            ]),
        );
        if let Some(Yaml::Map(compose)) = manifest.get_mut("compose") {
            compose.insert("env_file".to_owned(), Yaml::string("compose.env"));
        }
        assert_eq!(
            deferred_compose_sources(&manifest).unwrap(),
            vec!["compose".to_owned(), "env".to_owned()]
        );
        if let Some(Yaml::Map(compose)) = manifest.get_mut("compose") {
            compose.insert("env_file".to_owned(), Yaml::string("compose.env"));
        }
        manifest.insert(
            "sources".to_owned(),
            Yaml::map([
                (
                    "paths",
                    Yaml::seq([Yaml::map([
                        ("id", Yaml::string("compose")),
                        ("path", Yaml::string("compose.yaml")),
                        ("required", Yaml::Bool(true)),
                    ])]),
                ),
                ("volumes", Yaml::seq([])),
            ]),
        );
        let error = validate_rebuild_compose_sources(&manifest, "rebuild", &[]).unwrap_err();
        assert!(error.message.contains("env file"), "{error}");
    }

    #[test]
    fn missing_restore_workspace_is_a_normal_exception() {
        let root =
            std::env::temp_dir().join(format!("homelab-plan-missing-{}", std::process::id()));
        let manifest = base_manifest(&root.join("demo"));
        let error =
            prepare_restore_plan(&BTreeMap::new(), &manifest, &root.join("missing")).unwrap_err();
        assert!(error.message.contains("does not exist"), "{error}");
    }
}
