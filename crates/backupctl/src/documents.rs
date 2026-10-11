//! Protected YAML documents converted into the domain value tree.
//! Parsing uses noyalib, a maintained serde YAML 1.2 library.
//! `yes` and `on` stay strings unless they are written as real booleans.

use std::collections::BTreeMap;
use std::path::Path;

use homelab_backup_domain::config::validate_global_config;
use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{AtFlags, CWD, statat};
use rustix::io::Errno;

use crate::unix::control::validate_control_file;
use crate::unix::error::SecurityError;

pub const CONFIG_PATH: &str = "/etc/homelab-backup/config.yaml";

pub fn load_global_config(path: &str) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    let document = read_yaml_document(path).map_err(|error| match error {
        ReadError::Missing => SecurityError::invalid(format!("missing {path}")),
        ReadError::Other(message) => {
            SecurityError::invalid(format!("cannot securely read {path}: {message}"))
        }
    })?;
    let Yaml::Map(mut config) = document else {
        return Err(SecurityError::invalid(format!(
            "{path} must contain a YAML mapping"
        )));
    };
    let validated = validate_global_config(path, &config)
        .map_err(|error| SecurityError::invalid(error.message))?;
    if let Some(warning) = validated.deprecation_warning {
        eprintln!("{warning}");
    }
    config = validated.values;
    if let Ok(image) = std::env::var("HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE")
        && !image.is_empty()
    {
        config.insert("volume_helper_image".to_owned(), Yaml::string(image));
    }
    Ok(config)
}

pub fn read_manifest_document(path: &Path) -> Result<Yaml, String> {
    let path = path
        .to_str()
        .ok_or_else(|| "path is not utf-8".to_owned())?;
    match read_yaml_document(path) {
        Ok(Yaml::Null) => Ok(Yaml::Map(BTreeMap::new())),
        Ok(value) => Ok(value),
        Err(ReadError::Missing) => Err(format!("missing {path}")),
        Err(ReadError::Other(message)) => {
            if message.contains("YAML") {
                Err(format!("{path}: invalid manifest YAML: {message}"))
            } else {
                Err(message)
            }
        }
    }
}

enum ReadError {
    Missing,
    Other(String),
}

fn read_yaml_document(path: &str) -> Result<Yaml, ReadError> {
    let absolute = homelab_backup_domain::paths::lexical_absolute(path)
        .map_err(|error| ReadError::Other(error.message))?;
    match statat(CWD, absolute.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
        Err(Errno::NOENT) => return Err(ReadError::Missing),
        Err(error) => return Err(ReadError::Other(error.to_string())),
        Ok(_) => {}
    }
    validate_control_file(&absolute).map_err(|error| ReadError::Other(error.message))?;
    let text =
        std::fs::read_to_string(&absolute).map_err(|error| ReadError::Other(error.to_string()))?;
    if text.trim().is_empty() {
        return Ok(Yaml::Map(BTreeMap::new()));
    }
    let value = noyalib::from_str::<noyalib::Value>(&text)
        .map_err(|error| ReadError::Other(format!("YAML: {error}")))?;
    match convert(value) {
        Ok(Yaml::Null) => Ok(Yaml::Map(BTreeMap::new())),
        Ok(value) => Ok(value),
        Err(message) => Err(ReadError::Other(format!("YAML: {message}"))),
    }
}

fn convert(value: noyalib::Value) -> Result<Yaml, String> {
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
            .map(convert)
            .collect::<Result<Vec<_>, _>>()
            .map(Yaml::Seq),
        noyalib::Value::Mapping(mapping) => {
            let mut object = BTreeMap::new();
            for (key, value) in mapping {
                object.insert(key, convert(value)?);
            }
            Ok(Yaml::Map(object))
        }
        noyalib::Value::Tagged(_) => Err("YAML tags are not supported".to_owned()),
    }
}
