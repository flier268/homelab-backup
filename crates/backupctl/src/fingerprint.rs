//! Stage fingerprints. Inode and staging-wrapper mtimes are not part of the hash.

use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::os::fd::AsFd;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use rustix::fs::{Mode, OFlags, fstat, openat};
use sha2::{Digest, Sha256};

use crate::unix::control::{is_dir, is_lnk, is_reg};
use crate::unix::error::SecurityError;

const CHUNK: usize = 1024 * 1024;
const FINGERPRINT_VERSION: &[u8] = b"homelab-backup-stage-v1";
const CONTEXT_VERSION: &[u8] = b"homelab-backup-snapshot-context-v1";

pub fn stage_fingerprint(root: &Path) -> Result<String, SecurityError> {
    let initial = std::fs::symlink_metadata(root)
        .map_err(|error| SecurityError::invalid(format!("{}: {error}", root.display())))?;
    let mode = initial.mode();
    if !is_dir(mode) || is_lnk(mode) {
        return Err(SecurityError::invalid(format!(
            "staging root must be a real directory: {}",
            root.display()
        )));
    }
    let mut digest = Sha256::new();
    field(&mut digest, FINGERPRINT_VERSION);
    let mut hardlinks = HashMap::<(u64, u64), Vec<u8>>::new();
    let synthetic = synthetic_directories(root)?;
    visit(
        root,
        Path::new("."),
        &synthetic,
        &mut hardlinks,
        &mut digest,
    )?;
    Ok(format!("sha256:{:x}", digest.finalize()))
}

pub fn snapshot_context_fingerprint(host_id: &str, repository_id: &str) -> String {
    let mut digest = Sha256::new();
    field(&mut digest, CONTEXT_VERSION);
    field(&mut digest, repository_id.as_bytes());
    field(&mut digest, host_id.as_bytes());
    format!("sha256:{:x}", digest.finalize())
}

fn synthetic_directories(root: &Path) -> Result<std::collections::HashSet<String>, SecurityError> {
    let mut names = std::collections::HashSet::from([
        ".".to_owned(),
        "paths".to_owned(),
        "volumes".to_owned(),
        "_meta".to_owned(),
    ]);
    let inventory_path = root.join("_meta/inventory.json");
    let text = match std::fs::read_to_string(&inventory_path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(names),
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    let inventory: serde_json::Value = serde_json::from_str(&text).map_err(|error| {
        SecurityError::invalid(format!("staging inventory must be a JSON mapping: {error}"))
    })?;
    let Some(inventory) = inventory.as_object() else {
        return Err(SecurityError::invalid(
            "staging inventory must be a JSON mapping",
        ));
    };
    let empty = Vec::new();
    let paths = match inventory.get("paths") {
        None => &empty,
        Some(value) => value
            .as_array()
            .ok_or_else(|| SecurityError::invalid("staging inventory paths must be a JSON list"))?,
    };
    for item in paths {
        let Some(item) = item.as_object() else {
            return Err(SecurityError::invalid(
                "staging inventory path entries must be mappings",
            ));
        };
        let Some(source_id) = item.get("id").and_then(serde_json::Value::as_str) else {
            continue;
        };
        let single = !source_id.is_empty()
            && !source_id.contains('/')
            && source_id != "."
            && source_id != "..";
        let present = item.get("present") == Some(&serde_json::Value::Bool(true));
        let kind = item.get("type").and_then(serde_json::Value::as_str);
        if present && matches!(kind, Some("file" | "symlink")) && single {
            names.insert(format!("paths/{source_id}"));
        }
    }
    Ok(names)
}

fn visit(
    path: &Path,
    relative: &Path,
    synthetic: &std::collections::HashSet<String>,
    hardlinks: &mut HashMap<(u64, u64), Vec<u8>>,
    digest: &mut Sha256,
) -> Result<(), SecurityError> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| SecurityError::runtime(format!("{}: {error}", path.display())))?;
    let mode = metadata.mode();
    hash_metadata(digest, path, relative, &metadata, synthetic)?;
    if is_reg(mode) {
        hash_regular(digest, path, relative, &metadata, hardlinks)?;
        return Ok(());
    }
    if is_lnk(mode) {
        field(digest, b"symlink");
        let target =
            std::fs::read_link(path).map_err(|error| SecurityError::runtime(error.to_string()))?;
        field(digest, target.as_os_str().as_bytes());
        return Ok(());
    }
    if is_dir(mode) {
        field(digest, b"directory");
        let mut names = std::fs::read_dir(path)
            .map_err(|error| SecurityError::runtime(error.to_string()))?
            .map(|entry| entry.map(|entry| entry.file_name()))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        names.sort_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
        for name in names {
            visit(
                &path.join(&name),
                &relative.join(&name),
                synthetic,
                hardlinks,
                digest,
            )?;
        }
        return Ok(());
    }
    Err(SecurityError::runtime(format!(
        "unsupported staging entry type: {}",
        path.display()
    )))
}

