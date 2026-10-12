//! Encrypted recovery archives previously handled by the config shell scripts.

use std::fs::{self, File};
use std::io::{self, IsTerminal, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use rustix::fs::{FlockOperation, flock};
use rustix::process::geteuid;

use crate::host_install::require_root;

const MEMBERS: [&str; 3] = [
    "configs/config.yaml",
    "configs/rclone.conf",
    "configs/restic-password",
];

pub fn run_restore(root: &Path, yes: bool, archive: Option<PathBuf>) -> Result<(), String> {
    require_root("restore-configs")?;
    require_tool("age")?;
    require_tool("unzip")?;
    let archive = archive.unwrap_or_else(|| root.join("configs/homelab-backup-configs.zip.age"));
    regular_file(&archive)?;
    if !yes && !io::stdin().is_terminal() {
        return Err("non-interactive restore requires --yes".to_owned());
    }
    if !yes {
        let answer =
            prompt("Decrypt this archive and overwrite /etc/homelab-backup configs? [y/N]: ")?;
        if !matches!(answer.trim(), "y" | "Y") {
            println!("Cancelled.");
            return Ok(());
        }
    }
    require_tmpfs(Path::new("/run"))?;
    let work = tempfile("/run", "homelab-backup-restore.")?;
    let result = restore_in(&archive, &work);
    let _ = fs::remove_dir_all(&work);
    result?;
    println!("Restored encrypted recovery configuration into /etc/homelab-backup.");
    Ok(())
}

fn restore_in(archive: &Path, work: &Path) -> Result<(), String> {
    config_ops(&[
        "copy-regular".to_owned(),
        archive.display().to_string(),
        work.join("configs.zip.age").display().to_string(),
        (128 * 1024 * 1024).to_string(),
    ])?;
    eprintln!("Paste the complete SSH private key, then press Ctrl-D.");
    eprintln!("The key is passed directly to age and is not stored.");
    let status = age_decrypt(&work.join("configs.zip.age"), &work.join("configs.zip"))?;
    if status != 0 {
        return Err("decryption failed; no system config was changed".to_owned());
    }
    extract_archive(&work.join("configs.zip"), &work.join("extracted"))?;
    config_ops(&[
        "preflight-bundle".to_owned(),
        work.join("extracted").display().to_string(),
    ])?;
    let new_lock = lock_path(&work.join("extracted/configs/config.yaml"))?;
    let live = Path::new("/etc/homelab-backup/config.yaml");
    let mut current_lock = PathBuf::from("/run/homelab-backup/backupctl.lock");
    if live.is_file()
        && fs::symlink_metadata(live)
            .map(|m| m.is_file())
            .unwrap_or(false)
    {
        match lock_path(live) {
            Ok(path) => current_lock = path,
            Err(_) => {
                eprintln!(
                    "WARNING: live config is invalid; using the default global lock for recovery."
                );
            }
        }
    }
    let mut held = Vec::new();
    for lock in unique_sorted([&current_lock, &new_lock]) {
        held.push(acquire_lock(&lock)?);
    }
    if live.is_file()
        && lock_path(live)
            .ok()
            .is_some_and(|confirmed| confirmed != current_lock)
    {
        return Err("live config changed while acquiring its global operation lock".to_owned());
    }
    config_ops(&[
        "publish-bundle".to_owned(),
        work.join("extracted").display().to_string(),
        "/etc/homelab-backup".to_owned(),
        "--owner-uid".to_owned(),
        "0".to_owned(),
        "--owner-gid".to_owned(),
        "0".to_owned(),
    ])?;
    drop(held);
    Ok(())
}

pub fn run_backup(root: &Path, rotate: Option<PathBuf>) -> Result<(), String> {
    require_root("backup-configs")?;
    for tool in ["age", "zip", "unzip"] {
        require_tool(tool)?;
    }
    require_tmpfs(Path::new("/run"))?;
    let work = tempfile("/run", "homelab-backup-configs.")?;
    let result = if let Some(archive) = rotate {
        rotate_key(root, &work, &archive)
    } else {
        backup_interactive(root, &work)
    };
    let _ = fs::remove_dir_all(&work);
    result
}

fn backup_interactive(root: &Path, work: &Path) -> Result<(), String> {
    let _lock = consistent_lock()?;
    for source in [
        "/etc/homelab-backup/restic-password",
        "/etc/homelab-backup/rclone/rclone.conf",
        "/etc/homelab-backup/config.yaml",
    ] {
        regular_file(Path::new(source))?;
    }
    if !io::stdin().is_terminal() {
        return Err(
            "an interactive terminal is required to choose the output and paste a public key"
                .to_owned(),
        );
    }
    println!("How should the encrypted recovery archive be preserved?");
    println!("  1) Replace configs/homelab-backup-configs.zip.age and commit it");
    println!("  2) Create a timestamped .zip.age file");
    println!("  q) Cancel");
    let choice = prompt("Choose [1/2/q]: ")?;
    match choice.trim() {
        "1" => commit_archive(root, work),
        "2" => write_timestamped(root, work),
        "q" | "Q" => {
            println!("Cancelled.");
            Ok(())
        }
        _ => Err("invalid choice".to_owned()),
    }
}

fn commit_archive(root: &Path, work: &Path) -> Result<(), String> {
    let (user, uid, gid) = invoking_user(root)?;
    require_tool("runuser")?;
    let configs = root.join("configs");
    if fs::symlink_metadata(&configs)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("configs must not be a symlink".to_owned());
    }
    let archive = configs.join("homelab-backup-configs.zip.age");
    if fs::symlink_metadata(&archive)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("the encrypted Git archive must not be a symlink".to_owned());
    }
    git(&user, root, &["rev-parse", "--is-inside-work-tree"])?;
    println!("\nConfigured Git remotes:");
    let remotes = git_output(&user, root, &["remote"])?;
    if remotes.trim().is_empty() {
        println!("  (none; this commit could still be pushed after a remote is added)");
    } else {
        let _ = git(&user, root, &["remote", "-v"]);
    }
    println!("\nThe commit contains an age-encrypted recovery archive.");
    println!("Keep the repository private and retain the matching private key separately.");
    let confirmation = prompt("Type PRIVATE to continue with Git: ")?;
    if confirmation.trim() != "PRIVATE" {
        println!("Git operation cancelled.");
        return Ok(());
    }
    let recipient = read_recipient()?;
    create_plain_archive(work)?;
    encrypt_archive(
        &recipient,
        &work.join("configs.zip"),
        &work.join("configs.zip.age"),
    )?;
    runuser(
        &user,
        &[
            "install",
            "-d",
            "-m",
            "0755",
            &configs.display().to_string(),
        ],
    )?;
    commit_git_archive(
        &user,
        uid,
        gid,
        root,
        work,
        &work.join("configs.zip.age"),
        &archive,
    )?;
    let push = prompt("Push the current branch to its configured remote? [y/N]: ")?;
    if matches!(push.trim(), "y" | "Y") {
        git(&user, root, &["push"])?;
    }
    Ok(())
}

