//! Btrfs snapshot journals. `btrfs` and `findmnt` are injected and never spawned here.
//!
//! Btrfs may give each subvolume a distinct `st_dev` even when they share a filesystem.
//! The filesystem UUID is the cross-subvolume identity.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use homelab_backup_domain::manifest::source_path;
use homelab_backup_domain::names::valid_token;
use homelab_backup_domain::paths::{lexical_absolute, path_contains};
use homelab_backup_domain::yaml::{self, Yaml};
use rustix::fs::{Mode, OFlags, fstat, openat};
use rustix::io::Errno;
use rustix::process::{getegid, geteuid};

use crate::unix::atomic::atomic_write_json;
use crate::unix::control::{
    clear_control_leaf, ensure_control_directory, file_name, is_dir, is_lnk, is_reg, mode_of,
    open_directory, parent_path, validate_control_directory,
};
use crate::unix::data::open_data_path;
use crate::unix::error::SecurityError;
use crate::unix::mount::{MountRecord, containing_mount, current_mount_records};

const SNAPSHOT_DIRECTORY: &str = ".homelab-backup-snapshots";
const STATE_VERSION: i64 = 1;
const MODE_BITS: u32 = 0o7777;

const ENTRY_FIELDS: &[&str] = &[
    "phase",
    "source_id",
    "source_path",
    "source_subvolume_id",
    "source_uuid",
    "filesystem_uuid",
    "workspace_anchor",
    "workspace_path",
    "snapshot_path",
    "snapshot_subvolume_id",
    "snapshot_uuid",
];

const STATE_FIELDS: &[&str] = &["version", "service", "operation_id", "snapshots"];

/// One injected `btrfs` or `findmnt` invocation.
///
/// `pass_fds` are descriptors the child must inherit without `CLOEXEC`. Snapshot
/// creation passes the source directory fd and targets `/proc/self/fd/N`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BtrfsCommand {
    pub argv: Vec<String>,
    pub pass_fds: Vec<RawFd>,
}

/// Captured stdout and stderr from an injected command. Non-zero `code` is a failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapturedCommand {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Identity parsed from `btrfs subvolume show`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubvolumeDetails {
    pub subvolume_id: i64,
    pub uuid: String,
    pub parent_uuid: Option<String>,
    pub readonly: bool,
}

/// One snapshot create/cleanup cycle for a service manifest.
pub struct SnapshotTransaction<'a> {
    state_root: &'a str,
    manifest: &'a BTreeMap<String, Yaml>,
    conflicts: &'a mut dyn FnMut(&str) -> Result<Vec<String>, SecurityError>,
    run: &'a mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    /// `None` reads `/proc/self/mountinfo`. Tests pass records so no real btrfs mount is required.
    mount_records: Option<Vec<MountRecord>>,
    overrides: BTreeMap<String, String>,
}

struct Inspect<'a> {
    allow_plain: bool,
    filesystem_type: Option<&'a str>,
    pass_fds: &'a [RawFd],
    opened_mode: Option<u32>,
}

struct SourcePlan {
    id: Option<String>,
    path: String,
    required: bool,
}

pub fn snapshot_state_services(state_root: &str) -> Result<Vec<String>, SecurityError> {
    let root = absolute(state_root)?;
    match std::fs::symlink_metadata(&root) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(SecurityError::runtime(python_os_error(&root, &error))),
        Ok(_) => {}
    }
    validate_control_directory(&root, false, None)?;
    let mut services = Vec::new();
    for entry in std::fs::read_dir(&root).map_err(|error| {
        SecurityError::runtime(format!("cannot list Btrfs snapshot state {root}: {error}"))
    })? {
        let entry = entry.map_err(|error| SecurityError::runtime(error.to_string()))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if let Some(service) = state_file_service(name) {
            services.push(service);
        }
    }
    services.sort();
    Ok(services)
}

pub fn cleanup_snapshot_state(
    state_root: &str,
    service: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
) -> Result<(), SecurityError> {
    cleanup_in(state_root, service, run, None)
}

pub fn parse_subvolume_show(path: &str, output: &str) -> Result<SubvolumeDetails, SecurityError> {
    let mut values = BTreeMap::new();
    for line in output.lines() {
        let Some((key, value)) = line.trim().split_once(':') else {
            continue;
        };
        values.insert(key.trim().to_ascii_lowercase(), value.trim().to_owned());
    }
    let subvolume_id = match values
        .get("subvolume id")
        .and_then(|value| value.parse::<i64>().ok())
    {
        Some(id) => id,
        None => {
            return Err(SecurityError::runtime(format!(
                "cannot parse Btrfs subvolume identity for {path}"
            )));
        }
    };
    let Some(uuid_raw) = values.get("uuid") else {
        return Err(SecurityError::runtime(format!(
            "cannot parse Btrfs subvolume identity for {path}"
        )));
    };
    let uuid = normalise_uuid(uuid_raw);
    let parent_uuid = normalise_uuid(values.get("parent uuid").map(String::as_str).unwrap_or(""));
    if subvolume_id <= 0
        || uuid.is_none()
        || uuid.as_ref().is_some_and(|value| !is_uuid_token(value))
        || parent_uuid
            .as_ref()
            .is_some_and(|value| !is_uuid_token(value))
    {
        return Err(SecurityError::runtime(format!(
            "invalid Btrfs subvolume identity for {path}"
        )));
    }
    let flags = values.get("flags").map(String::as_str).unwrap_or("");
    Ok(SubvolumeDetails {
        subvolume_id,
        uuid: uuid.unwrap_or_default(),
        parent_uuid,
        readonly: flags
            .to_ascii_lowercase()
            .split_whitespace()
            .any(|flag| flag == "readonly"),
    })
}

impl<'a> SnapshotTransaction<'a> {
    /// `mount_records` replaces live mountinfo. Pass `None` outside tests.
    pub fn new(
        state_root: &'a str,
        manifest: &'a BTreeMap<String, Yaml>,
        conflicts: &'a mut dyn FnMut(&str) -> Result<Vec<String>, SecurityError>,
        run: &'a mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
        mount_records: Option<Vec<MountRecord>>,
    ) -> Self {
        Self {
            state_root,
            manifest,
            conflicts,
            run,
            mount_records,
            overrides: BTreeMap::new(),
        }
    }

    pub fn create(&mut self) -> Result<BTreeMap<String, String>, SecurityError> {
        let mounts = match &self.mount_records {
            Some(records) => records.clone(),
            None => current_mount_records()?,
        };
        self.create_with(&mounts)
    }

    pub fn cleanup(&mut self) -> Result<(), SecurityError> {
        let service = manifest_service(self.manifest)?.to_owned();
        let mounts = self.mount_records.clone();
        cleanup_in(self.state_root, &service, self.run, mounts.as_deref())
    }

