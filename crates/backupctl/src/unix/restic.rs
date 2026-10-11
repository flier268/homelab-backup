//! Restic's environment is only built from protected regular credential files.

use std::collections::BTreeMap;

use super::control::validate_control_file;
use super::error::SecurityError;

pub fn restic_env(
    config: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, String>, SecurityError> {
    let password = validate_control_file(required(config, "password_file")?)?;
    let rclone = validate_control_file(required(config, "rclone_config")?)?;
    let mut env = std::env::vars().collect::<BTreeMap<_, _>>();
    env.insert(
        "RESTIC_REPOSITORY".to_owned(),
        required(config, "repository")?.to_owned(),
    );
    env.insert("RESTIC_PASSWORD_FILE".to_owned(), password);
    env.insert(
        "RESTIC_CACHE_DIR".to_owned(),
        required(config, "cache_root")?.to_owned(),
    );
    env.insert("RCLONE_CONFIG".to_owned(), rclone);
    if let Some(limit) = config.get("bwlimit").filter(|value| !value.is_empty()) {
        env.insert("RCLONE_BWLIMIT".to_owned(), limit.clone());
    }
    Ok(env)
}

fn required<'a>(config: &'a BTreeMap<String, String>, key: &str) -> Result<&'a str, SecurityError> {
    config
        .get(key)
        .map(String::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| SecurityError::invalid(format!("missing {key}")))
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{PermissionsExt, symlink};

    use super::*;

    #[test]
    fn restic_environment_requires_protected_regular_credentials() {
        let root = std::env::temp_dir().join(format!("homelab-restic-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        let password = root.join("password");
        let rclone = root.join("rclone.conf");
        std::fs::write(&password, "secret").unwrap();
        std::fs::write(&rclone, "secret").unwrap();
        std::fs::set_permissions(&password, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::set_permissions(&rclone, std::fs::Permissions::from_mode(0o600)).unwrap();
        let mut config = BTreeMap::from([
            ("repository".to_owned(), "rclone:test:repo".to_owned()),
            ("password_file".to_owned(), password.display().to_string()),
            ("rclone_config".to_owned(), rclone.display().to_string()),
            (
                "cache_root".to_owned(),
                root.join("cache").display().to_string(),
            ),
        ]);
        let environment = restic_env(&config).unwrap();
        assert_eq!(
            environment.get("RESTIC_PASSWORD_FILE").map(String::as_str),
            Some(password.to_str().unwrap())
        );
        std::fs::set_permissions(&password, std::fs::Permissions::from_mode(0o666)).unwrap();
        let error = restic_env(&config).unwrap_err();
        assert!(
            error.message.contains("group/world writable"),
            "{}",
            error.message
        );
        std::fs::remove_file(&password).unwrap();
        symlink(&rclone, &password).unwrap();
        config.insert("password_file".to_owned(), password.display().to_string());
        let error = restic_env(&config).unwrap_err();
        assert!(error.message.contains("regular file"), "{}", error.message);
        let _ = std::fs::remove_dir_all(&root);
    }
}
