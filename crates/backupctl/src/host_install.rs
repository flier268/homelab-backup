//! Install and remove the release layout previously owned by the shell scripts.

use std::fs::{self, File};
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use rustix::fs::{FlockOperation, flock};
use rustix::process::geteuid;

use crate::launcher::{helper_image, protected_regular};

const LIB_ROOT: &str = "/usr/local/lib/homelab-backup";
const LAUNCHER: &str = "/usr/local/sbin/backupctl";
const UNITS: &[&str] = &[
    "homelab-backup.service",
    "homelab-backup.timer",
    "homelab-backup-maintenance.service",
    "homelab-backup-maintenance.timer",
    "homelab-backup-check.service",
    "homelab-backup-check.timer",
    "homelab-backup-status.service",
    "homelab-backup-status.timer",
    "homelab-backup-notify@.service",
];
const TIMERS: &[&str] = &[
    "homelab-backup.timer",
    "homelab-backup-maintenance.timer",
    "homelab-backup-check.timer",
    "homelab-backup-status.timer",
];
const SERVICES: &[&str] = &[
    "homelab-backup.service",
    "homelab-backup-maintenance.service",
    "homelab-backup-check.service",
    "homelab-backup-status.service",
];

pub fn require_root(command: &str) -> Result<(), String> {
    if geteuid().as_raw() != 0 {
        return Err(format!("{command} must be run as root"));
    }
    Ok(())
}