fn hash_metadata(
    digest: &mut Sha256,
    path: &Path,
    relative: &Path,
    metadata: &std::fs::Metadata,
    synthetic: &std::collections::HashSet<String>,
) -> Result<(), SecurityError> {
    let mode = metadata.mode();
    field(digest, relative_bytes(relative).as_slice());
    field_text(digest, &format!("{}", mode & 0o170000));
    field_text(digest, &format!("{}", mode & 0o7777));
    field_text(digest, &metadata.uid().to_string());
    field_text(digest, &metadata.gid().to_string());
    let relative_text = relative_text(relative);
    let synthetic_directory = is_dir(mode) && synthetic.contains(&relative_text);
    let generated = relative_text == "_meta" || relative_text.starts_with("_meta/");
    if !synthetic_directory && !generated {
        let mtime_ns = metadata.mtime() as i128 * 1_000_000_000 + metadata.mtime_nsec() as i128;
        field_text(digest, &mtime_ns.to_string());
    }
    let names = xattr_names(path)?;
    field_text(digest, &names.len().to_string());
    for name in &names {
        field(digest, name);
        field(digest, &xattr_value(path, name)?);
    }
    Ok(())
}

fn hash_regular(
    digest: &mut Sha256,
    path: &Path,
    relative: &Path,
    metadata: &std::fs::Metadata,
    hardlinks: &mut HashMap<(u64, u64), Vec<u8>>,
) -> Result<(), SecurityError> {
    if metadata.nlink() > 1 {
        let identity = (metadata.dev(), metadata.ino());
        let canonical = hardlinks
            .entry(identity)
            .or_insert_with(|| relative_bytes(relative));
        field(digest, b"hardlink");
        field(digest, canonical);
    } else {
        field(digest, b"file");
    }
    field_text(digest, &metadata.len().to_string());
    let fd = openat(
        rustix::fs::CWD,
        path,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| SecurityError::runtime(format!("{}: {error}", path.display())))?;
    let opened = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if opened.st_dev != metadata.dev()
        || opened.st_ino != metadata.ino()
        || opened.st_size as u64 != metadata.len()
        || mtime_ns_stat(&opened) != mtime_ns_meta(metadata)
    {
        return Err(SecurityError::runtime(format!(
            "staging file changed while hashing: {}",
            path.display()
        )));
    }
    let mut file = File::from(fd);
    let mut buffer = vec![0; CHUNK];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let finished =
        fstat(file.as_fd()).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if finished.st_size != opened.st_size || mtime_ns_stat(&finished) != mtime_ns_stat(&opened) {
        return Err(SecurityError::runtime(format!(
            "staging file changed while hashing: {}",
            path.display()
        )));
    }
    Ok(())
}

fn field(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value);
}

fn field_text(digest: &mut Sha256, value: &str) {
    field(digest, value.as_bytes());
}

fn relative_bytes(relative: &Path) -> Vec<u8> {
    let mut bytes = Vec::new();
    for component in relative.components() {
        let std::path::Component::Normal(name) = component else {
            continue;
        };
        if !bytes.is_empty() {
            bytes.push(b'/');
        }
        bytes.extend_from_slice(name.as_bytes());
    }
    if bytes.is_empty() {
        bytes.push(b'.');
    }
    bytes
}

fn relative_text(relative: &Path) -> String {
    String::from_utf8(relative_bytes(relative))
        .unwrap_or_else(|error| String::from_utf8_lossy(error.as_bytes()).into_owned())
}

fn mtime_ns_meta(metadata: &std::fs::Metadata) -> i128 {
    metadata.mtime() as i128 * 1_000_000_000 + metadata.mtime_nsec() as i128
}

fn mtime_ns_stat(metadata: &rustix::fs::Stat) -> i128 {
    metadata.st_mtime as i128 * 1_000_000_000 + metadata.st_mtime_nsec as i128
}