    fn create_with(
        &mut self,
        mounts: &[MountRecord],
    ) -> Result<BTreeMap<String, String>, SecurityError> {
        let state_root = self.state_root.to_owned();
        let service = manifest_service(self.manifest)?.to_owned();
        let plans = source_plans(self.manifest)?;
        let operation = new_operation_id()?;
        let mut state = initial_state(&service, &operation);
        let mut overrides = BTreeMap::new();
        for plan in plans {
            let opened = match open_data_path(Path::new(&plan.path)) {
                Ok(opened) => opened,
                Err(error) if error.is_missing_path() && !plan.required => continue,
                Err(error) => return Err(error),
            };
            let (fd, metadata, _) = opened;
            let raw = fd.as_raw_fd();
            let proc_path = format!("/proc/self/fd/{raw}");
            let mount = containing_mount(&plan.path, mounts)?;
            let source_details = subvolume_details(
                &proc_path,
                &Inspect {
                    allow_plain: true,
                    filesystem_type: Some(mount.filesystem_type.as_str()),
                    pass_fds: &[raw],
                    opened_mode: Some(mode_of(&metadata)),
                },
                self.run,
                mounts,
            )?;
            let Some(source_details) = source_details else {
                continue;
            };
            let Some(source_id) = plan.id.as_deref() else {
                return Err(SecurityError::runtime("path source is missing id"));
            };
            let anchor = snapshot_workspace_anchor(&plan.path, self.run, mounts)?;
            let workspace = validate_workspace(&snapshot_parent(&anchor)?)?;
            let source_filesystem = filesystem_uuid(&anchor, self.run)?;
            if filesystem_uuid(&workspace, self.run)? != source_filesystem {
                return Err(SecurityError::runtime(format!(
                    "Btrfs snapshot workspace is not on the source filesystem: {workspace}"
                )));
            }
            let conflicts = (self.conflicts)(&workspace)?;
            if !conflicts.is_empty() {
                return Err(SecurityError::runtime(format!(
                    "Btrfs snapshot workspace is mounted by containers: {}",
                    conflicts.join(", ")
                )));
            }
            let snapshot = format!("{workspace}/{service}-{operation}-{source_id}");
            let mut entry = snapshot_entry(
                source_id,
                &plan.path,
                &source_details,
                &source_filesystem,
                &anchor,
                &workspace,
                &snapshot,
            );
            push_snapshot(&mut state, &entry)?;
            save_state(&state_root, &state)?;
            checked(
                self.run,
                vec![
                    "btrfs".to_owned(),
                    "subvolume".to_owned(),
                    "snapshot".to_owned(),
                    "-r".to_owned(),
                    proc_path,
                    snapshot.clone(),
                ],
                vec![raw],
            )?;
            let snapshot_details = require_subvolume(&snapshot, self.run, mounts)?;
            if !snapshot_details.readonly {
                return Err(SecurityError::runtime(format!(
                    "Btrfs created a writable snapshot: {snapshot}"
                )));
            }
            if snapshot_details.parent_uuid.as_deref() != Some(source_details.uuid.as_str()) {
                return Err(SecurityError::runtime(format!(
                    "Btrfs snapshot parent UUID does not match source: {snapshot}"
                )));
            }
            assign(
                &mut entry,
                "snapshot_subvolume_id",
                ValueId(snapshot_details.subvolume_id),
            )?;
            assign(
                &mut entry,
                "snapshot_uuid",
                serde_json::Value::String(snapshot_details.uuid),
            )?;
            assign(
                &mut entry,
                "phase",
                serde_json::Value::String("ready".to_owned()),
            )?;
            replace_last(&mut state, &entry)?;
            save_state(&state_root, &state)?;
            overrides.insert(source_id.to_owned(), snapshot);
            drop(fd);
        }
        self.overrides.clone_from(&overrides);
        Ok(overrides)
    }
}

fn cleanup_in(
    state_root: &str,
    service: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mount_records: Option<&[MountRecord]>,
) -> Result<(), SecurityError> {
    let Some(mut state) = load_state(state_root, service)? else {
        return Ok(());
    };
    let owned;
    let mounts = match mount_records {
        Some(records) => records,
        None => {
            owned = current_mount_records()?;
            owned.as_slice()
        }
    };
    while let Some(mut entry) = first_snapshot(&state) {
        let (_anchor, workspace, snapshot) = entry_paths(&entry)?;
        let phase = entry
            .get("phase")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("")
            .to_owned();
        if phase == "creating" {
            if !snapshot_exists(&snapshot)? {
                commit_entry_removal(state_root, &mut state, &workspace)?;
                continue;
            }
            let details = validate_cleanup_identity(&entry, true, run, mounts)?;
            assign(
                &mut entry,
                "snapshot_subvolume_id",
                ValueId(details.subvolume_id),
            )?;
            assign(
                &mut entry,
                "snapshot_uuid",
                serde_json::Value::String(details.uuid),
            )?;
            assign(
                &mut entry,
                "phase",
                serde_json::Value::String("deleting".to_owned()),
            )?;
            write_first(&mut state, &entry)?;
            save_state(state_root, &state)?;
        } else if phase == "ready" {
            if !snapshot_exists(&snapshot)? {
                return Err(identity_error(&entry, "recorded snapshot is missing"));
            }
            validate_cleanup_identity(&entry, false, run, mounts)?;
            assign(
                &mut entry,
                "phase",
                serde_json::Value::String("deleting".to_owned()),
            )?;
            write_first(&mut state, &entry)?;
            save_state(state_root, &state)?;
        }
        if !snapshot_exists(&snapshot)? {
            commit_entry_removal(state_root, &mut state, &workspace)?;
            continue;
        }
        validate_cleanup_identity(&entry, false, run, mounts)?;
        checked(
            run,
            vec![
                "btrfs".to_owned(),
                "subvolume".to_owned(),
                "delete".to_owned(),
                snapshot.clone(),
            ],
            Vec::new(),
        )?;
        checked(
            run,
            vec![
                "btrfs".to_owned(),
                "subvolume".to_owned(),
                "sync".to_owned(),
                workspace.clone(),
            ],
            Vec::new(),
        )?;
        if snapshot_exists(&snapshot)? {
            return Err(identity_error(
                &entry,
                "snapshot still exists after deletion",
            ));
        }
        commit_entry_removal(state_root, &mut state, &workspace)?;
    }
    Ok(())
}

struct ValueId(i64);

fn assign(
    entry: &mut serde_json::Value,
    key: &str,
    value: impl IntoJson,
) -> Result<(), SecurityError> {
    let object = entry
        .as_object_mut()
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))?;
    object.insert(key.to_owned(), value.into_json());
    Ok(())
}

trait IntoJson {
    fn into_json(self) -> serde_json::Value;
}

impl IntoJson for serde_json::Value {
    fn into_json(self) -> serde_json::Value {
        self
    }
}

impl IntoJson for ValueId {
    fn into_json(self) -> serde_json::Value {
        serde_json::Value::from(self.0)
    }
}

fn require_subvolume(
    path: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mounts: &[MountRecord],
) -> Result<SubvolumeDetails, SecurityError> {
    match subvolume_details(
        path,
        &Inspect {
            allow_plain: false,
            filesystem_type: None,
            pass_fds: &[],
            opened_mode: None,
        },
        run,
        mounts,
    )? {
        Some(details) => Ok(details),
        None => Err(SecurityError::runtime(format!(
            "path is not a Btrfs subvolume: {path}"
        ))),
    }
}

fn subvolume_details(
    path: &str,
    inspect: &Inspect<'_>,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mounts: &[MountRecord],
) -> Result<Option<SubvolumeDetails>, SecurityError> {
    let display_path = path.to_owned();
    let (command_path, mode) = if let Some(mode) = inspect.opened_mode {
        (path.to_owned(), mode)
    } else {
        let command_path = absolute(path)?;
        let metadata = match std::fs::symlink_metadata(&command_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                if inspect.allow_plain {
                    return Ok(None);
                }
                return Err(SecurityError::runtime(python_os_error(
                    &command_path,
                    &error,
                )));
            }
            Err(error) => {
                return Err(SecurityError::runtime(python_os_error(
                    &command_path,
                    &error,
                )));
            }
        };
        (command_path, metadata.mode())
    };
    if !is_dir(mode) || is_lnk(mode) {
        return plain_or_reject(inspect.allow_plain, &display_path);
    }
    let filesystem = match inspect.filesystem_type {
        Some(filesystem) => filesystem.to_owned(),
        None => containing_mount(&command_path, mounts)?
            .filesystem_type
            .clone(),
    };
    if filesystem != "btrfs" {
        return plain_or_reject(inspect.allow_plain, &display_path);
    }
    let result = run(BtrfsCommand {
        argv: vec![
            "btrfs".to_owned(),
            "subvolume".to_owned(),
            "show".to_owned(),
            command_path,
        ],
        pass_fds: inspect.pass_fds.to_vec(),
    });
    if result.code != 0 {
        let diagnostic = command_diagnostic(&result);
        if inspect.allow_plain && is_not_subvolume_error(&diagnostic) {
            return Ok(None);
        }
        let mut message = format!("cannot inspect Btrfs subvolume {display_path}");
        if !diagnostic.is_empty() {
            message = format!("{message}: {diagnostic}");
        }
        return Err(SecurityError::runtime(message));
    }
    parse_subvolume_show(&display_path, &result.stdout).map(Some)
}