const ARCHIVE_RELATIVE: &str = "configs/homelab-backup-configs.zip.age";

fn commit_git_archive(
    user: &str,
    uid: u32,
    gid: u32,
    root: &Path,
    work: &Path,
    source: &Path,
    archive: &Path,
) -> Result<(), String> {
    let staged = git_output(user, root, &["diff", "--cached", "--name-only", "-z", "--"])?;
    let mut unrelated = Vec::new();
    for path in staged.split('\0') {
        if path.is_empty() || path == ARCHIVE_RELATIVE {
            continue;
        }
        unrelated.push(path);
    }
    if !unrelated.is_empty() {
        eprintln!(
            "ERROR: refusing to include unrelated staged changes in the config backup commit:"
        );
        for path in unrelated {
            eprintln!("  - {path}");
        }
        return Err("unrelated staged changes".to_owned());
    }
    let prior_index = git_output(user, root, &["ls-files", "--stage", "--", ARCHIVE_RELATIVE])?;
    let prior_index = prior_index.trim_end_matches(['\n', '\r']).to_owned();
    if !prior_index.is_empty() && parse_cacheinfo(&prior_index, ARCHIVE_RELATIVE).is_none() {
        eprintln!("ERROR: encrypted archive has an unsupported conflicted Git index entry");
        return Err("unsupported Git index entry".to_owned());
    }
    let prior_present = archive.exists() || fs::symlink_metadata(archive).is_ok();
    let publication_mode = if prior_present {
        regular_file(archive).map_err(|_| "existing Git archive must be a real file".to_owned())?;
        copy_as_user(
            user,
            uid,
            archive,
            &work.join("git-archive.before"),
            128 * 1024 * 1024,
        )?;
        "replace"
    } else {
        "create"
    };
    if publish_as_user(user, uid, gid, source, archive, publication_mode).is_err() {
        return Err("could not publish the encrypted archive".to_owned());
    }
    let added = git_status(user, root, &["add", "-f", "--", ARCHIVE_RELATIVE])?;
    if added == 0
        && git_status(
            user,
            root,
            &["diff", "--cached", "--quiet", "--", ARCHIVE_RELATIVE],
        )? == 0
    {
        println!("No encrypted configuration changes to commit.");
        if !rollback_git_archive(
            ArchiveActor { user, uid, gid },
            root,
            work,
            archive,
            prior_present,
            &prior_index,
        ) {
            eprintln!("ERROR: failed to restore the prior archive transaction state");
            return Err("failed to restore the prior archive transaction state".to_owned());
        }
        return Ok(());
    }
    let committed = if added == 0 {
        let stamp = timestamp();
        let message = format!("Backup encrypted recovery configs {stamp}");
        git_status(
            user,
            root,
            &["commit", "--only", "-m", &message, "--", ARCHIVE_RELATIVE],
        )?
    } else {
        1
    };
    if committed == 0 {
        return Ok(());
    }
    if !rollback_git_archive(
        ArchiveActor { user, uid, gid },
        root,
        work,
        archive,
        prior_present,
        &prior_index,
    ) {
        eprintln!("ERROR: failed to restore the prior archive transaction state");
    }
    Err("git commit failed".to_owned())
}

