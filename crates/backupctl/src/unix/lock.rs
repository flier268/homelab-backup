//! The process-wide backup lock. Reentrant in one process, never follows a symlink.

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{Local, SecondsFormat};
use rustix::fs::{FlockOperation, Mode, OFlags, flock, fstat, openat};
use rustix::io::Errno;

use super::control::{
    ensure_control_directory, file_name, is_reg, mode_of, open_directory, parent_path,
};
use super::error::SecurityError;

pub const RUN_DUE_LOCK_RETRIES: u32 = 3;
pub const LOCK_RETRY_SECONDS: f64 = 0.1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockHolder {
    pub pid: Option<u32>,
    pub started: Option<String>,
}

struct HeldLock {
    depth: usize,
    file: File,
}

static HELD: Mutex<Option<HashMap<String, HeldLock>>> = Mutex::new(None);

pub struct GlobalLock {
    path: PathBuf,
    nonblocking: bool,
    retries: u32,
    acquired: bool,
}

impl GlobalLock {
    pub fn new(path: impl Into<PathBuf>, nonblocking: bool, retries: u32) -> Self {
        Self {
            path: path.into(),
            nonblocking,
            retries,
            acquired: false,
        }
    }

    pub fn enter(&mut self, sleep: &mut dyn FnMut()) -> Result<bool, SecurityError> {
        let key = canonical_key(&self.path)?;
        {
            let mut held = HELD.lock().expect("lock map");
            let held = held.get_or_insert_with(HashMap::new);
            if let Some(state) = held.get_mut(&key) {
                state.depth += 1;
                self.acquired = true;
                return Ok(true);
            }
        }
        ensure_control_directory(&parent_path(&key), 0o700)?;
        let parent = open_directory(&parent_path(&key))?;
        let name = file_name(&key);
        let fd = openat(
            &parent,
            name.as_str(),
            OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_bits_truncate(0o600),
        )
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
        rustix::fs::fchmod(&fd, Mode::from_bits_truncate(0o600))
            .map_err(|error| SecurityError::runtime(error.to_string()))?;
        let mut file = File::from(fd);
        let operation = if self.nonblocking {
            FlockOperation::NonBlockingLockExclusive
        } else {
            FlockOperation::LockExclusive
        };
        let mut attempt = 0;
        loop {
            match flock(file.as_fd(), operation) {
                Ok(()) => break,
                Err(Errno::AGAIN) => {
                    if attempt >= self.retries {
                        return Ok(false);
                    }
                    attempt += 1;
                    sleep();
                }
                Err(error) => return Err(SecurityError::runtime(error.to_string())),
            }
        }
        file.seek(SeekFrom::Start(0)).map_err(io)?;
        file.set_len(0).map_err(io)?;
        let started = Local::now().to_rfc3339_opts(SecondsFormat::Secs, true);
        writeln!(file, "pid={} started={started}", std::process::id()).map_err(io)?;
        file.flush().map_err(io)?;
        HELD.lock()
            .expect("lock map")
            .get_or_insert_with(HashMap::new)
            .insert(key, HeldLock { depth: 1, file });
        self.acquired = true;
        Ok(true)
    }
}

impl Drop for GlobalLock {
    fn drop(&mut self) {
        if !self.acquired {
            return;
        }
        let Ok(key) = canonical_key(&self.path) else {
            return;
        };
        let mut held = HELD.lock().expect("lock map");
        let Some(map) = held.as_mut() else {
            return;
        };
        let Some(state) = map.get_mut(&key) else {
            return;
        };
        state.depth -= 1;
        if state.depth > 0 {
            return;
        }
        let mut state = map.remove(&key).expect("depth reached zero");
        let _ = state.file.seek(SeekFrom::Start(0));
        let _ = state.file.set_len(0);
        let _ = state.file.flush();
        let _ = flock(state.file.as_fd(), FlockOperation::Unlock);
    }
}

