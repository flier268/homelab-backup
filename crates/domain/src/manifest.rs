//! Service manifest checks that do not read the filesystem.
//! Discovery, control-file permissions, and Compose execution stay in the runtime.

use std::collections::{BTreeMap, HashSet};

use crate::error::PolicyError;
use crate::identity::validate_identity;
use crate::names::{
    valid_container_name, valid_display_name, valid_service_name, validate_docker_volume_name,
    validate_service_relative_directory,
};
use crate::paths::{join_service_path, lexical_absolute, paths_overlap};
use crate::schedule::validate_schedule_value;
use crate::yaml::{Yaml, python_repr, python_string_list, unknown_keys};

pub const RETENTION_FLAGS: &[(&str, &str)] = &[
    ("keep_last", "--keep-last"),
    ("keep_hourly", "--keep-hourly"),
    ("keep_daily", "--keep-daily"),
    ("keep_weekly", "--keep-weekly"),
    ("keep_monthly", "--keep-monthly"),
    ("keep_yearly", "--keep-yearly"),
];

const CONSISTENCY_MODES: &[&str] = &["stop", "hooks", "external", "live", "snapshot", "files"];
const ACTION_PHASES: &[&str] = &["before", "after", "finally", "on_success", "on_failure"];
const LEGACY_ACTION_PHASES: &[&str] = &["before", "finally", "on_success", "on_failure"];

pub fn service_label(manifest: &BTreeMap<String, Yaml>) -> String {
    let service = manifest
        .get("service")
        .and_then(Yaml::as_str)
        .unwrap_or("<unnamed>");
    let name = manifest
        .get("name")
        .and_then(Yaml::as_str)
        .unwrap_or(service);
    if name == service {
        service.to_owned()
    } else {
        format!("{name} [{service}]")
    }
}

pub fn requires_docker(manifest: &BTreeMap<String, Yaml>) -> bool {
    let Some(consistency) = manifest.get("consistency").and_then(Yaml::as_map) else {
        return true;
    };
    consistency
        .get("mode")
        .and_then(Yaml::as_str)
        .unwrap_or("stop")
        != "files"
}

pub fn validate_manifest(manifest: &Yaml) -> Result<(), PolicyError> {
    let Some(manifest) = manifest.as_map() else {
        return Err(PolicyError::new("manifest must be a mapping"));
    };
    let path = validate_header(manifest)?;
    validate_consistency(manifest, &path)?;
    validate_actions(manifest, &path)?;
    validate_compose(manifest, &path)?;
    validate_schedule_value(&path, manifest.get("schedule"))?;
    validate_retention(manifest, &path)?;
    validate_sources(manifest, &path)?;
    Ok(())
}

fn path_label(manifest: &BTreeMap<String, Yaml>) -> String {
    match manifest.get("_path") {
        None => "<manifest>".to_owned(),
        Some(Yaml::String(value)) => value.clone(),
        Some(value) => python_repr(value),
    }
}

fn validate_header(manifest: &BTreeMap<String, Yaml>) -> Result<String, PolicyError> {
    let path = path_label(manifest);
    let allowed = [
        "version",
        "service",
        "name",
        "enabled",
        "schedule",
        "retention",
        "create_snapshot_if_unchanged",
        "compose",
        "consistency",
        "actions",
        "sources",
        "_path",
        "_dir",
        "_relative_dir",
        "_snapshot_manifest",
        "_restore_manifest_requested",
    ];
    let unknown = unknown_keys(manifest, &allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported manifest fields: {}",
            python_string_list(unknown)
        )));
    }
    if manifest.get("version").and_then(Yaml::as_int) != Some(1) {
        return Err(PolicyError::new(format!("{path}: version must be 1")));
    }
    if !manifest.get("service").is_some_and(Yaml::is_truthy) {
        return Err(PolicyError::new(format!("{path}: missing service")));
    }
    let service = manifest.get("service").and_then(Yaml::as_str).unwrap_or("");
    if !valid_service_name(service) {
        return Err(PolicyError::new(format!(
            "{path}: service contains unsupported characters"
        )));
    }
    if let Some(name) = manifest.get("name") {
        let valid = name.as_str().is_some_and(valid_display_name);
        if !valid {
            return Err(PolicyError::new(format!(
                "{path}: name contains unsupported characters"
            )));
        }
    }
    if let Some(relative) = manifest.get("_relative_dir") {
        validate_service_relative_directory(relative, &format!("{path}: service directory"))?;
    }
    require_bool(
        manifest.get("enabled"),
        true,
        &format!("{path}: enabled must be boolean"),
    )?;
    require_bool(
        manifest.get("create_snapshot_if_unchanged"),
        false,
        &format!("{path}: create_snapshot_if_unchanged must be boolean"),
    )?;
    Ok(path)
}

