//! Config archive and bundle publication used by the recovery shell scripts.
//! The command stays hidden from `backupctl --help`.

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use homelab_backup_domain::config::validate_global_config;
use homelab_backup_domain::yaml::Yaml;
use rustix::fs::{
    AtFlags, CWD, Mode, OFlags, RenameFlags, fstat, fsync, linkat, openat, renameat, renameat_with,
    statat, unlinkat,
};
use rustix::io::Errno;
use rustix::process::geteuid;

use crate::documents::parse_yaml_text;
use crate::files::CommandError;
use crate::unix::control::{is_dir, is_reg, mode_of, validate_control_file};

const CONFIG_MEMBERS: &[(&str, &str)] = &[
    ("configs/restic-password", "restic-password"),
    ("configs/rclone.conf", "rclone/rclone.conf"),
    ("configs/config.yaml", "config.yaml"),
];

thread_local! {
    static FSYNC_BUDGET: Cell<Option<u32>> = const { Cell::new(None) };
}

#[derive(Debug)]
struct OpsFailure {
    message: String,
    code: u8,
}

impl std::fmt::Display for OpsFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl OpsFailure {
    fn error(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: 1,
        }
    }
}

pub fn run(args: &[String]) -> Result<(), CommandError> {
    match dispatch(args) {
        Ok(output) => {
            if let Some(output) = output {
                println!("{output}");
            }
            Ok(())
        }
        Err(failure) => {
            if !failure.message.is_empty() {
                eprintln!("{}", failure.message);
            }
            Err(match failure.code {
                2 => CommandError::Exit(2),
                75 => CommandError::Exit(75),
                _ => CommandError::Reported,
            })
        }
    }
}

fn dispatch(args: &[String]) -> Result<Option<String>, OpsFailure> {
    let command = args.first().map(String::as_str).unwrap_or("");
    match command {
        "lock-path" => one(args, |path| Ok(Some(config_lock_path(path)?))),
        "validate-lock" => one(args, |path| {
            validate_lock_path(path)?;
            Ok(None)
        }),
        "preflight-bundle" => one(args, |path| {
            preflight_bundle(Path::new(path))?;
            Ok(None)
        }),
        "publish-archive" => publish_archive_args(args),
        "publish-bundle" => publish_bundle_args(args),
        "copy-regular" => copy_regular_args(args),
        "copy-stdout" => copy_stdout_args(args),
        "remove-owned" => one(args, |path| {
            remove_owned(Path::new(path))?;
            Ok(None)
        }),
        _ => Err(OpsFailure::error(
            "usage: backupctl config-ops lock-path|validate-lock|preflight-bundle|publish-archive|publish-bundle|copy-regular|copy-stdout|remove-owned",
        )),
    }
}

fn one(
    args: &[String],
    action: impl FnOnce(&str) -> Result<Option<String>, OpsFailure>,
) -> Result<Option<String>, OpsFailure> {
    if args.len() != 2 {
        return Err(OpsFailure::error("ERROR: invalid config-ops arguments"));
    }
    action(&args[1])
}

fn config_lock_path(path: &str) -> Result<String, OpsFailure> {
    let text = read_protected(path)?;
    let Yaml::Map(config) = parse_yaml_text(&text).map_err(|error| {
        OpsFailure::error(format!("ERROR: cannot securely read {path}: {error}"))
    })?
    else {
        return Err(OpsFailure::error(format!(
            "ERROR: cannot securely read {path}: config must contain a YAML mapping"
        )));
    };
    match config.get("lock_file").and_then(Yaml::as_str) {
        Some(lock) if Path::new(lock).is_absolute() => Ok(lock.to_owned()),
        _ => Err(OpsFailure::error(
            "ERROR: config lock_file must be an absolute path",
        )),
    }
}

fn read_protected(path: &str) -> Result<String, OpsFailure> {
    let absolute = validate_control_file(path).map_err(|error| {
        OpsFailure::error(format!(
            "ERROR: cannot securely read {path}: {}",
            error.message
        ))
    })?;
    fs::read_to_string(absolute)
        .map_err(|error| OpsFailure::error(format!("ERROR: cannot securely read {path}: {error}")))
}

