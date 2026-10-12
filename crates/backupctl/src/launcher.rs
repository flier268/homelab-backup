//! Installed `/usr/local/sbin/backupctl` holds the release lease, then execs
//! that release's binary. Any other path runs the command in this process.

use std::fs::{self, File};
use std::os::unix::fs::MetadataExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use rustix::fs::{FlockOperation, flock};
use rustix::io::{FdFlags, fcntl_setfd};
use rustix::process::geteuid;

const LIB_ROOT: &str = "/usr/local/lib/homelab-backup";
const LAUNCHER: &str = "/usr/local/sbin/backupctl";

/// Re-exec the current release when this process is the installed launcher.
/// Returns when this binary should handle the command itself.
pub fn enter_if_installed_launcher() -> Result<(), String> {
    let exe = fs::canonicalize(std::env::current_exe().map_err(|error| error.to_string())?)
        .map_err(|error| error.to_string())?;
    if exe != Path::new(LAUNCHER) {
        return Ok(());
    }
    let release = release_root()?;
    let lease_path = release.join(".lease");
    protected_regular(&lease_path).map_err(|error| {
        format!(
            "installed release lease is invalid: {}: {error}",
            lease_path.display()
        )
    })?;
    let binary = release.join("bin/backupctl");
    let binary_meta = fs::symlink_metadata(&binary)
        .map_err(|_| format!("installed application is missing: {}", binary.display()))?;
    if !binary_meta.file_type().is_file() || binary_meta.mode() & 0o111 == 0 {
        return Err(format!(
            "installed application is missing: {}",
            binary.display()
        ));
    }
    let helper = helper_image(&release.join("volume-helper-image"))?;
    let dockerfile = release.join("Dockerfile.volume-rsync");
    let dockerfile_ok = fs::symlink_metadata(&dockerfile)
        .ok()
        .is_some_and(|meta| meta.file_type().is_file());
    let lease = File::open(&lease_path).map_err(|error| {
        format!(
            "installed release lease is invalid: {}: {error}",
            lease_path.display()
        )
    })?;
    flock(&lease, FlockOperation::LockShared).map_err(|error| {
        format!(
            "installed release lease is invalid: {}: {error}",
            lease_path.display()
        )
    })?;
    fcntl_setfd(&lease, FdFlags::empty()).map_err(|error| error.to_string())?;
    std::mem::forget(lease);

    let mut command = Command::new(&binary);
    command.args(std::env::args_os().skip(1));
    command.env("HOMELAB_BACKUP_RELEASE_ROOT", &release);
    command.env("HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_IMAGE", helper);
    if dockerfile_ok {
        command.env(
            "HOMELAB_BACKUP_RELEASE_VOLUME_HELPER_DOCKERFILE",
            &dockerfile,
        );
    }
    Err(command.exec().to_string())
}

pub fn release_root() -> Result<PathBuf, String> {
    let releases = Path::new(LIB_ROOT).join("releases");
    let explicit = std::env::var_os("HOMELAB_BACKUP_RELEASE_ROOT");
    let raw = if let Some(raw) = explicit {
        PathBuf::from(raw)
    } else {
        fs::canonicalize(Path::new(LIB_ROOT).join("current"))
            .map_err(|_| format!("installed current release is invalid: {LIB_ROOT}/current"))?
    };
    let release = fs::canonicalize(&raw).map_err(|_| {
        format!(
            "installed release is outside the release store: {}",
            raw.display()
        )
    })?;
    let name = release
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    let name_ok = name.strip_prefix("release.").is_some_and(|rest| {
        !rest.is_empty() && rest.bytes().all(|byte| byte.is_ascii_alphanumeric())
    });
    if release.parent() != Some(releases.as_path()) || !name_ok || !release.is_dir() {
        return Err(format!(
            "installed release is outside the release store: {}",
            release.display()
        ));
    }
    if fs::symlink_metadata(&release)
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(true)
    {
        return Err(format!(
            "installed release is outside the release store: {}",
            release.display()
        ));
    }
    Ok(release)
}

pub fn protected_regular(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if !meta.file_type().is_file() {
        return Err("lease is not a protected regular file".to_owned());
    }
    let owner_ok = meta.uid() == 0 || meta.uid() == geteuid().as_raw();
    if !owner_ok || meta.mode() & 0o022 != 0 {
        return Err("lease is not a protected regular file".to_owned());
    }
    Ok(())
}

pub fn helper_image(path: &Path) -> Result<String, String> {
    protected_regular(path).map_err(|_| {
        format!(
            "installed helper image metadata is invalid: {}",
            path.display()
        )
    })?;
    let text = fs::read_to_string(path).map_err(|_| {
        format!(
            "installed helper image metadata is invalid: {}",
            path.display()
        )
    })?;
    let line = text.lines().next().unwrap_or("").trim();
    let tag = if let Some(rest) = line.strip_prefix('{') {
        let marker = "\"tag\":\"";
        let start = rest.find(marker).ok_or_else(|| {
            format!(
                "installed helper image metadata is invalid: {}",
                path.display()
            )
        })?;
        let value = &rest[start + marker.len()..];
        let end = value.find('"').ok_or_else(|| {
            format!(
                "installed helper image metadata is invalid: {}",
                path.display()
            )
        })?;
        value[..end].to_owned()
    } else {
        line.to_owned()
    };
    if !tag.starts_with("homelab/volume-rsync:release.") {
        return Err(format!(
            "installed helper image metadata is invalid: {}",
            path.display()
        ));
    }
    let suffix = &tag["homelab/volume-rsync:release.".len()..];
    if suffix.is_empty() || !suffix.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        return Err(format!(
            "installed helper image metadata is invalid: {}",
            path.display()
        ));
    }
    Ok(tag)
}

pub fn source_root() -> Result<PathBuf, String> {
    if let Some(raw) = std::env::var_os("HOMELAB_BACKUP_SOURCE_ROOT") {
        return Ok(PathBuf::from(raw));
    }
    let cwd = std::env::current_dir().map_err(|error| error.to_string())?;
    if cwd.join("Dockerfile.volume-rsync").is_file() && cwd.join("systemd").is_dir() {
        return Ok(cwd);
    }
    Err(
        "run this command from a homelab-backup source tree, or set HOMELAB_BACKUP_SOURCE_ROOT"
            .to_owned(),
    )
}