fn xattr_names(path: &Path) -> Result<Vec<Vec<u8>>, SecurityError> {
    let mut listed = vec![0u8; 64 * 1024];
    let listed = match rustix::fs::llistxattr(path, listed.as_mut_slice()) {
        Ok(size) => {
            listed.truncate(size);
            listed
        }
        Err(error) if matches!(error.raw_os_error(), 38 | 95) => {
            return Ok(Vec::new());
        }
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    let mut names: Vec<Vec<u8>> = listed
        .split(|byte| *byte == 0)
        .filter(|name| !name.is_empty())
        .map(|name| name.to_vec())
        .collect();
    names.sort();
    Ok(names)
}

fn xattr_value(path: &Path, name: &[u8]) -> Result<Vec<u8>, SecurityError> {
    let name = std::ffi::CString::new(name.to_vec())
        .map_err(|_| SecurityError::runtime("xattr name contains NUL"))?;
    let mut value = vec![0u8; 64 * 1024];
    let size = rustix::fs::lgetxattr(path, &name, value.as_mut_slice())
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    value.truncate(size);
    Ok(value)
}

use std::os::unix::fs::MetadataExt;

#[cfg(test)]
mod tests {
    use super::*;

    fn tree(root: &Path, content: &[u8]) {
        std::fs::create_dir_all(root.join("paths/data")).unwrap();
        let payload = root.join("paths/data/payload.bin");
        std::fs::write(&payload, content).unwrap();
        set_times(&payload);
        set_times(&root.join("paths/data"));
        std::fs::create_dir_all(root.join("_meta")).unwrap();
        std::fs::write(root.join("_meta/inventory.json"), "{\"version\": 1}\n").unwrap();
    }

    fn set_times(path: &Path) {
        let stamp = rustix::fs::Timespec {
            tv_sec: 1_700_000_000,
            tv_nsec: 0,
        };
        rustix::fs::utimensat(
            rustix::fs::CWD,
            path,
            &rustix::fs::Timestamps {
                last_access: stamp,
                last_modification: stamp,
            },
            rustix::fs::AtFlags::empty(),
        )
        .unwrap();
    }

    #[test]
    fn recreated_trees_match_and_content_changes_do_not() {
        let root = std::env::temp_dir().join(format!("homelab-fp-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let first = root.join("first");
        let second = root.join("second");
        tree(&first, b"payload");
        tree(&second, b"payload");
        assert_eq!(
            stage_fingerprint(&first).unwrap(),
            stage_fingerprint(&second).unwrap()
        );
        let third = root.join("third");
        tree(&third, b"other");
        assert_ne!(
            stage_fingerprint(&first).unwrap(),
            stage_fingerprint(&third).unwrap()
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn wrapper_mtimes_are_ignored_and_payload_directory_mtimes_are_not() {
        let root = std::env::temp_dir().join(format!("homelab-fp-mtime-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let first = root.join("first");
        let second = root.join("second");
        tree(&first, b"payload");
        tree(&second, b"payload");
        for (path, seconds) in [
            (first.join("."), 1_600_000_000),
            (second.join("."), 1_800_000_000),
            (first.join("paths"), 1_600_000_000),
            (second.join("paths"), 1_800_000_000),
            (first.join("_meta"), 1_600_000_000),
            (second.join("_meta"), 1_800_000_000),
        ] {
            let stamp = rustix::fs::Timespec {
                tv_sec: seconds,
                tv_nsec: 0,
            };
            rustix::fs::utimensat(
                rustix::fs::CWD,
                &path,
                &rustix::fs::Timestamps {
                    last_access: stamp,
                    last_modification: stamp,
                },
                rustix::fs::AtFlags::empty(),
            )
            .unwrap();
        }
        assert_eq!(
            stage_fingerprint(&first).unwrap(),
            stage_fingerprint(&second).unwrap()
        );
        for (path, seconds) in [
            (first.join("paths/data"), 1_600_000_000),
            (second.join("paths/data"), 1_800_000_000),
        ] {
            let stamp = rustix::fs::Timespec {
                tv_sec: seconds,
                tv_nsec: 0,
            };
            rustix::fs::utimensat(
                rustix::fs::CWD,
                &path,
                &rustix::fs::Timestamps {
                    last_access: stamp,
                    last_modification: stamp,
                },
                rustix::fs::AtFlags::empty(),
            )
            .unwrap();
        }
        assert_ne!(
            stage_fingerprint(&first).unwrap(),
            stage_fingerprint(&second).unwrap()
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn snapshot_context_binds_repository_and_host() {
        let repository = "1".repeat(64);
        assert_eq!(
            snapshot_context_fingerprint("server-a", &repository),
            snapshot_context_fingerprint("server-a", &repository)
        );
        assert_ne!(
            snapshot_context_fingerprint("server-a", &repository),
            snapshot_context_fingerprint("server-a", &"2".repeat(64))
        );
        assert_ne!(
            snapshot_context_fingerprint("server-a", &repository),
            snapshot_context_fingerprint("server-b", &repository)
        );
    }
}