fn validate_lock_path(lock_file: &str) -> Result<(), OpsFailure> {
    let path = Path::new(lock_file);
    let Some(parent) = path.parent().and_then(Path::to_str) else {
        return Err(OpsFailure::error(format!(
            "ERROR: lock parent is not a real directory: {lock_file}"
        )));
    };
    let parent_meta = statat(CWD, parent, AtFlags::SYMLINK_NOFOLLOW).map_err(|_| {
        OpsFailure::error(format!(
            "ERROR: lock parent is not a real directory: {parent}"
        ))
    })?;
    let parent_mode = mode_of(&parent_meta);
    if !is_dir(parent_mode) || parent_meta.st_uid != geteuid().as_raw() || parent_mode & 0o022 != 0
    {
        return Err(OpsFailure::error(format!(
            "ERROR: lock parent is not private and root-controlled: {parent}"
        )));
    }
    match statat(CWD, lock_file, AtFlags::SYMLINK_NOFOLLOW) {
        Err(Errno::NOENT) => Ok(()),
        Err(error) => Err(OpsFailure::error(format!(
            "ERROR: lock file is not a root-owned regular file: {lock_file}: {error}"
        ))),
        Ok(metadata) => {
            if !is_reg(mode_of(&metadata)) || metadata.st_uid != geteuid().as_raw() {
                Err(OpsFailure::error(format!(
                    "ERROR: lock file is not a root-owned regular file: {lock_file}"
                )))
            } else {
                Ok(())
            }
        }
    }
}

fn preflight_bundle(source_root: &Path) -> Result<(), OpsFailure> {
    let root = source_root.join("configs");
    let password_path = root.join("restic-password");
    let rclone_path = root.join("rclone.conf");
    let config_path = root.join("config.yaml");
    for path in [&password_path, &rclone_path, &config_path] {
        require_regular(path, "restored config member")?;
    }
    let password = fs::read(&password_path).map_err(|error| {
        OpsFailure::error(format!(
            "ERROR: cannot read {}: {error}",
            password_path.display()
        ))
    })?;
    if password.iter().all(u8::is_ascii_whitespace) || password.contains(&0) {
        return Err(OpsFailure::error(
            "ERROR: restic password must be non-empty and contain no NUL bytes",
        ));
    }
    let config_text = fs::read_to_string(&config_path).map_err(|error| {
        OpsFailure::error(format!(
            "ERROR: cannot read {}: {error}",
            config_path.display()
        ))
    })?;
    let Yaml::Map(config) = parse_yaml_text(&config_text)
        .map_err(|error| OpsFailure::error(format!("ERROR: {}: {error}", config_path.display())))?
    else {
        return Err(OpsFailure::error(format!(
            "ERROR: {} must contain a YAML mapping",
            config_path.display()
        )));
    };
    let display = config_path.display().to_string();
    let validated = validate_global_config(&display, &config)
        .map_err(|error| OpsFailure::error(format!("ERROR: {}", error.message)))?;
    if let Some(warning) = validated.deprecation_warning {
        eprintln!("{warning}");
    }
    let config = validated.values;
    require_exact(
        &config,
        "password_file",
        "/etc/homelab-backup/restic-password",
        "ERROR: restored config password_file must name the bundled restic password",
    )?;
    require_exact(
        &config,
        "rclone_config",
        "/etc/homelab-backup/rclone/rclone.conf",
        "ERROR: restored config rclone_config must name the bundled rclone config",
    )?;
    let sections = parse_rclone(&rclone_path)?;
    let repository = config
        .get("repository")
        .and_then(Yaml::as_str)
        .unwrap_or("");
    if let Some(rest) = repository.strip_prefix("rclone:") {
        let mut parts = rest.splitn(2, ':');
        let remote = parts.next().unwrap_or("");
        let path = parts.next().unwrap_or("");
        if remote.is_empty() || path.is_empty() || !sections.contains(remote) {
            return Err(OpsFailure::error(
                "ERROR: configured repository references a missing rclone remote",
            ));
        }
    }
    Ok(())
}

fn require_exact(
    config: &BTreeMap<String, Yaml>,
    key: &str,
    expected: &str,
    message: &str,
) -> Result<(), OpsFailure> {
    if config.get(key).and_then(Yaml::as_str) == Some(expected) {
        Ok(())
    } else {
        Err(OpsFailure::error(message))
    }
}

fn require_regular(path: &Path, label: &str) -> Result<(), OpsFailure> {
    let display = path.display().to_string();
    let metadata = statat(CWD, display.as_str(), AtFlags::SYMLINK_NOFOLLOW).map_err(|error| {
        OpsFailure::error(format!(
            "ERROR: {label} is not a regular file: {display}: {error}"
        ))
    })?;
    if is_reg(mode_of(&metadata)) {
        Ok(())
    } else {
        Err(OpsFailure::error(format!(
            "ERROR: {label} is not a regular file: {display}"
        )))
    }
}

