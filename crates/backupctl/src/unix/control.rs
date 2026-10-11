//! Control-path ownership and the no-follow directory chain.

use std::os::fd::OwnedFd;
use std::path::Path;

use homelab_backup_domain::paths::lexical_absolute;
use rustix::fs::{AtFlags, CWD, Mode, OFlags, fstat, fsync, mkdirat, openat, statat};
use rustix::io::Errno;
use rustix::process::geteuid;

use super::error::{SecurityError, permission_error};

const IWGRP: u32 = 0o020;
const IWOTH: u32 = 0o002;
const ISVTX: u32 = 0o1000;

pub fn validate_control_directory(
    path: &str,
    allow_missing: bool,
    owner_uid: Option<u32>,
) -> Result<String, SecurityError> {
    let path = absolute(path)?;
    let effective_uid = geteuid().as_raw();
    let owner_uid = owner_uid.unwrap_or(0);
    let mut allowed_owners = vec![owner_uid];
    if owner_uid == 0 && effective_uid != 0 {
        allowed_owners.push(root_directory_uid());
    }
    if effective_uid != 0 {
        allowed_owners.push(effective_uid);
    }
    let mut missing = false;
    for component in components(&path) {
        if missing {
            continue;
        }
        let metadata = match statat(CWD, component.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
            Ok(metadata) => metadata,
            Err(Errno::NOENT) => {
                if allow_missing {
                    missing = true;
                    continue;
                }
                return Err(SecurityError::invalid(format!(
                    "control directory does not exist: {component}"
                )));
            }
            Err(error) => {
                return Err(SecurityError::runtime(format!(
                    "cannot inspect control directory {component}: {error}"
                )));
            }
        };
        assess_component(
            &component,
            mode_of(&metadata),
            metadata.st_uid,
            &allowed_owners,
            effective_uid,
        )?;
    }
    Ok(path)
}

pub fn assess_component(
    component: &str,
    mode: u32,
    uid: u32,
    allowed_owners: &[u32],
    effective_uid: u32,
) -> Result<(), SecurityError> {
    if !is_dir(mode) {
        return Err(SecurityError::invalid(format!(
            "control path component is not a real directory: {component}"
        )));
    }
    if !allowed_owners.contains(&uid) {
        return Err(permission_error(
            format!("control directory is not owned by root: {component}"),
            "chown",
            component,
            0,
            false,
        ));
    }
    if mode & (IWGRP | IWOTH) != 0 && !(effective_uid != 0 && uid == 0 && mode & ISVTX != 0) {
        return Err(permission_error(
            format!("control directory is group/world writable: {component}"),
            "chmod",
            component,
            0,
            false,
        ));
    }
    Ok(())
}

pub fn validate_control_file(path: &str) -> Result<String, SecurityError> {
    let path = absolute(path)?;
    let parent = parent_path(&path);
    validate_control_directory(&parent, false, None)?;
    let parent_fd = open_directory(&parent)?;
    let name = file_name(&path);
    let fd = match openat(
        &parent_fd,
        name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::LOOP) => {
            return Err(SecurityError::invalid(format!(
                "control file must be a regular file: {path}"
            )));
        }
        Err(error) => return Err(io_error(&path, error)),
    };
    let metadata = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(SecurityError::invalid(format!(
            "control file must be a regular file: {path}"
        )));
    }
    let mut allowed_owners = vec![0];
    let effective_uid = geteuid().as_raw();
    if effective_uid != 0 {
        allowed_owners.push(effective_uid);
    }
    if !allowed_owners.contains(&metadata.st_uid) {
        return Err(permission_error(
            format!("control file is not owned by root: {path}"),
            "chown",
            &path,
            0,
            true,
        ));
    }
    if mode_of(&metadata) & (IWGRP | IWOTH) != 0 {
        return Err(permission_error(
            format!("control file is group/world writable: {path}"),
            "chmod",
            &path,
            0,
            true,
        ));
    }
    Ok(path)
}