struct ArchiveActor<'a> {
    user: &'a str,
    uid: u32,
    gid: u32,
}

fn rollback_git_archive(
    actor: ArchiveActor<'_>,
    root: &Path,
    work: &Path,
    archive: &Path,
    prior_present: bool,
    prior_index: &str,
) -> bool {
    let ArchiveActor { user, uid, gid } = actor;
    let mut failed = false;
    if prior_present {
        if publish_as_user(
            user,
            uid,
            gid,
            &work.join("git-archive.before"),
            archive,
            "replace",
        )
        .is_err()
        {
            failed = true;
        }
    } else if config_ops_as_user(
        user,
        uid,
        &["remove-owned".to_owned(), archive.display().to_string()],
    )
    .is_err()
    {
        failed = true;
    }
    if let Some((mode, hash)) = parse_cacheinfo(prior_index, ARCHIVE_RELATIVE) {
        if git(
            user,
            root,
            &[
                "update-index",
                "--add",
                "--cacheinfo",
                &format!("{mode},{hash},{ARCHIVE_RELATIVE}"),
            ],
        )
        .is_err()
        {
            failed = true;
        }
    } else if !prior_index.is_empty() {
        eprintln!("ERROR: cannot restore unsupported prior Git index entry");
        failed = true;
    } else if git(
        user,
        root,
        &[
            "rm",
            "--cached",
            "-q",
            "--ignore-unmatch",
            "--",
            ARCHIVE_RELATIVE,
        ],
    )
    .is_err()
    {
        failed = true;
    }
    !failed
}

