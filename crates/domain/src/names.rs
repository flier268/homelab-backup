//! Service, display, volume, and container name grammars.

use crate::error::PolicyError;
use crate::yaml::Yaml;

pub fn valid_token(value: &str) -> bool {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    chars.all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '-'))
}

pub fn valid_service_name(value: &str) -> bool {
    valid_token(value)
}

pub fn valid_display_name(value: &str) -> bool {
    !value.is_empty() && value.split(' ').all(valid_token)
}

pub fn valid_container_name(value: &str) -> bool {
    valid_token(value) && value.len() <= 255
}

pub fn validate_docker_volume_name(value: &str, field: &str) -> Result<(), PolicyError> {
    if valid_token(value) {
        Ok(())
    } else {
        Err(PolicyError::new(format!(
            "{field} must be a safe Docker volume name"
        )))
    }
}

pub fn validate_service_relative_directory(value: &Yaml, field: &str) -> Result<(), PolicyError> {
    let Some(text) = value.as_str().filter(|text| !text.is_empty()) else {
        return Err(PolicyError::new(format!(
            "{field} must be a non-empty relative path"
        )));
    };
    if text.starts_with('/') || posix_text(text) != text || text.split('/').next().is_none() {
        return Err(PolicyError::new(format!(
            "{field} must be a normalized relative path"
        )));
    }
    if text.split('/').any(|part| !valid_display_name(part)) {
        return Err(PolicyError::new(format!(
            "{field} contains an unsupported path component"
        )));
    }
    Ok(())
}

/// `Path.as_posix()` without resolving `.` or `..`.
fn posix_text(value: &str) -> String {
    let parts: Vec<&str> = value.split('/').filter(|part| !part.is_empty()).collect();
    parts.join("/")
}