pub fn global_lock_holder(path: &Path) -> Result<Option<LockHolder>, SecurityError> {
    let key = match path.to_str() {
        Some(text) => homelab_backup_domain::paths::lexical_absolute(text)
            .map_err(|error| SecurityError::invalid(error.message))?,
        None => return Err(SecurityError::invalid("path is not utf-8")),
    };
    let fd = match openat(
        rustix::fs::CWD,
        key.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Ok(None),
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    };
    let metadata = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Ok(None);
    }
    match flock(fd.as_fd(), FlockOperation::NonBlockingLockShared) {
        Ok(()) => {
            let _ = flock(fd.as_fd(), FlockOperation::Unlock);
            return Ok(None);
        }
        Err(Errno::AGAIN) => {}
        Err(error) => return Err(SecurityError::runtime(error.to_string())),
    }
    let mut file = File::from(fd);
    let mut buffer = [0u8; 4096];
    let count = file.read(&mut buffer).map_err(io)?;
    Ok(Some(parse_holder(&String::from_utf8_lossy(
        &buffer[..count],
    ))))
}

fn parse_holder(content: &str) -> LockHolder {
    let mut pid = None;
    let mut started = None;
    for item in content.split_whitespace() {
        let Some((key, value)) = item.split_once('=') else {
            continue;
        };
        if key == "pid" {
            pid = value
                .parse()
                .ok()
                .filter(|_| value.bytes().all(|byte| byte.is_ascii_digit()));
        } else if key == "started" {
            started = Some(value.to_owned());
        }
    }
    LockHolder { pid, started }
}

fn canonical_key(path: &Path) -> Result<String, SecurityError> {
    let text = path
        .to_str()
        .ok_or_else(|| SecurityError::invalid("path is not utf-8"))?;
    homelab_backup_domain::paths::lexical_absolute(text)
        .map_err(|error| SecurityError::invalid(error.message))
}

fn io(error: std::io::Error) -> SecurityError {
    SecurityError::runtime(error.to_string())
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    fn scratch() -> PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("homelab-lock-{}-{id}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[test]
    fn release_clears_holder_line_and_nesting_waits_for_the_outer_guard() {
        let root = scratch();
        let path = root.join("backupctl.lock");
        let mut outer = GlobalLock::new(&path, false, 0);
        assert!(outer.enter(&mut || {}).unwrap());
        assert!(
            std::fs::read_to_string(&path)
                .unwrap()
                .contains(&format!("pid={}", std::process::id()))
        );
        let mut inner = GlobalLock::new(&path, true, 0);
        assert!(inner.enter(&mut || {}).unwrap());
        drop(inner);
        assert!(
            std::fs::read_to_string(&path)
                .unwrap()
                .contains(&format!("pid={}", std::process::id()))
        );
        drop(outer);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "");
        assert!(global_lock_holder(&path).unwrap().is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn busy_nonblocking_lock_retries_then_gives_up() {
        let root = scratch();
        let path = root.join("backupctl.lock");
        let holder = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .unwrap();
        flock(holder.as_fd(), FlockOperation::LockExclusive).unwrap();
        let mut sleeps = 0;
        let mut contender = GlobalLock::new(&path, true, RUN_DUE_LOCK_RETRIES);
        assert!(!contender.enter(&mut || sleeps += 1).unwrap());
        assert_eq!(sleeps, 3);
        drop(holder);
        drop(contender);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn holder_reports_a_live_lock_without_releasing_it() {
        let root = scratch();
        let path = root.join("backupctl.lock");
        assert!(global_lock_holder(&path).unwrap().is_none());
        assert!(!path.exists());
        std::fs::write(&path, "pid=1 started=2026-10-11T00:00:00+08:00\n").unwrap();
        assert!(global_lock_holder(&path).unwrap().is_none());
        let mut holder = GlobalLock::new(&path, false, 0);
        assert!(holder.enter(&mut || {}).unwrap());
        let seen = global_lock_holder(&path).unwrap().unwrap();
        assert_eq!(seen.pid, Some(std::process::id()));
        assert!(seen.started.is_some());
        let other = File::open(&path).unwrap();
        let busy = flock(other.as_fd(), FlockOperation::NonBlockingLockExclusive);
        assert!(busy.is_err());
        drop(other);
        drop(holder);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn symlink_is_not_followed() {
        let root = scratch();
        let target = root.join("target");
        std::fs::write(&target, "").unwrap();
        let link = root.join("backupctl.lock");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(global_lock_holder(&link).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }
}