fn parse_cacheinfo(line: &str, relative: &str) -> Option<(String, String)> {
    if line.is_empty() {
        return None;
    }
    let (meta, path) = line.split_once('\t')?;
    if path != relative {
        return None;
    }
    let mut parts = meta.split(' ');
    let mode = parts.next()?;
    let hash = parts.next()?;
    let stage = parts.next()?;
    if parts.next().is_some() || stage != "0" {
        return None;
    }
    let mode_ok = mode.len() == 6 && mode.bytes().all(|byte| (b'0'..=b'7').contains(&byte));
    let hash_ok =
        (40..=64).contains(&hash.len()) && hash.bytes().all(|byte| byte.is_ascii_hexdigit());
    if mode_ok && hash_ok {
        Some((mode.to_owned(), hash.to_owned()))
    } else {
        None
    }
}

fn write_timestamped(root: &Path, work: &Path) -> Result<(), String> {
    let recipient = read_recipient()?;
    create_plain_archive(work)?;
    let archive = root.join(format!("homelab-backup-configs-{}.zip.age", timestamp()));
    if archive.exists() {
        return Err(format!("output already exists: {}", archive.display()));
    }
    encrypt_archive(
        &recipient,
        &work.join("configs.zip"),
        &work.join("configs.zip.age"),
    )?;
    let (user, uid, gid) = match invoking_user(root) {
        Ok(user) => user,
        Err(_) => ("root".to_owned(), 0, 0),
    };
    publish_as_user(
        &user,
        uid,
        gid,
        &work.join("configs.zip.age"),
        &archive,
        "create",
    )?;
    println!("Created encrypted recovery archive: {}", archive.display());
    Ok(())
}

fn rotate_key(root: &Path, work: &Path, archive: &Path) -> Result<(), String> {
    let _ = root;
    let (user, uid, gid) = invoking_user(archive.parent().unwrap_or(archive))?;
    if uid == 0 {
        return Err("key rotation requires a non-root invoking user or archive owner".to_owned());
    }
    require_tool("runuser")?;
    let encrypted = work.join("rotation.zip.age");
    copy_as_user(&user, uid, archive, &encrypted, 128 * 1024 * 1024)?;
    eprintln!("Paste the complete old SSH private key, then press Ctrl-D.");
    if age_decrypt(&encrypted, &work.join("rotation.zip"))? != 0 {
        return Err("decryption failed; the old archive was not changed".to_owned());
    }
    validate_zip(&work.join("rotation.zip"))?;
    let recipient = read_recipient()?;
    encrypt_archive(
        &recipient,
        &work.join("rotation.zip"),
        &work.join("replacement.zip.age"),
    )?;
    publish_as_user(
        &user,
        uid,
        gid,
        &work.join("replacement.zip.age"),
        archive,
        "replace",
    )?;
    println!("Re-encrypted with the new key: {}", archive.display());
    Ok(())
}

