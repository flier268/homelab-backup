//! Snapshot inventory authorization for restore.
//! The JSON shape matches `inventory_models.RestoreInventoryModel`.

#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::os::fd::OwnedFd;
use std::path::Path;

use homelab_backup_domain::manifest::{requires_docker, source_path};
use homelab_backup_domain::names::{
    valid_service_name, validate_docker_volume_name, validate_service_relative_directory,
};
use homelab_backup_domain::paths::{lexical_absolute, paths_overlap};
use homelab_backup_domain::yaml::{Yaml, python_str};
use rustix::fs::{AtFlags, Mode, OFlags, fstat, openat};
use rustix::io::Errno;
use serde_json::{Map, Value};

use crate::unix::control::{file_name, is_reg, mode_of, open_directory, parent_path};
use crate::unix::data::validate_payload;
use crate::unix::error::SecurityError;

const CAPTURE_METHODS: &[&str] = &["btrfs-snapshot", "quiesced-copy", "best-effort"];
const CONSISTENCY_MODES: &[&str] = &["stop", "hooks", "external", "live", "snapshot", "files"];
const ACTION_PHASES: &[&str] = &["before", "after", "finally", "on_success", "on_failure"];
const PATH_TYPES: &[&str] = &["file", "directory", "symlink"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelFailure {
    pub location: Vec<String>,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InventoryAncestor {
    pub path: String,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InventoryPath {
    pub id: String,
    pub path: String,
    pub type_name: Option<String>,
    pub present: bool,
    pub ancestors: Vec<InventoryAncestor>,
    pub capture_method: Option<String>,
    pub writers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InventoryVolume {
    pub id: String,
    pub name: Option<String>,
    pub compose_volume: Option<String>,
    pub actual_name: String,
    pub present: bool,
    pub capture_method: Option<String>,
    pub writers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComposeVolumeIdentity {
    pub id: String,
    pub logical_name: Option<String>,
    pub actual_name: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComposeIdentity {
    pub project_name: String,
    pub compose_files: Vec<String>,
    pub services: Vec<String>,
    pub volumes: Vec<ComposeVolumeIdentity>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionalActionFailure {
    pub phase: String,
    pub name: String,
    pub result: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConsistencyMetadata {
    pub mode: String,
    pub guarantee: String,
    pub optional_action_failures: Vec<OptionalActionFailure>,
    pub writers: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestoreInventory {
    pub version: i64,
    pub service: String,
    pub service_directory: Option<String>,
    pub service_relative_directory: Option<String>,
    pub relative_directory_set: bool,
    pub paths: Vec<InventoryPath>,
    pub volumes: Vec<InventoryVolume>,
    pub compose: Option<ComposeIdentity>,
    pub consistency: Option<ConsistencyMetadata>,
}

impl RestoreInventory {
    pub fn from_snapshot_data(data: &Value) -> Result<Self, ModelFailure> {
        parse_inventory(data)
    }

    pub fn to_snapshot_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert("version".to_owned(), Value::from(self.version));
        map.insert("service".to_owned(), Value::String(self.service.clone()));
        if let Some(directory) = &self.service_directory {
            map.insert(
                "service_directory".to_owned(),
                Value::String(directory.clone()),
            );
        }
        if let Some(directory) = &self.service_relative_directory {
            map.insert(
                "service_relative_directory".to_owned(),
                Value::String(directory.clone()),
            );
        }
        map.insert(
            "paths".to_owned(),
            Value::Array(self.paths.iter().map(dump_path).collect()),
        );
        map.insert(
            "volumes".to_owned(),
            Value::Array(self.volumes.iter().map(dump_volume).collect()),
        );
        if let Some(compose) = &self.compose {
            map.insert("compose".to_owned(), dump_compose(compose));
        }
        if let Some(consistency) = &self.consistency {
            map.insert("consistency".to_owned(), dump_consistency(consistency));
        }
        Value::Object(map)
    }

    pub fn paths_by_id(&self) -> BTreeMap<&str, &InventoryPath> {
        self.paths
            .iter()
            .map(|item| (item.id.as_str(), item))
            .collect()
    }

    pub fn volumes_by_id(&self) -> BTreeMap<&str, &InventoryVolume> {
        self.volumes
            .iter()
            .map(|item| (item.id.as_str(), item))
            .collect()
    }
}

pub fn model_error_message(location: &[String], message: &str) -> String {
    if location.iter().any(|item| item == "capture_method") {
        return "restore inventory capture method is invalid".to_owned();
    }
    if location.iter().any(|item| item == "ancestors") {
        return "restore inventory ancestor is invalid".to_owned();
    }
    if location == ["version".to_owned()] {
        return "restore inventory version must be 1".to_owned();
    }
    if location.first().map(String::as_str) == Some("consistency") {
        return format!("restore inventory consistency metadata is invalid: {message}");
    }
    if matches!(
        location.first().map(String::as_str),
        Some("paths" | "volumes")
    ) {
        let kind = location.first().map(String::as_str).unwrap_or("paths");
        return format!("restore inventory {kind} are invalid: {message}");
    }
    format!("restore inventory is invalid: {message}")
}

pub fn load_restore_inventory(
    root: &Path,
    expected_service: Option<&str>,
) -> Result<RestoreInventory, SecurityError> {
    let path = root.join("_meta").join("inventory.json");
    let display = path.display().to_string();
    let payload = match read_unprotected_text(&display) {
        Ok(text) => text,
        Err(error) if error.is_missing_path() => {
            return Err(SecurityError::runtime(format!(
                "restore inventory is missing: {display}"
            )));
        }
        Err(error) => {
            return Err(SecurityError::runtime(format!(
                "cannot read restore inventory {display}: {error}"
            )));
        }
    };
    let data: Value = serde_json::from_str(&payload).map_err(|error| {
        SecurityError::runtime(format!("cannot read restore inventory {display}: {error}"))
    })?;
    if !data.is_object() {
        return Err(SecurityError::runtime(format!(
            "restore inventory must be a JSON object: {display}"
        )));
    }
    if let Some(service) = expected_service
        && data.get("service").and_then(Value::as_str) != Some(service)
    {
        return Err(SecurityError::runtime(format!(
            "restore inventory belongs to service {}, expected {}",
            python_str(data.get("service").and_then(Value::as_str).unwrap_or("")),
            python_str(service),
        )));
    }
    parse_inventory(&data).map_err(|failure| {
        SecurityError::runtime(format!(
            "cannot read restore inventory {display}: {}",
            model_error_message(&failure.location, &failure.message)
        ))
    })
}

pub fn restore_inventory_service_directory(
    inventory: &Value,
    service: &str,
) -> Result<String, SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "invalid service ID: {}",
            python_str(service)
        )));
    }
    let Some(object) = inventory.as_object() else {
        return Err(SecurityError::runtime(
            "restore inventory must be a JSON object".to_owned(),
        ));
    };
    let version = object.get("version").unwrap_or(&Value::Null);
    if json_int(version).is_none_or(|value| value != 1) {
        return Err(SecurityError::runtime(
            "restore inventory version must be 1".to_owned(),
        ));
    }
    let inventory_service = object.get("service").and_then(Value::as_str);
    if inventory_service != Some(service) {
        return Err(SecurityError::runtime(format!(
            "restore inventory belongs to service {}, expected {}",
            python_str(inventory_service.unwrap_or("")),
            python_str(service),
        )));
    }
    let value = if object.contains_key("service_relative_directory") {
        object
            .get("service_relative_directory")
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        Value::String(service.to_owned())
    };
    let yaml = json_to_yaml(&value);
    validate_service_relative_directory(&yaml, "restore inventory service directory")
        .map_err(|error| SecurityError::invalid(error.message))?;
    Ok(value.as_str().unwrap_or(service).to_owned())
}

pub fn validate_restore_inventory(
    manifest: &BTreeMap<String, Yaml>,
    inventory: &Value,
) -> Result<RestoreInventory, SecurityError> {
    let parsed = parse_inventory(inventory).map_err(|failure| {
        SecurityError::runtime(model_error_message(&failure.location, &failure.message))
    })?;
    authorize_inventory_against_manifest(manifest, &parsed)
}

pub fn authorize_inventory_against_manifest(
    manifest: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
) -> Result<RestoreInventory, SecurityError> {
    let service = manifest.get("service").and_then(Yaml::as_str).unwrap_or("");
    let raw = inventory.to_snapshot_dict();
    let mut probe = raw;
    if inventory.relative_directory_set {
        if let Some(directory) = &inventory.service_relative_directory {
            probe.as_object_mut().unwrap().insert(
                "service_relative_directory".to_owned(),
                Value::String(directory.clone()),
            );
        } else if let Some(object) = probe.as_object_mut() {
            object.insert("service_relative_directory".to_owned(), Value::Null);
        }
    } else if let Some(object) = probe.as_object_mut() {
        object.remove("service_relative_directory");
    }
    restore_inventory_service_directory(&probe, service)?;
    let declared_paths = source_maps(manifest, "paths");
    let declared_volumes = source_maps(manifest, "volumes");
    let inventory_paths = inventory.paths_by_id();
    let inventory_volumes = inventory.volumes_by_id();
    if declared_paths.keys().copied().collect::<BTreeSet<_>>()
        != inventory_paths.keys().copied().collect::<BTreeSet<_>>()
    {
        return Err(SecurityError::runtime(
            "restore inventory path source IDs do not match manifest".to_owned(),
        ));
    }
    if declared_volumes.keys().copied().collect::<BTreeSet<_>>()
        != inventory_volumes.keys().copied().collect::<BTreeSet<_>>()
    {
        return Err(SecurityError::runtime(
            "restore inventory volume source IDs do not match manifest".to_owned(),
        ));
    }
    for (source_id, source) in &declared_paths {
        let entry = inventory_paths[*source_id];
        let declared = source.get("path").and_then(Yaml::as_str).unwrap_or("");
        if entry.path != declared {
            return Err(SecurityError::runtime(format!(
                "restore inventory path differs for source {}",
                python_str(source_id)
            )));
        }
        if source_required(source) && !entry.present {
            return Err(SecurityError::runtime(format!(
                "required path is marked absent: {}",
                python_str(source_id)
            )));
        }
    }
    if !requires_docker(manifest) {
        if !inventory.volumes.is_empty() {
            return Err(SecurityError::runtime(
                "files restore inventory must not contain volumes".to_owned(),
            ));
        }
        if inventory.compose.is_some() {
            return Err(SecurityError::runtime(
                "files restore inventory must not contain Compose identity".to_owned(),
            ));
        }
        if inventory
            .consistency
            .as_ref()
            .is_none_or(|item| item.mode != "files")
        {
            return Err(SecurityError::runtime(
                "files restore inventory consistency mode is invalid".to_owned(),
            ));
        }
        return Ok(inventory.clone());
    }
    let Some(compose) = &inventory.compose else {
        return Err(SecurityError::runtime(
            "restore inventory has no Compose identity".to_owned(),
        ));
    };
    let by_identity_id = compose
        .volumes
        .iter()
        .map(|item| (item.id.as_str(), item))
        .collect::<BTreeMap<_, _>>();
    if by_identity_id.keys().copied().collect::<BTreeSet<_>>()
        != declared_volumes.keys().copied().collect::<BTreeSet<_>>()
    {
        return Err(SecurityError::runtime(
            "restore inventory Compose volume mapping differs from sources".to_owned(),
        ));
    }
    for (source_id, source) in &declared_volumes {
        let entry = inventory_volumes[*source_id];
        if source_required(source) && !entry.present {
            return Err(SecurityError::runtime(format!(
                "required volume is marked absent: {}",
                python_str(source_id)
            )));
        }
        validate_docker_volume_name(
            &entry.actual_name,
            &format!("inventory volume {} actual_name", python_str(source_id)),
        )
        .map_err(|error| SecurityError::invalid(error.message))?;
        let snapshot_fields = [entry.name.as_deref(), entry.compose_volume.as_deref()]
            .into_iter()
            .flatten()
            .filter(|value| !value.is_empty())
            .count();
        if snapshot_fields != 1 {
            return Err(SecurityError::runtime(format!(
                "restore inventory volume declaration differs: {}",
                python_str(source_id)
            )));
        }
        if entry
            .name
            .as_ref()
            .is_some_and(|name| name != &entry.actual_name)
        {
            return Err(SecurityError::runtime(format!(
                "restore inventory volume target differs: {}",
                python_str(source_id)
            )));
        }
        let identity_entry = by_identity_id[*source_id];
        if identity_entry.actual_name != entry.actual_name
            || identity_entry.logical_name != entry.compose_volume
        {
            return Err(SecurityError::runtime(format!(
                "restore inventory volume identity differs: {}",
                python_str(source_id)
            )));
        }
        if let Some(name) = source.get("name").and_then(Yaml::as_str) {
            if entry.actual_name != name {
                return Err(SecurityError::runtime(format!(
                    "restore inventory volume target differs: {}",
                    python_str(source_id)
                )));
            }
        } else if entry.name.is_some()
            || entry.compose_volume.as_deref()
                != source.get("compose_volume").and_then(Yaml::as_str)
        {
            return Err(SecurityError::runtime(format!(
                "restore inventory volume declaration differs: {}",
                python_str(source_id)
            )));
        }
    }
    let expected_files = compose_file_names(manifest);
    if compose.compose_files != expected_files {
        return Err(SecurityError::runtime(
            "restore inventory Compose file list differs from manifest".to_owned(),
        ));
    }
    Ok(inventory.clone())
}

pub struct PathArtifact {
    pub source_type: String,
    pub restored: std::path::PathBuf,
    pub target: String,
}

pub fn restored_path_details(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    source: &BTreeMap<String, Yaml>,
    inventory: &RestoreInventory,
) -> Result<PathArtifact, SecurityError> {
    let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
    let Some(entry) = inventory.paths_by_id().get(source_id).copied() else {
        return Err(SecurityError::runtime(format!(
            "restore inventory has no path source {}",
            python_str(source_id)
        )));
    };
    path_artifact(
        manifest,
        root,
        source,
        entry.present,
        entry.type_name.as_deref(),
        &entry.path,
    )
}

pub fn restored_path_details_raw(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    source: &BTreeMap<String, Yaml>,
    inventory: &Value,
) -> Result<PathArtifact, SecurityError> {
    let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
    let Some(entry) = inventory
        .get("paths")
        .and_then(Value::as_array)
        .and_then(|items| {
            items
                .iter()
                .find(|item| item.get("id").and_then(Value::as_str) == Some(source_id))
        })
    else {
        return Err(SecurityError::runtime(format!(
            "restore inventory has no path source {}",
            python_str(source_id)
        )));
    };
    let present = entry
        .get("present")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let source_type = entry.get("type").and_then(Value::as_str);
    let archived = entry
        .get("path")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| source.get("path").and_then(Yaml::as_str).unwrap_or(""));
    path_artifact(manifest, root, source, present, source_type, archived)
}

pub fn validate_restore_sources(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    inventory: &Value,
) -> Result<(), SecurityError> {
    for source in source_maps(manifest, "paths").values() {
        let details = restored_path_details_raw(manifest, root, source, inventory)?;
        if details.source_type == "missing" {
            continue;
        }
        let exists = match details.source_type.as_str() {
            "file" => is_real_file(&details.restored),
            "directory" => is_real_dir(&details.restored),
            "symlink" => is_real_link(&details.restored),
            _ => false,
        };
        if !exists {
            return Err(SecurityError::runtime(format!(
                "restored {} artifact is missing: {}",
                details.source_type,
                details.restored.display()
            )));
        }
        validate_payload(
            &details.restored,
            None,
            crate::unix::data::current_euid(),
            None,
        )?;
    }
    let volumes = inventory
        .get("volumes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for source in source_maps(manifest, "volumes").values() {
        let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        let Some(entry) = volumes
            .iter()
            .find(|item| item.get("id").and_then(Value::as_str) == Some(source_id))
        else {
            return Err(SecurityError::runtime(format!(
                "restore inventory has no volume source {}",
                python_str(source_id)
            )));
        };
        if entry.get("present").and_then(Value::as_bool) == Some(false) {
            continue;
        }
        let restored = root.join("volumes").join(source_id);
        if !is_real_dir(&restored) {
            return Err(SecurityError::runtime(format!(
                "restored volume artifact is missing or not a real directory: {}",
                restored.display()
            )));
        }
        validate_payload(&restored, None, crate::unix::data::current_euid(), None)?;
    }
    Ok(())
}

pub fn validate_restore_path_separation(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    inventory: &RestoreInventory,
) -> Result<(), SecurityError> {
    let root = lexical_absolute(&root.display().to_string())
        .map_err(|error| SecurityError::invalid(error.message))?;
    let sources = source_maps(manifest, "paths");
    let staged_sources = sources
        .values()
        .map(|source| {
            let details = restored_path_details(manifest, Path::new(&root), source, inventory)?;
            lexical_absolute(&details.restored.display().to_string())
                .map_err(|error| SecurityError::invalid(error.message))
        })
        .collect::<Result<Vec<_>, _>>()?;
    for source in sources.values() {
        let target =
            lexical_absolute(&source_path(manifest, source).map_err(invalid)?).map_err(invalid)?;
        if paths_overlap(&root, &target).unwrap_or(false) {
            return Err(SecurityError::invalid(format!(
                "restore directory {root} overlaps live path target {target}"
            )));
        }
        for staged in &staged_sources {
            if paths_overlap(staged, &target).unwrap_or(false) {
                return Err(SecurityError::invalid(format!(
                    "restored source {staged} overlaps live path target {target}"
                )));
            }
        }
    }
    Ok(())
}

pub fn read_unprotected_text(path: &str) -> Result<String, SecurityError> {
    let path = lexical_absolute(path).map_err(invalid)?;
    let parent = parent_path(&path);
    let parent_fd = match open_directory(&parent) {
        Ok(fd) => fd,
        Err(error) if error.message.contains("No such file") || error.is_missing_path() => {
            return Err(SecurityError::invalid(format!("missing path: {path}")));
        }
        Err(_) => {
            return match rustix::fs::statat(
                rustix::fs::CWD,
                parent.as_str(),
                AtFlags::SYMLINK_NOFOLLOW,
            ) {
                Err(Errno::NOENT) => Err(SecurityError::invalid(format!("missing path: {path}"))),
                Err(error) => Err(SecurityError::runtime(error.to_string())),
                Ok(_) => Err(SecurityError::invalid(format!(
                    "control file could not be opened: {path}"
                ))),
            };
        }
    };
    let name = file_name(&path);
    let fd = match openat(
        &parent_fd,
        name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Err(SecurityError::invalid(format!("missing path: {path}"))),
        Err(Errno::LOOP) => {
            return Err(SecurityError::invalid(format!(
                "control file must be a regular file: {path}"
            )));
        }
        Err(error) => {
            return Err(SecurityError::invalid(format!(
                "control file could not be opened: {path}: {error}"
            )));
        }
    };
    finish_read(&path, fd)
}

fn finish_read(path: &str, fd: OwnedFd) -> Result<String, SecurityError> {
    let metadata = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(SecurityError::invalid(format!(
            "control file must be a regular file: {path}"
        )));
    }
    let mut file = std::fs::File::from(fd);
    let mut text = String::new();
    std::io::Read::read_to_string(&mut file, &mut text)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(text)
}

fn path_artifact(
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    source: &BTreeMap<String, Yaml>,
    present: bool,
    source_type: Option<&str>,
    archived_path: &str,
) -> Result<PathArtifact, SecurityError> {
    let source_id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
    let staged = root.join("paths").join(source_id);
    let target = source_path(manifest, source).map_err(invalid)?;
    if !present {
        return Ok(PathArtifact {
            source_type: "missing".to_owned(),
            restored: staged,
            target,
        });
    }
    if matches!(source_type, Some("file" | "symlink")) {
        let name = Path::new(archived_path)
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or(archived_path);
        return Ok(PathArtifact {
            source_type: source_type.unwrap_or("file").to_owned(),
            restored: staged.join(name),
            target,
        });
    }
    if source_type != Some("directory") {
        return Err(SecurityError::runtime(format!(
            "restore inventory has invalid type for path source {}: {}",
            python_str(source_id),
            python_str(source_type.unwrap_or(""))
        )));
    }
    Ok(PathArtifact {
        source_type: "directory".to_owned(),
        restored: staged,
        target,
    })
}

fn parse_inventory(data: &Value) -> Result<RestoreInventory, ModelFailure> {
    let Some(object) = data.as_object() else {
        return Err(failure(&[] as &[String], "input should be a mapping"));
    };
    reject_unknown(
        object,
        &[
            "version",
            "service",
            "service_directory",
            "service_relative_directory",
            "paths",
            "volumes",
            "compose",
            "consistency",
        ],
        &[],
    )?;
    let version = require_version(object.get("version").unwrap_or(&Value::Null))?;
    let service = require_string(object, "service", &[])?;
    let service_directory = optional_string(object, "service_directory", &[])?;
    let relative_directory_set = object.contains_key("service_relative_directory");
    let service_relative_directory = if relative_directory_set {
        match object.get("service_relative_directory") {
            Some(Value::Null) | None => None,
            Some(Value::String(value)) => Some(value.clone()),
            Some(_) => {
                return Err(failure(
                    &["service_relative_directory".to_owned()],
                    "service_relative_directory must be a string",
                ));
            }
        }
    } else {
        None
    };
    let paths = parse_paths(object.get("paths").unwrap_or(&Value::Null))?;
    let volumes = parse_volumes(object.get("volumes").unwrap_or(&Value::Null))?;
    let compose = match object.get("compose") {
        None | Some(Value::Null) => None,
        Some(value) => Some(parse_compose(value)?),
    };
    let consistency = match object.get("consistency") {
        None | Some(Value::Null) => None,
        Some(value) => Some(parse_consistency(value)?),
    };
    unique_ids(
        &paths
            .iter()
            .map(|item| item.id.as_str())
            .collect::<Vec<_>>(),
        "path source IDs",
    )?;
    unique_ids(
        &volumes
            .iter()
            .map(|item| item.id.as_str())
            .collect::<Vec<_>>(),
        "volume source IDs",
    )?;
    Ok(RestoreInventory {
        version,
        service,
        service_directory,
        service_relative_directory,
        relative_directory_set,
        paths,
        volumes,
        compose,
        consistency,
    })
}

fn parse_paths(value: &Value) -> Result<Vec<InventoryPath>, ModelFailure> {
    let items = expect_array(value, &["paths"])?;
    let mut paths = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let loc = vec!["paths".to_owned(), index.to_string()];
        let Some(object) = item.as_object() else {
            return Err(failure(&loc, "path entry must be a mapping"));
        };
        reject_unknown(
            object,
            &[
                "id",
                "path",
                "type",
                "present",
                "ancestors",
                "capture_method",
                "writers",
            ],
            &loc,
        )?;
        let id = require_string(object, "id", &loc)?;
        let path = require_string(object, "path", &loc)?;
        let type_name = optional_choice(object, "type", PATH_TYPES, &loc)?;
        let present = require_bool(object, "present", &loc)?;
        if present != type_name.is_some() {
            return Err(failure(
                &loc,
                "present path entries require a type and absent entries forbid it",
            ));
        }
        let ancestors = parse_ancestors(object.get("ancestors"), &loc)?;
        let capture_method = optional_choice(object, "capture_method", CAPTURE_METHODS, &loc)?;
        let writers = parse_writers(object.get("writers"), "path writers", &loc, false)?;
        paths.push(InventoryPath {
            id,
            path,
            type_name,
            present,
            ancestors,
            capture_method,
            writers,
        });
    }
    Ok(paths)
}

