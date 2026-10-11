//! Copy path sources into a stage without following intermediate symlinks.

use std::os::fd::{AsRawFd, OwnedFd};
use std::path::Path;
use std::process::Command;

use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{AtFlags, Timespec, Timestamps, utimensat};
use rustix::io::{FdFlags, fcntl_getfd, fcntl_setfd};
use rustix::process::geteuid;

use crate::storage::policy::build_path_filter_args;
use crate::storage::policy::validate_path_source_policy;
use crate::unix::control::{clear_control_leaf, is_dir, is_lnk, is_reg, mode_of};
use crate::unix::data::{current_euid, open_data_path, validate_payload};
use crate::unix::error::SecurityError;
use crate::unix::mount::{MountSnapshot, data_mount_snapshot};

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathInventory {
    pub id: String,
    pub path: String,
    pub kind: Option<String>,
    pub present: bool,
    pub capture_method: String,
}

type RunHook<'a> = Option<&'a mut dyn FnMut(&[String]) -> Result<(), SecurityError>>;
type MountHook<'a> = Option<&'a mut dyn FnMut(&str) -> Result<MountSnapshot, SecurityError>>;

#[derive(Default)]
pub struct SyncOptions<'a> {
    pub run: RunHook<'a>,
    pub mounts: MountHook<'a>,
}

pub fn sync_paths(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    stage: &Path,
    options: &mut SyncOptions<'_>,
) -> Result<Vec<PathInventory>, SecurityError> {
    let Some(sources) = manifest.get("sources").and_then(Yaml::as_map) else {
        return Ok(Vec::new());
    };
    let paths = match sources.get("paths") {
        None => return Ok(Vec::new()),
        Some(Yaml::Seq(paths)) => paths,
        Some(_) => {
            return Err(SecurityError::invalid("sources.paths must be a list"));
        }
    };
    let mut inventory = Vec::new();
    for source in paths {
        let Some(source) = source.as_map() else {
            return Err(SecurityError::invalid("path source must be a mapping"));
        };
        let id = source.get("id").and_then(Yaml::as_str).unwrap_or("");
        let declared = source.get("path").and_then(Yaml::as_str).unwrap_or("");
        let required = source
            .get("required")
            .and_then(Yaml::as_bool)
            .unwrap_or(true);
        let resolved = validate_path_source_policy(config, manifest, source)?;
        let destination = stage.join("paths").join(id);
        match copy_one(&resolved, &destination, source, options) {
            Ok(kind) => inventory.push(PathInventory {
                id: id.to_owned(),
                path: declared.to_owned(),
                kind: Some(kind),
                present: true,
                capture_method: "quiesced-copy".to_owned(),
            }),
            Err(error) if error.is_missing_path() && !required => {
                if let Some(parent) = destination.parent() {
                    std::fs::create_dir_all(parent)
                        .map_err(|error| SecurityError::runtime(error.to_string()))?;
                }
                clear_control_leaf(&destination.to_string_lossy())?;
                inventory.push(PathInventory {
                    id: id.to_owned(),
                    path: declared.to_owned(),
                    kind: None,
                    present: false,
                    capture_method: "quiesced-copy".to_owned(),
                });
            }
            Err(error) if error.is_missing_path() => {
                return Err(SecurityError::invalid(format!("missing source {resolved}")));
            }
            Err(error) => return Err(error),
        }
    }
    Ok(inventory)
}