fn create_plain_archive(work: &Path) -> Result<(), String> {
    let source = work.join("plain");
    fs::create_dir_all(source.join("configs")).map_err(|error| error.to_string())?;
    for (from, name) in [
        ("/etc/homelab-backup/restic-password", "restic-password"),
        ("/etc/homelab-backup/rclone/rclone.conf", "rclone.conf"),
        ("/etc/homelab-backup/config.yaml", "config.yaml"),
    ] {
        let destination = source.join("configs").join(name);
        fs::copy(from, &destination).map_err(|error| error.to_string())?;
        fs::set_permissions(&destination, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    let archive = work.join("configs.zip");
    let status = Command::new("zip")
        .args(["-X", "-q", "--"])
        .arg(&archive)
        .args(MEMBERS)
        .current_dir(&source)
        .status()
        .map_err(|error| error.to_string())?;
    if !status.success() {
        return Err("zip failed".to_owned());
    }
    validate_zip(&archive)
}

fn validate_zip(archive: &Path) -> Result<(), String> {
    let listing = Command::new("unzip")
        .args(["-Z1", "--"])
        .arg(archive)
        .output()
        .map_err(|error| error.to_string())?;
    if !listing.status.success() {
        return Err("archive member is damaged".to_owned());
    }
    let mut names: Vec<_> = String::from_utf8_lossy(&listing.stdout)
        .lines()
        .map(str::to_owned)
        .collect();
    names.sort();
    let mut expected = MEMBERS.map(str::to_owned);
    expected.sort();
    if names != expected {
        return Err("archive contains unexpected or missing files".to_owned());
    }
    let test = Command::new("unzip")
        .args(["-t", "-qq", "--"])
        .arg(archive)
        .status()
        .map_err(|error| error.to_string())?;
    if test.success() {
        Ok(())
    } else {
        Err("archive member is damaged".to_owned())
    }
}

fn extract_archive(archive: &Path, destination: &Path) -> Result<(), String> {
    validate_zip(archive)?;
    fs::create_dir_all(destination.join("configs")).map_err(|error| error.to_string())?;
    for member in MEMBERS {
        let output = Command::new("unzip")
            .args(["-p", "--"])
            .arg(archive)
            .arg(member)
            .output()
            .map_err(|error| error.to_string())?;
        if !output.status.success() {
            return Err(format!("archive member is damaged: {member}"));
        }
        let target = destination.join(member);
        fs::write(&target, &output.stdout).map_err(|error| error.to_string())?;
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn encrypt_archive(recipient: &str, input: &Path, output: &Path) -> Result<(), String> {
    let mut child = Command::new("age")
        .args(["--encrypt", "-R", "-", "-o"])
        .arg(output)
        .arg(input)
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    child
        .stdin
        .take()
        .ok_or_else(|| "age stdin is unavailable".to_owned())?
        .write_all(format!("{recipient}\n").as_bytes())
        .map_err(|error| error.to_string())?;
    let status = child.wait().map_err(|error| error.to_string())?;
    if !status.success() {
        return Err("age encryption failed".to_owned());
    }
    fs::set_permissions(output, fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn age_decrypt(input: &Path, output: &Path) -> Result<i32, String> {
    let status = Command::new("age")
        .args(["--decrypt", "-i", "-", "-o"])
        .arg(output)
        .arg(input)
        .stdin(Stdio::inherit())
        .status()
        .map_err(|error| error.to_string())?;
    Ok(status.code().unwrap_or(1))
}

fn read_recipient() -> Result<String, String> {
    if !io::stdin().is_terminal() {
        return Err(
            "an interactive terminal is required to paste the new SSH public key".to_owned(),
        );
    }
    eprintln!("Paste the SSH public key that should decrypt this archive.");
    let line = prompt("SSH public key: ")?;
    let key = line.trim();
    if key.starts_with("ssh-ed25519 ") || key.starts_with("ssh-rsa ") {
        Ok(key.to_owned())
    } else {
        Err("only ssh-ed25519 and ssh-rsa public keys are supported".to_owned())
    }
}

fn publish_as_user(
    user: &str,
    uid: u32,
    gid: u32,
    source: &Path,
    archive: &Path,
    mode: &str,
) -> Result<(), String> {
    let publish = tempfile("/run", "homelab-backup-publish.")?;
    let staged = publish.join("archive.zip.age");
    fs::copy(source, &staged).map_err(|error| error.to_string())?;
    fs::set_permissions(&staged, fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    if uid != 0 {
        runuser(
            user,
            &[
                "chown",
                &format!("{uid}:{gid}"),
                &staged.display().to_string(),
            ],
        )?;
        runuser(
            user,
            &[
                "chown",
                &format!("{uid}:{gid}"),
                &publish.display().to_string(),
            ],
        )?;
    }
    let result = config_ops_as_user(
        user,
        uid,
        &[
            "publish-archive".to_owned(),
            staged.display().to_string(),
            archive.display().to_string(),
            mode.to_owned(),
        ],
    );
    let _ = fs::remove_dir_all(&publish);
    result
}

fn copy_as_user(
    user: &str,
    uid: u32,
    input: &Path,
    output: &Path,
    maximum: u64,
) -> Result<(), String> {
    let mut command = if geteuid().as_raw() == uid {
        Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
    } else {
        let mut command = Command::new("runuser");
        command.args(["--user", user, "--"]);
        command.arg(std::env::current_exe().map_err(|error| error.to_string())?);
        command
    };
    let child = command
        .args([
            "config-ops",
            "copy-stdout",
            &input.display().to_string(),
            &maximum.to_string(),
        ])
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let output_bytes = child
        .wait_with_output()
        .map_err(|error| error.to_string())?;
    if !output_bytes.status.success() {
        return Err(format!(
            "encrypted archive is not a readable regular non-symlink file for {user}: {}",
            input.display()
        ));
    }
    fs::write(output, output_bytes.stdout).map_err(|error| error.to_string())?;
    fs::set_permissions(output, fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn config_ops(args: &[String]) -> Result<(), String> {
    crate::config_ops::run(args).map_err(|error| {
        let text = error.to_string();
        if text.is_empty() {
            "config operation failed".to_owned()
        } else {
            text
        }
    })
}

fn config_ops_as_user(user: &str, uid: u32, args: &[String]) -> Result<(), String> {
    if geteuid().as_raw() == uid {
        return config_ops(args);
    }
    let mut command = Command::new("runuser");
    command.args(["--user", user, "--"]);
    command.arg(std::env::current_exe().map_err(|error| error.to_string())?);
    command.arg("config-ops");
    command.args(args);
    let status = command.status().map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err("config operation failed".to_owned())
    }
}

fn lock_path(config: &Path) -> Result<PathBuf, String> {
    let output = Command::new(std::env::current_exe().map_err(|error| error.to_string())?)
        .args(["config-ops", "lock-path"])
        .arg(config)
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_owned());
    }
    Ok(PathBuf::from(
        String::from_utf8_lossy(&output.stdout).trim(),
    ))
}

fn consistent_lock() -> Result<File, String> {
    loop {
        let selected = lock_path(Path::new("/etc/homelab-backup/config.yaml"))?;
        let held = acquire_lock(&selected)?;
        let confirmed = lock_path(Path::new("/etc/homelab-backup/config.yaml"))?;
        if confirmed == selected {
            return Ok(held);
        }
    }
}

fn acquire_lock(path: &Path) -> Result<File, String> {
    config_ops(&["validate-lock".to_owned(), path.display().to_string()])?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|error| error.to_string())?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    flock(&file, FlockOperation::LockExclusive).map_err(|error| error.to_string())?;
    Ok(file)
}

fn invoking_user(fallback: &Path) -> Result<(String, u32, u32), String> {
    let name = std::env::var("SUDO_USER")
        .ok()
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| {
            Command::new("stat")
                .args(["-c", "%U", "--"])
                .arg(fallback)
                .output()
                .ok()
                .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
                .unwrap_or_default()
        });
    let uid = id(&name, "-u")?;
    if uid == 0 {
        return Err("Git backup requires a non-root invoking user or repository owner".to_owned());
    }
    let gid = id(&name, "-g")?;
    Ok((name, uid, gid))
}

fn id(user: &str, flag: &str) -> Result<u32, String> {
    let output = Command::new("id")
        .args([flag, "--", user])
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!("cannot resolve user {user}"));
    }
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .map_err(|_| format!("cannot resolve user {user}"))
}

fn git_status(user: &str, root: &Path, args: &[&str]) -> Result<i32, String> {
    let status = Command::new("runuser")
        .args(["--user", user, "--", "git", "-C"])
        .arg(root)
        .args(args)
        .status()
        .map_err(|error| error.to_string())?;
    Ok(status.code().unwrap_or(1))
}

fn git(user: &str, root: &Path, args: &[&str]) -> Result<(), String> {
    let status = Command::new("runuser")
        .args(["--user", user, "--", "git", "-C"])
        .arg(root)
        .args(args)
        .status()
        .map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err("git command failed".to_owned())
    }
}

fn git_output(user: &str, root: &Path, args: &[&str]) -> Result<String, String> {
    let output = Command::new("runuser")
        .args(["--user", user, "--", "git", "-C"])
        .arg(root)
        .args(args)
        .output()
        .map_err(|error| error.to_string())?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        Err("git command failed".to_owned())
    }
}