fn require_bool(value: Option<&Yaml>, default: bool, message: &str) -> Result<bool, PolicyError> {
    match value {
        None => Ok(default),
        Some(Yaml::Bool(value)) => Ok(*value),
        Some(_) => Err(PolicyError::new(message)),
    }
}

fn validate_consistency(manifest: &BTreeMap<String, Yaml>, path: &str) -> Result<(), PolicyError> {
    let consistency = match manifest.get("consistency") {
        None | Some(Yaml::Null) => None,
        Some(Yaml::Map(map)) => Some(map),
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: consistency must be a mapping"
            )));
        }
    };
    let empty = BTreeMap::new();
    let consistency = consistency.unwrap_or(&empty);
    let unknown = unknown_keys(consistency, &["mode", "timeout", "before", "after"]);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported consistency fields: {}",
            python_string_list(unknown)
        )));
    }
    let mode = match consistency.get("mode") {
        None => "stop",
        Some(Yaml::String(value)) => value.as_str(),
        Some(other) => {
            return Err(PolicyError::new(format!(
                "{path}: invalid consistency.mode {}; expected {}",
                python_repr(other),
                CONSISTENCY_MODES.join(", ")
            )));
        }
    };
    if !CONSISTENCY_MODES.contains(&mode) {
        return Err(PolicyError::new(format!(
            "{path}: invalid consistency.mode {}; expected {}",
            python_repr(&Yaml::string(mode)),
            CONSISTENCY_MODES.join(", ")
        )));
    }
    let timeout = match consistency.get("timeout") {
        None => 120,
        Some(Yaml::Int(value)) => *value,
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: consistency.timeout must be an integer greater than zero"
            )));
        }
    };
    if timeout <= 0 {
        return Err(PolicyError::new(format!(
            "{path}: consistency.timeout must be an integer greater than zero"
        )));
    }
    for hook in ["before", "after"] {
        let values = consistency.get(hook);
        let hooks = match values {
            None => Vec::new(),
            Some(Yaml::Seq(items)) => items
                .iter()
                .map(|item| {
                    item.as_str()
                        .filter(|text| !text.is_empty())
                        .map(str::to_owned)
                })
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| {
                    PolicyError::new(format!(
                        "{path}: consistency.{hook} must be a list of non-empty strings"
                    ))
                })?,
            Some(_) => {
                return Err(PolicyError::new(format!(
                    "{path}: consistency.{hook} must be a list of non-empty strings"
                )));
            }
        };
        if mode != "hooks" && !hooks.is_empty() {
            return Err(PolicyError::new(format!(
                "{path}: consistency.{hook} is only valid with mode hooks"
            )));
        }
    }
    Ok(())
}

fn validate_retention(manifest: &BTreeMap<String, Yaml>, path: &str) -> Result<(), PolicyError> {
    let Some(Yaml::Map(retention)) = manifest.get("retention") else {
        return Err(PolicyError::new(format!(
            "{path}: retention must be a non-empty mapping"
        )));
    };
    if retention.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: retention must be a non-empty mapping"
        )));
    }
    let allowed: Vec<&str> = RETENTION_FLAGS.iter().map(|(key, _)| *key).collect();
    let unknown = unknown_keys(retention, &allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported retention fields: {}",
            python_string_list(unknown)
        )));
    }
    let mut found = false;
    let mut keeps_one = false;
    for (key, _) in RETENTION_FLAGS {
        if let Some(value) = retention.get(*key) {
            let Some(count) = value.as_int() else {
                return Err(PolicyError::new(format!(
                    "{path}: retention.{key} must be an integer >= 0"
                )));
            };
            if count < 0 {
                return Err(PolicyError::new(format!(
                    "{path}: retention.{key} must be an integer >= 0"
                )));
            }
            found = true;
            if count > 0 {
                keeps_one = true;
            }
        }
    }
    if !found {
        return Err(PolicyError::new(format!(
            "{path}: retention must define at least one keep_* rule"
        )));
    }
    if !keeps_one {
        return Err(PolicyError::new(format!(
            "{path}: retention must keep at least one snapshot"
        )));
    }
    Ok(())
}