fn parse_ancestors(
    value: Option<&Value>,
    parent: &[String],
) -> Result<Vec<InventoryAncestor>, ModelFailure> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value.is_null() {
        return Ok(Vec::new());
    }
    let mut loc = parent.to_vec();
    loc.push("ancestors".to_owned());
    let items = expect_array(value, &loc)?;
    let mut ancestors = Vec::new();
    let mut previous: Option<String> = None;
    for (index, item) in items.iter().enumerate() {
        let mut item_loc = loc.clone();
        item_loc.push(index.to_string());
        let Some(object) = item.as_object() else {
            return Err(failure(
                &item_loc,
                "ancestor path and ownership must be valid",
            ));
        };
        reject_unknown(object, &["path", "uid", "gid", "mode"], &item_loc)?;
        let path = require_string(object, "path", &item_loc)?;
        let candidate = Path::new(&path);
        let valid = !path.is_empty()
            && !candidate.is_absolute()
            && candidate.components().next().is_some()
            && path
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != "..");
        let uid = json_u32(object.get("uid")).filter(|_| valid);
        let gid = json_u32(object.get("gid"));
        let mode = json_int(object.get("mode").unwrap_or(&Value::Null));
        if uid.is_none() || gid.is_none() || !valid {
            return Err(failure(
                &item_loc,
                "ancestor path and ownership must be valid",
            ));
        }
        let Some(mode) = mode.filter(|mode| (0..=0o7777).contains(mode)) else {
            return Err(failure(
                &item_loc,
                "ancestor mode must be between 0 and 07777",
            ));
        };
        let current = candidate
            .components()
            .map(|part| part.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join("/");
        if let Some(previous) = &previous {
            let parent_path = parent_posix(&current);
            if &parent_path != previous {
                return Err(failure(
                    &loc,
                    "ancestor entries must form one ordered chain",
                ));
            }
        }
        previous = Some(current.clone());
        ancestors.push(InventoryAncestor {
            path: current,
            uid: uid.unwrap_or(0),
            gid: gid.unwrap_or(0),
            mode: u32::try_from(mode).unwrap_or(0),
        });
    }
    Ok(ancestors)
}