fn parse_rclone(path: &Path) -> Result<BTreeSet<String>, OpsFailure> {
    let text = fs::read_to_string(path)
        .map_err(|error| OpsFailure::error(format!("ERROR: invalid rclone config: {error}")))?;
    let mut sections = BTreeSet::new();
    let mut current: Option<String> = None;
    let mut saw_type = BTreeSet::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') && line.len() > 2 {
            let name = line[1..line.len() - 1].to_owned();
            if !sections.insert(name.clone()) {
                return Err(OpsFailure::error(format!(
                    "ERROR: invalid rclone config: duplicate section {name}"
                )));
            }
            current = Some(name);
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            return Err(OpsFailure::error(format!(
                "ERROR: invalid rclone config: {line}"
            )));
        };
        let Some(section) = current.as_deref() else {
            return Err(OpsFailure::error(
                "ERROR: invalid rclone config: option outside a section",
            ));
        };
        if key.trim() == "type" && !value.trim().is_empty() {
            saw_type.insert(section.to_owned());
        }
    }
    if sections.is_empty() {
        return Err(OpsFailure::error(
            "ERROR: rclone config must contain at least one remote",
        ));
    }
    for section in &sections {
        if !saw_type.contains(section) {
            return Err(OpsFailure::error(format!(
                "ERROR: rclone remote '{section}' is missing a type"
            )));
        }
    }
    Ok(sections)
}

fn publish_archive_args(args: &[String]) -> Result<Option<String>, OpsFailure> {
    let mut fail_after_publish = false;
    let mut positional = Vec::new();
    for arg in args.iter().skip(1) {
        if arg == "--fail-after-publish" {
            fail_after_publish = true;
        } else {
            positional.push(arg.as_str());
        }
    }
    if positional.len() != 3 {
        return Err(OpsFailure::error("ERROR: invalid config-ops arguments"));
    }
    publish_archive(
        Path::new(positional[0]),
        Path::new(positional[1]),
        positional[2],
        fail_after_publish,
    )?;
    Ok(None)
}

fn publish_archive(
    source: &Path,
    archive: &Path,
    mode: &str,
    fail_after_publish: bool,
) -> Result<(), OpsFailure> {
    if mode != "create" && mode != "replace" {
        return Err(OpsFailure::error(format!(
            "ERROR: invalid archive publication mode: {mode}"
        )));
    }
    let Some(directory) = archive.parent().and_then(Path::to_str) else {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not a regular file: {}",
            archive.display()
        )));
    };
    let Some(name) = archive.file_name().and_then(|value| value.to_str()) else {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not a regular file: {}",
            archive.display()
        )));
    };
    let directory_fd = openat(
        CWD,
        directory,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| {
        OpsFailure::error(format!(
            "ERROR: cannot open archive directory {directory}: {error}"
        ))
    })?;
    let token = random_token();
    let temporary = format!(".{name}.next.{token}");
    let rollback = format!(".{name}.rollback.{token}");
    let result = publish_archive_in(
        source,
        archive,
        mode,
        fail_after_publish,
        &directory_fd,
        name,
        &temporary,
        &rollback,
    );
    for candidate in [&temporary, &rollback] {
        let _ = unlinkat(&directory_fd, candidate.as_str(), AtFlags::empty());
    }
    result
}

#[allow(clippy::too_many_arguments)]
fn publish_archive_in(
    source: &Path,
    archive: &Path,
    mode: &str,
    fail_after_publish: bool,
    directory_fd: &OwnedFd,
    name: &str,
    temporary: &str,
    rollback: &str,
) -> Result<(), OpsFailure> {
    if mode == "replace" {
        existing_archive(directory_fd, name, archive)?;
    } else {
        match stat_at(directory_fd, name) {
            Ok(_) => {
                return Err(OpsFailure::error(format!(
                    "ERROR: output already exists: {}",
                    archive.display()
                )));
            }
            Err(Errno::NOENT) => {}
            Err(error) => {
                return Err(OpsFailure::error(format!(
                    "ERROR: output already exists: {}: {error}",
                    archive.display()
                )));
            }
        }
    }
    copy_into_temporary(source, directory_fd, temporary)?;
    if mode == "replace" {
        linkat(directory_fd, name, directory_fd, rollback, AtFlags::empty())
            .map_err(|error| OpsFailure::error(error.to_string()))?;
        renameat(directory_fd, temporary, directory_fd, name)
            .map_err(|error| OpsFailure::error(error.to_string()))?;
    } else {
        linkat(
            directory_fd,
            temporary,
            directory_fd,
            name,
            AtFlags::empty(),
        )
        .map_err(|error| OpsFailure::error(error.to_string()))?;
        unlinkat(directory_fd, temporary, AtFlags::empty())
            .map_err(|error| OpsFailure::error(error.to_string()))?;
    }
    if let Err(publication_error) = sync_publication(directory_fd, fail_after_publish) {
        if let Err(rollback_error) = rollback_publication(directory_fd, mode, name, rollback) {
            eprintln!("ERROR: archive publication state is uncertain: {rollback_error}");
            return Err(OpsFailure {
                message: String::new(),
                code: 75,
            });
        }
        return Err(publication_error);
    }
    Ok(())
}

