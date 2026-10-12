//! Payload opens that refuse intermediate symlinks and odd file types.

use std::os::fd::OwnedFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use homelab_backup_domain::paths::lexical_absolute;
use rustix::fs::{AtFlags, Mode, OFlags, fstat, openat, statat};
use rustix::io::Errno;
use rustix::process::geteuid;

use super::control::{is_dir, is_lnk, is_reg, mode_of};
use super::error::SecurityError;
use super::mount::validate_data_mount_boundary;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapturedCommand {
    pub code: i32,
    pub stdout: String,
}

/// One real directory between `/` and the payload, as recorded for restore.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ancestor {
    pub path: String,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

pub fn open_data_path(
    path: &Path,
) -> Result<(OwnedFd, rustix::fs::Stat, Vec<Ancestor>), SecurityError> {
    let path = absolute(path)?;
    if path == "/" {
        return Err(SecurityError::invalid(
            "data path must not be the filesystem root: /",
        ));
    }
    let mut current = openat(
        rustix::fs::CWD,
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let parts: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    let mut ancestors = Vec::new();
    for (index, component) in parts.iter().enumerate() {
        let final_component = index + 1 == parts.len();
        let metadata = match statat(&current, *component, AtFlags::SYMLINK_NOFOLLOW) {
            Ok(metadata) => metadata,
            Err(Errno::NOENT) => {
                return Err(SecurityError::invalid(format!("missing path: {path}")));
            }
            Err(error) => {
                return Err(SecurityError::runtime(format!("{path}: {error}")));
            }
        };
        let mode = mode_of(&metadata);
        let flags = if !final_component {
            if !is_dir(mode) {
                let display = format!("/{}", parts[..=index].join("/"));
                return Err(SecurityError::invalid(format!(
                    "data path component is not a real directory: {display}"
                )));
            }
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else if is_dir(mode) {
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else if is_reg(mode) {
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else if is_lnk(mode) {
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else {
            return Err(SecurityError::invalid(format!(
                "unsupported payload object: {path}"
            )));
        };
        let next = openat(&current, *component, flags, Mode::empty())
            .map_err(|error| SecurityError::runtime(format!("{path}: {error}")))?;
        let opened = fstat(&next).map_err(|error| SecurityError::runtime(error.to_string()))?;
        if metadata.st_dev != opened.st_dev
            || metadata.st_ino != opened.st_ino
            || file_type(mode) != file_type(mode_of(&opened))
        {
            return Err(SecurityError::runtime(format!(
                "data path changed while being opened: {path}"
            )));
        }
        if !final_component {
            ancestors.push(Ancestor {
                path: parts[..=index].join("/"),
                uid: opened.st_uid,
                gid: opened.st_gid,
                mode: mode_of(&opened) & 0o7777,
            });
        }
        current = next;
    }
    let metadata = fstat(&current).map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok((current, metadata, ancestors))
}

type PayloadRunner<'a> = &'a mut dyn FnMut(&[String]) -> CapturedCommand;

pub fn validate_payload(
    path: &Path,
    filesystem_type: Option<&str>,
    euid: u32,
    mut run: Option<PayloadRunner<'_>>,
) -> Result<(), SecurityError> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| SecurityError::invalid(format!("{}: {error}", path.display())))?;
    let mode = metadata.mode();
    if is_dir(mode) {
        if filesystem_type == Some("btrfs") && euid == 0 {
            let argv = vec![
                "btrfs".to_owned(),
                "subvolume".to_owned(),
                "list".to_owned(),
                "-o".to_owned(),
                path.display().to_string(),
            ];
            let result = match run.as_mut() {
                Some(run) => run(&argv),
                None => run_system(&argv)?,
            };
            if result.code != 0 {
                return Err(SecurityError::runtime(format!(
                    "cannot inspect Btrfs subvolumes below {}",
                    path.display()
                )));
            }
            if !result.stdout.trim().is_empty() {
                return Err(SecurityError::invalid(format!(
                    "nested Btrfs subvolume is not supported: {}; declare each nested subvolume as a separate source",
                    path.display()
                )));
            }
        }
        scan_tree(path)?;
    } else if !(is_reg(mode) || is_lnk(mode)) {
        return Err(SecurityError::invalid(format!(
            "unsupported payload object: {}",
            path.display()
        )));
    }
    Ok(())
}

fn scan_tree(path: &Path) -> Result<(), SecurityError> {
    for entry in
        std::fs::read_dir(path).map_err(|error| SecurityError::runtime(error.to_string()))?
    {
        let entry = entry.map_err(|error| SecurityError::runtime(error.to_string()))?;
        let metadata = entry
            .metadata()
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        // `DirEntry::metadata` follows symlinks on some platforms. Use symlink_metadata.
        let metadata = std::fs::symlink_metadata(entry.path()).unwrap_or(metadata);
        let mode = metadata.mode();
        let child = entry.path();
        if is_lnk(mode) || is_reg(mode) {
            continue;
        }
        if is_dir(mode) {
            scan_tree(&child)?;
            continue;
        }
        return Err(SecurityError::invalid(format!(
            "unsupported payload object: {}",
            child.display()
        )));
    }
    Ok(())
}

fn run_system(argv: &[String]) -> Result<CapturedCommand, SecurityError> {
    let mut command = std::process::Command::new(&argv[0]);
    command.args(&argv[1..]);
    let output = command
        .output()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(CapturedCommand {
        code: output.status.code().unwrap_or(1),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
    })
}

fn absolute(path: &Path) -> Result<String, SecurityError> {
    let text = path
        .to_str()
        .ok_or_else(|| SecurityError::invalid("path is not utf-8"))?;
    lexical_absolute(text).map_err(|error| SecurityError::invalid(error.message))
}

fn file_type(mode: u32) -> u32 {
    mode & 0o170000
}

pub fn current_euid() -> u32 {
    geteuid().as_raw()
}

#[allow(dead_code)]
pub fn validate_payload_mount(
    path: &str,
    records: &[super::mount::MountRecord],
) -> Result<(), SecurityError> {
    validate_data_mount_boundary(path, records).map(|_| ())
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    #[test]
    fn container_owned_ancestors_are_allowed() {
        let root = std::env::temp_dir().join(format!("homelab-data-{}", std::process::id()));
        let saved = root.join("palworld").join("Pal").join("Saved");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&saved).unwrap();
        std::fs::write(saved.join("world.sav"), "world").unwrap();
        std::fs::set_permissions(
            root.join("palworld"),
            std::fs::Permissions::from_mode(0o777),
        )
        .unwrap();
        std::fs::set_permissions(
            root.join("palworld").join("Pal"),
            std::fs::Permissions::from_mode(0o777),
        )
        .unwrap();
        let (fd, metadata, ancestors) = open_data_path(&saved).unwrap();
        assert!(is_dir(mode_of(&metadata)));
        assert!(
            ancestors
                .iter()
                .any(|item| item.path.ends_with("Pal") && item.mode == 0o777)
        );
        drop(fd);
        let _ = std::fs::set_permissions(
            root.join("palworld").join("Pal"),
            std::fs::Permissions::from_mode(0o755),
        );
        let _ = std::fs::set_permissions(
            root.join("palworld"),
            std::fs::Permissions::from_mode(0o755),
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn intermediate_symlink_is_rejected() {
        let root = std::env::temp_dir().join(format!("homelab-link-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let trusted = root.join("data");
        let outside = root.join("outside");
        std::fs::create_dir_all(&trusted).unwrap();
        std::fs::create_dir_all(outside.join("Saved")).unwrap();
        std::os::unix::fs::symlink(&outside, trusted.join("palworld")).unwrap();
        let error = open_data_path(&trusted.join("palworld").join("Saved")).unwrap_err();
        assert!(
            error.message.contains("real directory"),
            "{}",
            error.message
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn filesystem_root_is_not_a_data_path() {
        let error = open_data_path(Path::new("/")).unwrap_err();
        assert!(error.message.contains("filesystem root"));
    }

    #[test]
    fn btrfs_nested_subvolume_is_rejected() {
        let root = std::env::temp_dir().join(format!("homelab-btrfs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let mut calls = 0;
        let error = validate_payload(
            &root,
            Some("btrfs"),
            0,
            Some(&mut |_argv| {
                calls += 1;
                CapturedCommand {
                    code: 0,
                    stdout: "ID 257 path child\n".to_owned(),
                }
            }),
        )
        .unwrap_err();
        assert!(error.message.contains("nested Btrfs"), "{}", error.message);
        assert_eq!(calls, 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn socket_payload_is_rejected() {
        let root = std::env::temp_dir().join(format!("homelab-sock-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let socket_path = root.join("socket");
        let socket = match std::os::unix::net::UnixListener::bind(&socket_path) {
            Ok(socket) => socket,
            Err(_) => return,
        };
        let error = validate_payload(&root, None, current_euid(), None).unwrap_err();
        drop(socket);
        assert!(
            error.message.contains("unsupported payload"),
            "{}",
            error.message
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
