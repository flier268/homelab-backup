//! mountinfo parsing and the nested-mount boundary.

use homelab_backup_domain::paths::{lexical_absolute, path_contains};

use super::control::validate_control_directory;
use super::error::SecurityError;

const ALLOWED_FILESYSTEMS: &[&str] = &["ext4", "xfs", "btrfs"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountRecord {
    pub mount_id: i32,
    pub parent_id: i32,
    pub device: String,
    pub filesystem_root: String,
    pub mount_point: String,
    pub filesystem_type: String,
    pub source: String,
}

pub fn mount_records(text: &str, origin: &str) -> Result<Vec<MountRecord>, SecurityError> {
    let mut records = Vec::new();
    for line in text.lines() {
        if line.is_empty() {
            continue;
        }
        let fields: Vec<&str> = line.split_whitespace().collect();
        let separator = fields
            .iter()
            .position(|field| *field == "-")
            .ok_or_else(|| malformed(origin))?;
        if fields.len() < separator + 3 || fields.len() < 5 {
            return Err(malformed(origin));
        }
        let mount_id = fields[0].parse().map_err(|_| malformed(origin))?;
        let parent_id = fields[1].parse().map_err(|_| malformed(origin))?;
        records.push(MountRecord {
            mount_id,
            parent_id,
            device: fields[2].to_owned(),
            filesystem_root: decode_mountinfo(fields[3]),
            mount_point: decode_mountinfo(fields[4]),
            filesystem_type: fields[separator + 1].to_owned(),
            source: decode_mountinfo(fields[separator + 2]),
        });
    }
    Ok(records)
}

fn malformed(origin: &str) -> SecurityError {
    SecurityError::runtime(format!(
        "cannot parse mounted filesystems: malformed {origin}"
    ))
}

fn decode_mountinfo(value: &str) -> String {
    value
        .replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
}

pub fn containing_mount<'a>(
    path: &str,
    records: &'a [MountRecord],
) -> Result<&'a MountRecord, SecurityError> {
    let path = absolute(path)?;
    let mut matches: Vec<&MountRecord> = records
        .iter()
        .filter(|item| path_contains(&item.mount_point, &path).unwrap_or(false))
        .collect();
    if matches.is_empty() {
        return Err(SecurityError::runtime(format!(
            "cannot determine filesystem for {path}"
        )));
    }
    matches.sort_by_key(|item| (part_count(&item.mount_point), item.mount_id));
    Ok(matches.pop().expect("matches is non-empty"))
}

pub fn validate_mount_boundary(
    root: &str,
    records: &[MountRecord],
    explicit_records: bool,
) -> Result<(), SecurityError> {
    let root = absolute(root)?;
    let anchor = containing_mount(&root, records)?;
    let privileged = rustix::process::geteuid().as_raw() == 0;
    if !ALLOWED_FILESYSTEMS.contains(&anchor.filesystem_type.as_str())
        && (privileged || explicit_records)
    {
        return Err(SecurityError::invalid(format!(
            "unsupported filesystem '{}' for control root {root}",
            anchor.filesystem_type
        )));
    }
    let mut nested: Vec<&str> = records
        .iter()
        .filter(|item| {
            item.mount_point != anchor.mount_point
                && path_contains(&root, &item.mount_point).unwrap_or(false)
        })
        .map(|item| item.mount_point.as_str())
        .collect();
    nested.sort_unstable();
    if let Some(mount) = nested.first() {
        return Err(SecurityError::invalid(format!(
            "nested mount is not supported: {mount}"
        )));
    }
    Ok(())
}

pub fn validate_data_mount_boundary(
    path: &str,
    records: &[MountRecord],
) -> Result<MountRecord, SecurityError> {
    let path = absolute(path)?;
    let anchor = containing_mount(&path, records)?.clone();
    let mut nested: Vec<&MountRecord> = records
        .iter()
        .filter(|item| {
            item.mount_point != path && path_contains(&path, &item.mount_point).unwrap_or(false)
        })
        .collect();
    nested.sort_by(|left, right| {
        (
            left.mount_id,
            left.parent_id,
            left.device.as_str(),
            left.filesystem_root.as_str(),
            left.mount_point.as_str(),
            left.filesystem_type.as_str(),
            left.source.as_str(),
        )
            .cmp(&(
                right.mount_id,
                right.parent_id,
                right.device.as_str(),
                right.filesystem_root.as_str(),
                right.mount_point.as_str(),
                right.filesystem_type.as_str(),
                right.source.as_str(),
            ))
    });
    if let Some(mount) = nested.first() {
        return Err(SecurityError::invalid(format!(
            "nested mount is not supported: {}",
            mount.mount_point
        )));
    }
    Ok(anchor)
}

