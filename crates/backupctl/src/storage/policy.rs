//! Where a path source is allowed to point.

use std::collections::BTreeMap;

use homelab_backup_domain::manifest::path_source_control_overlap;
use homelab_backup_domain::manifest::source_path;
use homelab_backup_domain::paths::{lexical_absolute, paths_overlap};
use homelab_backup_domain::yaml::Yaml;

use crate::unix::error::SecurityError;

const FORBIDDEN_ROOTS: &[&str] = &[
    "staging_root",
    "restore_root",
    "cache_root",
    "state_root",
    "password_file",
    "rclone_config",
    "lock_file",
];

pub fn build_path_filter_args(
    exclude: &[String],
    include: &[String],
    protect_dirs: bool,
) -> Vec<String> {
    let mut args = Vec::new();
    for item in exclude {
        args.push("--exclude".to_owned());
        args.push(item.clone());
    }
    if !include.is_empty() {
        if protect_dirs {
            args.push("--filter".to_owned());
            args.push("P */".to_owned());
        }
        args.push("--include".to_owned());
        args.push("*/".to_owned());
        for item in include {
            args.push("--include".to_owned());
            args.push(item.clone());
        }
        args.push("--exclude".to_owned());
        args.push("*".to_owned());
        args.push("--prune-empty-dirs".to_owned());
    }
    args
}

pub fn validate_path_source_policy(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    source: &BTreeMap<String, Yaml>,
) -> Result<String, SecurityError> {
    let resolved =
        source_path(manifest, source).map_err(|error| SecurityError::invalid(error.message))?;
    let path =
        lexical_absolute(&resolved).map_err(|error| SecurityError::invalid(error.message))?;
    if path == "/" {
        return Err(SecurityError::invalid(
            "path source must not be the filesystem root: /",
        ));
    }
    for key in FORBIDDEN_ROOTS {
        if let Some(value) = config.get(*key).filter(|value| !value.is_empty())
            && paths_overlap(&path, value).unwrap_or(false)
        {
            return Err(SecurityError::invalid(format!(
                "path source {path} must not overlap {key}: {value}"
            )));
        }
    }
    if let Some(repository) = local_repository_path(config.get("repository").map(String::as_str))
        && paths_overlap(&path, &repository).unwrap_or(false)
    {
        return Err(SecurityError::invalid(format!(
            "path source {path} must not overlap repository: {repository}"
        )));
    }
    if let Some((name, control)) = path_source_control_overlap(manifest, source)
        .map_err(|error| SecurityError::invalid(error.message))?
    {
        return Err(SecurityError::invalid(format!(
            "path source {path} must not overlap protected {name}: {control}"
        )));
    }
    Ok(path)
}

fn local_repository_path(repository: Option<&str>) -> Option<String> {
    let repository = repository?;
    if let Some(path) = repository.strip_prefix("local:")
        && path.starts_with('/')
    {
        return lexical_absolute(path).ok();
    }
    if repository.starts_with('/') {
        return lexical_absolute(repository).ok();
    }
    None
}