fn existing_archive(directory_fd: &OwnedFd, name: &str, archive: &Path) -> Result<(), OpsFailure> {
    let fd = openat(
        directory_fd,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| {
        OpsFailure::error(format!(
            "ERROR: encrypted archive is not a regular file: {}",
            archive.display()
        ))
    })?;
    let metadata = fstat(&fd).map_err(|error| OpsFailure::error(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not a regular file: {}",
            archive.display()
        )));
    }
    if metadata.st_uid != geteuid().as_raw() {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not owned by the output user: {}",
            archive.display()
        )));
    }
    Ok(())
}

fn copy_into_temporary(
    source: &Path,
    directory_fd: &OwnedFd,
    temporary: &str,
) -> Result<(), OpsFailure> {
    let mut input = File::open(source).map_err(|error| OpsFailure::error(error.to_string()))?;
    let fd = openat(
        directory_fd,
        temporary,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    let mut output = File::from(fd);
    io::copy(&mut input, &mut output).map_err(|error| OpsFailure::error(error.to_string()))?;
    output
        .flush()
        .map_err(|error| OpsFailure::error(error.to_string()))?;
    fsync_fd(output.as_fd())
}

fn sync_publication(directory_fd: &OwnedFd, fail_after_publish: bool) -> Result<(), OpsFailure> {
    if fail_after_publish {
        return Err(OpsFailure::error("injected late publication failure"));
    }
    fsync_fd(directory_fd.as_fd())
}

fn rollback_publication(
    directory_fd: &OwnedFd,
    mode: &str,
    name: &str,
    rollback: &str,
) -> Result<(), OpsFailure> {
    if mode == "replace" {
        renameat(directory_fd, rollback, directory_fd, name)
            .map_err(|error| OpsFailure::error(error.to_string()))?;
    } else {
        unlinkat(directory_fd, name, AtFlags::empty())
            .map_err(|error| OpsFailure::error(error.to_string()))?;
    }
    fsync_fd(directory_fd.as_fd())
}

fn fsync_fd(fd: impl AsFd) -> Result<(), OpsFailure> {
    if let Some(0) = FSYNC_BUDGET.with(Cell::get) {
        return Err(OpsFailure::error("sync failed"));
    }
    if let Some(remaining) = FSYNC_BUDGET.with(Cell::get) {
        FSYNC_BUDGET.with(|budget| budget.set(Some(remaining.saturating_sub(1))));
    }
    fsync(fd).map_err(|error| OpsFailure::error(error.to_string()))
}

fn stat_at(directory_fd: &OwnedFd, name: &str) -> Result<rustix::fs::Stat, Errno> {
    statat(directory_fd, name, AtFlags::SYMLINK_NOFOLLOW)
}

fn publish_bundle_args(args: &[String]) -> Result<Option<String>, OpsFailure> {
    let mut owner_uid = 0u32;
    let mut owner_gid = 0u32;
    let mut fail_after = false;
    let mut fail_after_commit = false;
    let mut positional = Vec::new();
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--owner-uid" => {
                owner_uid = number_arg(args, &mut index, "--owner-uid")?;
            }
            "--owner-gid" => {
                owner_gid = number_arg(args, &mut index, "--owner-gid")?;
            }
            "--fail-after" => fail_after = true,
            "--fail-after-commit" => fail_after_commit = true,
            other if other.starts_with('-') => {
                return Err(OpsFailure::error(format!(
                    "ERROR: unknown config-ops argument: {other}"
                )));
            }
            _ => positional.push(args[index].clone()),
        }
        index += 1;
    }
    if positional.len() != 2 {
        return Err(OpsFailure::error("ERROR: invalid config-ops arguments"));
    }
    publish_bundle(
        Path::new(&positional[0]),
        Path::new(&positional[1]),
        owner_uid,
        owner_gid,
        fail_after,
        fail_after_commit,
    )?;
    Ok(None)
}

fn number_arg(args: &[String], index: &mut usize, flag: &str) -> Result<u32, OpsFailure> {
    *index += 1;
    let Some(value) = args.get(*index) else {
        return Err(OpsFailure::error(format!("ERROR: {flag} requires a value")));
    };
    value
        .parse()
        .map_err(|_| OpsFailure::error(format!("ERROR: {flag} requires an integer")))
}