fn parse_volumes(value: &Value) -> Result<Vec<InventoryVolume>, ModelFailure> {
    let items = expect_array(value, &["volumes"])?;
    let mut volumes = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let loc = vec!["volumes".to_owned(), index.to_string()];
        let Some(object) = item.as_object() else {
            return Err(failure(&loc, "volume entry must be a mapping"));
        };
        reject_unknown(
            object,
            &[
                "id",
                "name",
                "compose_volume",
                "actual_name",
                "present",
                "capture_method",
                "writers",
            ],
            &loc,
        )?;
        volumes.push(InventoryVolume {
            id: require_string(object, "id", &loc)?,
            name: optional_string(object, "name", &loc)?,
            compose_volume: optional_string(object, "compose_volume", &loc)?,
            actual_name: require_string(object, "actual_name", &loc)?,
            present: require_bool(object, "present", &loc)?,
            capture_method: optional_choice(object, "capture_method", CAPTURE_METHODS, &loc)?,
            writers: parse_writers(object.get("writers"), "volume writers", &loc, false)?,
        });
    }
    Ok(volumes)
}

fn parse_compose(value: &Value) -> Result<ComposeIdentity, ModelFailure> {
    let loc = vec!["compose".to_owned()];
    let Some(object) = value.as_object() else {
        return Err(failure(&loc, "compose identity must be a mapping"));
    };
    reject_unknown(
        object,
        &["project_name", "compose_files", "services", "volumes"],
        &loc,
    )?;
    let services = require_string_list(object, "services", &loc)?;
    unique_nonempty(&services, "Compose services", &loc)?;
    let mut sorted = services.clone();
    sorted.sort();
    if services != sorted {
        return Err(failure(&loc, "Compose services must be sorted"));
    }
    let mut volume_loc = loc.clone();
    volume_loc.push("volumes".to_owned());
    let volume_items = expect_array(object.get("volumes").unwrap_or(&Value::Null), &volume_loc)?;
    let mut volumes = Vec::new();
    for (index, item) in volume_items.iter().enumerate() {
        let mut item_loc = volume_loc.clone();
        item_loc.push(index.to_string());
        let Some(entry) = item.as_object() else {
            return Err(failure(&item_loc, "compose volume must be a mapping"));
        };
        reject_unknown(entry, &["id", "logical_name", "actual_name"], &item_loc)?;
        volumes.push(ComposeVolumeIdentity {
            id: require_string(entry, "id", &item_loc)?,
            logical_name: optional_string(entry, "logical_name", &item_loc)?,
            actual_name: require_string(entry, "actual_name", &item_loc)?,
        });
    }
    unique_ids(
        &volumes
            .iter()
            .map(|item| item.id.as_str())
            .collect::<Vec<_>>(),
        "Compose volume IDs",
    )
    .map_err(|failure| ModelFailure {
        location: loc.clone(),
        message: failure.message,
    })?;
    Ok(ComposeIdentity {
        project_name: require_string(object, "project_name", &loc)?,
        compose_files: require_string_list(object, "compose_files", &loc)?,
        services,
        volumes,
    })
}