pub fn run_install(source: &Path) -> Result<(), String> {
    require_root("install")?;
    run_ok(Command::new("apt-get").arg("update"))?;
    run_ok(Command::new("apt-get").args([
        "install",
        "-y",
        "rsync",
        "restic",
        "rclone",
        "age",
        "ca-certificates",
        "curl",
        "openssl",
        "btrfs-progs",
        "util-linux",
        "zip",
        "unzip",
    ]))?;
    let lib = PathBuf::from(LIB_ROOT);
    let releases = lib.join("releases");
    fs::create_dir_all(&releases).map_err(|error| error.to_string())?;
    let _lock = hold_install_lock(&lib)?;
    let old = current_release(&lib)?;
    let release_next = unique_dir(&releases, "release.")?;
    let release_name = file_name(&release_next)?;
    let helper_tag = format!("homelab/volume-rsync:{release_name}");
    let backup = unique_dir(&lib, ".install-backup.")?;
    let mut published_current = false;
    let mut published_previous = false;
    let mut published_launcher = false;
    let mut published_units = false;
    let had_previous = lib.join("previous").exists();
    let had_launcher = Path::new(LAUNCHER).exists();
    let result = (|| {
        fs::set_permissions(&release_next, fs::Permissions::from_mode(0o755))
            .map_err(|error| error.to_string())?;
        File::create(release_next.join(".lease")).map_err(|error| error.to_string())?;
        let binary = std::env::current_exe().map_err(|error| error.to_string())?;
        let meta = fs::symlink_metadata(&binary).map_err(|error| error.to_string())?;
        if !meta.is_file() {
            return Err("running backupctl is not a regular file".to_owned());
        }
        let bin_dir = release_next.join("bin");
        fs::create_dir_all(&bin_dir).map_err(|error| error.to_string())?;
        install_file(&binary, &bin_dir.join("backupctl"), 0o755)?;
        if source.join("GUIDE.html").is_file() {
            install_file(
                &source.join("GUIDE.html"),
                &release_next.join("GUIDE.html"),
                0o644,
            )?;
        }
        run_ok(Command::new(bin_dir.join("backupctl")).arg("--version"))?;
        ensure_runtime_dirs()?;
        if !Path::new("/etc/homelab-backup/restic-password").exists() {
            let password = Command::new("openssl")
                .args(["rand", "-base64", "48"])
                .output()
                .map_err(|error| error.to_string())?;
            if !password.status.success() {
                return Err("openssl could not create the restic password".to_owned());
            }
            let mut file = File::create("/etc/homelab-backup/restic-password")
                .map_err(|error| error.to_string())?;
            file.write_all(&password.stdout)
                .map_err(|error| error.to_string())?;
        }
        fs::set_permissions(
            "/etc/homelab-backup/restic-password",
            fs::Permissions::from_mode(0o600),
        )
        .map_err(|error| error.to_string())?;
        let rclone = Path::new("/etc/homelab-backup/rclone/rclone.conf");
        if rclone.is_file() {
            fs::set_permissions(rclone, fs::Permissions::from_mode(0o600))
                .map_err(|error| error.to_string())?;
        }
        if !Path::new("/etc/homelab-backup/config.yaml").exists() {
            install_file(
                &source.join("config.yaml.example"),
                Path::new("/etc/homelab-backup/config.yaml"),
                0o600,
            )?;
        }
        run_ok(
            Command::new("docker")
                .args(["build", "-t", &helper_tag, "-f"])
                .arg(source.join("Dockerfile.volume-rsync"))
                .arg(source),
        )?;
        install_file(
            &source.join("Dockerfile.volume-rsync"),
            &release_next.join("Dockerfile.volume-rsync"),
            0o644,
        )?;
        let image_id = docker_image_id(&helper_tag)?;
        run_ok(Command::new("docker").args([
            "run",
            "--rm",
            "--network",
            "none",
            &image_id,
            "rsync",
            "--version",
        ]))?;
        fs::write(
            release_next.join("volume-helper-image"),
            format!("{{\"tag\":\"{helper_tag}\",\"image_id\":\"{image_id}\"}}\n"),
        )
        .map_err(|error| error.to_string())?;

        if had_previous {
            copy_path(&lib.join("previous"), &backup.join("previous"))?;
        }
        if had_launcher {
            copy_path(Path::new(LAUNCHER), &backup.join("backupctl"))?;
        }
        let mut next_units = Vec::new();
        for unit in UNITS {
            let live = Path::new("/etc/systemd/system").join(unit);
            if live.exists() {
                copy_path(&live, &backup.join(unit))?;
            }
            let next = unique_file(&live)?;
            install_file(&source.join("systemd").join(unit), &next, 0o644)?;
            next_units.push((next, live));
        }
        for (next, live) in &next_units {
            replace_path(next, live)?;
        }
        published_units = true;
        run_ok(Command::new("systemctl").arg("daemon-reload"))?;
        if let Some(old) = &old {
            let link = format!("releases/{}", file_name(old)?);
            publish_symlink(&link, &lib.join("previous"))?;
            published_previous = true;
        }
        let link = format!("releases/{release_name}");
        publish_symlink(&link, &lib.join("current"))?;
        published_current = true;
        install_file(&binary, Path::new(LAUNCHER), 0o755)?;
        published_launcher = true;
        Ok::<(), String>(())
    })();
    if let Err(error) = result {
        rollback(&Rollback {
            lib: &lib,
            old: &old,
            release_next: &release_next,
            helper_tag: &helper_tag,
            backup: &backup,
            published_current,
            published_previous,
            published_launcher,
            published_units,
            had_previous,
            had_launcher,
        });
        let _ = fs::remove_dir_all(&backup);
        return Err(error);
    }
    let _ = fs::remove_dir_all(&backup);
    prune_obsolete(&releases, &release_next, old.as_deref());
    println!("Installed. Timers are not enabled yet.");
    let _ = (had_previous, had_launcher);
    Ok(())
}

struct Rollback<'a> {
    lib: &'a Path,
    old: &'a Option<PathBuf>,
    release_next: &'a Path,
    helper_tag: &'a str,
    backup: &'a Path,
    published_current: bool,
    published_previous: bool,
    published_launcher: bool,
    published_units: bool,
    had_previous: bool,
    had_launcher: bool,
}