fn plain_or_reject(
    allow_plain: bool,
    path: &str,
) -> Result<Option<SubvolumeDetails>, SecurityError> {
    if allow_plain {
        Ok(None)
    } else {
        Err(SecurityError::runtime(format!(
            "path is not a Btrfs subvolume: {path}"
        )))
    }
}

fn snapshot_workspace_anchor(
    source: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mounts: &[MountRecord],
) -> Result<String, SecurityError> {
    let source = absolute(source)?;
    let source_uuid = filesystem_uuid(&source, run)?;
    let mut candidate = parent_path(&source);
    while candidate != "/" {
        let accepted = candidate_anchor(&candidate, &source_uuid, run, mounts).unwrap_or_default();
        if accepted {
            return Ok(candidate);
        }
        candidate = parent_path(&candidate);
    }
    Err(SecurityError::runtime(format!(
        "cannot place a protected Btrfs snapshot workspace outside {source}; \
         mount the Btrfs top-level or a common parent subvolume at a root-protected path"
    )))
}

fn candidate_anchor(
    candidate: &str,
    source_uuid: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mounts: &[MountRecord],
) -> Result<bool, SecurityError> {
    let mount = containing_mount(candidate, mounts)?;
    if mount.filesystem_type != "btrfs" {
        return Ok(false);
    }
    if filesystem_uuid(candidate, run)? != source_uuid {
        return Ok(false);
    }
    validate_control_directory(candidate, false, None)?;
    Ok(true)
}

fn validate_workspace(path: &str) -> Result<String, SecurityError> {
    let path = ensure_control_directory(path, 0o700)?;
    validate_control_directory(&path, false, None)?;
    let metadata = std::fs::symlink_metadata(&path)
        .map_err(|error| SecurityError::runtime(python_os_error(&path, &error)))?;
    let (owners, groups) = allowed_identity();
    let mode = metadata.mode();
    if !is_dir(mode)
        || is_lnk(mode)
        || !owners.contains(&metadata.uid())
        || !groups.contains(&metadata.gid())
        || mode & MODE_BITS != 0o700
    {
        return Err(SecurityError::invalid(format!(
            "Btrfs snapshot workspace must be root-only mode 0700: {path}"
        )));
    }
    Ok(path)
}

fn validate_cleanup_identity(
    entry: &serde_json::Value,
    creating: bool,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    mounts: &[MountRecord],
) -> Result<SubvolumeDetails, SecurityError> {
    let (anchor, workspace, snapshot) = entry_paths(entry)?;
    if let Err(error) = validate_control_directory(&workspace, false, None) {
        if error.is_runtime() {
            return Err(error);
        }
        return Err(identity_error(
            entry,
            &format!("workspace or snapshot path is invalid: {error}"),
        ));
    }
    let workspace_meta = match lstat_for_identity(&workspace) {
        Ok(metadata) => metadata,
        Err(error) => return Err(wrap_identity_stat(entry, error)),
    };
    let snapshot_meta = match lstat_for_identity(&snapshot) {
        Ok(metadata) => metadata,
        Err(error) => return Err(wrap_identity_stat(entry, error)),
    };
    let (owners, groups) = allowed_identity();
    if !owners.contains(&workspace_meta.uid())
        || !groups.contains(&workspace_meta.gid())
        || workspace_meta.mode() & MODE_BITS != 0o700
        || !is_dir(snapshot_meta.mode())
        || is_lnk(snapshot_meta.mode())
    {
        return Err(identity_error(
            entry,
            "workspace or snapshot path is invalid",
        ));
    }
    if filesystem_uuid(&anchor, run)? != entry_text(entry, "filesystem_uuid")
        || filesystem_uuid(&workspace, run)? != entry_text(entry, "filesystem_uuid")
        || filesystem_uuid(&snapshot, run)? != entry_text(entry, "filesystem_uuid")
    {
        return Err(identity_error(entry, "filesystem UUID"));
    }
    let details = require_subvolume(&snapshot, run, mounts)?;
    if !details.readonly {
        return Err(identity_error(entry, "snapshot is not read-only"));
    }
    let source_uuid = entry_text(entry, "source_uuid");
    if details.parent_uuid.as_deref() != Some(source_uuid.as_str()) {
        return Err(identity_error(entry, "parent UUID"));
    }
    if !creating {
        if details.subvolume_id != positive_field(entry, "snapshot_subvolume_id").unwrap_or(0) {
            return Err(identity_error(entry, "snapshot subvolume ID"));
        }
        if details.uuid != entry_text(entry, "snapshot_uuid") {
            return Err(identity_error(entry, "snapshot UUID"));
        }
    }
    Ok(details)
}

fn wrap_identity_stat(entry: &serde_json::Value, error: SecurityError) -> SecurityError {
    if error.is_runtime() {
        error
    } else {
        identity_error(
            entry,
            &format!("workspace or snapshot path is invalid: {error}"),
        )
    }
}

fn lstat_for_identity(path: &str) -> Result<std::fs::Metadata, SecurityError> {
    match std::fs::symlink_metadata(path) {
        Ok(metadata) => Ok(metadata),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err(SecurityError::invalid(python_os_error(path, &error)))
        }
        Err(error) => Err(SecurityError::runtime(python_os_error(path, &error))),
    }
}

fn entry_paths(entry: &serde_json::Value) -> Result<(String, String, String), SecurityError> {
    let anchor = absolute(entry_text(entry, "workspace_anchor").as_str())?;
    let workspace = absolute(entry_text(entry, "workspace_path").as_str())?;
    let snapshot = absolute(entry_text(entry, "snapshot_path").as_str())?;
    let expected = snapshot_parent(&anchor)?;
    if workspace != expected
        || parent_path(&snapshot) != workspace
        || !path_contains(&workspace, &snapshot).unwrap_or(false)
    {
        return Err(identity_error(entry, "unexpected snapshot path"));
    }
    Ok((anchor, workspace, snapshot))
}

fn snapshot_parent(anchor: &str) -> Result<String, SecurityError> {
    let anchor = absolute(anchor)?;
    if anchor == "/" {
        Ok(format!("/{SNAPSHOT_DIRECTORY}"))
    } else {
        Ok(format!("{anchor}/{SNAPSHOT_DIRECTORY}"))
    }
}

fn filesystem_uuid(
    path: &str,
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
) -> Result<String, SecurityError> {
    let result = checked(
        run,
        vec![
            "findmnt".to_owned(),
            "--noheadings".to_owned(),
            "--output".to_owned(),
            "UUID".to_owned(),
            "--target".to_owned(),
            path.to_owned(),
        ],
        Vec::new(),
    )?;
    let value = result.stdout.trim();
    if !is_uuid_token(value) {
        return Err(SecurityError::runtime(format!(
            "cannot determine Btrfs filesystem UUID for {path}"
        )));
    }
    Ok(value.to_ascii_lowercase())
}

fn commit_entry_removal(
    state_root: &str,
    state: &mut serde_json::Value,
    workspace: &str,
) -> Result<(), SecurityError> {
    let empty = pop_snapshot(state)?;
    if empty {
        clear_state(state_root, state_service(state)?)?;
    } else {
        save_state(state_root, state)?;
    }
    let _ = std::fs::remove_dir(workspace);
    Ok(())
}

fn pop_snapshot(state: &mut serde_json::Value) -> Result<bool, SecurityError> {
    let snapshots = state
        .get_mut("snapshots")
        .and_then(serde_json::Value::as_array_mut)
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))?;
    if snapshots.is_empty() {
        return Err(SecurityError::runtime("invalid Btrfs snapshot state"));
    }
    snapshots.remove(0);
    Ok(snapshots.is_empty())
}

fn first_snapshot(state: &serde_json::Value) -> Option<serde_json::Value> {
    state
        .get("snapshots")
        .and_then(serde_json::Value::as_array)
        .and_then(|entries| entries.first())
        .cloned()
}

fn write_first(
    state: &mut serde_json::Value,
    entry: &serde_json::Value,
) -> Result<(), SecurityError> {
    let snapshots = state
        .get_mut("snapshots")
        .and_then(serde_json::Value::as_array_mut)
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))?;
    let Some(first) = snapshots.first_mut() else {
        return Err(SecurityError::runtime("invalid Btrfs snapshot state"));
    };
    *first = entry.clone();
    Ok(())
}