fn parse_consistency(value: &Value) -> Result<ConsistencyMetadata, ModelFailure> {
    let loc = vec!["consistency".to_owned()];
    let Some(object) = value.as_object() else {
        return Err(failure(&loc, "consistency metadata must be a mapping"));
    };
    reject_unknown(
        object,
        &["mode", "guarantee", "optional_action_failures", "writers"],
        &loc,
    )?;
    let mode = require_choice(object, "mode", CONSISTENCY_MODES, &loc)?;
    let guarantee = require_choice(object, "guarantee", CAPTURE_METHODS, &loc)?;
    let mut failure_loc = loc.clone();
    failure_loc.push("optional_action_failures".to_owned());
    let failure_items = expect_array(
        object
            .get("optional_action_failures")
            .unwrap_or(&Value::Null),
        &failure_loc,
    )?;
    let mut failures = Vec::new();
    for (index, item) in failure_items.iter().enumerate() {
        let mut item_loc = failure_loc.clone();
        item_loc.push(index.to_string());
        let Some(entry) = item.as_object() else {
            return Err(failure(&item_loc, "action failure must be a mapping"));
        };
        reject_unknown(entry, &["phase", "name", "result"], &item_loc)?;
        let name = require_string(entry, "name", &item_loc)?;
        if name.is_empty() {
            return Err(failure(&item_loc, "action failure name must not be empty"));
        }
        failures.push(OptionalActionFailure {
            phase: require_choice(entry, "phase", ACTION_PHASES, &item_loc)?,
            name,
            result: require_choice(entry, "result", &["failed", "timeout"], &item_loc)?,
        });
    }
    let writers = parse_writers(object.get("writers"), "consistency writers", &loc, true)?;
    Ok(ConsistencyMetadata {
        mode,
        guarantee,
        optional_action_failures: failures,
        writers,
    })
}

