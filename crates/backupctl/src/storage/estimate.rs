//! How many bytes a path copy is expected to occupy on the staging filesystem.

use std::collections::BTreeMap;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{AtFlags, Mode, OFlags, fstat, openat, statat};
use rustix::io::{FdFlags, fcntl_getfd, fcntl_setfd};

use crate::storage::policy::{build_path_filter_args, validate_path_source_policy};
use crate::unix::control::{is_dir, is_lnk, is_reg, mode_of};
use crate::unix::data::{current_euid, open_data_path, validate_payload};
use crate::unix::error::SecurityError;
use crate::unix::mount::data_mount_snapshot;

pub fn estimate_path_sources(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    stage: &str,
) -> Result<u64, SecurityError> {
    let Some(sources) = manifest.get("sources").and_then(Yaml::as_map) else {
        return Ok(0);
    };
    let paths = match sources.get("paths") {
        None => return Ok(0),
        Some(Yaml::Seq(paths)) => paths,
        Some(_) => return Err(SecurityError::invalid("sources.paths must be a list")),
    };
    let allocation_unit = rustix::fs::statvfs(stage)
        .map_err(|error| SecurityError::runtime(error.to_string()))?
        .f_frsize;
    let mut total = 0u64;
    for source in paths {
        let Some(source) = source.as_map() else {
            return Err(SecurityError::invalid("path source must be a mapping"));
        };
        total = total.saturating_add(estimate_one(config, manifest, source, allocation_unit)?);
    }
    Ok(total)
}

fn estimate_one(
    config: &BTreeMap<String, String>,
    manifest: &BTreeMap<String, Yaml>,
    source: &BTreeMap<String, Yaml>,
    allocation_unit: u64,
) -> Result<u64, SecurityError> {
    let resolved = validate_path_source_policy(config, manifest, source)?;
    let required = source
        .get("required")
        .and_then(Yaml::as_bool)
        .unwrap_or(true);
    let (fd, metadata, _) = match open_data_path(Path::new(&resolved)) {
        Ok(opened) => opened,
        Err(error) if error.is_missing_path() && !required => return Ok(0),
        Err(error) if error.is_missing_path() => {
            return Err(SecurityError::invalid(format!("missing source {resolved}")));
        }
        Err(error) => return Err(error),
    };
    let include = string_list(source.get("include"));
    if !include.is_empty() && !is_dir(mode_of(&metadata)) {
        return Err(SecurityError::invalid(format!(
            "include is only supported for directory path sources: {resolved}"
        )));
    }
    let before = data_mount_snapshot(&resolved)?;
    validate_payload(Path::new(&resolved), None, current_euid(), None)?;
    let filters = build_path_filter_args(&string_list(source.get("exclude")), &include, false);
    let size = if is_dir(mode_of(&metadata)) && !filters.is_empty() {
        estimate_filtered(&fd, source_id(source), &filters, allocation_unit)?
    } else {
        estimate_tree(&fd, &metadata, &resolved)?
    };
    let (_reopened, current, _) = open_data_path(Path::new(&resolved)).map_err(|_| {
        SecurityError::runtime(format!("path source changed during backup: {resolved}"))
    })?;
    if metadata.st_dev != current.st_dev
        || metadata.st_ino != current.st_ino
        || (mode_of(&metadata) & 0o170000) != (mode_of(&current) & 0o170000)
    {
        return Err(SecurityError::runtime(format!(
            "path source changed during backup: {resolved}"
        )));
    }
    let after = data_mount_snapshot(&resolved)?;
    if before != after {
        return Err(SecurityError::runtime(format!(
            "mount topology changed during size estimation: {resolved}"
        )));
    }
    Ok(size)
}