fn rollback(state: &Rollback<'_>) {
    let Rollback {
        lib,
        old,
        release_next,
        helper_tag,
        backup,
        published_current,
        published_previous,
        published_launcher,
        published_units,
        had_previous,
        had_launcher,
    } = *state;
    if published_current {
        let current = lib.join("current");
        if let Some(old) = old {
            let link = format!(
                "releases/{}",
                old.file_name().and_then(|n| n.to_str()).unwrap_or("")
            );
            if publish_symlink(&link, &current).is_err() {
                eprintln!("WARNING: could not roll back current release; retaining new release");
            }
        } else if fs::remove_file(&current).is_err() {
            eprintln!("WARNING: could not remove newly published current release");
        }
    }
    if published_launcher {
        if had_launcher {
            if replace_path(&backup.join("backupctl"), Path::new(LAUNCHER)).is_err() {
                eprintln!("WARNING: could not roll back launcher");
            }
        } else if fs::remove_file(LAUNCHER).is_err() {
            eprintln!("WARNING: could not remove newly published launcher");
        }
    }
    if published_previous {
        let previous = lib.join("previous");
        if had_previous {
            if replace_path(&backup.join("previous"), &previous).is_err() {
                eprintln!("WARNING: could not roll back previous release link");
            }
        } else if fs::remove_file(&previous).is_err() {
            eprintln!("WARNING: could not remove newly published previous release link");
        }
    }
    if published_units {
        for unit in UNITS {
            let live = Path::new("/etc/systemd/system").join(unit);
            let saved = backup.join(unit);
            if saved.exists() {
                if replace_path(&saved, &live).is_err() {
                    eprintln!("WARNING: could not roll back systemd unit: {unit}");
                }
            } else if fs::remove_file(&live).is_err() {
                eprintln!("WARNING: could not remove newly published systemd unit: {unit}");
            }
        }
        if Command::new("systemctl")
            .arg("daemon-reload")
            .status()
            .is_err()
        {
            eprintln!("WARNING: systemd daemon-reload failed during rollback");
        }
    }
    if !published_current {
        let _ = fs::remove_dir_all(release_next);
        let _ = Command::new("docker")
            .args(["image", "rm", helper_tag])
            .status();
    }
}

pub fn run_uninstall(purge: bool) -> Result<(), String> {
    require_root("uninstall")?;
    let lib = PathBuf::from(LIB_ROOT);
    let _lock = if lib.is_dir()
        && !fs::symlink_metadata(&lib)
            .map_err(|error| error.to_string())?
            .file_type()
            .is_symlink()
    {
        Some(hold_install_lock(&lib)?)
    } else if lib.exists() {
        return Err(format!(
            "installation root is not a real directory: {LIB_ROOT}"
        ));
    } else {
        None
    };
    for timer in TIMERS {
        let path = Path::new("/etc/systemd/system").join(timer);
        if path.exists() {
            let _ = Command::new("systemctl")
                .args(["disable", "--now", timer])
                .status();
            if systemctl_quiet(&["is-active", "--quiet", timer])
                || systemctl_quiet(&["is-enabled", "--quiet", timer])
            {
                return Err(format!("could not disable and stop systemd timer: {timer}"));
            }
        }
    }
    for service in SERVICES {
        let path = Path::new("/etc/systemd/system").join(service);
        if path.exists() {
            let _ = Command::new("systemctl").args(["stop", service]).status();
            if systemctl_quiet(&["is-active", "--quiet", service]) {
                return Err(format!("systemd unit is still active: {service}"));
            }
        }
    }
    let releases = lib.join("releases");
    let mut leases = Vec::new();
    if releases.is_dir() {
        for release in release_dirs(&releases)? {
            let lease = release.join(".lease");
            if !release.is_dir()
                || lease
                    .symlink_metadata()
                    .map(|m| !m.is_file())
                    .unwrap_or(true)
            {
                return Err(format!(
                    "unsafe installed release; refusing removal: {}",
                    release.display()
                ));
            }
            let file = File::open(&lease).map_err(|error| error.to_string())?;
            if flock(&file, FlockOperation::NonBlockingLockExclusive).is_err() {
                return Err(format!(
                    "an active process still uses release: {}",
                    release.display()
                ));
            }
            leases.push(file);
        }
    } else if releases.exists() {
        return Err(format!(
            "release store is not a real directory: {}",
            releases.display()
        ));
    }
    let mut images = Vec::new();
    if releases.is_dir() {
        for release in release_dirs(&releases)? {
            let metadata = release.join("volume-helper-image");
            if !metadata.is_file() {
                continue;
            }
            match helper_image(&metadata) {
                Ok(tag) => images.push(tag),
                Err(_) => eprintln!(
                    "WARNING: invalid helper image metadata; image was not removed: {}",
                    metadata.display()
                ),
            }
        }
    }
    let _ = fs::remove_file(LAUNCHER);
    for unit in UNITS {
        let _ = fs::remove_file(Path::new("/etc/systemd/system").join(unit));
    }
    let _ = Command::new("systemctl").arg("daemon-reload").status();
    let _ = Command::new("systemctl")
        .arg("reset-failed")
        .args(UNITS)
        .status();
    for image in images {
        if !Command::new("docker")
            .args(["image", "rm", &image])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
        {
            eprintln!("WARNING: helper image is absent or still in use: {image}");
        }
    }
    if lib.is_dir() {
        fs::remove_dir_all(&lib).map_err(|error| error.to_string())?;
    }
    let _ = fs::remove_dir_all("/run/homelab-backup");
    drop(leases);
    if purge {
        let _ = fs::remove_dir_all("/etc/homelab-backup");
        let _ = fs::remove_dir_all("/var/lib/homelab-backup");
        let _ = fs::remove_dir_all("/var/cache/homelab-backup");
        println!("Uninstalled homelab-backup and purged its local configuration and working data.");
    } else {
        println!("Uninstalled homelab-backup.");
        println!(
            "Preserved /etc/homelab-backup, /var/lib/homelab-backup, and /var/cache/homelab-backup."
        );
    }
    println!("System packages, Restic repositories, and data below /srv were not removed.");
    Ok(())
}

