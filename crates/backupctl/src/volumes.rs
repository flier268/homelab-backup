//! Docker volume copy, Compose identity, and writer checks.
//! External programs stay behind a runner so tests do not need a daemon.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use homelab_backup_domain::names::validate_docker_volume_name;
use homelab_backup_domain::paths::lexical_absolute;
use homelab_backup_domain::yaml::Yaml;

use crate::unix::control::{clear_control_leaf, validate_control_file};
use crate::unix::data::{CapturedCommand, validate_payload};
use crate::unix::docker::docker_mount_users;
use crate::unix::error::SecurityError;

const HELPER_DOCKERFILE_ENV: &str = "HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_DOCKERFILE";

type CommandRunner = Box<dyn FnMut(&Exec) -> Result<CapturedText, SecurityError>>;

thread_local! {
    static RUNNER: RefCell<Option<CommandRunner>> = const { RefCell::new(None) };
}

#[cfg(test)]
pub struct RunnerGuard;

#[cfg(test)]
impl Drop for RunnerGuard {
    fn drop(&mut self) {
        RUNNER.with(|slot| *slot.borrow_mut() = None);
    }
}

#[cfg(test)]
pub fn install_runner(runner: CommandRunner) -> RunnerGuard {
    RUNNER.with(|slot| *slot.borrow_mut() = Some(runner));
    RunnerGuard
}

#[derive(Debug, Clone)]
pub struct Exec {
    pub argv: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub clean_env: bool,
    pub capture: bool,
}