fn runuser(user: &str, args: &[&str]) -> Result<(), String> {
    let status = Command::new("runuser")
        .args(["--user", user, "--"])
        .args(args)
        .status()
        .map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("command failed: {}", args.join(" ")))
    }
}

fn regular_file(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|_| {
        format!(
            "required source is missing or is a symlink: {}",
            path.display()
        )
    })?;
    if meta.is_file() {
        Ok(())
    } else {
        Err(format!(
            "required source is missing or is a symlink: {}",
            path.display()
        ))
    }
}

fn require_tool(name: &str) -> Result<(), String> {
    if std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(name).is_file()))
    {
        Ok(())
    } else {
        Err(format!("{name} is required; run backupctl install first"))
    }
}

fn require_tmpfs(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if !meta.is_dir() {
        return Err(format!(
            "runtime directory is not a real directory: {}",
            path.display()
        ));
    }
    let output = Command::new("stat")
        .args(["-f", "-c", "%T", "--"])
        .arg(path)
        .output()
        .map_err(|error| error.to_string())?;
    if String::from_utf8_lossy(&output.stdout).trim() == "tmpfs" {
        Ok(())
    } else {
        Err(format!(
            "runtime directory must be on tmpfs: {}",
            path.display()
        ))
    }
}

fn tempfile(parent: &str, prefix: &str) -> Result<PathBuf, String> {
    for _ in 0..16 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.subsec_nanos())
            .unwrap_or(0);
        let path = Path::new(parent).join(format!("{prefix}{:x}{nanos:08x}", std::process::id()));
        match fs::create_dir(&path) {
            Ok(()) => {
                fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
                return Ok(path);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.to_string()),
        }
    }
    Err("could not create a private runtime directory".to_owned())
}