fn push_snapshot(
    state: &mut serde_json::Value,
    entry: &serde_json::Value,
) -> Result<(), SecurityError> {
    let snapshots = state
        .get_mut("snapshots")
        .and_then(serde_json::Value::as_array_mut)
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))?;
    snapshots.push(entry.clone());
    Ok(())
}

fn replace_last(
    state: &mut serde_json::Value,
    entry: &serde_json::Value,
) -> Result<(), SecurityError> {
    let snapshots = state
        .get_mut("snapshots")
        .and_then(serde_json::Value::as_array_mut)
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))?;
    let Some(last) = snapshots.last_mut() else {
        return Err(SecurityError::runtime("invalid Btrfs snapshot state"));
    };
    *last = entry.clone();
    Ok(())
}

fn load_state(state_root: &str, service: &str) -> Result<Option<serde_json::Value>, SecurityError> {
    let path = state_path(state_root, service)?;
    let text = match read_control_text(&path) {
        Ok(Some(text)) => text,
        Ok(None) => return Ok(None),
        Err(error) => {
            return Err(SecurityError::runtime(format!(
                "cannot load Btrfs snapshot state {path}: {error}"
            )));
        }
    };
    let mut data: serde_json::Value = match serde_json::from_str(&text) {
        Ok(data) => data,
        Err(error) => {
            return Err(SecurityError::runtime(format!(
                "cannot load Btrfs snapshot state {path}: {error}"
            )));
        }
    };
    migrate_workspace_anchor(&mut data);
    if !valid_state(&data, service) {
        return Err(SecurityError::runtime(format!(
            "invalid Btrfs snapshot state: {path}"
        )));
    }
    Ok(Some(data))
}

fn migrate_workspace_anchor(data: &mut serde_json::Value) {
    let Some(entries) = data
        .get_mut("snapshots")
        .and_then(serde_json::Value::as_array_mut)
    else {
        return;
    };
    for entry in entries {
        let Some(object) = entry.as_object_mut() else {
            continue;
        };
        if object.contains_key("workspace_anchor") {
            continue;
        }
        if let Some(trusted) = object.remove("trusted_root") {
            object.insert("workspace_anchor".to_owned(), trusted);
        }
    }
}

fn valid_state(data: &serde_json::Value, service: &str) -> bool {
    let Some(object) = data.as_object() else {
        return false;
    };
    if !exact_keys(object, STATE_FIELDS) {
        return false;
    }
    if object.get("version").and_then(serde_json::Value::as_i64) != Some(STATE_VERSION) {
        return false;
    }
    if object.get("service").and_then(serde_json::Value::as_str) != Some(service) {
        return false;
    }
    let Some(operation) = object
        .get("operation_id")
        .and_then(serde_json::Value::as_str)
    else {
        return false;
    };
    if !is_operation_id(operation) {
        return false;
    }
    let Some(snapshots) = object
        .get("snapshots")
        .and_then(serde_json::Value::as_array)
    else {
        return false;
    };
    snapshots
        .iter()
        .all(|entry| valid_entry(entry, service, operation))
}

fn valid_entry(entry: &serde_json::Value, service: &str, operation: &str) -> bool {
    let Some(object) = entry.as_object() else {
        return false;
    };
    if !exact_keys(object, ENTRY_FIELDS) {
        return false;
    }
    let Some(phase) = object.get("phase").and_then(serde_json::Value::as_str) else {
        return false;
    };
    if !matches!(phase, "creating" | "ready" | "deleting") {
        return false;
    }
    for field in [
        "source_id",
        "source_path",
        "source_uuid",
        "filesystem_uuid",
        "workspace_anchor",
        "workspace_path",
        "snapshot_path",
    ] {
        if object
            .get(field)
            .and_then(serde_json::Value::as_str)
            .is_none_or(str::is_empty)
        {
            return false;
        }
    }
    let source_id = object["source_id"].as_str().unwrap_or("");
    if !valid_token(source_id)
        || !is_uuid_token(object["source_uuid"].as_str().unwrap_or(""))
        || !is_uuid_token(object["filesystem_uuid"].as_str().unwrap_or(""))
        || positive_field(entry, "source_subvolume_id").is_none()
    {
        return false;
    }
    let snapshot_id = object.get("snapshot_subvolume_id");
    let snapshot_uuid = object.get("snapshot_uuid");
    if phase == "creating" {
        if snapshot_id != Some(&serde_json::Value::Null)
            || snapshot_uuid != Some(&serde_json::Value::Null)
        {
            return false;
        }
    } else if positive_field(entry, "snapshot_subvolume_id").is_none()
        || snapshot_uuid
            .and_then(serde_json::Value::as_str)
            .is_none_or(|value| !is_uuid_token(value))
    {
        return false;
    }
    let snapshot_path = object["snapshot_path"].as_str().unwrap_or("");
    file_name(snapshot_path) == format!("{service}-{operation}-{source_id}")
}

fn exact_keys(object: &serde_json::Map<String, serde_json::Value>, keys: &[&str]) -> bool {
    object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
}

fn positive_field(entry: &serde_json::Value, key: &str) -> Option<i64> {
    match entry.get(key)? {
        serde_json::Value::Number(number) => number.as_i64().filter(|value| *value > 0),
        _ => None,
    }
}

fn save_state(state_root: &str, state: &serde_json::Value) -> Result<(), SecurityError> {
    ensure_control_directory(state_root, 0o700)?;
    let path = state_path(state_root, state_service(state)?)?;
    atomic_write_json(Path::new(&path), state)
}

fn clear_state(state_root: &str, service: &str) -> Result<(), SecurityError> {
    let path = state_path(state_root, service)?;
    match std::fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(SecurityError::runtime(python_os_error(&path, &error))),
        Ok(_) => clear_control_leaf(&path),
    }
}

fn state_path(state_root: &str, service: &str) -> Result<String, SecurityError> {
    let root = absolute(state_root)?;
    let path = if root == "/" {
        format!("/.{service}.btrfs-snapshots.json")
    } else {
        format!("{root}/.{service}.btrfs-snapshots.json")
    };
    if !path_contains(&root, &path).unwrap_or(false) {
        return Err(SecurityError::invalid(format!(
            "Btrfs snapshot state escapes state_root: {path}"
        )));
    }
    Ok(path)
}

fn state_file_service(name: &str) -> Option<String> {
    let rest = name.strip_prefix('.')?;
    let service = rest.strip_suffix(".btrfs-snapshots.json")?;
    if valid_token(service) {
        Some(service.to_owned())
    } else {
        None
    }
}

fn read_control_text(path: &str) -> Result<Option<String>, SecurityError> {
    let path = absolute(path)?;
    validate_control_directory(&parent_path(&path), false, None)?;
    let parent = open_directory(&parent_path(&path))?;
    let name = file_name(&path);
    let fd = match openat(
        &parent,
        name.as_str(),
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Ok(None),
        Err(Errno::LOOP) => {
            return Err(SecurityError::invalid(format!(
                "control file must be a regular file: {path}"
            )));
        }
        Err(error) => {
            return Err(SecurityError::invalid(format!(
                "control file could not be opened: {path}: {error}"
            )));
        }
    };
    let metadata = fstat(&fd).map_err(|error| SecurityError::runtime(error.to_string()))?;
    if !is_reg(mode_of(&metadata)) {
        return Err(SecurityError::invalid(format!(
            "control file must be a regular file: {path}"
        )));
    }
    let mut allowed = vec![0];
    let effective = geteuid().as_raw();
    if effective != 0 {
        allowed.push(effective);
    }
    if !allowed.contains(&metadata.st_uid) {
        return Err(SecurityError::invalid(format!(
            "control file is not owned by root: {path}"
        )));
    }
    if mode_of(&metadata) & 0o022 != 0 {
        return Err(SecurityError::invalid(format!(
            "control file is group/world writable: {path}"
        )));
    }
    let mut file = File::from(fd);
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|error| SecurityError::invalid(error.to_string()))
}

