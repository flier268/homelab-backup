//! `run_as` grammar. Looking up a local account stays in the runtime.

use crate::error::PolicyError;
use crate::yaml::Yaml;

const MAX_ID: u64 = u32::MAX as u64 - 1;

pub fn validate_identity(value: Option<&Yaml>, field: &str) -> Result<(), PolicyError> {
    let Some(Yaml::String(value)) = value else {
        return Err(identity_error(field));
    };
    if let Some((uid, gid)) = numeric_identity(value) {
        if uid <= MAX_ID && gid <= MAX_ID {
            return Ok(());
        }
    } else if valid_user_name(value) {
        return Ok(());
    }
    Err(identity_error(field))
}

fn identity_error(field: &str) -> PolicyError {
    PolicyError::new(format!(
        "{field} must be a local user name or quoted \"UID:GID\" string"
    ))
}

fn numeric_identity(value: &str) -> Option<(u64, u64)> {
    let (uid, gid) = value.split_once(':')?;
    if uid.is_empty() || gid.is_empty() || value.matches(':').count() != 1 {
        return None;
    }
    if !uid.bytes().all(|byte| byte.is_ascii_digit())
        || !gid.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    Some((uid.parse().ok()?, gid.parse().ok()?))
}

fn valid_user_name(value: &str) -> bool {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) if first == '_' || first.is_ascii_lowercase() => {}
        _ => return false,
    }
    let rest = chars.as_str();
    (1 + rest.chars().count()) <= 32
        && rest
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_' || ch == '-')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_names_and_numeric_pairs() {
        validate_identity(Some(&Yaml::string("root")), "run_as").unwrap();
        validate_identity(Some(&Yaml::string("2000:3000")), "run_as").unwrap();
        validate_identity(Some(&Yaml::string("4294967294:0")), "run_as").unwrap();
    }

    #[test]
    fn rejects_bad_identities() {
        for value in [
            Yaml::Int(1000),
            Yaml::string("Root"),
            Yaml::string("4294967295:0"),
            Yaml::string("1:2:3"),
        ] {
            assert!(validate_identity(Some(&value), "field").is_err());
        }
    }
}