fn parse_writers(
    value: Option<&Value>,
    label: &str,
    parent: &[String],
    sorted: bool,
) -> Result<Vec<String>, ModelFailure> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if value.is_null() {
        return Ok(Vec::new());
    }
    let mut loc = parent.to_vec();
    loc.push("writers".to_owned());
    let items = require_string_items(value, &loc)?;
    unique_nonempty(&items, label, &loc)?;
    if sorted {
        let mut ordered = items.clone();
        ordered.sort();
        if ordered != items {
            return Err(failure(&loc, "consistency writers must be sorted"));
        }
    }
    Ok(items)
}

fn dump_path(path: &InventoryPath) -> Value {
    let mut map = Map::new();
    map.insert("id".to_owned(), Value::String(path.id.clone()));
    map.insert("path".to_owned(), Value::String(path.path.clone()));
    if let Some(type_name) = &path.type_name {
        map.insert("type".to_owned(), Value::String(type_name.clone()));
    }
    map.insert("present".to_owned(), Value::Bool(path.present));
    if !path.ancestors.is_empty() {
        map.insert(
            "ancestors".to_owned(),
            Value::Array(
                path.ancestors
                    .iter()
                    .map(|ancestor| {
                        Value::Object(Map::from_iter([
                            ("path".to_owned(), Value::String(ancestor.path.clone())),
                            ("uid".to_owned(), Value::from(ancestor.uid)),
                            ("gid".to_owned(), Value::from(ancestor.gid)),
                            ("mode".to_owned(), Value::from(ancestor.mode)),
                        ]))
                    })
                    .collect(),
            ),
        );
    }
    if let Some(method) = &path.capture_method {
        map.insert("capture_method".to_owned(), Value::String(method.clone()));
    }
    if !path.writers.is_empty() {
        map.insert("writers".to_owned(), Value::Array(strings(&path.writers)));
    }
    Value::Object(map)
}