fn estimate_tree(
    fd: &OwnedFd,
    metadata: &rustix::fs::Stat,
    display: &str,
) -> Result<u64, SecurityError> {
    let allocated = allocated_bytes(metadata);
    let mode = mode_of(metadata);
    if is_reg(mode) {
        return Ok(file_size(metadata).max(allocated));
    }
    if is_lnk(mode) {
        let target = std::fs::read_link(display)
            .map_err(|error| SecurityError::runtime(format!("{display}: {error}")))?;
        return Ok((target.as_os_str().as_bytes().len() as u64).max(allocated));
    }
    if !is_dir(mode) {
        return Err(SecurityError::invalid(format!(
            "unsupported payload object: {display}"
        )));
    }
    let mut total = 4096u64.max(allocated);
    let mut names = Vec::new();
    for entry in rustix::fs::Dir::read_from(fd)
        .map_err(|error| SecurityError::runtime(format!("{display}: {error}")))?
    {
        let entry = entry.map_err(|error| SecurityError::runtime(format!("{display}: {error}")))?;
        let name = entry.file_name().to_string_lossy();
        if name == "." || name == ".." {
            continue;
        }
        names.push(name.into_owned());
    }
    for name in names {
        let child_path = format!("{display}/{name}");
        let before = statat(fd, name.as_str(), AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| SecurityError::runtime(format!("{child_path}: {error}")))?;
        let child_mode = mode_of(&before);
        let flags = if is_dir(child_mode) {
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else if is_reg(child_mode) {
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else if is_lnk(child_mode) {
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else {
            return Err(SecurityError::invalid(format!(
                "unsupported payload object: {child_path}"
            )));
        };
        let child = openat(fd, name.as_str(), flags, Mode::empty())
            .map_err(|error| SecurityError::runtime(format!("{child_path}: {error}")))?;
        let current = fstat(&child).map_err(|error| SecurityError::runtime(error.to_string()))?;
        if before.st_dev != current.st_dev
            || before.st_ino != current.st_ino
            || (child_mode & 0o170000) != (mode_of(&current) & 0o170000)
        {
            return Err(SecurityError::runtime(format!(
                "path source changed during size estimation: {child_path}"
            )));
        }
        total = total.saturating_add(estimate_tree(&child, &current, &child_path)?);
    }
    Ok(total)
}

fn estimate_filtered(
    fd: &OwnedFd,
    source_id: &str,
    filters: &[String],
    allocation_unit: u64,
) -> Result<u64, SecurityError> {
    let destination = std::env::temp_dir().join(format!(
        "homelab-estimate-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&destination)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let cleanup = destination.clone();
    let result = (|| {
        let mut command = vec![
            "rsync".to_owned(),
            "-aHAX".to_owned(),
            "--numeric-ids".to_owned(),
            "--dry-run".to_owned(),
            "--stats".to_owned(),
            "--one-file-system".to_owned(),
        ];
        command.extend(filters.iter().cloned());
        command.push(format!("/proc/self/fd/{}/", fd.as_raw_fd()));
        command.push(format!("{}/", destination.display()));
        let original =
            fcntl_getfd(fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
        fcntl_setfd(fd, original.difference(FdFlags::CLOEXEC))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let output = std::process::Command::new(&command[0])
            .args(&command[1..])
            .env("LC_ALL", "C")
            .output();
        let _ = fcntl_setfd(fd, original);
        let output = output.map_err(|error| SecurityError::runtime(error.to_string()))?;
        if !output.status.success() {
            return Err(SecurityError::runtime(format!(
                "command failed: {}",
                command.join(" ")
            )));
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        let (count, logical) = parse_filtered_stats(&stdout, source_id)?;
        Ok(logical.saturating_add(count.saturating_mul(allocation_unit)))
    })();
    let _ = std::fs::remove_dir_all(cleanup);
    result
}

fn parse_filtered_stats(output: &str, source_id: &str) -> Result<(u64, u64), SecurityError> {
    let mut created = None;
    let mut logical = None;
    for line in output.lines() {
        if let Some(rest) = line.strip_prefix("Number of created files:") {
            created = leading_integer(rest);
        } else if let Some(rest) = line.strip_prefix("Total file size:") {
            logical = leading_integer(rest.trim_end_matches(" bytes"));
        }
    }
    match (created, logical) {
        (Some(created), Some(logical)) => Ok((created, logical)),
        _ => Err(SecurityError::runtime(format!(
            "invalid filtered size estimate for path source '{source_id}'"
        ))),
    }
}

fn leading_integer(text: &str) -> Option<u64> {
    let digits: String = text
        .trim()
        .chars()
        .take_while(|ch| ch.is_ascii_digit() || *ch == ',')
        .collect();
    if digits.is_empty() {
        None
    } else {
        digits.replace(',', "").parse().ok()
    }
}

fn allocated_bytes(metadata: &rustix::fs::Stat) -> u64 {
    u64::try_from(metadata.st_blocks)
        .unwrap_or(0)
        .saturating_mul(512)
}

fn file_size(metadata: &rustix::fs::Stat) -> u64 {
    u64::try_from(metadata.st_size).unwrap_or(0)
}

fn source_id(source: &BTreeMap<String, Yaml>) -> &str {
    source.get("id").and_then(Yaml::as_str).unwrap_or("")
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

#[cfg(test)]
mod tests {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static TEMP: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn file_symlink_and_directory_sizes_follow_allocated_blocks() {
        let root = temp_root();
        let file = root.join("payload.txt");
        std::fs::write(&file, "hello").unwrap();
        let link = root.join("alias");
        std::os::unix::fs::symlink("hello", &link).unwrap();
        let directory = root.join("dir");
        std::fs::create_dir(&directory).unwrap();
        std::fs::write(directory.join("nested.txt"), "hello").unwrap();

        let file_meta = std::fs::symlink_metadata(&file).unwrap();
        assert_eq!(
            estimate_path_sources(
                &BTreeMap::new(),
                &manifest(&root, &file, &[]),
                &root.to_string_lossy()
            )
            .unwrap(),
            file_meta.len().max(file_meta.blocks() * 512)
        );
        let link_meta = std::fs::symlink_metadata(&link).unwrap();
        assert_eq!(
            estimate_path_sources(
                &BTreeMap::new(),
                &manifest(&root, &link, &[]),
                &root.to_string_lossy()
            )
            .unwrap(),
            (OsStr::new("hello").as_bytes().len() as u64).max(link_meta.blocks() * 512)
        );
        let directory_size = estimate_path_sources(
            &BTreeMap::new(),
            &manifest(&root, &directory, &[]),
            &root.to_string_lossy(),
        )
        .unwrap();
        assert!(directory_size >= 4096 + 5, "{directory_size}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn optional_missing_path_adds_nothing_and_required_missing_fails() {
        let root = temp_root();
        let missing = root.join("gone");
        let optional = manifest(&root, &missing, &[("required", Yaml::Bool(false))]);
        assert_eq!(
            estimate_path_sources(&BTreeMap::new(), &optional, &root.to_string_lossy()).unwrap(),
            0
        );
        let required = manifest(&root, &missing, &[]);
        let error = estimate_path_sources(&BTreeMap::new(), &required, &root.to_string_lossy())
            .unwrap_err();
        assert!(
            error.message.contains("missing source"),
            "{}",
            error.message
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn include_filters_drop_the_excluded_file_from_the_estimate() {
        let root = temp_root();
        let directory = root.join("dir");
        std::fs::create_dir(&directory).unwrap();
        std::fs::write(directory.join("keep.txt"), "ok").unwrap();
        std::fs::write(directory.join("skip.bin"), vec![0u8; 256 * 1024]).unwrap();
        let full = estimate_path_sources(
            &BTreeMap::new(),
            &manifest(&root, &directory, &[]),
            &root.to_string_lossy(),
        )
        .unwrap();
        let filtered = estimate_path_sources(
            &BTreeMap::new(),
            &manifest(
                &root,
                &directory,
                &[("include", Yaml::seq([Yaml::string("keep.txt")]))],
            ),
            &root.to_string_lossy(),
        )
        .unwrap();
        assert!(filtered < full, "filtered {filtered} full {full}");
        assert!(filtered < 128 * 1024, "{filtered}");
        let _ = std::fs::remove_dir_all(&root);
    }

    fn manifest(root: &Path, source: &Path, extra: &[(&str, Yaml)]) -> BTreeMap<String, Yaml> {
        let mut path = BTreeMap::from([
            ("id".to_owned(), Yaml::string("data")),
            (
                "path".to_owned(),
                Yaml::string(source.display().to_string()),
            ),
        ]);
        for (key, value) in extra {
            path.insert((*key).to_owned(), value.clone());
        }
        BTreeMap::from([
            ("_dir".to_owned(), Yaml::string(root.display().to_string())),
            (
                "_path".to_owned(),
                Yaml::string(root.join("backup.yaml").display().to_string()),
            ),
            ("service".to_owned(), Yaml::string("demo")),
            (
                "consistency".to_owned(),
                Yaml::map([("mode".to_owned(), Yaml::string("files"))]),
            ),
            (
                "sources".to_owned(),
                Yaml::map([("paths".to_owned(), Yaml::seq([Yaml::Map(path)]))]),
            ),
        ])
    }

    fn temp_root() -> std::path::PathBuf {
        let id = TEMP.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("homelab-estimate-{}-{id}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        root
    }
}