fn hold_install_lock(lib: &Path) -> Result<File, String> {
    let path = lib.join("install.lock");
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|error| error.to_string())?;
    flock(&file, FlockOperation::NonBlockingLockExclusive)
        .map_err(|_| "another homelab-backup installation or removal is running".to_owned())?;
    Ok(file)
}

fn current_release(lib: &Path) -> Result<Option<PathBuf>, String> {
    let current = lib.join("current");
    if !current.exists() {
        return Ok(None);
    }
    let meta = fs::symlink_metadata(&current).map_err(|error| error.to_string())?;
    if !meta.file_type().is_symlink() {
        return Err(format!(
            "current release is not a symbolic link: {}",
            current.display()
        ));
    }
    let resolved = fs::canonicalize(&current).map_err(|error| error.to_string())?;
    let releases = lib.join("releases");
    let name = file_name(&resolved)?;
    if resolved.parent() != Some(releases.as_path()) || !name.starts_with("release.") {
        return Err(format!(
            "current release points outside {}",
            releases.display()
        ));
    }
    if !resolved.is_dir() {
        return Err(format!(
            "current release does not exist: {}",
            resolved.display()
        ));
    }
    Ok(Some(resolved))
}

fn ensure_runtime_dirs() -> Result<(), String> {
    for path in [
        "/etc/homelab-backup",
        "/etc/homelab-backup/rclone",
        "/var/lib/homelab-backup/staging",
        "/var/lib/homelab-backup/restores",
        "/var/lib/homelab-backup/state",
        "/run/homelab-backup",
        "/var/cache/homelab-backup/restic",
    ] {
        fs::create_dir_all(path).map_err(|error| error.to_string())?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| error.to_string())?;
    }
    for path in ["/srv/stacks", "/srv/data"] {
        fs::create_dir_all(path).map_err(|error| error.to_string())?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn docker_image_id(tag: &str) -> Result<String, String> {
    let output = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", tag])
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!("Docker could not inspect helper image {tag}"));
    }
    let id = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let hex = id.strip_prefix("sha256:").unwrap_or("");
    if hex.len() != 64 || !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("Docker returned an invalid helper image ID: {id}"));
    }
    Ok(id)
}

fn remove_obsolete_helper(tag: &str) -> bool {
    let listed = Command::new("docker")
        .args(["image", "ls", "--quiet", "--no-trunc", tag])
        .output();
    let Ok(listed) = listed else {
        eprintln!(
            "WARNING: could not inspect obsolete helper image; retaining release metadata for retry: {tag}"
        );
        return false;
    };
    if !listed.status.success() {
        eprintln!(
            "WARNING: could not inspect obsolete helper image; retaining release metadata for retry: {tag}"
        );
        return false;
    }
    if String::from_utf8_lossy(&listed.stdout).trim().is_empty() {
        return true;
    }
    let removed = Command::new("docker").args(["image", "rm", tag]).status();
    if removed.map(|status| status.success()).unwrap_or(false) {
        return true;
    }
    eprintln!(
        "WARNING: could not remove obsolete helper image; retaining release metadata for retry: {tag}"
    );
    false
}