fn validate_actions(manifest: &BTreeMap<String, Yaml>, path: &str) -> Result<(), PolicyError> {
    let Some(actions) = manifest.get("actions") else {
        return Ok(());
    };
    let Some(actions) = actions.as_map() else {
        return Err(PolicyError::new(format!(
            "{path}: actions must be a mapping"
        )));
    };
    let operations = ["backup", "restore"];
    let nested = operations.iter().any(|key| actions.contains_key(*key));
    let legacy = LEGACY_ACTION_PHASES
        .iter()
        .any(|key| actions.contains_key(*key));
    if nested && legacy {
        return Err(PolicyError::new(format!(
            "{path}: nested backup/restore actions cannot be mixed with legacy flat actions"
        )));
    }
    let allowed: Vec<&str> = if nested {
        operations.to_vec()
    } else {
        LEGACY_ACTION_PHASES.to_vec()
    };
    let unknown = unknown_keys(actions, &allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported actions fields: {}",
            python_string_list(unknown)
        )));
    }
    let consistency = manifest.get("consistency").and_then(Yaml::as_map);
    if nested
        && consistency.is_some_and(|map| map.contains_key("before") || map.contains_key("after"))
    {
        return Err(PolicyError::new(format!(
            "{path}: nested actions cannot be mixed with consistency.before/after hooks"
        )));
    }
    let mut seen = HashSet::new();
    if nested {
        for operation in operations {
            let section = match actions.get(operation) {
                None => {
                    let empty = BTreeMap::new();
                    validate_action_section(path, operation, &empty, &mut seen)?;
                    continue;
                }
                Some(Yaml::Map(section)) => section,
                Some(_) => {
                    return Err(PolicyError::new(format!(
                        "{path}: actions.{operation} must be a mapping"
                    )));
                }
            };
            validate_action_section(path, operation, section, &mut seen)?;
        }
    } else {
        validate_action_section(path, "", actions, &mut seen)?;
    }
    Ok(())
}

fn validate_action_section(
    path: &str,
    operation: &str,
    section: &BTreeMap<String, Yaml>,
    seen: &mut HashSet<String>,
) -> Result<(), PolicyError> {
    let phases: &[&str] = if operation.is_empty() {
        LEGACY_ACTION_PHASES
    } else {
        ACTION_PHASES
    };
    let unknown = unknown_keys(section, phases);
    if !unknown.is_empty() {
        let prefix = if operation.is_empty() {
            "actions".to_owned()
        } else {
            format!("actions.{operation}")
        };
        return Err(PolicyError::new(format!(
            "{path}: unsupported {prefix} fields: {}",
            python_string_list(unknown)
        )));
    }
    for phase in phases {
        let entries = match section.get(*phase) {
            None => continue,
            Some(Yaml::Seq(entries)) => entries,
            Some(_) => {
                let prefix = if operation.is_empty() {
                    String::new()
                } else {
                    format!("{operation}.")
                };
                return Err(PolicyError::new(format!(
                    "{path}: actions.{prefix}{phase} must be a list"
                )));
            }
        };
        for (index, action) in entries.iter().enumerate() {
            let prefix = if operation.is_empty() {
                String::new()
            } else {
                format!("{operation}.")
            };
            let field = format!("{path}: actions.{prefix}{phase}[{index}]");
            if operation == "restore"
                && action
                    .as_map()
                    .is_some_and(|map| map.contains_key("skip_if_not_running"))
            {
                return Err(PolicyError::new(format!(
                    "{field}.skip_if_not_running is only supported for backup actions"
                )));
            }
            if let Err(error) = validate_action_definition(action, &field, seen) {
                if error.message.starts_with("duplicate action name") {
                    return Err(PolicyError::new(format!("{path}: {}", error.message)));
                }
                return Err(error);
            }
        }
    }
    Ok(())
}