fn prompt(question: &str) -> Result<String, String> {
    eprint!("{question}");
    io::stderr().flush().ok();
    let mut line = String::new();
    io::stdin()
        .read_line(&mut line)
        .map_err(|error| error.to_string())?;
    Ok(line)
}

fn timestamp() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{now}")
}

fn unique_sorted(paths: [&PathBuf; 2]) -> Vec<PathBuf> {
    let mut values = vec![paths[0].clone(), paths[1].clone()];
    values.sort();
    values.dedup();
    values
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zip_member_order_is_the_three_recovery_files() {
        let mut expected = MEMBERS.map(str::to_owned);
        expected.sort();
        assert_eq!(
            expected,
            [
                "configs/config.yaml".to_owned(),
                "configs/rclone.conf".to_owned(),
                "configs/restic-password".to_owned(),
            ]
        );
    }

    #[test]
    fn git_index_line_accepts_only_a_normal_archive_entry() {
        let hash = "a".repeat(40);
        assert_eq!(
            parse_cacheinfo(
                &format!("100644 {hash} 0\t{ARCHIVE_RELATIVE}"),
                ARCHIVE_RELATIVE
            )
            .unwrap()
            .0,
            "100644"
        );
        assert!(parse_cacheinfo("", ARCHIVE_RELATIVE).is_none());
        assert!(
            parse_cacheinfo(
                &format!("100644 {hash} 1\t{ARCHIVE_RELATIVE}"),
                ARCHIVE_RELATIVE
            )
            .is_none()
        );
        assert!(parse_cacheinfo(&format!("100644 {hash} 0\tother"), ARCHIVE_RELATIVE).is_none());
    }
}