fn copy_one(
    source_path: &str,
    destination: &Path,
    source: &BTreeMap<String, Yaml>,
    options: &mut SyncOptions<'_>,
) -> Result<String, SecurityError> {
    let (fd, metadata) = open_data_path(Path::new(source_path))?;
    let mode = mode_of(&metadata);
    let include = string_list(source.get("include"));
    if !include.is_empty() && !is_dir(mode) {
        return Err(SecurityError::invalid(format!(
            "include is only supported for directory path sources: {source_path}"
        )));
    }
    let before = mount_state(source_path, options)?;
    validate_payload(Path::new(source_path), None, current_euid(), None)?;
    let kind = if is_lnk(mode) {
        archive_symlink(source_path, destination, &fd, &metadata)?;
        "symlink"
    } else if is_dir(mode) {
        let mut command = vec![
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--delete".to_owned(),
            "--one-file-system".to_owned(),
        ];
        command.extend(build_path_filter_args(
            &string_list(source.get("exclude")),
            &include,
            false,
        ));
        command.push(format!("/proc/self/fd/{}/", fd.as_raw_fd()));
        command.push(format!("{}/", destination.display()));
        prepare_destination(destination)?;
        run_with_fd(&command, Some(&fd), options)?;
        "directory"
    } else if is_reg(mode) {
        let archived = destination.join(name_of(source_path));
        prepare_destination(destination)?;
        let command = vec![
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--copy-unsafe-links".to_owned(),
            format!("/proc/self/fd/{}", fd.as_raw_fd()),
            archived.display().to_string(),
        ];
        run_with_fd(&command, Some(&fd), options)?;
        "file"
    } else {
        return Err(SecurityError::runtime(format!(
            "unsupported path source type: {source_path}"
        )));
    };
    let (_reopened, current) = open_data_path(Path::new(source_path))?;
    if metadata.st_dev != current.st_dev
        || metadata.st_ino != current.st_ino
        || (mode & 0o170000) != (mode_of(&current) & 0o170000)
    {
        return Err(SecurityError::runtime(format!(
            "path source changed during backup: {source_path}"
        )));
    }
    let after = mount_state(source_path, options)?;
    if before != after {
        return Err(SecurityError::runtime(format!(
            "mount topology changed during backup: {source_path}"
        )));
    }
    Ok(kind.to_owned())
}

fn archive_symlink(
    source_path: &str,
    destination: &Path,
    fd: &OwnedFd,
    metadata: &rustix::fs::Stat,
) -> Result<(), SecurityError> {
    prepare_destination(destination)?;
    let target = std::fs::read_link(source_path)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let archived = destination.join(name_of(source_path));
    std::os::unix::fs::symlink(&target, &archived)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    if let Err(error) = rustix::fs::chownat(
        rustix::fs::CWD,
        &archived,
        Some(rustix::fs::Uid::from_raw(metadata.st_uid)),
        Some(rustix::fs::Gid::from_raw(metadata.st_gid)),
        AtFlags::SYMLINK_NOFOLLOW,
    ) && geteuid().as_raw() == 0
    {
        return Err(SecurityError::runtime(error.to_string()));
    }
    utimensat(
        rustix::fs::CWD,
        &archived,
        &Timestamps {
            last_access: Timespec {
                tv_sec: metadata.st_atime,
                tv_nsec: metadata.st_atime_nsec as i64,
            },
            last_modification: Timespec {
                tv_sec: metadata.st_mtime,
                tv_nsec: metadata.st_mtime_nsec as i64,
            },
        },
        AtFlags::SYMLINK_NOFOLLOW,
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let _ = fd;
    Ok(())
}

fn prepare_destination(destination: &Path) -> Result<(), SecurityError> {
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
    }
    clear_control_leaf(&destination.to_string_lossy())?;
    std::fs::create_dir_all(destination)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(())
}

fn mount_state(path: &str, options: &mut SyncOptions<'_>) -> Result<MountSnapshot, SecurityError> {
    if let Some(mounts) = options.mounts.as_mut() {
        mounts(path)
    } else {
        data_mount_snapshot(path)
    }
}

fn run_with_fd(
    command: &[String],
    fd: Option<&OwnedFd>,
    options: &mut SyncOptions<'_>,
) -> Result<(), SecurityError> {
    if let Some(fd) = fd {
        let original =
            fcntl_getfd(fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
        fcntl_setfd(fd, original.difference(FdFlags::CLOEXEC))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let result = dispatch(command, options);
        let _ = fcntl_setfd(fd, original);
        result
    } else {
        dispatch(command, options)
    }
}

fn dispatch(command: &[String], options: &mut SyncOptions<'_>) -> Result<(), SecurityError> {
    if let Some(run) = options.run.as_mut() {
        return run(command);
    }
    let status = Command::new(&command[0])
        .args(&command[1..])
        .status()
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    if status.success() {
        Ok(())
    } else {
        Err(SecurityError::runtime(format!(
            "command failed: {}",
            command.join(" ")
        )))
    }
}

fn string_list(value: Option<&Yaml>) -> Vec<String> {
    value
        .and_then(Yaml::as_seq)
        .map(|items| {
            items
                .iter()
                .filter_map(Yaml::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn name_of(path: &str) -> &str {
    path.rsplit('/')
        .find(|part| !part.is_empty())
        .unwrap_or(path)
}