fn validate_action_definition(
    action: &Yaml,
    field: &str,
    seen: &mut HashSet<String>,
) -> Result<(), PolicyError> {
    let Some(action) = action.as_map() else {
        return Err(PolicyError::new(format!("{field} must be a mapping")));
    };
    let unknown = unknown_keys(
        action,
        &[
            "name",
            "command",
            "script",
            "timeout",
            "required",
            "run_as",
            "skip_if_not_running",
        ],
    );
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{field} has unsupported fields: {}",
            python_string_list(unknown)
        )));
    }
    let name = action.get("name").and_then(Yaml::as_str).unwrap_or("");
    if name.is_empty() {
        return Err(PolicyError::new(format!(
            "{field}.name must be a non-empty string"
        )));
    }
    if !seen.insert(name.to_owned()) {
        return Err(PolicyError::new(format!(
            "duplicate action name {}",
            python_repr(&Yaml::string(name))
        )));
    }
    validate_identity(action.get("run_as"), &format!("{field}.run_as"))?;
    let command = action.get("command");
    let script = action.get("script");
    if command.is_none() == script.is_none() {
        return Err(PolicyError::new(format!(
            "{field} must define exactly one of command or script"
        )));
    }
    if let Some(command) = command {
        let valid = command.as_seq().is_some_and(|items| {
            !items.is_empty()
                && items
                    .iter()
                    .all(|item| item.as_str().is_some_and(|text| !text.is_empty()))
        });
        if !valid {
            return Err(PolicyError::new(format!(
                "{field}.command must be a non-empty list of non-empty strings"
            )));
        }
    }
    if let Some(script) = script
        && script.as_str().is_none_or(str::is_empty)
    {
        return Err(PolicyError::new(format!(
            "{field}.script must be a non-empty string"
        )));
    }
    let timeout = match action.get("timeout") {
        None => 30,
        Some(Yaml::Int(value)) => *value,
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{field}.timeout must be an integer greater than zero"
            )));
        }
    };
    if timeout <= 0 {
        return Err(PolicyError::new(format!(
            "{field}.timeout must be an integer greater than zero"
        )));
    }
    require_bool(
        action.get("required"),
        true,
        &format!("{field}.required must be boolean"),
    )?;
    if let Some(containers) = action.get("skip_if_not_running") {
        let items: Vec<&str> = match containers {
            Yaml::String(value) => vec![value.as_str()],
            Yaml::Seq(items) => items.iter().filter_map(Yaml::as_str).collect(),
            _ => Vec::new(),
        };
        let count_matches = match containers {
            Yaml::String(_) => true,
            Yaml::Seq(values) => values.len() == items.len(),
            _ => false,
        };
        if !count_matches
            || items.is_empty()
            || items.iter().any(|item| !valid_container_name(item))
        {
            return Err(PolicyError::new(format!(
                "{field}.skip_if_not_running must be a container name or a non-empty list of container names"
            )));
        }
    }
    Ok(())
}

fn validate_compose(manifest: &BTreeMap<String, Yaml>, path: &str) -> Result<(), PolicyError> {
    if !requires_docker(manifest) && manifest.contains_key("compose") {
        return Err(PolicyError::new(format!(
            "{path}: compose is not supported with consistency.mode files"
        )));
    }
    let compose = match manifest.get("compose") {
        None | Some(Yaml::Null) => None,
        Some(Yaml::Map(map)) => Some(map),
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: compose must be a mapping"
            )));
        }
    };
    let empty = BTreeMap::new();
    let compose = compose.unwrap_or(&empty);
    let unknown = unknown_keys(compose, &["files", "env_file"]);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported compose fields: {}",
            python_string_list(unknown)
        )));
    }
    match compose.get("files") {
        None => {}
        Some(Yaml::Seq(files))
            if !files.is_empty()
                && files
                    .iter()
                    .all(|item| item.as_str().is_some_and(|text| !text.is_empty())) => {}
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: compose.files must be a non-empty list of strings"
            )));
        }
    }
    if let Some(env_file) = compose.get("env_file")
        && !matches!(env_file, Yaml::Null)
        && env_file.as_str().is_none_or(str::is_empty)
    {
        return Err(PolicyError::new(format!(
            "{path}: compose.env_file must be a non-empty string"
        )));
    }
    Ok(())
}

