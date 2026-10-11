//! Global `config.yaml` checks. Reading the protected file stays in the runtime.

use std::collections::BTreeMap;

use crate::duration::parse_duration;
use crate::error::PolicyError;
use crate::paths::paths_overlap;
use crate::yaml::{Yaml, python_string_list, unknown_keys};

#[derive(Debug)]
pub struct ValidatedConfig {
    pub values: BTreeMap<String, Yaml>,
    pub deprecation_warning: Option<String>,
}

pub fn validate_global_config(
    cfg_path: &str,
    config: &BTreeMap<String, Yaml>,
) -> Result<ValidatedConfig, PolicyError> {
    validate_header(cfg_path, config)?;
    let mut values = config.clone();
    let deprecation_warning = discard_trusted_roots(cfg_path, &mut values);
    validate_optional_sections(cfg_path, &values)?;
    validate_root_separation(&values)?;
    Ok(ValidatedConfig {
        values,
        deprecation_warning,
    })
}

fn validate_header(cfg_path: &str, config: &BTreeMap<String, Yaml>) -> Result<(), PolicyError> {
    let allowed = [
        "version",
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
        "trusted_data_roots",
        "rclone",
        "check",
        "alert",
    ];
    let unknown = unknown_keys(config, &allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{cfg_path}: unsupported fields: {}",
            python_string_list(unknown)
        )));
    }
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
        if config
            .get(key)
            .and_then(Yaml::as_str)
            .is_none_or(str::is_empty)
        {
            return Err(PolicyError::new(format!(
                "{cfg_path}: {key} must be a non-empty string"
            )));
        }
    }
    if config.get("version").and_then(Yaml::as_int) != Some(1) {
        return Err(PolicyError::new(format!("{cfg_path}: version must be 1")));
    }
    Ok(())
}

fn discard_trusted_roots(cfg_path: &str, config: &mut BTreeMap<String, Yaml>) -> Option<String> {
    config.remove("trusted_data_roots")?;
    Some(format!(
        "WARNING: {cfg_path}: trusted_data_roots is deprecated and ignored; remove it from the configuration"
    ))
}

fn validate_optional_sections(
    cfg_path: &str,
    config: &BTreeMap<String, Yaml>,
) -> Result<(), PolicyError> {
    let sections = [
        ("rclone", &["bwlimit"][..]),
        ("check", &["read_data_subset"][..]),
        (
            "alert",
            &[
                "max_success_age",
                "max_lock_age",
                "notify_url_file",
                "renotify_after",
            ][..],
        ),
    ];
    for (name, fields) in sections {
        let Some(section) = config.get(name) else {
            continue;
        };
        let Some(section) = section.as_map() else {
            return Err(PolicyError::new(format!(
                "{cfg_path}: {name} must be a mapping"
            )));
        };
        let unknown = unknown_keys(section, fields);
        if !unknown.is_empty() {
            return Err(PolicyError::new(format!(
                "{cfg_path}: unsupported {name} fields: {}",
                python_string_list(unknown)
            )));
        }
    }
    if let Some(limit) = config
        .get("rclone")
        .and_then(Yaml::as_map)
        .and_then(|section| section.get("bwlimit"))
        && limit.as_str().is_none()
    {
        return Err(PolicyError::new(format!(
            "{cfg_path}: rclone.bwlimit must be a string"
        )));
    }
    if let Some(subset) = config
        .get("check")
        .and_then(Yaml::as_map)
        .and_then(|section| section.get("read_data_subset"))
        && subset.as_str().is_none_or(str::is_empty)
    {
        return Err(PolicyError::new(format!(
            "{cfg_path}: check.read_data_subset must be a non-empty string"
        )));
    }
    let alert = config.get("alert").and_then(Yaml::as_map);
    if let Some(alert) = alert {
        for field in ["max_success_age", "max_lock_age", "renotify_after"] {
            if let Some(value) = alert.get(field) {
                let Some(text) = value.as_str() else {
                    return Err(PolicyError::new(format!(
                        "{cfg_path}: alert.{field} must be a duration string such as 30m, 6h, or 1d"
                    )));
                };
                parse_duration(text, &format!("alert.{field}"))
                    .map_err(|err| PolicyError::new(format!("{cfg_path}: {err}")))?;
            }
        }
        if let Some(url_file) = alert.get("notify_url_file")
            && url_file
                .as_str()
                .is_none_or(|value| !value.starts_with('/'))
        {
            return Err(PolicyError::new(format!(
                "{cfg_path}: alert.notify_url_file must be an absolute path"
            )));
        }
    }
    Ok(())
}

fn validate_root_separation(config: &BTreeMap<String, Yaml>) -> Result<(), PolicyError> {
    let roots = ["services_root", "staging_root", "restore_root"];
    for (index, left_name) in roots.iter().enumerate() {
        for right_name in &roots[index + 1..] {
            if paths_overlap(required(config, left_name), required(config, right_name))? {
                return Err(PolicyError::new(format!(
                    "{left_name} must not overlap {right_name}"
                )));
            }
        }
    }
    let protected = [
        "password_file",
        "rclone_config",
        "cache_root",
        "state_root",
        "lock_file",
    ];
    for root_name in ["staging_root", "restore_root"] {
        for path_name in protected {
            if paths_overlap(required(config, root_name), required(config, path_name))? {
                return Err(PolicyError::new(format!(
                    "{root_name} must not overlap {path_name}"
                )));
            }
        }
    }
    Ok(())
}

fn required<'a>(config: &'a BTreeMap<String, Yaml>, key: &str) -> &'a str {
    config.get(key).and_then(Yaml::as_str).unwrap_or("")
}