fn initial_state(service: &str, operation: &str) -> serde_json::Value {
    let mut object = serde_json::Map::new();
    object.insert("version".to_owned(), serde_json::Value::from(STATE_VERSION));
    object.insert(
        "service".to_owned(),
        serde_json::Value::String(service.to_owned()),
    );
    object.insert(
        "operation_id".to_owned(),
        serde_json::Value::String(operation.to_owned()),
    );
    object.insert("snapshots".to_owned(), serde_json::Value::Array(Vec::new()));
    serde_json::Value::Object(object)
}

fn snapshot_entry(
    source_id: &str,
    source_path: &str,
    source: &SubvolumeDetails,
    filesystem_uuid: &str,
    anchor: &str,
    workspace: &str,
    snapshot: &str,
) -> serde_json::Value {
    let mut object = serde_json::Map::new();
    object.insert(
        "phase".to_owned(),
        serde_json::Value::String("creating".to_owned()),
    );
    object.insert(
        "source_id".to_owned(),
        serde_json::Value::String(source_id.to_owned()),
    );
    object.insert(
        "source_path".to_owned(),
        serde_json::Value::String(source_path.to_owned()),
    );
    object.insert(
        "source_subvolume_id".to_owned(),
        serde_json::Value::from(source.subvolume_id),
    );
    object.insert(
        "source_uuid".to_owned(),
        serde_json::Value::String(source.uuid.clone()),
    );
    object.insert(
        "filesystem_uuid".to_owned(),
        serde_json::Value::String(filesystem_uuid.to_owned()),
    );
    object.insert(
        "workspace_anchor".to_owned(),
        serde_json::Value::String(anchor.to_owned()),
    );
    object.insert(
        "workspace_path".to_owned(),
        serde_json::Value::String(workspace.to_owned()),
    );
    object.insert(
        "snapshot_path".to_owned(),
        serde_json::Value::String(snapshot.to_owned()),
    );
    object.insert("snapshot_subvolume_id".to_owned(), serde_json::Value::Null);
    object.insert("snapshot_uuid".to_owned(), serde_json::Value::Null);
    serde_json::Value::Object(object)
}

fn source_plans(manifest: &BTreeMap<String, Yaml>) -> Result<Vec<SourcePlan>, SecurityError> {
    let Some(sources) = manifest.get("sources") else {
        return Ok(Vec::new());
    };
    let Some(sources) = sources.as_map() else {
        return Err(SecurityError::runtime("sources must be a mapping"));
    };
    let Some(paths) = sources.get("paths") else {
        return Ok(Vec::new());
    };
    let Some(paths) = paths.as_seq() else {
        return Err(SecurityError::runtime("sources.paths must be a list"));
    };
    let mut plans = Vec::new();
    for source in paths {
        let Some(source) = source.as_map() else {
            return Err(SecurityError::runtime("path source must be a mapping"));
        };
        let unresolved =
            source_path(manifest, source).map_err(|error| SecurityError::invalid(error.message))?;
        let path = absolute(&unresolved)?;
        let required = source.get("required").map(Yaml::is_truthy).unwrap_or(true);
        let id = source.get("id").and_then(Yaml::as_str).map(str::to_owned);
        plans.push(SourcePlan { id, path, required });
    }
    Ok(plans)
}

fn manifest_service(manifest: &BTreeMap<String, Yaml>) -> Result<&str, SecurityError> {
    manifest
        .get("service")
        .and_then(Yaml::as_str)
        .filter(|service| !service.is_empty())
        .ok_or_else(|| SecurityError::runtime("manifest is missing service"))
}

fn state_service(state: &serde_json::Value) -> Result<&str, SecurityError> {
    state
        .get("service")
        .and_then(serde_json::Value::as_str)
        .filter(|service| !service.is_empty())
        .ok_or_else(|| SecurityError::runtime("invalid Btrfs snapshot state"))
}

fn entry_text(entry: &serde_json::Value, key: &str) -> String {
    entry
        .get(key)
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
        .to_owned()
}

fn identity_error(entry: &serde_json::Value, reason: &str) -> SecurityError {
    let snapshot = entry
        .get("snapshot_path")
        .and_then(serde_json::Value::as_str)
        .map(yaml::python_str)
        .unwrap_or_else(|| "None".to_owned());
    SecurityError::runtime(format!(
        "Btrfs snapshot identity does not fully match state ({reason}); \
         refusing automatic deletion of {snapshot}. Inspect the snapshot and state file manually."
    ))
}

fn snapshot_exists(path: &str) -> Result<bool, SecurityError> {
    match std::fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(SecurityError::runtime(python_os_error(path, &error))),
    }
}

fn checked(
    run: &mut dyn FnMut(BtrfsCommand) -> CapturedCommand,
    argv: Vec<String>,
    pass_fds: Vec<RawFd>,
) -> Result<CapturedCommand, SecurityError> {
    let result = run(BtrfsCommand { argv, pass_fds });
    if result.code != 0 {
        return Err(SecurityError::runtime(format!(
            "command failed with exit code {}",
            result.code
        )));
    }
    Ok(result)
}

fn command_diagnostic(result: &CapturedCommand) -> String {
    [result.stderr.as_str(), result.stdout.as_str()]
        .into_iter()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn is_not_subvolume_error(output: &str) -> bool {
    let lower = output.to_ascii_lowercase();
    let bytes = lower.as_bytes();
    let mut index = 0;
    while let Some(relative) = lower[index..].find("not ") {
        let start = index + relative;
        let word_before = start > 0 && is_word_byte(bytes[start - 1]);
        if !word_before {
            let mut rest = &lower[start + 4..];
            if let Some(stripped) = rest.strip_prefix("a ") {
                rest = stripped;
            }
            if let Some(stripped) = rest.strip_prefix("btrfs ") {
                rest = stripped;
            }
            if let Some(after) = rest.strip_prefix("subvolume") {
                let boundary = after.chars().next().is_none_or(|ch| !is_word_char(ch));
                if boundary {
                    return true;
                }
            }
        }
        index = start + 1;
    }
    false
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn is_word_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || ch == '_'
}

fn normalise_uuid(value: &str) -> Option<String> {
    let value = value.trim().to_ascii_lowercase();
    if value.is_empty() || value == "-" {
        None
    } else {
        Some(value)
    }
}

fn is_uuid_token(value: &str) -> bool {
    value.len() >= 16 && value.chars().all(|ch| ch.is_ascii_hexdigit() || ch == '-')
}

fn is_operation_id(value: &str) -> bool {
    value.len() == 32
        && value
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
}

fn allowed_identity() -> (Vec<u32>, Vec<u32>) {
    let uid = geteuid().as_raw();
    let gid = getegid().as_raw();
    let mut owners = vec![0];
    let mut groups = vec![0];
    if uid != 0 {
        owners.push(uid);
        groups.push(gid);
    }
    (owners, groups)
}

fn new_operation_id() -> Result<String, SecurityError> {
    let mut bytes = [0u8; 16];
    let mut file =
        File::open("/dev/urandom").map_err(|error| SecurityError::runtime(error.to_string()))?;
    file.read_exact(&mut bytes)
        .map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(hex_encode(&bytes))
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0xf) as usize] as char);
    }
    out
}

fn absolute(path: &str) -> Result<String, SecurityError> {
    lexical_absolute(path).map_err(|error| SecurityError::invalid(error.message))
}