fn prune_obsolete(releases: &Path, current: &Path, previous: Option<&Path>) {
    let Ok(dirs) = release_dirs(releases) else {
        return;
    };
    for release in dirs {
        if release == current || Some(release.as_path()) == previous {
            continue;
        }
        let lease = release.join(".lease");
        if protected_regular(&lease).is_err() {
            eprintln!(
                "WARNING: release lease is missing or unsafe; retaining release: {}",
                release.display()
            );
            continue;
        }
        let Ok(file) = File::open(&lease) else {
            continue;
        };
        if flock(&file, FlockOperation::NonBlockingLockExclusive).is_err() {
            eprintln!(
                "WARNING: active process still uses release; retaining it: {}",
                release.display()
            );
            continue;
        }
        let metadata = release.join("volume-helper-image");
        if metadata.is_file() {
            let tag = match helper_image(&metadata) {
                Ok(tag) => tag,
                Err(_) => {
                    eprintln!(
                        "WARNING: invalid obsolete helper image metadata; retaining release for inspection: {}",
                        release.display()
                    );
                    continue;
                }
            };
            if !remove_obsolete_helper(&tag) {
                continue;
            }
        }
        if fs::remove_dir_all(&release).is_err() {
            eprintln!(
                "WARNING: could not remove obsolete release: {}",
                release.display()
            );
        }
    }
}

fn release_dirs(releases: &Path) -> Result<Vec<PathBuf>, String> {
    let mut found = Vec::new();
    for entry in fs::read_dir(releases).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with("release.") {
            found.push(entry.path());
        }
    }
    found.sort();
    Ok(found)
}

fn unique_dir(parent: &Path, prefix: &str) -> Result<PathBuf, String> {
    for _ in 0..16 {
        let name = format!(
            "{prefix}{:08x}",
            rustix::process::getpid().as_raw_pid() as u32 ^ rand_nibble()
        );
        let path = parent.join(name);
        match fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.to_string()),
        }
    }
    Err("could not reserve a release directory".to_owned())
}

fn unique_file(live: &Path) -> Result<PathBuf, String> {
    let name = format!(
        "{}.next.{}",
        live.file_name().and_then(|n| n.to_str()).unwrap_or("unit"),
        std::process::id()
    );
    Ok(live.with_file_name(name))
}

fn rand_nibble() -> u32 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(1)
}

fn publish_symlink(target: &str, path: &Path) -> Result<(), String> {
    let next = path.with_file_name(format!(
        ".{}.next.{}",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("link"),
        std::process::id()
    ));
    std::os::unix::fs::symlink(target, &next).map_err(|error| error.to_string())?;
    replace_path(&next, path)
}

fn replace_path(source: &Path, destination: &Path) -> Result<(), String> {
    fs::rename(source, destination).map_err(|error| error.to_string())
}

fn copy_path(source: &Path, destination: &Path) -> Result<(), String> {
    let status = Command::new("cp")
        .args(["-a", "--"])
        .arg(source)
        .arg(destination)
        .status()
        .map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("could not copy {}", source.display()))
    }
}

fn install_file(source: &Path, destination: &Path, mode: u32) -> Result<(), String> {
    fs::copy(source, destination).map_err(|error| error.to_string())?;
    fs::set_permissions(destination, fs::Permissions::from_mode(mode))
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn file_name(path: &Path) -> Result<String, String> {
    path.file_name()
        .and_then(|name| name.to_str())
        .map(str::to_owned)
        .ok_or_else(|| format!("invalid path name: {}", path.display()))
}

fn run_ok(command: &mut Command) -> Result<(), String> {
    let status = command.status().map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("command failed: {command:?}"))
    }
}

fn systemctl_quiet(args: &[&str]) -> bool {
    Command::new("systemctl")
        .args(args)
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}