fn dump_volume(volume: &InventoryVolume) -> Value {
    let mut map = Map::new();
    map.insert("id".to_owned(), Value::String(volume.id.clone()));
    if let Some(name) = &volume.name {
        map.insert("name".to_owned(), Value::String(name.clone()));
    }
    if let Some(name) = &volume.compose_volume {
        map.insert("compose_volume".to_owned(), Value::String(name.clone()));
    }
    map.insert(
        "actual_name".to_owned(),
        Value::String(volume.actual_name.clone()),
    );
    map.insert("present".to_owned(), Value::Bool(volume.present));
    if let Some(method) = &volume.capture_method {
        map.insert("capture_method".to_owned(), Value::String(method.clone()));
    }
    if !volume.writers.is_empty() {
        map.insert("writers".to_owned(), Value::Array(strings(&volume.writers)));
    }
    Value::Object(map)
}

fn dump_compose(compose: &ComposeIdentity) -> Value {
    let mut map = Map::new();
    map.insert(
        "project_name".to_owned(),
        Value::String(compose.project_name.clone()),
    );
    map.insert(
        "compose_files".to_owned(),
        Value::Array(strings(&compose.compose_files)),
    );
    map.insert(
        "services".to_owned(),
        Value::Array(strings(&compose.services)),
    );
    map.insert(
        "volumes".to_owned(),
        Value::Array(
            compose
                .volumes
                .iter()
                .map(|volume| {
                    let mut item = Map::new();
                    item.insert("id".to_owned(), Value::String(volume.id.clone()));
                    if let Some(name) = &volume.logical_name {
                        item.insert("logical_name".to_owned(), Value::String(name.clone()));
                    }
                    item.insert(
                        "actual_name".to_owned(),
                        Value::String(volume.actual_name.clone()),
                    );
                    Value::Object(item)
                })
                .collect(),
        ),
    );
    Value::Object(map)
}

fn dump_consistency(consistency: &ConsistencyMetadata) -> Value {
    Value::Object(Map::from_iter([
        ("mode".to_owned(), Value::String(consistency.mode.clone())),
        (
            "guarantee".to_owned(),
            Value::String(consistency.guarantee.clone()),
        ),
        (
            "optional_action_failures".to_owned(),
            Value::Array(
                consistency
                    .optional_action_failures
                    .iter()
                    .map(|failure| {
                        Value::Object(Map::from_iter([
                            ("phase".to_owned(), Value::String(failure.phase.clone())),
                            ("name".to_owned(), Value::String(failure.name.clone())),
                            ("result".to_owned(), Value::String(failure.result.clone())),
                        ]))
                    })
                    .collect(),
            ),
        ),
        (
            "writers".to_owned(),
            Value::Array(strings(&consistency.writers)),
        ),
    ]))
}

fn source_maps<'a>(
    manifest: &'a BTreeMap<String, Yaml>,
    key: &str,
) -> BTreeMap<&'a str, &'a BTreeMap<String, Yaml>> {
    manifest
        .get("sources")
        .and_then(Yaml::as_map)
        .and_then(|sources| sources.get(key))
        .and_then(Yaml::as_seq)
        .map(|items| {
            items
                .iter()
                .filter_map(Yaml::as_map)
                .filter_map(|source| {
                    source
                        .get("id")
                        .and_then(Yaml::as_str)
                        .map(|id| (id, source))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn source_required(source: &BTreeMap<String, Yaml>) -> bool {
    source
        .get("required")
        .and_then(Yaml::as_bool)
        .unwrap_or(true)
}

pub(crate) fn compose_file_names(manifest: &BTreeMap<String, Yaml>) -> Vec<String> {
    manifest
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
        .unwrap_or_else(|| vec!["compose.yaml".to_owned()])
}

fn json_to_yaml(value: &Value) -> Yaml {
    match value {
        Value::Null => Yaml::Null,
        Value::String(value) => Yaml::string(value),
        _ => Yaml::Null,
    }
}

fn json_int(value: &Value) -> Option<i64> {
    let Value::Number(number) = value else {
        return None;
    };
    if number.is_i64() {
        return number.as_i64();
    }
    if number.is_u64() {
        return number.as_u64().and_then(|value| i64::try_from(value).ok());
    }
    None
}

fn json_u32(value: Option<&Value>) -> Option<u32> {
    let number = json_int(value?)?;
    u32::try_from(number).ok()
}

fn require_version(value: &Value) -> Result<i64, ModelFailure> {
    match json_int(value) {
        Some(1) => Ok(1),
        Some(_) => Err(failure(
            &["version".to_owned()],
            "version must be an integer",
        )),
        None => Err(failure(
            &["version".to_owned()],
            "version must be an integer",
        )),
    }
}

fn require_string(
    object: &Map<String, Value>,
    key: &str,
    parent: &[String],
) -> Result<String, ModelFailure> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    match object.get(key) {
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err(failure(&loc, &format!("{key} must be a string"))),
    }
}

fn optional_string(
    object: &Map<String, Value>,
    key: &str,
    parent: &[String],
) -> Result<Option<String>, ModelFailure> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    match object.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(failure(&loc, &format!("{key} must be a string"))),
    }
}

fn require_bool(
    object: &Map<String, Value>,
    key: &str,
    parent: &[String],
) -> Result<bool, ModelFailure> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    match object.get(key) {
        Some(Value::Bool(value)) => Ok(*value),
        _ => Err(failure(&loc, &format!("{key} must be a boolean"))),
    }
}

fn optional_choice(
    object: &Map<String, Value>,
    key: &str,
    allowed: &[&str],
    parent: &[String],
) -> Result<Option<String>, ModelFailure> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    match object.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if allowed.contains(&value.as_str()) => Ok(Some(value.clone())),
        Some(_) => Err(failure(&loc, &format!("{key} is invalid"))),
    }
}

fn require_choice(
    object: &Map<String, Value>,
    key: &str,
    allowed: &[&str],
    parent: &[String],
) -> Result<String, ModelFailure> {
    optional_choice(object, key, allowed, parent)?
        .ok_or_else(|| failure(&append(parent, key), &format!("{key} is invalid")))
}

fn require_string_list(
    object: &Map<String, Value>,
    key: &str,
    parent: &[String],
) -> Result<Vec<String>, ModelFailure> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    require_string_items(object.get(key).unwrap_or(&Value::Null), &loc)
}

