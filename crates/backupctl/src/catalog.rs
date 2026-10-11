//! Discover `backup.yaml` manifests without following directory symlinks.

use std::collections::BTreeMap;
use std::path::Path;

use homelab_backup_domain::names::{valid_service_name, validate_service_relative_directory};
use homelab_backup_domain::yaml::Yaml;

use crate::documents::read_manifest_document;
use crate::unix::control::validate_control_directory;
use crate::unix::error::SecurityError;

pub struct SelectedService {
    pub manifest: BTreeMap<String, Yaml>,
}

pub struct SelectionFailure {
    pub operation: String,
    pub summary: String,
    pub line: String,
}

pub fn select_services(
    config: &BTreeMap<String, Yaml>,
    names: &[String],
) -> Result<(Vec<SelectedService>, Vec<SelectionFailure>), SecurityError> {
    select_services_filtered(config, names, false)
}

pub fn select_every_manifest(
    config: &BTreeMap<String, Yaml>,
) -> Result<(Vec<SelectedService>, Vec<SelectionFailure>), SecurityError> {
    select_services_filtered(config, &[], true)
}

fn select_services_filtered(
    config: &BTreeMap<String, Yaml>,
    names: &[String],
    include_disabled: bool,
) -> Result<(Vec<SelectedService>, Vec<SelectionFailure>), SecurityError> {
    let services_root = config
        .get("services_root")
        .and_then(Yaml::as_str)
        .ok_or_else(|| SecurityError::invalid("missing services_root"))?;
    let services_root = validate_control_directory(services_root, false, None)?;
    let mut paths = Vec::new();
    walk(Path::new(&services_root), &mut paths)?;
    paths.sort();
    let mut loaded = Vec::new();
    let mut failures = Vec::new();
    for path in paths {
        match load_manifest(&services_root, &path) {
            Ok(manifest) => loaded.push(manifest),
            Err(message) => {
                let display = path.display().to_string();
                failures.push(SelectionFailure {
                    operation: display.clone(),
                    summary: format!("manifest loading failed: {message}"),
                    line: format!("ERROR: cannot load manifest {display}: {message}"),
                });
            }
        }
    }
    let mut grouped: BTreeMap<String, Vec<BTreeMap<String, Yaml>>> = BTreeMap::new();
    for manifest in loaded {
        if let Some(service) = manifest.get("service").and_then(Yaml::as_str)
            && valid_service_name(service)
        {
            grouped
                .entry(service.to_owned())
                .or_default()
                .push(manifest);
        }
    }
    let mut duplicates = BTreeMap::new();
    for (service, manifests) in &grouped {
        if manifests.len() > 1 {
            let paths = manifests
                .iter()
                .filter_map(|manifest| manifest.get("_path").and_then(Yaml::as_str))
                .collect::<Vec<_>>()
                .join(", ");
            duplicates.insert(
                service.clone(),
                format!("duplicate service ID '{service}': {paths}"),
            );
        }
    }
    if names.is_empty() {
        for (service, message) in &duplicates {
            if let Some(manifests) = grouped.get(service) {
                for manifest in manifests {
                    let path = manifest
                        .get("_path")
                        .and_then(Yaml::as_str)
                        .unwrap_or(service);
                    failures.push(SelectionFailure {
                        operation: path.to_owned(),
                        summary: format!("manifest loading failed: {message}"),
                        line: format!("ERROR: cannot load manifest {path}: {message}"),
                    });
                }
            }
        }
        let ready = grouped
            .into_iter()
            .filter(|(service, _)| !duplicates.contains_key(service))
            .flat_map(|(_, manifests)| manifests)
            .filter(|manifest| {
                include_disabled || manifest.get("enabled").and_then(Yaml::as_bool) != Some(false)
            })
            .map(|manifest| SelectedService { manifest })
            .collect();
        return Ok((ready, failures));
    }
    let mut ready = Vec::new();
    let mut seen = BTreeMap::<String, ()>::new();
    for name in names {
        if seen.insert(name.clone(), ()).is_some() {
            continue;
        }
        if let Some(message) = duplicates.get(name) {
            failures.push(named_failure(name, message));
            continue;
        }
        let found = grouped.get(name).and_then(|manifests| manifests.first());
        match found {
            Some(manifest) if manifest.get("enabled").and_then(Yaml::as_bool) != Some(false) => {
                ready.push(SelectedService {
                    manifest: manifest.clone(),
                });
            }
            _ => failures.push(named_failure(
                name,
                &format!("unknown or disabled service: {name}"),
            )),
        }
    }
    Ok((ready, failures))
}

fn named_failure(name: &str, message: &str) -> SelectionFailure {
    SelectionFailure {
        operation: name.to_owned(),
        summary: format!("manifest loading failed: {message}"),
        line: format!("ERROR: cannot load service {name}: {message}"),
    }
}

fn load_manifest(services_root: &str, path: &Path) -> Result<BTreeMap<String, Yaml>, String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("{}: manifest must be a YAML mapping", path.display()))?;
    let relative = parent
        .strip_prefix(services_root)
        .map_err(|_| {
            format!(
                "{} parent directory must be a normalized relative path",
                path.display()
            )
        })?
        .to_string_lossy()
        .replace('\\', "/");
    validate_service_relative_directory(
        &Yaml::string(&relative),
        &format!("{} parent directory", path.display()),
    )
    .map_err(|error| error.message)?;
    let parent = parent
        .to_str()
        .ok_or_else(|| "path is not utf-8".to_owned())?;
    validate_control_directory(parent, false, None).map_err(|error| error.message)?;
    let document = read_manifest_document(path)?;
    let Some(mut manifest) = document.as_map().cloned() else {
        return Err(format!(
            "{}: manifest must be a YAML mapping",
            path.display()
        ));
    };
    manifest.insert("_path".to_owned(), Yaml::string(path.display().to_string()));
    manifest.insert("_dir".to_owned(), Yaml::string(parent));
    manifest.insert("_relative_dir".to_owned(), Yaml::string(relative));
    Ok(manifest)
}

fn walk(directory: &Path, paths: &mut Vec<std::path::PathBuf>) -> Result<(), SecurityError> {
    let entries = std::fs::read_dir(directory)
        .map_err(|error| SecurityError::runtime(format!("{}: {error}", directory.display())))?;
    for entry in entries {
        let entry = entry
            .map_err(|error| SecurityError::runtime(format!("{}: {error}", directory.display())))?;
        let file_type = entry
            .file_type()
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        if file_type.is_symlink() && entry.file_name() != "backup.yaml" {
            continue;
        }
        if entry.file_name() == "backup.yaml" {
            paths.push(entry.path());
            continue;
        }
        if file_type.is_dir() {
            walk(&entry.path(), paths)?;
        }
    }
    Ok(())
}