pub fn current_mount_records() -> Result<Vec<MountRecord>, SecurityError> {
    let text = std::fs::read_to_string("/proc/self/mountinfo").map_err(|error| {
        SecurityError::runtime(format!("cannot inspect mounted filesystems: {error}"))
    })?;
    mount_records(&text, "/proc/self/mountinfo")
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountSnapshot {
    pub mount_id: i32,
    pub device: String,
    pub mount_point: String,
    pub nested: Vec<String>,
}

pub fn data_mount_snapshot(path: &str) -> Result<MountSnapshot, SecurityError> {
    let records = current_mount_records()?;
    snapshot_from_records(path, &records)
}

pub fn snapshot_from_records(
    path: &str,
    records: &[MountRecord],
) -> Result<MountSnapshot, SecurityError> {
    let anchor = validate_data_mount_boundary(path, records)?;
    let path = absolute(path)?;
    let mut nested: Vec<String> = records
        .iter()
        .filter(|item| {
            item.mount_point != path && path_contains(&path, &item.mount_point).unwrap_or(false)
        })
        .map(|item| item.mount_point.clone())
        .collect();
    nested.sort();
    Ok(MountSnapshot {
        mount_id: anchor.mount_id,
        device: anchor.device,
        mount_point: anchor.mount_point,
        nested,
    })
}

pub fn validate_live_control_root(
    path: &str,
    allow_missing: bool,
) -> Result<String, SecurityError> {
    let records = current_mount_records()?;
    validate_control_root_explicit(path, allow_missing, &records, false)
}

pub fn validate_control_root(
    path: &str,
    allow_missing: bool,
    records: &[MountRecord],
) -> Result<String, SecurityError> {
    validate_control_root_explicit(path, allow_missing, records, true)
}

fn validate_control_root_explicit(
    path: &str,
    allow_missing: bool,
    records: &[MountRecord],
    explicit_records: bool,
) -> Result<String, SecurityError> {
    let path = absolute(path)?;
    let exists = std::fs::symlink_metadata(&path).is_ok();
    if exists {
        validate_control_directory(&path, false, None)?;
    } else if allow_missing {
        let parent = parent_path(&path);
        validate_control_directory(&parent, false, None)?;
    } else {
        return Err(SecurityError::invalid(format!(
            "control root does not exist: {path}"
        )));
    }
    validate_mount_boundary(&path, records, explicit_records)?;
    Ok(path)
}

fn absolute(path: &str) -> Result<String, SecurityError> {
    lexical_absolute(path).map_err(|error| SecurityError::invalid(error.message))
}

fn parent_path(path: &str) -> String {
    match path.rsplit_once('/') {
        Some(("", _)) | Some(("/", _)) => "/".to_owned(),
        Some((parent, _)) => parent.to_owned(),
        None => "/".to_owned(),
    }
}

fn part_count(path: &str) -> usize {
    if path == "/" {
        1
    } else {
        path.split('/').filter(|part| !part.is_empty()).count() + 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(mount_point: &str, filesystem: &str, mount_id: i32) -> MountRecord {
        MountRecord {
            mount_id,
            parent_id: 0,
            device: "8:1".to_owned(),
            filesystem_root: "/".to_owned(),
            mount_point: mount_point.to_owned(),
            filesystem_type: filesystem.to_owned(),
            source: "/dev/test".to_owned(),
        }
    }

    #[test]
    fn allowlist_accepts_ext4_xfs_and_btrfs_only() {
        for filesystem in ["ext4", "xfs", "btrfs"] {
            validate_mount_boundary("/srv/data", &[record("/", filesystem, 1)], true).unwrap();
        }
        for filesystem in ["ext2", "ext3", "zfs", "nfs", "fuse"] {
            let error = validate_mount_boundary("/srv/data", &[record("/", filesystem, 1)], true)
                .expect_err(filesystem);
            assert!(
                error.message.contains("unsupported filesystem"),
                "{}",
                error.message
            );
        }
    }

    #[test]
    fn same_device_bind_mount_below_payload_is_rejected() {
        let records = [
            record("/", "ext4", 1),
            record("/srv/data/app/cache", "ext4", 2),
        ];
        let error = validate_data_mount_boundary("/srv/data", &records).unwrap_err();
        assert!(error.message.contains("nested mount"), "{}", error.message);
    }

    #[test]
    fn data_path_may_itself_be_a_mount_point() {
        let records = [record("/", "ext4", 1), record("/mnt/media", "ntfs3", 2)];
        let anchor = validate_data_mount_boundary("/mnt/media", &records).unwrap();
        assert_eq!(anchor.mount_id, 2);
    }

    #[test]
    fn control_root_rejects_unsupported_filesystem_and_nested_mount() {
        let root = std::env::temp_dir();
        let root = root.to_str().expect("temp dir is utf-8");
        let error = validate_control_root(root, false, &[record("/", "nfs", 1)]).unwrap_err();
        assert!(error.message.contains("unsupported filesystem"));
        let nested = format!("{root}/nested");
        let error = validate_control_root(
            root,
            false,
            &[record("/", "ext4", 1), record(&nested, "ext4", 2)],
        )
        .unwrap_err();
        assert!(error.message.contains("nested mount"), "{}", error.message);
    }
}