fn python_os_error(path: &str, error: &std::io::Error) -> String {
    match error.raw_os_error() {
        Some(2) => format!("[Errno 2] No such file or directory: '{path}'"),
        Some(13) => format!("[Errno 13] Permission denied: '{path}'"),
        Some(20) => format!("[Errno 20] Not a directory: '{path}'"),
        Some(errno) => format!("[Errno {errno}] {error}"),
        None => error.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use std::fs::Permissions;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    const FILESYSTEM_UUID: &str = "11111111-2222-3333-4444-555555555555";
    const SOURCE_UUID: &str = "22222222-3333-4444-5555-666666666666";
    const SNAPSHOT_UUID: &str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
    const OPERATION: &str = "0123456789abcdef0123456789abcdef";

    struct Temp(PathBuf);

    impl Temp {
        fn new(label: &str) -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "homelab-btrfs-{label}-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            chmod(&path, 0o755);
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Temp {
        fn drop(&mut self) {
            chmod(&self.0, 0o755);
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn chmod(path: &Path, mode: u32) {
        std::fs::set_permissions(path, Permissions::from_mode(mode)).unwrap();
    }

    fn captured(code: i32, stdout: &str, stderr: &str) -> CapturedCommand {
        CapturedCommand {
            code,
            stdout: stdout.to_owned(),
            stderr: stderr.to_owned(),
        }
    }

    fn mount_at(path: &Path, filesystem: &str) -> MountRecord {
        MountRecord {
            mount_id: 10,
            parent_id: 1,
            device: "0:41".to_owned(),
            filesystem_root: "/".to_owned(),
            mount_point: path.to_str().unwrap().to_owned(),
            filesystem_type: filesystem.to_owned(),
            source: "/dev/test".to_owned(),
        }
    }

    fn show_text(id: i64, uuid: &str, parent: &str, readonly: bool) -> String {
        let flags = if readonly { "readonly" } else { "-" };
        format!("UUID: {uuid}\nParent UUID: {parent}\nSubvolume ID: {id}\nFlags: {flags}\n")
    }

    fn uuid_runner() -> impl FnMut(BtrfsCommand) -> CapturedCommand {
        move |command| match command.argv.first().map(String::as_str) {
            Some("findmnt") => captured(0, &format!("{FILESYSTEM_UUID}\n"), ""),
            Some("btrfs") if command.argv.get(2).map(String::as_str) == Some("show") => {
                let proc_fd = command
                    .argv
                    .get(3)
                    .is_some_and(|path| path.contains("/proc/self/fd/"));
                if proc_fd {
                    captured(0, &show_text(256, SOURCE_UUID, "-", false), "")
                } else {
                    captured(0, &show_text(300, SNAPSHOT_UUID, SOURCE_UUID, true), "")
                }
            }
            _ => captured(0, "", ""),
        }
    }

    fn journal_entry(
        anchor: &str,
        phase: &str,
        workspace: &str,
        snapshot: &str,
    ) -> serde_json::Value {
        let creating = phase == "creating";
        serde_json::json!({
            "phase": phase,
            "source_id": "world",
            "source_path": format!("{anchor}/container/world"),
            "source_subvolume_id": 256,
            "source_uuid": SOURCE_UUID,
            "filesystem_uuid": FILESYSTEM_UUID,
            "workspace_anchor": anchor,
            "workspace_path": workspace,
            "snapshot_path": snapshot,
            "snapshot_subvolume_id": if creating { serde_json::Value::Null } else { serde_json::Value::from(300) },
            "snapshot_uuid": if creating { serde_json::Value::Null } else { serde_json::Value::from(SNAPSHOT_UUID) },
        })
    }

    fn write_journal(state_root: &Path, service: &str, snapshots: serde_json::Value) {
        let value = serde_json::json!({
            "version": 1,
            "service": service,
            "operation_id": OPERATION,
            "snapshots": snapshots,
        });
        let path = state_root.join(format!(".{service}.btrfs-snapshots.json"));
        std::fs::write(&path, format!("{value}\n")).unwrap();
        chmod(&path, 0o644);
    }

    #[test]
    fn subvolume_show_parser_includes_parent_uuid_and_readonly() {
        let parsed = parse_subvolume_show(
            "/data/world",
            &format!(
                "UUID: {SNAPSHOT_UUID}\nParent UUID: {SOURCE_UUID}\nSubvolume ID: 256\nFlags: readonly\n"
            ),
        )
        .unwrap();
        assert_eq!(parsed.parent_uuid.as_deref(), Some(SOURCE_UUID));
        assert!(parsed.readonly);
        assert_eq!(parsed.subvolume_id, 256);

        let plain = parse_subvolume_show(
            "/data/world",
            "UUID: aaaaaaaaaaaaaaaa\nParent UUID: -\nSubvolume ID: 7\nFlags: -\n",
        )
        .unwrap();
        assert_eq!(plain.parent_uuid, None);
        assert!(!plain.readonly);
    }

    #[test]
    fn subvolume_show_parser_rejects_incomplete_identity() {
        let missing = parse_subvolume_show("/data/world", "Subvolume ID: 4\n").unwrap_err();
        assert!(
            missing
                .message
                .contains("cannot parse Btrfs subvolume identity"),
            "{}",
            missing.message
        );
        let invalid = parse_subvolume_show(
            "/data/world",
            "UUID: short\nParent UUID: -\nSubvolume ID: 0\n",
        )
        .unwrap_err();
        assert!(
            invalid
                .message
                .contains("cannot parse Btrfs subvolume identity")
                || invalid.message.contains("invalid Btrfs subvolume identity"),
            "{}",
            invalid.message
        );
    }

    #[test]
    fn incomplete_legacy_journal_is_rejected() {
        let root = Temp::new("invalid");
        write_journal(
            root.path(),
            "demo",
            serde_json::json!([{
                "source_path": "/data/world",
                "snapshot_path": "/data/.homelab-backup-snapshots/old",
            }]),
        );
        let error = load_state(root.path().to_str().unwrap(), "demo").unwrap_err();
        assert!(
            error.message.contains("invalid Btrfs snapshot state"),
            "{}",
            error.message
        );
        assert!(error.is_runtime());
    }

    #[test]
    fn legacy_workspace_anchor_name_is_migrated_on_load() {
        let root = Temp::new("legacy");
        let anchor = root.path().to_str().unwrap();
        let workspace = format!("{anchor}/{SNAPSHOT_DIRECTORY}");
        let snapshot = format!("{workspace}/demo-{OPERATION}-world");
        let mut entry = journal_entry(anchor, "ready", &workspace, &snapshot);
        let trusted = entry
            .as_object_mut()
            .unwrap()
            .remove("workspace_anchor")
            .unwrap();
        entry
            .as_object_mut()
            .unwrap()
            .insert("trusted_root".to_owned(), trusted);
        write_journal(root.path(), "demo", serde_json::Value::Array(vec![entry]));
        let loaded = load_state(root.path().to_str().unwrap(), "demo")
            .unwrap()
            .unwrap();
        let restored = &loaded["snapshots"][0];
        assert_eq!(restored["workspace_anchor"].as_str(), Some(anchor));
        assert!(restored.get("trusted_root").is_none());
    }

    #[test]
    fn plain_btrfs_directory_is_not_a_snapshot_source() {
        let root = Temp::new("plain");
        let source = root.path().join("world");
        std::fs::create_dir_all(&source).unwrap();
        let mut run = |command: BtrfsCommand| {
            assert_eq!(command.argv[2], "show");
            captured(1, "", "ERROR: Not a Btrfs subvolume: Invalid argument")
        };
        let details = subvolume_details(
            source.to_str().unwrap(),
            &Inspect {
                allow_plain: true,
                filesystem_type: Some("btrfs"),
                pass_fds: &[],
                opened_mode: None,
            },
            &mut run,
            &[],
        )
        .unwrap();
        assert!(details.is_none());
    }

    #[test]
    fn subvolume_inspection_failure_includes_command_diagnostics() {
        let root = Temp::new("inspect");
        std::fs::create_dir_all(root.path()).unwrap();
        let mut run = |_command| captured(1, "stdout detail", "ERROR: cannot access device");
        let error = subvolume_details(
            root.path().to_str().unwrap(),
            &Inspect {
                allow_plain: true,
                filesystem_type: Some("btrfs"),
                pass_fds: &[],
                opened_mode: None,
            },
            &mut run,
            &[],
        )
        .unwrap_err();
        assert!(
            error.message.contains("cannot access device"),
            "{}",
            error.message
        );
        assert!(error.message.contains("stdout detail"), "{}", error.message);
        assert!(error.is_runtime());
    }

    #[test]
    fn workspace_is_under_inferred_anchor_not_source_parent() {
        assert_eq!(
            snapshot_parent("/data").unwrap(),
            "/data/.homelab-backup-snapshots"
        );
        assert_ne!(
            snapshot_parent("/data").unwrap(),
            "/data/container/.homelab-backup-snapshots"
        );
    }

    #[test]
    fn workspace_anchor_skips_unprotected_parent() {
        let root = Temp::new("anchor");
        let source = root.path().join("container").join("world");
        std::fs::create_dir_all(&source).unwrap();
        chmod(&root.path().join("container"), 0o777);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut run = uuid_runner();
        let anchor =
            snapshot_workspace_anchor(source.to_str().unwrap(), &mut run, &mounts).unwrap();
        assert_eq!(anchor, root.path().to_str().unwrap());
    }

    #[test]
    fn workspace_anchor_rejects_without_safe_same_filesystem_parent() {
        let root = Temp::new("noanchor");
        let source = root.path().join("standalone").join("world");
        std::fs::create_dir_all(&source).unwrap();
        chmod(root.path(), 0o777);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut run = uuid_runner();
        let error =
            snapshot_workspace_anchor(source.to_str().unwrap(), &mut run, &mounts).unwrap_err();
        assert!(
            error
                .message
                .contains("mount the Btrfs top-level or a common parent"),
            "{}",
            error.message
        );
        chmod(root.path(), 0o755);
    }

    fn identity_layout(label: &str) -> (Temp, String, String, String) {
        let root = Temp::new(label);
        let anchor = root.path().to_str().unwrap().to_owned();
        let workspace = root.path().join(SNAPSHOT_DIRECTORY);
        std::fs::create_dir_all(&workspace).unwrap();
        chmod(&workspace, 0o700);
        let snapshot = workspace.join(format!("demo-{OPERATION}-world"));
        std::fs::create_dir_all(&snapshot).unwrap();
        (
            root,
            anchor,
            workspace.to_str().unwrap().to_owned(),
            snapshot.to_str().unwrap().to_owned(),
        )
    }

    #[test]
    fn cleanup_does_not_depend_on_live_source() {
        let (root, anchor, workspace, snapshot) = identity_layout("live");
        let entry = journal_entry(&anchor, "ready", &workspace, &snapshot);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut targets = Vec::new();
        let mut run = |command: BtrfsCommand| {
            if command.argv[0] == "findmnt" {
                targets.push(command.argv[5].clone());
            }
            if command.argv.get(2).map(String::as_str) == Some("show") {
                assert_eq!(command.argv[3], snapshot);
            }
            uuid_runner()(command)
        };
        let details = validate_cleanup_identity(&entry, false, &mut run, &mounts).unwrap();
        assert_eq!(details.uuid, SNAPSHOT_UUID);
        assert!(!targets.iter().any(|path| path.contains("container")));
    }

    #[test]
    fn cleanup_rejects_each_snapshot_identity_mismatch() {
        let (root, anchor, workspace, snapshot) = identity_layout("mismatch");
        let entry = journal_entry(&anchor, "ready", &workspace, &snapshot);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let repeated = "f".repeat(32);
        let cases = [
            (
                999,
                SNAPSHOT_UUID,
                SOURCE_UUID,
                true,
                "snapshot subvolume ID",
            ),
            (300, repeated.as_str(), SOURCE_UUID, true, "snapshot UUID"),
            (300, SNAPSHOT_UUID, repeated.as_str(), true, "parent UUID"),
            (300, SNAPSHOT_UUID, SOURCE_UUID, false, "not read-only"),
        ];
        for (id, uuid, parent, readonly, reason) in cases {
            let text = show_text(id, uuid, parent, readonly);
            let mut run = |command: BtrfsCommand| {
                if command.argv[0] == "findmnt" {
                    captured(0, &format!("{FILESYSTEM_UUID}\n"), "")
                } else {
                    captured(0, &text, "")
                }
            };
            let error = validate_cleanup_identity(&entry, false, &mut run, &mounts).unwrap_err();
            assert!(
                error.message.contains(reason),
                "{}: {}",
                reason,
                error.message
            );
        }
    }

    #[test]
    fn cleanup_rejects_filesystem_or_workspace_identity_mismatch() {
        let (root, anchor, workspace, snapshot) = identity_layout("fs");
        let entry = journal_entry(&anchor, "ready", &workspace, &snapshot);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut run = |_command| captured(0, &format!("{}\n", "f".repeat(32)), "");
        let error = validate_cleanup_identity(&entry, false, &mut run, &mounts).unwrap_err();
        assert!(
            error.message.contains("filesystem UUID"),
            "{}",
            error.message
        );

        let other_workspace = format!("{anchor}/container/{SNAPSHOT_DIRECTORY}");
        let other_snapshot = format!("{other_workspace}/demo-{OPERATION}-world");
        let moved = journal_entry(&anchor, "ready", &other_workspace, &other_snapshot);
        let error = entry_paths(&moved).unwrap_err();
        assert!(
            error.message.contains("unexpected snapshot path"),
            "{}",
            error.message
        );
    }

    #[test]
    fn creating_recovery_adopts_existing_snapshot_before_delete() {
        let (root, anchor, workspace, snapshot) = identity_layout("adopt");
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        write_journal(
            &state_root,
            "demo",
            serde_json::Value::Array(vec![journal_entry(
                &anchor, "creating", &workspace, &snapshot,
            )]),
        );
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut phases = Vec::new();
        let mut commands = Vec::new();
        let state_file = state_root.join(".demo.btrfs-snapshots.json");
        let mut run = |command: BtrfsCommand| {
            if command.argv.get(2).map(String::as_str) == Some("delete") {
                let saved: serde_json::Value =
                    serde_json::from_str(&std::fs::read_to_string(&state_file).unwrap()).unwrap();
                phases.push(saved["snapshots"][0]["phase"].as_str().unwrap().to_owned());
                assert_eq!(saved["snapshots"][0]["snapshot_uuid"], SNAPSHOT_UUID);
                assert_eq!(command.argv[3], snapshot);
                std::fs::remove_dir(&snapshot).unwrap();
            }
            if command.argv[0] == "btrfs" {
                commands.push(command.argv[2].clone());
            }
            uuid_runner()(command)
        };
        cleanup_in(
            state_root.to_str().unwrap(),
            "demo",
            &mut run,
            Some(&mounts),
        )
        .unwrap();
        assert_eq!(phases, vec!["deleting".to_owned()]);
        assert_eq!(
            commands,
            vec![
                "show".to_owned(),
                "show".to_owned(),
                "delete".to_owned(),
                "sync".to_owned()
            ]
        );
        assert!(!state_file.exists());
        cleanup_snapshot_state(state_root.to_str().unwrap(), "demo", &mut |_command| {
            panic!("second cleanup has no journal");
        })
        .unwrap();
    }

    #[test]
    fn creating_recovery_clears_absent_intent() {
        let (root, anchor, workspace, snapshot) = identity_layout("absent-create");
        let snapshot_path = PathBuf::from(&snapshot);
        std::fs::remove_dir(&snapshot_path).unwrap();
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        write_journal(
            &state_root,
            "demo",
            serde_json::Value::Array(vec![journal_entry(
                &anchor, "creating", &workspace, &snapshot,
            )]),
        );
        let mut run = |_command: BtrfsCommand| panic!("absent creating snapshot is not deleted");
        cleanup_in(state_root.to_str().unwrap(), "demo", &mut run, Some(&[])).unwrap();
        assert!(!state_root.join(".demo.btrfs-snapshots.json").exists());
    }

    #[test]
    fn deleting_recovery_commits_absent_snapshot() {
        let (root, anchor, workspace, snapshot) = identity_layout("absent-delete");
        std::fs::remove_dir(&snapshot).unwrap();
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        write_journal(
            &state_root,
            "demo",
            serde_json::Value::Array(vec![journal_entry(
                &anchor, "deleting", &workspace, &snapshot,
            )]),
        );
        let mut run = |_command: BtrfsCommand| panic!("btrfs was invoked");
        cleanup_in(state_root.to_str().unwrap(), "demo", &mut run, Some(&[])).unwrap();
        assert!(!state_root.join(".demo.btrfs-snapshots.json").exists());
    }

    #[test]
    fn ready_snapshot_missing_is_not_deleted_automatically() {
        let (root, anchor, workspace, snapshot) = identity_layout("missing-ready");
        std::fs::remove_dir(&snapshot).unwrap();
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        write_journal(
            &state_root,
            "demo",
            serde_json::Value::Array(vec![journal_entry(&anchor, "ready", &workspace, &snapshot)]),
        );
        let mut run = |_command: BtrfsCommand| panic!("btrfs was invoked");
        let error =
            cleanup_in(state_root.to_str().unwrap(), "demo", &mut run, Some(&[])).unwrap_err();
        assert!(
            error.message.contains("recorded snapshot is missing"),
            "{}",
            error.message
        );
    }

    #[test]
    fn delete_failure_uses_command_exit_text() {
        let (root, anchor, workspace, snapshot) = identity_layout("exit");
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        write_journal(
            &state_root,
            "demo",
            serde_json::Value::Array(vec![journal_entry(
                &anchor, "deleting", &workspace, &snapshot,
            )]),
        );
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut run = |command: BtrfsCommand| {
            if command.argv.get(2).map(String::as_str) == Some("delete") {
                captured(7, "", "delete failed")
            } else {
                uuid_runner()(command)
            }
        };
        let error = cleanup_in(
            state_root.to_str().unwrap(),
            "demo",
            &mut run,
            Some(&mounts),
        )
        .unwrap_err();
        assert_eq!(error.message, "command failed with exit code 7");
        assert!(state_root.join(".demo.btrfs-snapshots.json").exists());
    }

    #[test]
    fn snapshot_state_services_lists_sorted_journal_names() {
        let missing = Temp::new("services-missing");
        let absent = missing.path().join("no-such-state");
        assert!(
            snapshot_state_services(absent.to_str().unwrap())
                .unwrap()
                .is_empty()
        );

        let root = Temp::new("services");
        for name in [".demo.btrfs-snapshots.json", ".other.btrfs-snapshots.json"] {
            let path = root.path().join(name);
            std::fs::write(&path, "{}\n").unwrap();
            chmod(&path, 0o644);
        }
        std::fs::write(root.path().join("demo.json"), "{}\n").unwrap();
        std::fs::write(root.path().join(".btrfs-snapshots.json"), "{}\n").unwrap();
        assert_eq!(
            snapshot_state_services(root.path().to_str().unwrap()).unwrap(),
            vec!["demo".to_owned(), "other".to_owned()]
        );
    }

    fn service_manifest(dir: &Path, source: &Path, required: bool) -> BTreeMap<String, Yaml> {
        BTreeMap::from([
            ("service".to_owned(), Yaml::string("demo")),
            ("_dir".to_owned(), Yaml::string(dir.to_str().unwrap())),
            (
                "sources".to_owned(),
                Yaml::map([(
                    "paths",
                    Yaml::seq([Yaml::map([
                        ("id", Yaml::string("world")),
                        ("path", Yaml::string(source.to_str().unwrap())),
                        ("required", Yaml::Bool(required)),
                    ])]),
                )]),
            ),
        ])
    }

    #[test]
    fn transaction_journals_intent_then_uses_pinned_fd() {
        let root = Temp::new("transaction");
        let source = root.path().join("container").join("world");
        std::fs::create_dir_all(&source).unwrap();
        // Unprotected parent is skipped so the snapshot workspace stays outside it.
        chmod(&root.path().join("container"), 0o777);
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        let manifest = service_manifest(root.path(), &source, true);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let state_file = state_root.join(".demo.btrfs-snapshots.json");
        let mut phases = Vec::new();
        let mut snapshot_argv = None;
        let renamed = root.path().join("renamed-container");
        let mut run = |command: BtrfsCommand| {
            if command.argv.get(2).map(String::as_str) == Some("snapshot") {
                let saved: serde_json::Value =
                    serde_json::from_str(&std::fs::read_to_string(&state_file).unwrap()).unwrap();
                phases.push(saved["snapshots"][0]["phase"].as_str().unwrap().to_owned());
                snapshot_argv = Some(command.clone());
                let destination = PathBuf::from(&command.argv[5]);
                std::fs::create_dir_all(&destination).unwrap();
                std::fs::rename(source.parent().unwrap(), &renamed).unwrap();
            }
            uuid_runner()(command)
        };
        let mut conflicts = |_path: &str| Ok(Vec::new());
        let mut transaction = SnapshotTransaction::new(
            state_root.to_str().unwrap(),
            &manifest,
            &mut conflicts,
            &mut run,
            Some(mounts),
        );
        let overrides = transaction.create().unwrap();
        let saved: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&state_file).unwrap()).unwrap();
        assert_eq!(phases, vec!["creating".to_owned()]);
        assert_eq!(saved["snapshots"][0]["phase"], "ready");
        assert_eq!(saved["snapshots"][0]["snapshot_subvolume_id"], 300);
        let command = snapshot_argv.unwrap();
        assert_eq!(command.argv[3], "-r");
        assert_eq!(
            command.argv[4],
            format!("/proc/self/fd/{}", command.pass_fds[0])
        );
        assert_eq!(command.pass_fds.len(), 1);
        assert!(!source.exists());
        assert!(renamed.join("world").is_dir());
        assert!(overrides.contains_key("world"));
        assert_eq!(
            overrides.get("world").map(String::as_str),
            saved["snapshots"][0]["snapshot_path"].as_str()
        );
    }

    #[test]
    fn transaction_skips_plain_directories_and_optional_missing_paths() {
        let root = Temp::new("skip");
        let source = root.path().join("plain");
        std::fs::create_dir_all(&source).unwrap();
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        let manifest = service_manifest(root.path(), &source, true);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut calls = 0;
        let mut run = |command: BtrfsCommand| {
            calls += 1;
            assert_eq!(command.argv[2], "show");
            captured(1, "", "ERROR: not a subvolume")
        };
        let mut conflicts = |_path: &str| Ok(Vec::new());
        let mut transaction = SnapshotTransaction::new(
            state_root.to_str().unwrap(),
            &manifest,
            &mut conflicts,
            &mut run,
            Some(mounts.clone()),
        );
        assert!(transaction.create().unwrap().is_empty());
        assert_eq!(calls, 1);
        assert!(!state_root.join(".demo.btrfs-snapshots.json").exists());

        let missing = root.path().join("gone");
        let manifest = service_manifest(root.path(), &missing, false);
        let mut run = |_command: BtrfsCommand| panic!("optional missing path is not inspected");
        let mut transaction = SnapshotTransaction::new(
            state_root.to_str().unwrap(),
            &manifest,
            &mut conflicts,
            &mut run,
            Some(mounts),
        );
        assert!(transaction.create().unwrap().is_empty());
    }

    #[test]
    fn transaction_rejects_container_conflicts_and_writable_snapshots() {
        let root = Temp::new("conflict");
        let source = root.path().join("world");
        std::fs::create_dir_all(&source).unwrap();
        let state_root = root.path().join("state");
        std::fs::create_dir_all(&state_root).unwrap();
        chmod(&state_root, 0o755);
        let manifest = service_manifest(root.path(), &source, true);
        let mounts = vec![mount_at(root.path(), "btrfs")];
        let mut run = uuid_runner();
        let mut conflicts =
            |_path: &str| Ok(vec!["container-a".to_owned(), "container-b".to_owned()]);
        let mut transaction = SnapshotTransaction::new(
            state_root.to_str().unwrap(),
            &manifest,
            &mut conflicts,
            &mut run,
            Some(mounts.clone()),
        );
        let error = transaction.create().unwrap_err();
        assert_eq!(
            error.message,
            "Btrfs snapshot workspace is mounted by containers: container-a, container-b"
        );

        let mut run = |command: BtrfsCommand| {
            if command.argv.get(2).map(String::as_str) == Some("snapshot") {
                std::fs::create_dir_all(&command.argv[5]).unwrap();
            }
            if command.argv.get(2).map(String::as_str) == Some("show")
                && command
                    .argv
                    .get(3)
                    .is_some_and(|path| !path.contains("/proc/self/fd/"))
            {
                return captured(0, &show_text(300, SNAPSHOT_UUID, SOURCE_UUID, false), "");
            }
            uuid_runner()(command)
        };
        let mut conflicts = |_path: &str| Ok(Vec::new());
        let mut transaction = SnapshotTransaction::new(
            state_root.to_str().unwrap(),
            &manifest,
            &mut conflicts,
            &mut run,
            Some(mounts),
        );
        let error = transaction.create().unwrap_err();
        assert!(
            error.message.contains("Btrfs created a writable snapshot"),
            "{}",
            error.message
        );
    }
}