#[derive(Debug, Clone)]
pub struct CapturedText {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeInventory {
    pub id: String,
    pub name: Option<String>,
    pub compose_volume: Option<String>,
    pub actual_name: String,
    pub present: bool,
    pub capture_method: String,
    pub writers: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct DockerContext {
    pub project_name: String,
    pub services: Vec<String>,
    pub compose_files: Vec<String>,
    pub volumes: Vec<(String, Option<String>, String)>,
}

pub fn prepare_docker(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
) -> Result<DockerContext, SecurityError> {
    validate_docker_environment()?;
    let model = compose_model(manifest)?;
    let resolved = resolved_volumes(manifest, &model)?;
    for (id, _, name) in &resolved {
        if !docker_volume_exists(name)? && source_required(manifest, id) {
            return Err(SecurityError::runtime(format!(
                "Docker volume does not exist or is inaccessible: {name}"
            )));
        }
    }
    if !resolved.is_empty() {
        let image = config
            .get("volume_helper_image")
            .map(String::as_str)
            .unwrap_or("");
        ensure_volume_helper_image(image)?;
    }
    Ok(context_from(manifest, &model, &resolved))
}

pub fn validate_docker_environment() -> Result<String, SecurityError> {
    let mut overrides = Vec::new();
    for name in ["DOCKER_HOST", "DOCKER_CONTEXT"] {
        if std::env::var_os(name).is_some_and(|value| !value.is_empty()) {
            overrides.push(name);
        }
    }
    if !overrides.is_empty() {
        return Err(SecurityError::runtime(format!(
            "Docker endpoint overrides are not supported: {}",
            overrides.join(", ")
        )));
    }
    let inspected = run(Exec {
        argv: vec![
            "docker".to_owned(),
            "context".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{json .Endpoints.docker.Host}}".to_owned(),
        ],
        cwd: None,
        clean_env: false,
        capture: true,
    })?;
    let endpoint: serde_json::Value = serde_json::from_str(inspected.stdout.trim())
        .map_err(|_| SecurityError::runtime("cannot determine Docker daemon endpoint"))?;
    let Some(endpoint) = endpoint.as_str() else {
        return Err(SecurityError::runtime(
            "cannot determine Docker daemon endpoint",
        ));
    };
    if !endpoint.starts_with("unix://") || endpoint.starts_with("unix:///run/user/") {
        return Err(SecurityError::runtime(format!(
            "only a local rootful Docker Unix socket is supported: {endpoint}"
        )));
    }
    let info = run(Exec {
        argv: vec![
            "docker".to_owned(),
            "info".to_owned(),
            "--format".to_owned(),
            "{{json .SecurityOptions}}".to_owned(),
        ],
        cwd: None,
        clean_env: false,
        capture: true,
    })?;
    if info.stdout.to_ascii_lowercase().contains("rootless") {
        return Err(SecurityError::runtime("rootless Docker is not supported"));
    }
    Ok(endpoint.to_owned())
}

pub fn compose_model(
    manifest: &BTreeMap<String, Yaml>,
) -> Result<serde_json::Value, SecurityError> {
    let mut argv = compose_argv(manifest)?;
    argv.extend([
        "config".to_owned(),
        "--format".to_owned(),
        "json".to_owned(),
    ]);
    let directory = service_dir(manifest)?;
    let captured = run(Exec {
        argv,
        cwd: Some(PathBuf::from(&directory)),
        clean_env: true,
        capture: true,
    });
    let captured = match captured {
        Ok(value) => value,
        Err(error) => {
            eprintln!("  diagnostic hints:");
            eprintln!("    - Check that every compose.files entry exists.");
            eprintln!("    - Check compose.env_file and ${{VAR:?message}} expressions.");
            eprintln!("    - Run the printed docker compose command from the shown directory.");
            return Err(error);
        }
    };
    serde_json::from_str(captured.stdout.trim()).map_err(|error| {
        SecurityError::runtime(format!("invalid JSON from docker compose config: {error}"))
    })
}

pub fn compose_argv(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError> {
    let directory = lexical_absolute(&service_dir(manifest)?)
        .map_err(|error| SecurityError::invalid(error.message))?;
    let mut argv = vec![
        "docker".to_owned(),
        "compose".to_owned(),
        "--project-directory".to_owned(),
        directory,
    ];
    let env_file = compose_env_file(manifest)?;
    argv.push("--env-file".to_owned());
    argv.push(env_file);
    for file in compose_files(manifest)? {
        argv.push("-f".to_owned());
        argv.push(file);
    }
    Ok(argv)
}

fn compose_files(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError> {
    let declared = manifest
        .get("compose")
        .and_then(Yaml::as_map)
        .and_then(|compose| compose.get("files"))
        .and_then(Yaml::as_seq);
    let names = if let Some(files) = declared {
        files
            .iter()
            .filter_map(Yaml::as_str)
            .map(str::to_owned)
            .collect::<Vec<_>>()
    } else {
        vec!["compose.yaml".to_owned()]
    };
    let mut paths = Vec::new();
    for name in names {
        let path = service_control(manifest, &name)?;
        paths.push(validate_control_file(&path)?);
    }
    Ok(paths)
}

fn compose_env_file(manifest: &BTreeMap<String, Yaml>) -> Result<String, SecurityError> {
    let env_file = manifest
        .get("compose")
        .and_then(Yaml::as_map)
        .and_then(|compose| compose.get("env_file"))
        .and_then(Yaml::as_str)
        .filter(|value| !value.is_empty());
    match env_file {
        Some(file) => validate_control_file(&service_control(manifest, file)?),
        None => Ok("/dev/null".to_owned()),
    }
}

fn service_control(
    manifest: &BTreeMap<String, Yaml>,
    value: &str,
) -> Result<String, SecurityError> {
    if value.starts_with('/') {
        lexical_absolute(value).map_err(|error| SecurityError::invalid(error.message))
    } else {
        lexical_absolute(&format!("{}/{value}", service_dir(manifest)?))
            .map_err(|error| SecurityError::invalid(error.message))
    }
}

fn service_dir(manifest: &BTreeMap<String, Yaml>) -> Result<String, SecurityError> {
    manifest
        .get("_dir")
        .and_then(Yaml::as_str)
        .map(str::to_owned)
        .ok_or_else(|| SecurityError::invalid("manifest is missing _dir"))
}

pub fn resolved_volumes(
    manifest: &BTreeMap<String, Yaml>,
    model: &serde_json::Value,
) -> Result<Vec<(String, Option<String>, String)>, SecurityError> {
    let Some(sources) = manifest.get("sources").and_then(Yaml::as_map) else {
        return Ok(Vec::new());
    };
    let Some(volumes) = sources.get("volumes").and_then(Yaml::as_seq) else {
        return Ok(Vec::new());
    };
    let mut resolved = Vec::new();
    let mut seen = BTreeMap::<String, String>::new();
    for source in volumes.iter().filter_map(Yaml::as_map) {
        let id = source
            .get("id")
            .and_then(Yaml::as_str)
            .unwrap_or("")
            .to_owned();
        let name = actual_volume_name(source, model)?;
        if let Some(previous) = seen.get(&name) {
            return Err(SecurityError::invalid(format!(
                "duplicate Docker volume target '{name}' for sources '{previous}' and '{id}'"
            )));
        }
        seen.insert(name.clone(), id.clone());
        let compose_volume = source
            .get("compose_volume")
            .and_then(Yaml::as_str)
            .map(str::to_owned);
        resolved.push((id, compose_volume, name));
    }
    Ok(resolved)
}

fn actual_volume_name(
    source: &BTreeMap<String, Yaml>,
    model: &serde_json::Value,
) -> Result<String, SecurityError> {
    if let Some(name) = source.get("name").and_then(Yaml::as_str) {
        return safe_volume_name(name);
    }
    let Some(key) = source.get("compose_volume").and_then(Yaml::as_str) else {
        let id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        return Err(SecurityError::invalid(format!(
            "volume {id} needs name or compose_volume"
        )));
    };
    let volume = model
        .get("volumes")
        .and_then(|volumes| volumes.get(key))
        .ok_or_else(|| SecurityError::invalid(format!("compose volume key not found: {key}")))?;
    let name = volume
        .get("name")
        .and_then(|value| value.as_str())
        .unwrap_or(key);
    safe_volume_name(name)
}

fn safe_volume_name(name: &str) -> Result<String, SecurityError> {
    validate_docker_volume_name(name, "Docker volume name")
        .map_err(|error| SecurityError::invalid(error.message))?;
    Ok(name.to_owned())
}

pub fn docker_volume_exists(name: &str) -> Result<bool, SecurityError> {
    let _ = safe_volume_name(name)?;
    match run(Exec {
        argv: vec![
            "docker".to_owned(),
            "volume".to_owned(),
            "inspect".to_owned(),
            name.to_owned(),
        ],
        cwd: None,
        clean_env: false,
        capture: true,
    }) {
        Ok(_) => Ok(true),
        Err(error) => {
            let text = error.message.to_ascii_lowercase();
            if text.contains("no such volume") {
                Ok(false)
            } else {
                Err(error)
            }
        }
    }
}

pub fn ensure_volume_helper_image(image: &str) -> Result<(), SecurityError> {
    if image_present(image)? {
        return Ok(());
    }
    let dockerfile = std::env::var(HELPER_DOCKERFILE_ENV).ok();
    let dockerfile =
        dockerfile.filter(|path| Path::new(path).is_absolute() && Path::new(path).is_file());
    let Some(dockerfile) = dockerfile else {
        return Err(SecurityError::runtime(format!(
            "Docker helper image is missing: {image}; rebuild it with `docker build -t {image} -f Dockerfile.volume-rsync .` from the homelab-backup source or rerun install.sh"
        )));
    };
    eprintln!("WARNING: Docker helper image {image} is missing; rebuilding it from {dockerfile}");
    let context = std::env::temp_dir().join(format!(
        "homelab-backup-helper-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&context).map_err(|error| SecurityError::runtime(error.to_string()))?;
    let built = run(Exec {
        argv: vec![
            "docker".to_owned(),
            "build".to_owned(),
            "--tag".to_owned(),
            image.to_owned(),
            "--file".to_owned(),
            dockerfile,
            context.display().to_string(),
        ],
        cwd: None,
        clean_env: false,
        capture: false,
    });
    let _ = std::fs::remove_dir_all(&context);
    built.map(|_| ())
}

fn image_present(image: &str) -> Result<bool, SecurityError> {
    match run(Exec {
        argv: vec![
            "docker".to_owned(),
            "image".to_owned(),
            "inspect".to_owned(),
            image.to_owned(),
        ],
        cwd: None,
        clean_env: false,
        capture: true,
    }) {
        Ok(_) => Ok(true),
        Err(_) => Ok(false),
    }
}

pub fn estimate_volume(image: &str, name: &str, required: bool) -> Result<u64, SecurityError> {
    if !docker_volume_exists(name)? {
        if required {
            return Err(SecurityError::runtime(format!(
                "Docker volume does not exist or is inaccessible: {name}"
            )));
        }
        return Ok(0);
    }
    let base = vec![
        "docker".to_owned(),
        "run".to_owned(),
        "--rm".to_owned(),
        "--network".to_owned(),
        "none".to_owned(),
        "--mount".to_owned(),
        format!("type=volume,src={name},dst=/src,readonly"),
        image.to_owned(),
        "du".to_owned(),
        "-s".to_owned(),
        "-B1".to_owned(),
        "-x".to_owned(),
    ];
    let allocated = du_size(&base, name, &["/src".to_owned()])?;
    let apparent = du_size(
        &base,
        name,
        &["--apparent-size".to_owned(), "/src".to_owned()],
    )?;
    Ok(allocated.max(apparent))
}

fn du_size(base: &[String], name: &str, extra: &[String]) -> Result<u64, SecurityError> {
    let mut argv = base.to_vec();
    argv.extend(extra.iter().cloned());
    let captured = run(Exec {
        argv,
        cwd: None,
        clean_env: false,
        capture: true,
    })?;
    let size = captured
        .stdout
        .split_whitespace()
        .next()
        .filter(|field| field.chars().all(|ch| ch.is_ascii_digit()))
        .and_then(|field| field.parse::<u64>().ok())
        .ok_or_else(|| {
            SecurityError::runtime(format!("invalid size estimate for Docker volume: {name}"))
        })?;
    Ok(size)
}

pub fn running_services(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError> {
    let mut argv = compose_argv(manifest)?;
    argv.extend([
        "ps".to_owned(),
        "--services".to_owned(),
        "--filter".to_owned(),
        "status=running".to_owned(),
    ]);
    let captured = run(Exec {
        argv,
        cwd: Some(PathBuf::from(service_dir(manifest)?)),
        clean_env: true,
        capture: true,
    })?;
    Ok(captured
        .stdout
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(str::to_owned)
        .collect())
}

pub fn compose_lifecycle(
    manifest: &BTreeMap<String, Yaml>,
    command: &str,
    targets: &[String],
    timeout: i64,
) -> Result<(), SecurityError> {
    let mut argv = compose_argv(manifest)?;
    argv.push(command.to_owned());
    if command == "stop" {
        argv.push("-t".to_owned());
        argv.push(timeout.to_string());
    }
    argv.extend(targets.iter().cloned());
    run(Exec {
        argv,
        cwd: Some(PathBuf::from(service_dir(manifest)?)),
        clean_env: true,
        capture: false,
    })
    .map(|_| ())
}

pub fn validate_no_writers(
    manifest: &BTreeMap<String, Yaml>,
    context: &DockerContext,
    project_must_be_stopped: bool,
) -> Result<(), SecurityError> {
    if project_must_be_stopped {
        let running = project_containers(&context.project_name)?;
        if !running.is_empty() {
            return Err(SecurityError::runtime(format!(
                "Compose project still has running containers: {}",
                running.join(", ")
            )));
        }
    }
    let paths = path_targets(manifest)?;
    let path_refs = paths.iter().map(String::as_str).collect::<Vec<_>>();
    let names = context
        .volumes
        .iter()
        .map(|(_, _, name)| name.as_str())
        .collect::<Vec<_>>();
    let conflicts = mount_conflicts(&path_refs, &names)?;
    if !conflicts.is_empty() {
        return Err(SecurityError::runtime(format!(
            "backup sources are still mounted by containers: {}",
            conflicts.join(", ")
        )));
    }
    Ok(())
}

pub struct WriterMaps {
    pub paths: BTreeMap<String, Vec<String>>,
    pub volumes: BTreeMap<String, Vec<String>>,
}

pub fn writer_maps(
    manifest: &BTreeMap<String, Yaml>,
    context: &DockerContext,
) -> Result<WriterMaps, SecurityError> {
    let mut paths = BTreeMap::new();
    let mut volumes = BTreeMap::new();
    let Some(sources) = manifest.get("sources").and_then(Yaml::as_map) else {
        return Ok(WriterMaps { paths, volumes });
    };
    if let Some(Yaml::Seq(entries)) = sources.get("paths") {
        for source in entries.iter().filter_map(Yaml::as_map) {
            let id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
            let declared = homelab_backup_domain::manifest::source_path(manifest, source)
                .map_err(|error| SecurityError::invalid(error.message))?;
            let conflicts = mount_conflicts(&[declared.as_str()], &[])?;
            if !conflicts.is_empty() {
                paths.insert(id.to_owned(), conflicts);
            }
        }
    }
    for (id, _, name) in &context.volumes {
        let conflicts = mount_conflicts(&[], &[name.as_str()])?;
        if !conflicts.is_empty() {
            volumes.insert(id.clone(), conflicts);
        }
    }
    Ok(WriterMaps { paths, volumes })
}

fn path_targets(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError> {
    let Some(sources) = manifest.get("sources").and_then(Yaml::as_map) else {
        return Ok(Vec::new());
    };
    let Some(Yaml::Seq(entries)) = sources.get("paths") else {
        return Ok(Vec::new());
    };
    let mut targets = Vec::new();
    for source in entries.iter().filter_map(Yaml::as_map) {
        targets.push(
            homelab_backup_domain::manifest::source_path(manifest, source)
                .map_err(|error| SecurityError::invalid(error.message))?,
        );
    }
    Ok(targets)
}

fn project_containers(project: &str) -> Result<Vec<String>, SecurityError> {
    let captured = run(Exec {
        argv: vec![
            "docker".to_owned(),
            "ps".to_owned(),
            "-q".to_owned(),
            "--filter".to_owned(),
            format!("label=com.docker.compose.project={project}"),
        ],
        cwd: None,
        clean_env: false,
        capture: true,
    })?;
    Ok(captured
        .stdout
        .split_whitespace()
        .map(str::to_owned)
        .collect())
}

pub fn docker_users(
    paths: &[&str],
    volumes: &[&str],
    writable_only: bool,
) -> Result<Vec<String>, SecurityError> {
    docker_mount_users(
        paths,
        volumes,
        false,
        writable_only,
        &mut |argv| match run(Exec {
            argv: argv.to_vec(),
            cwd: None,
            clean_env: false,
            capture: true,
        }) {
            Ok(captured) => CapturedCommand {
                code: captured.code,
                stdout: captured.stdout,
            },
            Err(error) => CapturedCommand {
                code: 1,
                stdout: error.message,
            },
        },
    )
}

fn mount_conflicts(paths: &[&str], volumes: &[&str]) -> Result<Vec<String>, SecurityError> {
    docker_users(paths, volumes, true)
}

pub fn warn_writers(
    paths: &BTreeMap<String, Vec<String>>,
    volumes: &BTreeMap<String, Vec<String>>,
) {
    for (id, containers) in paths {
        eprintln!(
            "WARNING: path source '{id}' is being written by containers: {}; using best-effort copy",
            containers.join(", ")
        );
    }
    for (id, containers) in volumes {
        eprintln!(
            "WARNING: volume source '{id}' is being written by containers: {}; using best-effort copy",
            containers.join(", ")
        );
    }
}

pub fn method_map(
    ids: impl IntoIterator<Item = String>,
    writers: &BTreeMap<String, Vec<String>>,
    overrides: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    ids.into_iter()
        .map(|id| {
            let method = if overrides.contains_key(&id) {
                "btrfs-snapshot"
            } else if writers.contains_key(&id) {
                "best-effort"
            } else {
                "quiesced-copy"
            };
            (id, method.to_owned())
        })
        .collect()
}

pub fn sync_volumes(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    stage: &Path,
    resolved: &[(String, Option<String>, String)],
    capture_methods: &BTreeMap<String, String>,
    writers: &BTreeMap<String, Vec<String>>,
    before_copy: &mut dyn FnMut() -> Result<(), SecurityError>,
) -> Result<Vec<VolumeInventory>, SecurityError> {
    let image = config
        .get("volume_helper_image")
        .cloned()
        .unwrap_or_default();
    let mut inventory = Vec::new();
    for (id, compose_volume, name) in resolved {
        let destination = stage.join("volumes").join(id);
        let required = source_required(manifest, id);
        if !docker_volume_exists(name)? {
            if required {
                return Err(SecurityError::runtime(format!(
                    "Docker volume does not exist or is inaccessible: {name}"
                )));
            }
            println!("SKIP: optional Docker volume is unavailable: {name}");
            inventory.push(VolumeInventory {
                id: id.clone(),
                name: declared_name(manifest, id),
                compose_volume: compose_volume.clone(),
                actual_name: name.clone(),
                present: false,
                capture_method: capture_methods
                    .get(id)
                    .cloned()
                    .unwrap_or_else(|| "quiesced-copy".to_owned()),
                writers: writers.get(id).cloned().unwrap_or_default(),
            });
            continue;
        }
        before_copy()?;
        if let Some(parent) = destination.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
            std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))
                .map_err(|error| SecurityError::runtime(error.to_string()))?;
        }
        if destination.exists() || destination.symlink_metadata().is_ok() {
            clear_control_leaf(&destination.to_string_lossy())?;
        }
        std::fs::create_dir(&destination)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        std::fs::set_permissions(&destination, std::fs::Permissions::from_mode(0o700))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let mut argv = vec![
            "docker".to_owned(),
            "run".to_owned(),
            "--rm".to_owned(),
            "--network".to_owned(),
            "none".to_owned(),
            "--mount".to_owned(),
            format!("type=volume,src={name},dst=/src,readonly"),
            "--mount".to_owned(),
            format!("type=bind,src={},dst=/dst", destination.display()),
            image.clone(),
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--delete".to_owned(),
        ];
        for item in excludes(manifest, id) {
            argv.push("--exclude".to_owned());
            argv.push(item);
        }
        argv.push("/src/".to_owned());
        argv.push("/dst/".to_owned());
        run(Exec {
            argv,
            cwd: None,
            clean_env: false,
            capture: false,
        })?;
        validate_payload(&destination, None, crate::unix::data::current_euid(), None)?;
        inventory.push(VolumeInventory {
            id: id.clone(),
            name: declared_name(manifest, id),
            compose_volume: compose_volume.clone(),
            actual_name: name.clone(),
            present: true,
            capture_method: capture_methods
                .get(id)
                .cloned()
                .unwrap_or_else(|| "quiesced-copy".to_owned()),
            writers: writers.get(id).cloned().unwrap_or_default(),
        });
    }
    Ok(inventory)
}

fn source_required(manifest: &BTreeMap<String, Yaml>, id: &str) -> bool {
    volume_source(manifest, id)
        .and_then(|source| source.get("required"))
        .and_then(Yaml::as_bool)
        .unwrap_or(true)
}

fn declared_name(manifest: &BTreeMap<String, Yaml>, id: &str) -> Option<String> {
    volume_source(manifest, id)
        .and_then(|source| source.get("name"))
        .and_then(Yaml::as_str)
        .map(str::to_owned)
}

fn excludes(manifest: &BTreeMap<String, Yaml>, id: &str) -> Vec<String> {
    volume_source(manifest, id)
        .and_then(|source| source.get("exclude"))
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

fn volume_source<'a>(
    manifest: &'a BTreeMap<String, Yaml>,
    id: &str,
) -> Option<&'a BTreeMap<String, Yaml>> {
    manifest
        .get("sources")
        .and_then(Yaml::as_map)
        .and_then(|sources| sources.get("volumes"))
        .and_then(Yaml::as_seq)?
        .iter()
        .filter_map(Yaml::as_map)
        .find(|source| source.get("id").and_then(Yaml::as_str) == Some(id))
}

pub fn context_from(
    manifest: &BTreeMap<String, Yaml>,
    model: &serde_json::Value,
    resolved: &[(String, Option<String>, String)],
) -> DockerContext {
    let directory = manifest.get("_dir").and_then(Yaml::as_str).unwrap_or("");
    let fallback = Path::new(directory)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let project_name = model
        .get("name")
        .and_then(|value| value.as_str())
        .filter(|name| !name.is_empty())
        .unwrap_or(fallback)
        .to_owned();
    let mut services = model
        .get("services")
        .and_then(|value| value.as_object())
        .map(|services| services.keys().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    services.sort();
    let compose_files = manifest
        .get("compose")
        .and_then(Yaml::as_map)
        .and_then(|compose| compose.get("files"))
        .and_then(Yaml::as_seq)
        .map(|files| {
            files
                .iter()
                .filter_map(Yaml::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_else(|| vec!["compose.yaml".to_owned()]);
    DockerContext {
        project_name,
        services,
        compose_files,
        volumes: resolved.to_vec(),
    }
}

fn run(exec: Exec) -> Result<CapturedText, SecurityError> {
    println!(
        "+ {}",
        exec.argv
            .iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let overridden = RUNNER.with(|slot| {
        let mut slot = slot.borrow_mut();
        slot.as_mut().map(|runner| runner(&exec))
    });
    if let Some(result) = overridden {
        let captured = result?;
        return finish_captured(exec.capture, captured);
    }
    let mut command = Command::new(exec.argv.first().map(String::as_str).unwrap_or("docker"));
    if exec.argv.len() > 1 {
        command.args(&exec.argv[1..]);
    }
    if let Some(cwd) = &exec.cwd {
        command.current_dir(cwd);
    }
    if exec.clean_env {
        command.env_clear();
        command.env("HOME", "/root");
        command.env("LANG", "C.UTF-8");
        command.env("LC_ALL", "C.UTF-8");
        command.env(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        );
    }
    let output = command
        .output()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    let code = output.status.code().unwrap_or(1);
    if !exec.capture {
        print!("{stdout}");
        eprint!("{stderr}");
    }
    finish_captured(
        exec.capture,
        CapturedText {
            code,
            stdout,
            stderr,
        },
    )
}

fn finish_captured(capture: bool, captured: CapturedText) -> Result<CapturedText, SecurityError> {
    if captured.code == 0 {
        return Ok(captured);
    }
    let mut message = format!("command failed with exit code {}", captured.code);
    if capture {
        let diagnostic = format!("{}\n{}", captured.stderr, captured.stdout);
        if diagnostic.to_ascii_lowercase().contains("no such volume") {
            message = format!("{message}: no such volume");
        }
    }
    Err(SecurityError::runtime(message))
}

fn shell_quote(value: &str) -> String {
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

    fn scripted(responses: Vec<CapturedText>) -> RunnerGuard {
        let responses = std::cell::Cell::new(responses);
        install_runner(Box::new(move |_exec| {
            let mut left = responses.take();
            let next = left.remove(0);
            responses.set(left);
            Ok(next)
        }))
    }

    #[test]
    fn volume_estimate_uses_the_larger_du_reading() {
        let _guard = scripted(vec![
            CapturedText {
                code: 0,
                stdout: "[]".to_owned(),
                stderr: String::new(),
            },
            CapturedText {
                code: 0,
                stdout: "100\n".to_owned(),
                stderr: String::new(),
            },
            CapturedText {
                code: 0,
                stdout: "250\n".to_owned(),
                stderr: String::new(),
            },
        ]);
        assert_eq!(
            estimate_volume("helper:latest", "demo_data", true).unwrap(),
            250
        );
    }

    #[test]
    fn missing_volume_reports_absence() {
        let _guard = scripted(vec![CapturedText {
            code: 1,
            stdout: String::new(),
            stderr: "Error: no such volume".to_owned(),
        }]);
        assert!(!docker_volume_exists("missing_data").unwrap());
    }

    #[test]
    fn optional_compose_volume_is_identified_by_source_id() {
        let manifest = BTreeMap::from([(
            "sources".to_owned(),
            Yaml::map([(
                "volumes",
                Yaml::seq([Yaml::map([
                    ("id", Yaml::string("data")),
                    ("compose_volume", Yaml::string("data")),
                    ("required", Yaml::Bool(false)),
                ])]),
            )]),
        )]);
        assert!(!source_required(&manifest, "data"));
        assert!(source_required(&manifest, "other"));
    }
}