pub fn ensure_control_directory(path: &str, mode: u32) -> Result<String, SecurityError> {
    let path = validate_control_directory(path, true, None)?;
    let (existing, missing) = existing_prefix(&path)?;
    let mut current = open_directory(&existing)?;
    for component in missing.into_iter().rev() {
        match mkdirat(&current, component.as_str(), Mode::from_bits_truncate(mode)) {
            Ok(()) => fsync(&current).map_err(runtime)?,
            Err(Errno::EXIST) => {}
            Err(error) => return Err(runtime(error)),
        }
        let next = openat(
            &current,
            component.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(runtime)?;
        let metadata = fstat(&next).map_err(runtime)?;
        let mut allowed = vec![0];
        let effective_uid = geteuid().as_raw();
        if effective_uid != 0 {
            allowed.push(effective_uid);
        }
        if !allowed.contains(&metadata.st_uid) || mode_of(&metadata) & (IWGRP | IWOTH) != 0 {
            return Err(SecurityError::invalid(format!(
                "created control directory is not protected: {path}"
            )));
        }
        current = next;
    }
    fsync(&current).map_err(runtime)?;
    validate_control_directory(&path, false, None)
}

fn existing_prefix(path: &str) -> Result<(String, Vec<String>), SecurityError> {
    let mut missing = Vec::new();
    let mut current = path.to_owned();
    loop {
        match statat(CWD, current.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
            Ok(metadata) => {
                if !is_dir(mode_of(&metadata)) {
                    return Err(SecurityError::invalid(format!(
                        "control path component is not a real directory: {current}"
                    )));
                }
                return Ok((current, missing));
            }
            Err(Errno::NOENT) => {
                missing.push(file_name(&current));
                current = parent_path(&current);
            }
            Err(error) => return Err(runtime(error)),
        }
    }
}

pub fn open_directory(path: &str) -> Result<OwnedFd, SecurityError> {
    openat(
        CWD,
        path,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(runtime)
}

fn absolute(path: &str) -> Result<String, SecurityError> {
    lexical_absolute(path).map_err(|error| SecurityError::invalid(error.message))
}

fn components(path: &str) -> Vec<String> {
    let mut current = "/".to_owned();
    let mut items = vec![current.clone()];
    if path == "/" {
        return items;
    }
    for part in path.split('/').filter(|part| !part.is_empty()) {
        current = if current == "/" {
            format!("/{part}")
        } else {
            format!("{current}/{part}")
        };
        items.push(current.clone());
    }
    items
}

pub fn parent_path(path: &str) -> String {
    match path.rsplit_once('/') {
        Some(("", _)) | Some(("/", _)) => "/".to_owned(),
        Some((parent, _)) => parent.to_owned(),
        None => "/".to_owned(),
    }
}

pub fn file_name(path: &str) -> String {
    path.rsplit_once('/')
        .map(|(_, name)| name)
        .unwrap_or(path)
        .to_owned()
}

fn root_directory_uid() -> u32 {
    statat(CWD, "/", AtFlags::empty())
        .map(|metadata| metadata.st_uid)
        .unwrap_or(0)
}

pub fn mode_of(metadata: &rustix::fs::Stat) -> u32 {
    metadata.st_mode
}

pub fn is_dir(mode: u32) -> bool {
    mode & 0o170000 == 0o040000
}

pub fn is_reg(mode: u32) -> bool {
    mode & 0o170000 == 0o100000
}

pub fn is_lnk(mode: u32) -> bool {
    mode & 0o170000 == 0o120000
}

fn runtime(error: impl ToString) -> SecurityError {
    SecurityError::runtime(error.to_string())
}

fn io_error(path: &str, error: Errno) -> SecurityError {
    SecurityError::invalid(format!("control file could not be opened: {path}: {error}"))
}

pub fn path_str(path: &Path) -> Result<&str, SecurityError> {
    path.to_str()
        .ok_or_else(|| SecurityError::invalid("path is not utf-8"))
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    #[test]
    fn writable_shared_ancestor_does_not_suggest_chmod() {
        let error = assess_component("/tmp", 0o040000 | 0o1777, 0, &[0], 0).unwrap_err();
        assert!(error.suggested_command.is_none());
        assert!(error.suggested_action.unwrap().contains("/tmp"));
    }

    #[test]
    fn user_owned_shared_ancestor_does_not_suggest_chown() {
        let error = assess_component("/home/alice", 0o040000 | 0o755, 1000, &[0], 0).unwrap_err();
        assert!(error.suggested_command.is_none());
        assert!(error.suggested_action.unwrap().contains("/home/alice"));
    }

    #[test]
    fn writable_control_file_provides_a_safe_chmod_command() {
        let root = std::env::temp_dir().join(format!("homelab-file-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        let path = root.join("secret with spaces");
        std::fs::write(&path, "secret").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)).unwrap();
        let error = validate_control_file(path.to_str().unwrap()).unwrap_err();
        assert_eq!(
            error.suggested_command.unwrap(),
            vec![
                "sudo".to_owned(),
                "chmod".to_owned(),
                "go-w".to_owned(),
                "--".to_owned(),
                path.to_str().unwrap().to_owned(),
            ]
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn writable_control_directory_requires_manual_review() {
        let root = std::env::temp_dir().join(format!("homelab-dir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o777)).unwrap();
        let error = validate_control_directory(root.to_str().unwrap(), false, None).unwrap_err();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(error.suggested_command.is_none());
        assert!(
            error
                .suggested_action
                .unwrap()
                .contains(root.to_str().unwrap())
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