fn validate_sources(manifest: &BTreeMap<String, Yaml>, path: &str) -> Result<(), PolicyError> {
    let sources = match manifest.get("sources") {
        None | Some(Yaml::Null) => None,
        Some(Yaml::Map(map)) => Some(map),
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: sources must be a mapping"
            )));
        }
    };
    let empty = BTreeMap::new();
    let sources = sources.unwrap_or(&empty);
    let unknown = unknown_keys(sources, &["paths", "volumes"]);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported sources fields: {}",
            python_string_list(unknown)
        )));
    }
    let mut ids = HashSet::new();
    let mut path_targets = Vec::new();
    let mut volume_targets = HashSet::new();
    for kind in ["paths", "volumes"] {
        let entries = match sources.get(kind) {
            None => continue,
            Some(Yaml::Seq(entries)) => entries,
            Some(_) => {
                return Err(PolicyError::new(format!(
                    "{path}: sources.{kind} must be a list"
                )));
            }
        };
        if kind == "volumes" && !entries.is_empty() && !requires_docker(manifest) {
            return Err(PolicyError::new(format!(
                "{path}: sources.volumes is not supported with consistency.mode files"
            )));
        }
        for (index, source) in entries.iter().enumerate() {
            let Some(source) = source.as_map() else {
                return Err(PolicyError::new(format!(
                    "{path}: sources.{kind}[{index}] must be a mapping"
                )));
            };
            validate_source_common(path, kind, index, source, &mut ids)?;
            if kind == "paths" {
                validate_path_source(manifest, path, index, source, &mut path_targets)?;
            } else {
                validate_volume_source(path, index, source, &mut volume_targets)?;
            }
        }
    }
    Ok(())
}

fn validate_source_common(
    path: &str,
    kind: &str,
    index: usize,
    source: &BTreeMap<String, Yaml>,
    ids: &mut HashSet<String>,
) -> Result<(), PolicyError> {
    let allowed: &[&str] = if kind == "paths" {
        &["id", "path", "required", "include", "exclude"]
    } else {
        &["id", "name", "compose_volume", "required", "exclude"]
    };
    let unknown = unknown_keys(source, allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported sources.{kind}[{index}] fields: {}",
            python_string_list(unknown)
        )));
    }
    let source_id = source.get("id");
    if !source_id.is_some_and(Yaml::is_truthy) {
        return Err(PolicyError::new(format!(
            "{path}: sources.{kind}[{index}] is missing id"
        )));
    }
    let Some(source_id) = source_id.and_then(Yaml::as_str) else {
        return Err(PolicyError::new(format!(
            "{path}: sources.{kind}[{index}].id must be a safe single path component"
        )));
    };
    if !valid_service_name(source_id) {
        return Err(PolicyError::new(format!(
            "{path}: sources.{kind}[{index}].id must be a safe single path component"
        )));
    }
    if !ids.insert(source_id.to_owned()) {
        return Err(PolicyError::new(format!(
            "{path}: duplicate source id {}",
            python_repr(&Yaml::string(source_id))
        )));
    }
    match source.get("exclude") {
        None => {}
        Some(Yaml::Seq(items)) if items.iter().all(|item| item.as_str().is_some()) => {}
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: sources.{kind}[{index}].exclude must be a list of strings"
            )));
        }
    }
    if kind == "paths" && source.contains_key("include") {
        let valid = source
            .get("include")
            .and_then(Yaml::as_seq)
            .is_some_and(|items| {
                !items.is_empty()
                    && items
                        .iter()
                        .all(|item| item.as_str().is_some_and(|text| !text.is_empty()))
            });
        if !valid {
            return Err(PolicyError::new(format!(
                "{path}: sources.paths[{index}].include must be a non-empty list of non-empty strings"
            )));
        }
    }
    require_bool(
        source.get("required"),
        true,
        &format!("{path}: sources.{kind}[{index}].required must be boolean"),
    )?;
    Ok(())
}

fn validate_path_source(
    manifest: &BTreeMap<String, Yaml>,
    path: &str,
    index: usize,
    source: &BTreeMap<String, Yaml>,
    path_targets: &mut Vec<String>,
) -> Result<(), PolicyError> {
    if source
        .get("path")
        .and_then(Yaml::as_str)
        .is_none_or(str::is_empty)
    {
        return Err(PolicyError::new(format!(
            "{path}: sources.paths[{index}].path must be a non-empty string"
        )));
    }
    let target = lexical_absolute(&source_path(manifest, source)?)?;
    if path_targets
        .iter()
        .any(|existing| paths_overlap(&target, existing).unwrap_or(false))
    {
        return Err(PolicyError::new(format!(
            "{path}: duplicate or overlapping path target: {target}"
        )));
    }
    if let Some((name, control)) = path_source_control_overlap(manifest, source)? {
        return Err(PolicyError::new(format!(
            "{path}: path source {target} must not overlap protected {name}: {control}"
        )));
    }
    path_targets.push(target);
    Ok(())
}