fn require_string_items(value: &Value, loc: &[String]) -> Result<Vec<String>, ModelFailure> {
    let items = expect_array(value, loc)?;
    let mut values = Vec::new();
    for item in items {
        let Some(text) = item.as_str() else {
            return Err(failure(loc, "expected a string"));
        };
        values.push(text.to_owned());
    }
    Ok(values)
}

fn expect_array<'a>(
    value: &'a Value,
    loc: &[impl AsRef<str>],
) -> Result<&'a Vec<Value>, ModelFailure> {
    value.as_array().ok_or_else(|| {
        failure(
            &loc.iter()
                .map(|item| item.as_ref().to_owned())
                .collect::<Vec<_>>(),
            "expected an array",
        )
    })
}

fn unique_ids(ids: &[&str], label: &str) -> Result<(), ModelFailure> {
    unique_nonempty(ids, label, &[])
}

fn unique_nonempty(
    values: &[impl AsRef<str>],
    label: &str,
    loc: &[String],
) -> Result<(), ModelFailure> {
    let texts = values.iter().map(AsRef::as_ref).collect::<Vec<_>>();
    if texts.iter().any(|value| value.is_empty())
        || texts.len() != texts.iter().collect::<BTreeSet<_>>().len()
    {
        return Err(failure(
            loc,
            &format!("{label} must contain unique non-empty strings"),
        ));
    }
    Ok(())
}

fn reject_unknown(
    object: &Map<String, Value>,
    allowed: &[&str],
    parent: &[String],
) -> Result<(), ModelFailure> {
    if let Some(key) = object.keys().find(|key| !allowed.contains(&key.as_str())) {
        let mut loc = parent.to_vec();
        loc.push(key.clone());
        return Err(failure(&loc, "extra inputs are not permitted"));
    }
    Ok(())
}

fn failure(location: &[String], message: &str) -> ModelFailure {
    ModelFailure {
        location: location.to_vec(),
        message: message.to_owned(),
    }
}

fn append(parent: &[String], key: &str) -> Vec<String> {
    let mut loc = parent.to_vec();
    loc.push(key.to_owned());
    loc
}

fn strings(values: &[String]) -> Vec<Value> {
    values.iter().cloned().map(Value::String).collect()
}

fn parent_posix(path: &str) -> String {
    match path.rsplit_once('/') {
        Some((parent, _)) => parent.to_owned(),
        None => ".".to_owned(),
    }
}