fn publish_bundle(
    source_root: &Path,
    target_root: &Path,
    owner_uid: u32,
    owner_gid: u32,
    fail_after: bool,
    fail_after_commit: bool,
) -> Result<(), OpsFailure> {
    let Some(parent) = target_root.parent() else {
        return Err(OpsFailure::error(format!(
            "ERROR: config control path is not a real directory: {}",
            target_root.display()
        )));
    };
    validate_bundle_directory(parent, owner_uid, owner_gid)?;
    let target_name = target_root
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            OpsFailure::error(format!(
                "ERROR: config control path is not a real directory: {}",
                target_root.display()
            ))
        })?;
    let next_prefix = format!(".{target_name}.restore.next.");
    let retired_prefix = format!(".{target_name}.restore.retired.");
    for entry in fs::read_dir(parent).map_err(|error| OpsFailure::error(error.to_string()))? {
        let entry = entry.map_err(|error| OpsFailure::error(error.to_string()))?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.starts_with(&next_prefix) && !name.starts_with(&retired_prefix) {
            continue;
        }
        let stale = entry.path();
        let metadata = lstat(&stale)?;
        if !is_dir(mode_of(&metadata)) || metadata.st_uid != owner_uid {
            return Err(OpsFailure::error(format!(
                "ERROR: unsafe stale config generation: {}",
                stale.display()
            )));
        }
        fs::remove_dir_all(&stale).map_err(|error| OpsFailure::error(error.to_string()))?;
    }
    fsync_path(parent)?;
    let generation = parent.join(format!("{next_prefix}{}", random_token()));
    fs::create_dir(&generation).map_err(|error| OpsFailure::error(error.to_string()))?;
    let mut generation = generation;
    let mut committed = false;
    let result = (|| {
        for (source_name, relative) in CONFIG_MEMBERS {
            let source = source_root.join(source_name);
            let staged = generation.join(relative);
            if let Some(parent) = staged.parent() {
                fs::create_dir_all(parent).map_err(|error| OpsFailure::error(error.to_string()))?;
            }
            fs::copy(&source, &staged).map_err(|error| OpsFailure::error(error.to_string()))?;
            chown_chmod(&staged, owner_uid, owner_gid, 0o600)?;
            fsync_path(&staged)?;
        }
        for directory in [generation.join("rclone"), generation.clone()] {
            chown_chmod(&directory, owner_uid, owner_gid, 0o700)?;
            fsync_path(&directory)?;
        }
        if fail_after {
            return Err(OpsFailure::error(
                "injected configuration publication failure",
            ));
        }
        if path_exists_nofollow(target_root)? {
            validate_bundle_directory(target_root, owner_uid, owner_gid)?;
            validate_bundle_directory(&target_root.join("rclone"), owner_uid, owner_gid)?;
            for (_, relative) in CONFIG_MEMBERS {
                let target = target_root.join(relative);
                let metadata = lstat(&target)?;
                if !is_reg(mode_of(&metadata)) {
                    return Err(OpsFailure::error(format!(
                        "refusing non-regular config target: {}",
                        target.display()
                    )));
                }
            }
            let retired = generation.with_file_name(format!(
                "{retired_prefix}{}",
                generation
                    .file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("")
                    .trim_start_matches(&next_prefix)
            ));
            fs::rename(&generation, &retired)
                .map_err(|error| OpsFailure::error(error.to_string()))?;
            generation = retired;
            fsync_path(parent)?;
            renameat_with(
                CWD,
                generation.as_os_str(),
                CWD,
                target_root.as_os_str(),
                RenameFlags::EXCHANGE,
            )
            .map_err(|error| OpsFailure::error(error.to_string()))?;
        } else {
            fs::rename(&generation, target_root)
                .map_err(|error| OpsFailure::error(error.to_string()))?;
        }
        committed = true;
        if fail_after_commit {
            eprintln!(
                "WARNING: config generation was published, but its parent directory could not be synced: injected post-commit directory sync failure"
            );
        } else if let Err(error) = fsync_path(parent) {
            eprintln!(
                "WARNING: config generation was published, but its parent directory could not be synced: {}",
                error.message
            );
        }
        Ok(())
    })();
    if !committed && path_exists_nofollow(&generation).unwrap_or(false) {
        let _ = fs::remove_dir_all(&generation);
        let _ = fsync_path(parent);
    } else if committed && path_exists_nofollow(&generation).unwrap_or(false) {
        if let Err(error) = fs::remove_dir_all(&generation) {
            eprintln!(
                "WARNING: could not remove retired plaintext config generation {}: {error}",
                generation.display()
            );
        } else if let Err(error) = fsync_path(parent) {
            eprintln!(
                "WARNING: could not remove retired plaintext config generation {}: {}",
                generation.display(),
                error.message
            );
        }
    }
    result
}

fn validate_bundle_directory(
    path: &Path,
    owner_uid: u32,
    owner_gid: u32,
) -> Result<(), OpsFailure> {
    let metadata = lstat(path)?;
    let mode = mode_of(&metadata);
    if !is_dir(mode) {
        return Err(OpsFailure::error(format!(
            "ERROR: config control path is not a real directory: {}",
            path.display()
        )));
    }
    if metadata.st_uid != owner_uid || metadata.st_gid != owner_gid {
        return Err(OpsFailure::error(format!(
            "ERROR: config control directory has the wrong owner: {}",
            path.display()
        )));
    }
    if mode & 0o022 != 0 {
        return Err(OpsFailure::error(format!(
            "ERROR: config control directory is writable by other users: {}",
            path.display()
        )));
    }
    Ok(())
}