pub fn source_path(
    manifest: &BTreeMap<String, Yaml>,
    source: &BTreeMap<String, Yaml>,
) -> Result<String, PolicyError> {
    let value = source.get("path").and_then(Yaml::as_str).unwrap_or("");
    if value.starts_with('/') {
        return Ok(value.to_owned());
    }
    let Some(directory) = manifest.get("_dir").and_then(Yaml::as_str) else {
        return Err(PolicyError::new(
            "manifest is missing _dir for a relative source path",
        ));
    };
    Ok(join_service_path(directory, value))
}

pub fn path_source_control_overlap(
    manifest: &BTreeMap<String, Yaml>,
    source: &BTreeMap<String, Yaml>,
) -> Result<Option<(&'static str, String)>, PolicyError> {
    let target = lexical_absolute(&source_path(manifest, source)?)?;
    let manifest_target = manifest_target(manifest)?;
    if paths_overlap(&target, &manifest_target)? {
        return Ok(Some(("manifest", manifest_target)));
    }
    let files_mode = manifest
        .get("consistency")
        .and_then(Yaml::as_map)
        .and_then(|map| map.get("mode"))
        .and_then(Yaml::as_str)
        == Some("files");
    if files_mode {
        return Ok(None);
    }
    for control in compose_control_paths(manifest)? {
        if target != control && paths_overlap(&target, &control)? {
            return Ok(Some(("Compose control", control)));
        }
    }
    Ok(None)
}

fn manifest_target(manifest: &BTreeMap<String, Yaml>) -> Result<String, PolicyError> {
    if let Some(path) = manifest.get("_path").and_then(Yaml::as_str) {
        return lexical_absolute(path);
    }
    let Some(directory) = manifest.get("_dir").and_then(Yaml::as_str) else {
        return Err(PolicyError::new("manifest is missing _dir"));
    };
    lexical_absolute(&join_service_path(directory, "backup.yaml"))
}

fn compose_control_paths(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<String>, PolicyError> {
    let compose = manifest.get("compose").and_then(Yaml::as_map);
    let mut paths = Vec::new();
    let files = compose
        .and_then(|map| map.get("files"))
        .and_then(Yaml::as_seq);
    if let Some(files) = files {
        for file in files {
            if let Some(file) = file.as_str() {
                paths.push(service_control_path(manifest, file)?);
            }
        }
    } else {
        paths.push(service_control_path(manifest, "compose.yaml")?);
    }
    if let Some(env_file) = compose
        .and_then(|map| map.get("env_file"))
        .and_then(Yaml::as_str)
        .filter(|value| !value.is_empty())
    {
        paths.push(service_control_path(manifest, env_file)?);
    }
    Ok(paths)
}

fn service_control_path(
    manifest: &BTreeMap<String, Yaml>,
    value: &str,
) -> Result<String, PolicyError> {
    if value.starts_with('/') {
        lexical_absolute(value)
    } else {
        let Some(directory) = manifest.get("_dir").and_then(Yaml::as_str) else {
            return Err(PolicyError::new("manifest is missing _dir"));
        };
        lexical_absolute(&join_service_path(directory, value))
    }
}

fn validate_volume_source(
    path: &str,
    index: usize,
    source: &BTreeMap<String, Yaml>,
    volume_targets: &mut HashSet<(String, String)>,
) -> Result<(), PolicyError> {
    let fields: Vec<&str> = ["name", "compose_volume"]
        .into_iter()
        .filter(|key| source.get(*key).is_some_and(Yaml::is_truthy))
        .collect();
    if fields.len() != 1 {
        return Err(PolicyError::new(format!(
            "{path}: sources.volumes[{index}] must define exactly one of name or compose_volume"
        )));
    }
    let field_name = fields[0];
    let Some(value) = source.get(field_name).and_then(Yaml::as_str) else {
        return Err(PolicyError::new(format!(
            "{path}: sources.volumes[{index}].{field_name} must be a non-empty string"
        )));
    };
    if value.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: sources.volumes[{index}].{field_name} must be a non-empty string"
        )));
    }
    if field_name == "name" {
        validate_docker_volume_name(value, &format!("{path}: sources.volumes[{index}].name"))?;
    }
    if !volume_targets.insert((field_name.to_owned(), value.to_owned())) {
        return Err(PolicyError::new(format!(
            "{path}: duplicate Docker volume target declaration: {value}"
        )));
    }
    Ok(())
}
