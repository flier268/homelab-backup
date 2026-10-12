//! Publish a file by writing a sibling temporary and then linking or replacing it.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use rustix::fs::{AtFlags, Mode, OFlags, fstat, fsync, linkat, openat, renameat, unlinkat};
use rustix::io::Errno;

use super::control::{file_name, is_reg, open_directory, validate_control_directory};
use super::error::SecurityError;

pub type FileIdentity = (u64, u64);

pub(crate) trait AtomicSys {
    fn fsync(&mut self, fd: BorrowedFd<'_>) -> io::Result<()>;
    fn link(&mut self, parent: BorrowedFd<'_>, temporary: &str, target: &str) -> io::Result<()>;
    fn replace(&mut self, parent: BorrowedFd<'_>, temporary: &str, target: &str) -> io::Result<()>;
}

struct SystemOps;

impl AtomicSys for SystemOps {
    fn fsync(&mut self, fd: BorrowedFd<'_>) -> io::Result<()> {
        fsync(fd).map_err(io::Error::from)
    }

    fn link(&mut self, parent: BorrowedFd<'_>, temporary: &str, target: &str) -> io::Result<()> {
        linkat(parent, temporary, parent, target, AtFlags::empty()).map_err(io::Error::from)
    }

    fn replace(&mut self, parent: BorrowedFd<'_>, temporary: &str, target: &str) -> io::Result<()> {
        renameat(parent, temporary, parent, target).map_err(io::Error::from)
    }
}

pub fn atomic_copy_file(
    source: &Path,
    target: &Path,
    require_absent: bool,
    mut on_publish: Option<&mut dyn FnMut(FileIdentity)>,
) -> Result<FileIdentity, SecurityError> {
    atomic_copy_file_with(
        source,
        target,
        require_absent,
        &mut on_publish,
        &mut SystemOps,
    )
}

pub(crate) fn atomic_copy_file_with(
    source: &Path,
    target: &Path,
    require_absent: bool,
    on_publish: &mut Option<&mut dyn FnMut(FileIdentity)>,
    ops: &mut impl AtomicSys,
) -> Result<FileIdentity, SecurityError> {
    let source_meta = std::fs::symlink_metadata(source)
        .map_err(|error| SecurityError::invalid(format!("{}: {error}", source.display())))?;
    if !is_reg(source_meta.mode()) {
        return Err(SecurityError::invalid(format!(
            "atomic source is not a regular file: {}",
            source.display()
        )));
    }
    let target = target
        .to_str()
        .ok_or_else(|| SecurityError::invalid("path is not utf-8"))?;
    let target = homelab_backup_domain::paths::lexical_absolute(target)
        .map_err(|error| SecurityError::invalid(error.message))?;
    let parent = super::control::parent_path(&target);
    validate_control_directory(&parent, false, None)?;
    let parent_fd = open_directory(&parent)?;
    let temporary = format!(".{}.{}.tmp", file_name(&target), hex_token());
    let result = publish_copy(
        source,
        &target,
        &temporary,
        &parent_fd,
        require_absent,
        on_publish,
        ops,
    );
    let _ = unlinkat(&parent_fd, temporary.as_str(), AtFlags::empty());
    result
}

fn publish_copy(
    source: &Path,
    target: &str,
    temporary: &str,
    parent_fd: &OwnedFd,
    require_absent: bool,
    on_publish: &mut Option<&mut dyn FnMut(FileIdentity)>,
    ops: &mut impl AtomicSys,
) -> Result<FileIdentity, SecurityError> {
    let fd = openat(
        parent_fd,
        temporary,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    let identity = {
        let mut input =
            File::open(source).map_err(|error| SecurityError::runtime(error.to_string()))?;
        let mut output = File::from(fd);
        io::copy(&mut input, &mut output)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        output
            .flush()
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        ops.fsync(output.as_fd())
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let metadata =
            fstat(output.as_fd()).map_err(|error| SecurityError::runtime(error.to_string()))?;
        (metadata.st_dev, metadata.st_ino)
    };
    let name = file_name(target);
    if require_absent {
        ops.link(parent_fd.as_fd(), temporary, &name)
            .map_err(map_publish_error)?;
    } else {
        ops.replace(parent_fd.as_fd(), temporary, &name)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
    }
    if let Some(on_publish) = on_publish {
        on_publish(identity);
    }
    ops.fsync(parent_fd.as_fd())
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(identity)
}

pub fn atomic_write_json(path: &Path, value: &serde_json::Value) -> Result<(), SecurityError> {
    atomic_write_json_with(path, value, &mut SystemOps)
}

pub(crate) fn atomic_write_json_with(
    path: &Path,
    value: &serde_json::Value,
    ops: &mut impl AtomicSys,
) -> Result<(), SecurityError> {
    let path = path
        .to_str()
        .ok_or_else(|| SecurityError::invalid("path is not utf-8"))?;
    let path = homelab_backup_domain::paths::lexical_absolute(path)
        .map_err(|error| SecurityError::invalid(error.message))?;
    let parent = super::control::parent_path(&path);
    validate_control_directory(&parent, false, None)?;
    let parent_fd = open_directory(&parent)?;
    let temporary = format!(".{}.{}.tmp", file_name(&path), hex_token());
    let result = write_json(&path, &temporary, &parent_fd, value, ops);
    let _ = unlinkat(&parent_fd, temporary.as_str(), AtFlags::empty());
    result
}

fn write_json(
    path: &str,
    temporary: &str,
    parent_fd: &OwnedFd,
    value: &serde_json::Value,
    ops: &mut impl AtomicSys,
) -> Result<(), SecurityError> {
    let fd = openat(
        parent_fd,
        temporary,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(|error| SecurityError::runtime(error.to_string()))?;
    {
        let mut output = File::from(fd);
        let mut payload = serde_json::to_vec_pretty(value)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        payload.push(b'\n');
        output
            .write_all(&payload)
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        output
            .flush()
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        ops.fsync(output.as_fd())
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
    }
    ops.replace(parent_fd.as_fd(), temporary, &file_name(path))
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    ops.fsync(parent_fd.as_fd())
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(())
}

fn map_publish_error(error: io::Error) -> SecurityError {
    if error.kind() == io::ErrorKind::AlreadyExists
        || error.raw_os_error() == Some(Errno::EXIST.raw_os_error())
    {
        SecurityError::invalid(error.to_string())
    } else {
        SecurityError::runtime(error.to_string())
    }
}

fn hex_token() -> String {
    let mut bytes = [0u8; 8];
    if let Ok(mut file) = File::open("/dev/urandom") {
        let _ = file.read_exact(&mut bytes);
    }
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    use super::*;

    struct Recording {
        events: Vec<&'static str>,
        fsyncs: usize,
        fail_second_fsync: bool,
        fail_replace: bool,
        race_target: Option<std::path::PathBuf>,
    }

    impl AtomicSys for Recording {
        fn fsync(&mut self, fd: BorrowedFd<'_>) -> io::Result<()> {
            self.events.push("fsync");
            self.fsyncs += 1;
            if self.fail_second_fsync && self.fsyncs == 2 {
                return Err(io::Error::other("late fsync failure"));
            }
            SystemOps.fsync(fd)
        }

        fn link(
            &mut self,
            parent: BorrowedFd<'_>,
            temporary: &str,
            target: &str,
        ) -> io::Result<()> {
            if let Some(path) = &self.race_target {
                std::fs::write(path, "concurrent")?;
            }
            SystemOps.link(parent, temporary, target)
        }

        fn replace(
            &mut self,
            parent: BorrowedFd<'_>,
            temporary: &str,
            target: &str,
        ) -> io::Result<()> {
            self.events.push("replace");
            if self.fail_replace {
                return Err(io::Error::other("injected failure"));
            }
            SystemOps.replace(parent, temporary, target)
        }
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!("{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[test]
    fn publish_callback_runs_before_parent_fsync_failure() {
        let root = scratch("homelab-copy");
        let source = root.join("source");
        let target = root.join("target");
        std::fs::write(&source, "snapshot").unwrap();
        let mut events = Vec::new();
        let mut ops = Recording {
            events: Vec::new(),
            fsyncs: 0,
            fail_second_fsync: true,
            fail_replace: false,
            race_target: None,
        };
        {
            let mut publish = |identity| events.push(identity);
            let mut publish = Some(&mut publish as &mut dyn FnMut(FileIdentity));
            let error =
                atomic_copy_file_with(&source, &target, true, &mut publish, &mut ops).unwrap_err();
            assert!(error.message.contains("late fsync"), "{}", error.message);
        }
        let metadata = std::fs::metadata(&target).unwrap();
        assert_eq!(events, vec![(metadata.dev(), metadata.ino())]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn require_absent_publish_does_not_overwrite_racing_target() {
        let root = scratch("homelab-race");
        let source = root.join("source");
        std::fs::write(&source, "snapshot").unwrap();
        let target = root.join("target");
        let mut ops = Recording {
            events: Vec::new(),
            fsyncs: 0,
            fail_second_fsync: false,
            fail_replace: false,
            race_target: Some(target.clone()),
        };
        let mut publish = None;
        assert!(atomic_copy_file_with(&source, &target, true, &mut publish, &mut ops).is_err());
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "concurrent");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn json_fsyncs_file_before_replace_and_parent_after() {
        let root = scratch("homelab-json");
        let target = root.join("state.json");
        let mut ops = Recording {
            events: Vec::new(),
            fsyncs: 0,
            fail_second_fsync: false,
            fail_replace: false,
            race_target: None,
        };
        atomic_write_json_with(&target, &serde_json::json!({"ok": true}), &mut ops).unwrap();
        assert_eq!(ops.events, ["fsync", "replace", "fsync"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn replace_failure_preserves_old_json() {
        let root = scratch("homelab-old");
        let target = root.join("state.json");
        std::fs::write(&target, "{\"old\": true}\n").unwrap();
        let mut ops = Recording {
            events: Vec::new(),
            fsyncs: 0,
            fail_second_fsync: false,
            fail_replace: true,
            race_target: None,
        };
        let error = atomic_write_json_with(&target, &serde_json::json!({"old": false}), &mut ops)
            .unwrap_err();
        assert!(error.message.contains("injected"), "{}", error.message);
        assert_eq!(
            std::fs::read_to_string(&target).unwrap(),
            "{\"old\": true}\n"
        );
        let leftovers: Vec<_> = std::fs::read_dir(&root)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with('.'))
            .collect();
        assert!(leftovers.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }
}