fn lstat(path: &Path) -> Result<rustix::fs::Stat, OpsFailure> {
    let display = path.display().to_string();
    statat(CWD, display.as_str(), AtFlags::SYMLINK_NOFOLLOW)
        .map_err(|error| OpsFailure::error(format!("ERROR: cannot inspect {display}: {error}")))
}

fn path_exists_nofollow(path: &Path) -> Result<bool, OpsFailure> {
    let display = path.display().to_string();
    match statat(CWD, display.as_str(), AtFlags::SYMLINK_NOFOLLOW) {
        Ok(_) => Ok(true),
        Err(Errno::NOENT) => Ok(false),
        Err(error) => Err(OpsFailure::error(format!(
            "ERROR: cannot inspect {display}: {error}"
        ))),
    }
}

fn fsync_path(path: &Path) -> Result<(), OpsFailure> {
    let display = path.display().to_string();
    let fd = openat(
        CWD,
        display.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    fsync_fd(fd)
}

fn chown_chmod(path: &Path, uid: u32, gid: u32, mode: u32) -> Result<(), OpsFailure> {
    let display = path.display().to_string();
    let fd = openat(
        CWD,
        display.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    rustix::fs::fchown(
        &fd,
        Some(rustix::fs::Uid::from_raw(uid)),
        Some(rustix::fs::Gid::from_raw(gid)),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    rustix::fs::fchmod(&fd, Mode::from_bits_truncate(mode))
        .map_err(|error| OpsFailure::error(error.to_string()))?;
    Ok(())
}

fn copy_regular_args(args: &[String]) -> Result<Option<String>, OpsFailure> {
    if args.len() != 4 {
        return Err(OpsFailure::error("ERROR: invalid config-ops arguments"));
    }
    let maximum: u64 = args[3]
        .parse()
        .map_err(|_| OpsFailure::error("ERROR: invalid size limit"))?;
    copy_regular(Path::new(&args[1]), Path::new(&args[2]), maximum)?;
    Ok(None)
}

fn copy_regular(source: &Path, destination: &Path, maximum: u64) -> Result<(), OpsFailure> {
    let source_fd = open_regular(source, maximum, "input")?;
    let mut input = File::from(source_fd);
    let dest_fd = openat(
        CWD,
        destination.display().to_string(),
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    let mut output = File::from(dest_fd);
    copy_limited(
        &mut input,
        &mut output,
        maximum,
        "ERROR: input exceeds its size limit",
    )?;
    output
        .flush()
        .map_err(|error| OpsFailure::error(error.to_string()))?;
    fsync_fd(output.as_fd())
}

fn copy_stdout_args(args: &[String]) -> Result<Option<String>, OpsFailure> {
    if args.len() != 3 {
        return Err(OpsFailure::error("ERROR: invalid config-ops arguments"));
    }
    let maximum: u64 = args[2]
        .parse()
        .map_err(|_| OpsFailure::error("ERROR: invalid size limit"))?;
    let source = Path::new(&args[1]);
    let limit_message = format!(
        "ERROR: input file exceeds the {} MiB limit",
        maximum / (1024 * 1024)
    );
    let source_fd = open_regular_message(
        source,
        maximum,
        &format!(
            "ERROR: encrypted archive is not a regular file: {}",
            source.display()
        ),
        &limit_message,
    )?;
    let mut input = File::from(source_fd);
    let mut output = io::stdout().lock();
    copy_limited(
        &mut input,
        &mut output,
        maximum,
        &format!(
            "ERROR: input file exceeds the {} MiB limit",
            maximum / (1024 * 1024)
        ),
    )?;
    Ok(None)
}

fn open_regular(source: &Path, maximum: u64, label: &str) -> Result<OwnedFd, OpsFailure> {
    open_regular_message(
        source,
        maximum,
        &format!("ERROR: {label} is not a regular file: {}", source.display()),
        "ERROR: input exceeds its size limit",
    )
}

fn open_regular_message(
    source: &Path,
    maximum: u64,
    message: &str,
    limit_message: &str,
) -> Result<OwnedFd, OpsFailure> {
    let fd = openat(
        CWD,
        source.display().to_string(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|_| OpsFailure::error(message))?;
    let metadata = fstat(&fd).map_err(|error| OpsFailure::error(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(OpsFailure::error(message));
    }
    if metadata.st_size > i64::try_from(maximum).unwrap_or(i64::MAX) {
        return Err(OpsFailure::error(limit_message));
    }
    Ok(fd)
}

fn copy_limited(
    input: &mut impl Read,
    output: &mut impl Write,
    maximum: u64,
    message: &str,
) -> Result<(), OpsFailure> {
    let mut copied = 0u64;
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|error| OpsFailure::error(error.to_string()))?;
        if count == 0 {
            return Ok(());
        }
        copied += count as u64;
        if copied > maximum {
            return Err(OpsFailure::error(message));
        }
        output
            .write_all(&buffer[..count])
            .map_err(|error| OpsFailure::error(error.to_string()))?;
    }
}

fn remove_owned(archive: &Path) -> Result<(), OpsFailure> {
    let Some(parent) = archive.parent() else {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not an owned regular file: {}",
            archive.display()
        )));
    };
    let Some(name) = archive.file_name().and_then(|value| value.to_str()) else {
        return Err(OpsFailure::error(format!(
            "ERROR: encrypted archive is not an owned regular file: {}",
            archive.display()
        )));
    };
    let directory_fd = openat(
        CWD,
        parent.display().to_string(),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| OpsFailure::error(error.to_string()))?;
    match stat_at(&directory_fd, name) {
        Err(Errno::NOENT) => return Ok(()),
        Err(error) => return Err(OpsFailure::error(error.to_string())),
        Ok(metadata) => {
            if !is_reg(mode_of(&metadata)) || metadata.st_uid != geteuid().as_raw() {
                return Err(OpsFailure::error(format!(
                    "ERROR: encrypted archive is not an owned regular file: {}",
                    archive.display()
                )));
            }
        }
    }
    unlinkat(&directory_fd, name, AtFlags::empty())
        .map_err(|error| OpsFailure::error(error.to_string()))?;
    fsync_fd(directory_fd.as_fd())
}

fn random_token() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("{nanos:x}-{}", std::process::id())
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use rustix::process::getegid;

    use super::*;

    static TEMP: AtomicU64 = AtomicU64::new(0);

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "backupctl-config-ops-{label}-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    fn write_config(path: &Path, body: &str) {
        fs::write(path, body).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    #[test]
    fn lock_path_requires_an_absolute_value() {
        let root = temp_dir("lock");
        let config = root.join("config.yaml");
        write_config(&config, "lock_file: /run/demo.lock\n");
        assert_eq!(
            config_lock_path(config.to_str().unwrap()).unwrap(),
            "/run/demo.lock"
        );
        write_config(&config, "lock_file: relative.lock\n");
        let error = config_lock_path(config.to_str().unwrap()).unwrap_err();
        assert!(error.message.contains("absolute path"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn lock_path_rejects_symlinked_config() {
        let root = temp_dir("symlink-config");
        let target = root.join("target.yaml");
        write_config(&target, "lock_file: /run/demo.lock\n");
        let config = root.join("config.yaml");
        symlink(&target, &config).unwrap();
        let error = config_lock_path(config.to_str().unwrap()).unwrap_err();
        assert!(error.message.contains("regular file"), "{}", error.message);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn lock_validation_rejects_a_symlink_leaf() {
        let root = temp_dir("symlink-lock");
        fs::write(root.join("target"), "").unwrap();
        let lock = root.join("lock");
        symlink(root.join("target"), &lock).unwrap();
        let error = validate_lock_path(lock.to_str().unwrap()).unwrap_err();
        assert!(error.message.contains("regular file"), "{}", error.message);
        let _ = fs::remove_dir_all(root);
    }

    fn write_bundle(root: &Path, version: i32, password: &str, rclone: &str) {
        let configs = root.join("configs");
        fs::create_dir_all(&configs).unwrap();
        fs::write(configs.join("restic-password"), password).unwrap();
        fs::write(configs.join("rclone.conf"), rclone).unwrap();
        fs::write(
            configs.join("config.yaml"),
            format!(
                "version: {version}\nhost_id: server-a\nservices_root: /srv/stacks\nrepository: rclone:onedrive:Backups/restic/server-a\npassword_file: /etc/homelab-backup/restic-password\nrclone_config: /etc/homelab-backup/rclone/rclone.conf\nstaging_root: /var/lib/homelab-backup/staging\nrestore_root: /var/lib/homelab-backup/restores\nstate_root: /var/lib/homelab-backup/state\ncache_root: /var/cache/homelab-backup/restic\nlock_file: /run/homelab-backup/backupctl.lock\nvolume_helper_image: homelab/volume-rsync:release.test\n"
            ),
        )
        .unwrap();
    }

    #[test]
    fn preflight_rejects_invalid_bundle_content() {
        let root = temp_dir("preflight");
        let valid = root.join("valid");
        write_bundle(&valid, 1, "restic-secret", "[onedrive]\ntype = onedrive\n");
        preflight_bundle(&valid).unwrap();
        let invalid = root.join("invalid-schema");
        write_bundle(
            &invalid,
            2,
            "restic-secret",
            "[onedrive]\ntype = onedrive\n",
        );
        let error = preflight_bundle(&invalid).unwrap_err();
        assert!(
            error.message.contains("version must be 1"),
            "{}",
            error.message
        );
        let blank = root.join("invalid-password");
        write_bundle(&blank, 1, " \n", "[onedrive]\ntype = onedrive\n");
        let error = preflight_bundle(&blank).unwrap_err();
        assert!(
            error.message.contains("restic password"),
            "{}",
            error.message
        );
        let remote = root.join("invalid-rclone");
        write_bundle(&remote, 1, "restic-secret", "[other]\ntype = local\n");
        let error = preflight_bundle(&remote).unwrap_err();
        assert!(error.message.contains("rclone remote"), "{}", error.message);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn archive_publication_rolls_back_after_late_failure() {
        let root = temp_dir("archive");
        let source = root.join("new.age");
        let archive = root.join("archive.age");
        fs::write(&source, b"new-ciphertext").unwrap();
        fs::write(&archive, b"old-ciphertext").unwrap();
        let error = publish_archive(&source, &archive, "replace", true).unwrap_err();
        assert!(error.message.contains("late publication failure"));
        assert_eq!(fs::read(&archive).unwrap(), b"old-ciphertext");
        let leftovers: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .filter(|name| name.to_string_lossy().starts_with(".archive.age."))
            .collect();
        assert!(leftovers.is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn archive_publication_uses_exit_75_when_rollback_sync_fails() {
        let root = temp_dir("archive-75");
        let source = root.join("new.age");
        let archive = root.join("archive.age");
        fs::write(&source, b"new-ciphertext").unwrap();
        fs::write(&archive, b"old-ciphertext").unwrap();
        FSYNC_BUDGET.with(|budget| budget.set(Some(1)));
        let error = publish_archive(&source, &archive, "replace", true).unwrap_err();
        FSYNC_BUDGET.with(|budget| budget.set(None));
        assert_eq!(error.code, 75, "{}", error.message);
        assert_eq!(fs::read(&archive).unwrap(), b"old-ciphertext");
        let _ = fs::remove_dir_all(root);
    }

    fn live_bundle(target: &Path) {
        fs::create_dir_all(target.join("rclone")).unwrap();
        fs::set_permissions(target, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(target.join("rclone"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(target.join("restic-password"), "old-password").unwrap();
        fs::write(target.join("rclone/rclone.conf"), "old-rclone").unwrap();
        fs::write(target.join("config.yaml"), "old-config").unwrap();
    }

    #[test]
    fn bundle_publication_rolls_back_partial_failure() {
        let root = temp_dir("bundle-rollback");
        let source = root.join("source");
        let target = root.join("target");
        write_bundle(&source, 1, "restic-secret", "[onedrive]\ntype = onedrive\n");
        live_bundle(&target);
        let uid = geteuid().as_raw();
        let gid = getegid().as_raw();
        let error = publish_bundle(&source, &target, uid, gid, true, false).unwrap_err();
        assert!(error.message.contains("injected"), "{}", error.message);
        assert_eq!(
            fs::read_to_string(target.join("restic-password")).unwrap(),
            "old-password"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn bundle_publication_switches_complete_generation() {
        let root = temp_dir("bundle-switch");
        let source = root.join("source");
        let target = root.join("target");
        write_bundle(&source, 1, "restic-secret", "[onedrive]\ntype = onedrive\n");
        live_bundle(&target);
        let stale = root.join(".target.restore.next.crashed");
        fs::create_dir(&stale).unwrap();
        let uid = geteuid().as_raw();
        let gid = getegid().as_raw();
        publish_bundle(&source, &target, uid, gid, false, false).unwrap();
        assert_eq!(
            fs::read_to_string(target.join("restic-password")).unwrap(),
            "restic-secret"
        );
        assert!(!stale.exists());
        let retired: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .filter(|name| {
                name.to_string_lossy()
                    .starts_with(".target.restore.retired.")
            })
            .collect();
        assert!(retired.is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn bundle_publication_rejects_symlink_target_root() {
        let root = temp_dir("bundle-symlink");
        let source = root.join("source");
        let referent = root.join("referent");
        let target = root.join("target");
        write_bundle(&source, 1, "restic-secret", "[onedrive]\ntype = onedrive\n");
        fs::create_dir(&referent).unwrap();
        symlink(&referent, &target).unwrap();
        let uid = geteuid().as_raw();
        let gid = getegid().as_raw();
        let error = publish_bundle(&source, &target, uid, gid, false, false).unwrap_err();
        assert!(
            error.message.contains("real directory"),
            "{}",
            error.message
        );
        assert_eq!(fs::read_dir(&referent).unwrap().count(), 0);
        let _ = fs::remove_dir_all(root);
    }
}