fn invalid(error: homelab_backup_domain::error::PolicyError) -> SecurityError {
    SecurityError::invalid(error.message)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest_value(root: &Path, sources: Yaml, consistency: &str) -> BTreeMap<String, Yaml> {
        let mut manifest = BTreeMap::new();
        manifest.insert("version".to_owned(), Yaml::Int(1));
        manifest.insert("service".to_owned(), Yaml::string("demo"));
        manifest.insert("_dir".to_owned(), Yaml::string(root.display().to_string()));
        manifest.insert(
            "_path".to_owned(),
            Yaml::string(root.join("backup.yaml").display().to_string()),
        );
        manifest.insert(
            "consistency".to_owned(),
            Yaml::map([("mode", Yaml::string(consistency))]),
        );
        manifest.insert("sources".to_owned(), sources);
        manifest
    }

    fn compose_inventory(paths: Value, volumes: Value) -> Value {
        serde_json::json!({
            "version": 1,
            "service": "demo",
            "paths": paths,
            "volumes": volumes,
            "compose": {
                "project_name": "demo",
                "services": [],
                "compose_files": ["compose.yaml"],
                "volumes": volumes.as_array().unwrap().iter().map(|item| {
                    serde_json::json!({
                        "id": item["id"],
                        "logical_name": item.get("compose_volume").cloned().unwrap_or(Value::Null),
                        "actual_name": item["actual_name"],
                    })
                }).collect::<Vec<_>>()
            }
        })
    }

    #[test]
    fn service_directory_accepts_valid_relative_path() {
        let value = restore_inventory_service_directory(
            &serde_json::json!({
                "version": 1,
                "service": "advent-plus",
                "service_relative_directory": "Minecraft/Advent Plus",
            }),
            "advent-plus",
        )
        .unwrap();
        assert_eq!(value, "Minecraft/Advent Plus");
    }

    #[test]
    fn service_directory_falls_back_to_service_id_when_field_is_missing() {
        let value = restore_inventory_service_directory(
            &serde_json::json!({"version": 1, "service": "advent-plus"}),
            "advent-plus",
        )
        .unwrap();
        assert_eq!(value, "advent-plus");
    }

    #[test]
    fn service_directory_rejects_non_integer_version_one() {
        for version in [
            Value::Bool(true),
            serde_json::json!(1.5),
            Value::String("1".to_owned()),
            Value::from(2),
            Value::Null,
        ] {
            let error = restore_inventory_service_directory(
                &serde_json::json!({"version": version, "service": "demo"}),
                "demo",
            )
            .unwrap_err();
            assert!(error.is_runtime(), "{error}");
            assert!(error.message.contains("version must be 1"), "{error}");
        }
    }

    #[test]
    fn service_directory_rejects_wrong_service() {
        let error = restore_inventory_service_directory(
            &serde_json::json!({"version": 1, "service": "other"}),
            "demo",
        )
        .unwrap_err();
        assert!(error.message.contains("expected 'demo'"), "{error}");
    }

    #[test]
    fn service_directory_rejects_invalid_requested_service() {
        let error = restore_inventory_service_directory(
            &serde_json::json!({"version": 1, "service": "../outside"}),
            "../outside",
        )
        .unwrap_err();
        assert!(!error.is_runtime());
        assert!(error.message.contains("invalid service ID"), "{error}");
    }

    #[test]
    fn service_directory_rejects_unsafe_or_non_normalized_paths() {
        for value in [
            Value::Null,
            Value::String(String::new()),
            Value::String("/outside".to_owned()),
            Value::String("../outside".to_owned()),
            Value::String("Minecraft//Server".to_owned()),
            Value::String("Minecraft/./Server".to_owned()),
            Value::String("Minecraft/../Server".to_owned()),
            Value::String("Minecraft/Server/".to_owned()),
            Value::String("Minecraft/two  spaces".to_owned()),
        ] {
            let error = restore_inventory_service_directory(
                &serde_json::json!({
                    "version": 1,
                    "service": "demo",
                    "service_relative_directory": value,
                }),
                "demo",
            )
            .unwrap_err();
            assert!(!error.is_runtime(), "{error}");
        }
    }

    #[test]
    fn capture_method_and_ancestor_metadata_are_validated() {
        let root = std::env::temp_dir().join(format!("homelab-inv-{}", std::process::id()));
        let manifest = manifest_value(
            &root,
            Yaml::map([
                (
                    "paths",
                    Yaml::seq([Yaml::map([
                        ("id", Yaml::string("data")),
                        ("path", Yaml::string("data")),
                    ])]),
                ),
                ("volumes", Yaml::seq([])),
            ]),
            "snapshot",
        );
        let mut inventory = compose_inventory(
            serde_json::json!([{
                "id": "data",
                "path": "data",
                "type": "directory",
                "present": true,
                "capture_method": "btrfs-snapshot",
                "writers": ["container"],
            }]),
            serde_json::json!([]),
        );
        inventory["consistency"] = serde_json::json!({
            "mode": "snapshot",
            "guarantee": "btrfs-snapshot",
            "optional_action_failures": [{
                "phase": "finally",
                "name": "resume",
                "result": "failed",
            }],
            "writers": ["container"],
        });
        validate_restore_inventory(&manifest, &inventory).unwrap();
        inventory["paths"][0]["capture_method"] = Value::String("unknown".to_owned());
        let error = validate_restore_inventory(&manifest, &inventory).unwrap_err();
        assert!(error.message.contains("capture method"), "{error}");

        inventory["paths"][0]["capture_method"] = Value::String("btrfs-snapshot".to_owned());
        inventory["paths"][0]["ancestors"] = serde_json::json!([{
            "path": "../outside",
            "uid": 0,
            "gid": 0,
            "mode": 0o755,
        }]);
        let error = validate_restore_inventory(&manifest, &inventory).unwrap_err();
        assert!(error.message.contains("ancestor is invalid"), "{error}");
    }

    #[test]
    fn inventory_symlink_is_rejected_without_reading_referent() {
        let root = std::env::temp_dir().join(format!(
            "homelab-inv-link-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let restored = root.join("restore");
        let meta = restored.join("_meta");
        std::fs::create_dir_all(&meta).unwrap();
        let outside = root.join("outside.json");
        std::fs::write(&outside, "{\"version\": 1}").unwrap();
        std::os::unix::fs::symlink(&outside, meta.join("inventory.json")).unwrap();
        let error = load_restore_inventory(&restored, None).unwrap_err();
        assert!(error.message.contains("regular file"), "{error}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn present_optional_path_requires_its_staged_artifact() {
        let root = std::env::temp_dir().join(format!("homelab-inv-opt-{}", std::process::id()));
        let manifest = manifest_value(
            &root,
            Yaml::map([(
                "paths",
                Yaml::seq([Yaml::map([
                    ("id", Yaml::string("optional")),
                    ("path", Yaml::string("optional")),
                    ("required", Yaml::Bool(false)),
                ])]),
            )]),
            "external",
        );
        let inventory = serde_json::json!({
            "paths": [{
                "id": "optional",
                "path": "optional",
                "type": "file",
                "present": true,
            }],
            "volumes": [],
        });
        let error =
            validate_restore_sources(&manifest, &root.join("restore"), &inventory).unwrap_err();
        assert!(error.message.contains("artifact is missing"), "{error}");
    }

    #[test]
    fn volume_inventory_targets_are_authorized() {
        let root = std::env::temp_dir().join(format!("homelab-inv-vol-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("restore").join("_meta")).unwrap();
        let manifest = manifest_value(
            &root,
            Yaml::map([
                ("paths", Yaml::seq([])),
                (
                    "volumes",
                    Yaml::seq([Yaml::map([
                        ("id", Yaml::string("db")),
                        ("name", Yaml::string("demo_db")),
                        ("required", Yaml::Bool(true)),
                    ])]),
                ),
            ]),
            "external",
        );
        let absent = compose_inventory(
            serde_json::json!([]),
            serde_json::json!([{
                "id": "db",
                "name": "demo_db",
                "actual_name": "demo_db",
                "present": false,
            }]),
        );
        std::fs::write(
            root.join("restore").join("_meta").join("inventory.json"),
            absent.to_string(),
        )
        .unwrap();
        let loaded = load_restore_inventory(&root.join("restore"), None).unwrap();
        let error = validate_restore_inventory(&manifest, &loaded.to_snapshot_dict()).unwrap_err();
        assert!(
            error.message.contains("required volume") && error.message.contains("absent"),
            "{error}"
        );

        let redirected = compose_inventory(
            serde_json::json!([]),
            serde_json::json!([{
                "id": "db",
                "name": "demo_db",
                "actual_name": "attacker_selected",
                "present": true,
            }]),
        );
        let error = validate_restore_inventory(&manifest, &redirected).unwrap_err();
        assert!(error.message.contains("volume target differs"), "{error}");

        let mut pinned_manifest = manifest.clone();
        if let Some(Yaml::Map(sources)) = pinned_manifest.get_mut("sources")
            && let Some(Yaml::Seq(volumes)) = sources.get_mut("volumes")
            && let Some(Yaml::Map(volume)) = volumes.first_mut()
        {
            volume.insert("name".to_owned(), Yaml::string("reviewed_db"));
        }
        let pinned = compose_inventory(
            serde_json::json!([]),
            serde_json::json!([{
                "id": "db",
                "compose_volume": "db",
                "actual_name": "reviewed_db",
                "present": true,
            }]),
        );
        validate_restore_inventory(&pinned_manifest, &pinned).unwrap();
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn inventory_model_round_trips_and_rejects_coercion() {
        let data = serde_json::json!({
            "version": 1,
            "service": "demo",
            "service_directory": "/srv/stacks/demo",
            "service_relative_directory": "demo",
            "paths": [{
                "id": "data",
                "path": "data",
                "type": "directory",
                "present": true,
                "capture_method": "quiesced-copy",
            }],
            "volumes": [],
            "compose": {
                "project_name": "demo",
                "compose_files": ["compose.yaml"],
                "services": [],
                "volumes": [],
            },
            "consistency": {
                "mode": "external",
                "guarantee": "quiesced-copy",
                "optional_action_failures": [],
                "writers": [],
            },
        });
        let model = RestoreInventory::from_snapshot_data(&data).unwrap();
        assert_eq!(model.to_snapshot_dict(), data);
        for update in [
            serde_json::json!({"unknown": true}),
            serde_json::json!({"version": "1"}),
            serde_json::json!({"version": true}),
            serde_json::json!({"version": 1.5}),
        ] {
            let mut candidate = data.as_object().unwrap().clone();
            for (key, value) in update.as_object().unwrap() {
                candidate.insert(key.clone(), value.clone());
            }
            assert!(RestoreInventory::from_snapshot_data(&Value::Object(candidate)).is_err());
        }
    }
}
