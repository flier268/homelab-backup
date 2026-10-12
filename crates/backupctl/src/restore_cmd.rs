//! Restore download and apply orchestration.
//! Interactive selection is a testable state machine plus a TTY dialog.

#![allow(dead_code, clippy::type_complexity, clippy::too_many_arguments)]

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{self, BufRead, IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use homelab_backup_domain::manifest::{requires_docker, service_label, validate_manifest};
use homelab_backup_domain::names::valid_service_name;
use homelab_backup_domain::paths::{lexical_absolute, path_contains};
use homelab_backup_domain::yaml::{Yaml, python_str};
use serde_json::Value;

use crate::catalog::select_every_manifest;
use crate::documents::load_global_config;
use crate::files::{self, CommandError};
use crate::restore_apply::apply_one;
use crate::restore_inventory::{load_restore_inventory, restore_inventory_service_directory};
use crate::restore_plan::{parse_yaml_mapping, shell_quote};
use crate::storage::{MIN_FREE_BYTES, format_bytes, parse_snapshots, snapshots_command};
use crate::unix::control::{clear_control_leaf, ensure_private_directory};
use crate::unix::error::SecurityError;
use crate::unix::lock::GlobalLock;
use crate::unix::mount::validate_live_control_root;
use crate::unix::restic::restic_env;
use crate::volumes::{ensure_volume_helper_image, validate_docker_environment};

const SELECTOR_TITLE: &str = "Which services should be restored? (all selected by default)";
const SELECTOR_HELP: &str = "↑/↓ 移動  Space 切換  A 全選/取消  Enter 確認  Q 取消";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SelectorKey {
    Up,
    Down,
    Space,
    All,
    Enter,
    Quit,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SelectorAction {
    Continue,
    Confirm(Vec<String>),
    Cancel,
}

/// Curses selector state. Every service starts selected.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServiceSelector {
    pub items: Vec<String>,
    pub selected: Vec<bool>,
    pub index: usize,
}

impl ServiceSelector {
    pub fn new(items: &[String]) -> Self {
        let len = items.len();
        Self {
            items: items.to_vec(),
            selected: vec![true; len],
            index: 0,
        }
    }

    pub fn apply_key(&mut self, key: SelectorKey) -> SelectorAction {
        if self.items.is_empty() {
            return SelectorAction::Confirm(Vec::new());
        }
        match key {
            SelectorKey::Up => {
                self.index = if self.index == 0 {
                    self.items.len() - 1
                } else {
                    self.index - 1
                };
                SelectorAction::Continue
            }
            SelectorKey::Down => {
                self.index = (self.index + 1) % self.items.len();
                SelectorAction::Continue
            }
            SelectorKey::Space => {
                self.selected[self.index] = !self.selected[self.index];
                SelectorAction::Continue
            }
            SelectorKey::All => {
                let target = !self.selected.iter().all(|item| *item);
                self.selected.fill(target);
                SelectorAction::Continue
            }
            SelectorKey::Enter => SelectorAction::Confirm(self.confirmed()),
            SelectorKey::Quit => SelectorAction::Cancel,
        }
    }

    fn confirmed(&self) -> Vec<String> {
        self.items
            .iter()
            .zip(&self.selected)
            .filter(|(_, enabled)| **enabled)
            .map(|(item, _)| item.clone())
            .collect()
    }
}

#[derive(Default)]
struct CmdHooks {
    run: Option<Box<dyn FnMut(&[String]) -> Result<String, SecurityError>>>,
    free_bytes: Option<u64>,
    tty: Option<bool>,
    prompts: VecDeque<bool>,
    selector: Option<Box<dyn FnMut(&[String]) -> Result<Option<Vec<String>>, CommandError>>>,
    restore_id: Option<String>,
    restore_one: Option<
        Box<
            dyn FnMut(
                &BTreeMap<String, Yaml>,
                &str,
                &str,
                &str,
                bool,
            ) -> Result<(BTreeMap<String, Yaml>, PathBuf), SecurityError>,
        >,
    >,
    apply_one: Option<
        Box<
            dyn FnMut(
                &BTreeMap<String, Yaml>,
                &BTreeMap<String, Yaml>,
                &Path,
                bool,
            ) -> Result<(), SecurityError>,
        >,
    >,
    calls: Vec<Vec<String>>,
    restores: Vec<(String, String, String, bool)>,
}

thread_local! {
    static HOOKS: RefCell<CmdHooks> = RefCell::new(CmdHooks::default());
}

struct CmdGuard;

impl Drop for CmdGuard {
    fn drop(&mut self) {
        HOOKS.with(|hooks| *hooks.borrow_mut() = CmdHooks::default());
    }
}

#[derive(Default)]
struct CmdInstall {
    run: Option<Box<dyn FnMut(&[String]) -> Result<String, SecurityError>>>,
    free_bytes: Option<u64>,
    tty: Option<bool>,
    prompts: VecDeque<bool>,
    selector: Option<Box<dyn FnMut(&[String]) -> Result<Option<Vec<String>>, CommandError>>>,
    restore_id: Option<String>,
    restore_one: Option<
        Box<
            dyn FnMut(
                &BTreeMap<String, Yaml>,
                &str,
                &str,
                &str,
                bool,
            ) -> Result<(BTreeMap<String, Yaml>, PathBuf), SecurityError>,
        >,
    >,
    apply_one: Option<
        Box<
            dyn FnMut(
                &BTreeMap<String, Yaml>,
                &BTreeMap<String, Yaml>,
                &Path,
                bool,
            ) -> Result<(), SecurityError>,
        >,
    >,
}

fn install_cmd(hooks: CmdInstall) -> CmdGuard {
    HOOKS.with(|slot| {
        *slot.borrow_mut() = CmdHooks {
            run: hooks.run,
            free_bytes: hooks.free_bytes,
            tty: hooks.tty,
            prompts: hooks.prompts,
            selector: hooks.selector,
            restore_id: hooks.restore_id,
            restore_one: hooks.restore_one,
            apply_one: hooks.apply_one,
            calls: Vec::new(),
            restores: Vec::new(),
        };
    });
    CmdGuard
}

pub fn run_restore(
    config_path: &str,
    services: &[String],
    all: bool,
    snapshot: &str,
    apply: bool,
    start: bool,
    yes: bool,
    allow_low_space: bool,
    restore_manifest: bool,
    keep_manifest: bool,
) -> Result<(), CommandError> {
    let config = load_global_config(config_path).map_err(plain)?;
    run_restore_loaded(
        &config,
        services,
        all,
        snapshot,
        apply,
        start,
        yes,
        allow_low_space,
        restore_manifest,
        keep_manifest,
    )
}

pub fn run_apply(
    config_path: &str,
    service: &str,
    restore_dir: &str,
    start: bool,
    yes: bool,
) -> Result<(), CommandError> {
    if !yes {
        return Err(plain_message("apply requires --yes"));
    }
    let config = load_global_config(config_path).map_err(plain)?;
    run_apply_loaded(&config, service, restore_dir, start)
}

fn run_restore_loaded(
    config: &BTreeMap<String, Yaml>,
    services: &[String],
    all: bool,
    snapshot: &str,
    apply: bool,
    start: bool,
    yes: bool,
    allow_low_space: bool,
    restore_manifest: bool,
    keep_manifest: bool,
) -> Result<(), CommandError> {
    if start && !apply {
        return Err(plain_message("--start requires --apply"));
    }
    if !services.is_empty() && all {
        return Err(plain_message("specify services or --all, but not both"));
    }
    let available = repository_services(config).map_err(uncaught)?;
    if available.is_empty() {
        let host = config_str(config, "host_id").unwrap_or("");
        return Err(plain_message(format!(
            "no service snapshots found for host {}",
            python_str(host)
        )));
    }
    let selected = if !services.is_empty() {
        let known = available.iter().cloned().collect::<BTreeSet<_>>();
        let unknown = services
            .iter()
            .filter(|service| !known.contains(*service))
            .cloned()
            .collect::<BTreeSet<_>>();
        if !unknown.is_empty() {
            return Err(plain_message(format!(
                "services not found in repository: {}",
                unknown.into_iter().collect::<Vec<_>>().join(", ")
            )));
        }
        let mut ordered = Vec::new();
        for service in services {
            if !ordered.iter().any(|item: &String| item == service) {
                ordered.push(service.clone());
            }
        }
        ordered
    } else if all {
        available
    } else {
        match select_services_interactively(&available)? {
            None => {
                println!("Restore cancelled.");
                return Ok(());
            }
            Some(items) if items.is_empty() => {
                return Err(plain_message("no services selected"));
            }
            Some(items) => items,
        }
    };
    if snapshot != "latest" && selected.len() != 1 {
        return Err(plain_message(
            "an explicit snapshot ID can restore exactly one service",
        ));
    }
    if apply && config_str(config, "services_root").is_some() {
        let mut missing = Vec::new();
        for service in &selected {
            if !local_manifest_enabled(config, service).map_err(uncaught)? {
                missing.push(service.clone());
            }
        }
        if !missing.is_empty() {
            return Err(plain_message(format!(
                "restore --apply requires a protected local manifest for: {}; run restore without --apply, review and install _meta/backup.yaml under services_root, run backupctl validate, then run backupctl apply",
                missing.join(", ")
            )));
        }
    }
    println!("Selected services: {}", selected.join(", "));
    if !yes {
        if !stdin_tty() {
            return Err(plain_message("non-interactive restore requires --yes"));
        }
        if !prompt_yes("Continue with restore?")? {
            println!("Restore cancelled.");
            return Ok(());
        }
    }
    let snapshot = if snapshot == "latest" {
        snapshot.to_owned()
    } else {
        resolve_explicit_snapshot(config, &selected[0], snapshot)?
    };
    let policy = if restore_manifest {
        "restore"
    } else if keep_manifest {
        "keep"
    } else {
        "ask"
    };
    let mut restored = Vec::new();
    let mut failures = Vec::new();
    let mut docker_ready = false;
    {
        let lock_file = config_str(config, "lock_file")
            .ok_or_else(|| uncaught(SecurityError::runtime("missing lock_file")))?;
        let mut lock = GlobalLock::new(lock_file, false, 0);
        if !lock.enter(&mut || {}).map_err(uncaught)? {
            return Err(plain_message("another backupctl process is running"));
        }
        for service in &selected {
            println!("\n== Restore: {service} ==");
            match restore_service(config, service, &snapshot, policy, allow_low_space) {
                Ok((manifest, root)) => {
                    let label = service_label(&manifest);
                    if apply {
                        if let Err(error) =
                            apply_downloaded(config, &manifest, &root, start, &mut docker_ready)
                        {
                            record_failure(&mut failures, service, &error);
                            continue;
                        }
                        match clear_control_leaf(&root.display().to_string()) {
                            Ok(()) => {
                                println!("Applied restore for {label}; temporary restore removed");
                                restored
                                    .push((label, "applied; temporary restore removed".to_owned()));
                            }
                            Err(error) => {
                                eprintln!(
                                    "ERROR: {label} was applied, but its temporary restore could not be removed: {error}"
                                );
                                failures.push((format!("{label} cleanup"), error.message));
                                restored.push((
                                    label,
                                    format!(
                                        "applied; cleanup failed; retained at {}",
                                        root.display()
                                    ),
                                ));
                            }
                        }
                    } else {
                        restored.push((label, root.display().to_string()));
                    }
                }
                Err(error) => record_failure(&mut failures, service, &error),
            }
        }
    }
    println!("\nRESTORE SUMMARY");
    for (service, result) in &restored {
        println!("  - {service}: {result}");
    }
    if failures.is_empty() {
        return Ok(());
    }
    eprintln!("\nRESTORE FAILURES");
    for (operation, error) in failures {
        eprintln!("  - {operation}: {error}");
    }
    Err(CommandError::Reported)
}

fn run_apply_loaded(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    restore_dir: &str,
    start: bool,
) -> Result<(), CommandError> {
    let lock_file = config_str(config, "lock_file")
        .ok_or_else(|| uncaught(SecurityError::runtime("missing lock_file")))?;
    let mut lock = GlobalLock::new(lock_file, false, 0);
    if !lock.enter(&mut || {}).map_err(uncaught)? {
        return Err(plain_message("another backupctl process is running"));
    }
    let root = validated_apply_workspace(config, service, restore_dir).map_err(uncaught)?;
    let manifest = enabled_manifest(config, service).map_err(uncaught)?;
    if requires_docker(&manifest) {
        validate_docker_environment().map_err(uncaught)?;
        let image = config_str(config, "volume_helper_image").unwrap_or("");
        ensure_volume_helper_image(image).map_err(uncaught)?;
    }
    apply_downloaded(config, &manifest, &root, start, &mut true).map_err(uncaught)
}

fn apply_downloaded(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    root: &Path,
    start: bool,
    docker_ready: &mut bool,
) -> Result<(), SecurityError> {
    if requires_docker(manifest) && !*docker_ready {
        validate_docker_environment()?;
        let image = config_str(config, "volume_helper_image").unwrap_or("");
        ensure_volume_helper_image(image)?;
        *docker_ready = true;
    }
    let hooked = HOOKS.with(|hooks| hooks.borrow().apply_one.is_some());
    if hooked {
        return HOOKS.with(|hooks| {
            hooks.borrow_mut().apply_one.as_mut().expect("apply hook")(
                config, manifest, root, start,
            )
        });
    }
    apply_one(config, manifest, root, start)
}

fn restore_service(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    snapshot: &str,
    policy: &str,
    allow_low_space: bool,
) -> Result<(BTreeMap<String, Yaml>, PathBuf), SecurityError> {
    HOOKS.with(|hooks| {
        hooks.borrow_mut().restores.push((
            service.to_owned(),
            snapshot.to_owned(),
            policy.to_owned(),
            allow_low_space,
        ));
    });
    let hooked = HOOKS.with(|hooks| hooks.borrow().restore_one.is_some());
    if hooked {
        return HOOKS.with(|hooks| {
            hooks
                .borrow_mut()
                .restore_one
                .as_mut()
                .expect("restore hook")(
                config, service, snapshot, policy, allow_low_space
            )
        });
    }
    restore_one(config, service, snapshot, policy, allow_low_space)
}

pub fn select_services_interactively(
    services: &[String],
) -> Result<Option<Vec<String>>, CommandError> {
    if services.is_empty() {
        return Ok(Some(Vec::new()));
    }
    let hooked = HOOKS.with(|hooks| hooks.borrow().selector.is_some());
    if hooked {
        return HOOKS
            .with(|hooks| hooks.borrow_mut().selector.as_mut().expect("selector hook")(services));
    }
    if !stdin_tty() || !stdout_tty() {
        return Err(plain_message(
            "interactive service selection requires a TTY; specify service names or use --all",
        ));
    }
    run_selector_dialog(services)
}

fn run_selector_dialog(services: &[String]) -> Result<Option<Vec<String>>, CommandError> {
    let saved = Command::new("stty")
        .arg("-g")
        .output()
        .map_err(|error| plain_message(error.to_string()))?;
    if !saved.status.success() {
        return Err(plain_message(
            "interactive service selection requires a TTY; specify service names or use --all",
        ));
    }
    let saved = String::from_utf8_lossy(&saved.stdout).trim().to_owned();
    let _guard = SttyGuard { saved };
    let status = Command::new("stty")
        .args(["raw", "-echo", "min", "1", "time", "0"])
        .status()
        .map_err(|error| plain_message(error.to_string()))?;
    if !status.success() {
        return Err(plain_message(
            "cannot set the terminal for service selection",
        ));
    }
    let mut state = ServiceSelector::new(services);
    let mut input = io::stdin();
    loop {
        render_selector(&state);
        let key =
            read_selector_key(&mut input).map_err(|error| plain_message(error.to_string()))?;
        match state.apply_key(key) {
            SelectorAction::Continue => {}
            SelectorAction::Confirm(items) => return Ok(Some(items)),
            SelectorAction::Cancel => return Ok(None),
        }
    }
}

struct SttyGuard {
    saved: String,
}

impl Drop for SttyGuard {
    fn drop(&mut self) {
        let _ = Command::new("stty").arg(&self.saved).status();
    }
}

fn render_selector(state: &ServiceSelector) {
    println!("{SELECTOR_TITLE}");
    println!("{SELECTOR_HELP}");
    for (index, item) in state.items.iter().enumerate() {
        let mark = if state.selected[index] { "■" } else { "□" };
        let pointer = if index == state.index { "◆" } else { " " };
        println!("{pointer} {mark} {item}");
    }
    let _ = io::stdout().flush();
}

fn read_selector_key(input: &mut impl Read) -> io::Result<SelectorKey> {
    let mut first = [0u8; 1];
    if input.read(&mut first)? == 0 {
        return Ok(SelectorKey::Quit);
    }
    if first[0] == 0x1b {
        let _ = Command::new("stty")
            .args(["min", "0", "time", "1"])
            .status();
        let mut rest = [0u8; 6];
        let count = input.read(&mut rest)?;
        let _ = Command::new("stty")
            .args(["min", "1", "time", "0"])
            .status();
        if count >= 2 && rest[0] == b'[' && rest[1] == b'A' {
            return Ok(SelectorKey::Up);
        }
        if count >= 2 && rest[0] == b'[' && rest[1] == b'B' {
            return Ok(SelectorKey::Down);
        }
        return Ok(SelectorKey::Quit);
    }
    Ok(match first[0] {
        b'k' => SelectorKey::Up,
        b'j' => SelectorKey::Down,
        b' ' => SelectorKey::Space,
        b'a' | b'A' => SelectorKey::All,
        b'\n' | b'\r' => SelectorKey::Enter,
        b'q' | b'Q' => SelectorKey::Quit,
        _ => return read_selector_key(input),
    })
}

fn repository_services(config: &BTreeMap<String, Yaml>) -> Result<Vec<String>, SecurityError> {
    let host = required_str(config, "host_id")?;
    let output = invoke(&snapshots_command(host, &[]), true, config)?;
    let snapshots = parse_snapshots(&output)?;
    let mut services = BTreeSet::new();
    for snapshot in snapshots {
        let Some(tags) = snapshot.get("tags").and_then(Value::as_array) else {
            continue;
        };
        for tag in tags.iter().filter_map(Value::as_str) {
            let Some(service) = tag.strip_prefix("service:") else {
                continue;
            };
            if service.is_empty() {
                continue;
            }
            if !valid_service_name(service) {
                return Err(SecurityError::runtime(format!(
                    "invalid service tag in repository: {}",
                    python_str(tag)
                )));
            }
            services.insert(service.to_owned());
        }
    }
    Ok(services.into_iter().collect())
}

fn resolve_explicit_snapshot(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    selector: &str,
) -> Result<String, CommandError> {
    if !is_snapshot_id(selector) {
        return Err(plain_message(
            "snapshot ID must be an 8-64 character lowercase hexadecimal ID",
        ));
    }
    let host = required_str(config, "host_id").map_err(uncaught)?;
    let output = invoke(
        &snapshots_command(host, &["--tag".to_owned(), format!("service:{service}")]),
        true,
        config,
    )
    .map_err(uncaught)?;
    let snapshots = parse_snapshots(&output).map_err(uncaught)?;
    let expected = format!("service:{service}");
    let mut matches = Vec::new();
    for snapshot in snapshots {
        let Some(id) = snapshot.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !id.starts_with(selector)
            || snapshot.get("hostname").and_then(Value::as_str) != Some(host)
        {
            continue;
        }
        let tagged = snapshot
            .get("tags")
            .and_then(Value::as_array)
            .is_some_and(|tags| {
                tags.iter()
                    .any(|tag| tag.as_str() == Some(expected.as_str()))
            });
        if tagged {
            matches.push(id.to_owned());
        }
    }
    match matches.as_slice() {
        [one] => Ok(one.clone()),
        [] => Err(plain_message(format!(
            "snapshot {} does not belong to service {} on this host",
            python_str(selector),
            python_str(service)
        ))),
        _ => Err(plain_message(format!(
            "snapshot prefix {} is ambiguous for service {}",
            python_str(selector),
            python_str(service)
        ))),
    }
}

fn estimate_restore_size(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    snapshot: &str,
) -> Result<u64, SecurityError> {
    let host = required_str(config, "host_id")?;
    let output = invoke(
        &[
            "restic".to_owned(),
            "stats".to_owned(),
            "--json".to_owned(),
            "--mode".to_owned(),
            "restore-size".to_owned(),
            "--host".to_owned(),
            host.to_owned(),
            "--tag".to_owned(),
            format!("service:{service}"),
            snapshot.to_owned(),
        ],
        true,
        config,
    )?;
    let stats: Value = serde_json::from_str(&output).map_err(|error| {
        SecurityError::runtime(format!("invalid JSON from restic restore size: {error}"))
    })?;
    let size = stats.get("total_size").and_then(json_size);
    size.ok_or_else(|| SecurityError::runtime("invalid restore size returned by restic stats"))
}

fn json_size(value: &Value) -> Option<u64> {
    let Value::Number(number) = value else {
        return None;
    };
    if number.is_u64() {
        return number.as_u64();
    }
    if number.is_i64() {
        return number.as_i64().and_then(|value| u64::try_from(value).ok());
    }
    None
}

fn check_restore_space(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    snapshot: &str,
    allow_low_space: bool,
) -> Result<(), SecurityError> {
    let restore_root = required_str(config, "restore_root")?;
    let restore_root = ensure_private_directory(restore_root, false)?;
    let restore_size = estimate_restore_size(config, service, snapshot)?;
    let free = free_bytes(&restore_root)?;
    let required = restore_size.saturating_add(MIN_FREE_BYTES);
    if free >= required {
        return Ok(());
    }
    let shortfall = required - free;
    eprintln!(
        "WARNING: restore for {service} is estimated at {}, but only {} is free on {restore_root}; the required 1.00 GiB reserve would be short by {}.",
        format_bytes(restore_size),
        format_bytes(free),
        format_bytes(shortfall),
    );
    if allow_low_space {
        eprintln!("WARNING: continuing because --allow-low-space was specified.");
        return Ok(());
    }
    if stdin_tty()
        && prompt_yes("Continue despite insufficient restore space?")
            .map_err(|error| SecurityError::runtime(error.to_string()))?
    {
        return Ok(());
    }
    Err(SecurityError::runtime(
        "insufficient restore space; free additional space or explicitly use --allow-low-space",
    ))
}

fn restore_one(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    snapshot: &str,
    policy: &str,
    allow_low_space: bool,
) -> Result<(BTreeMap<String, Yaml>, PathBuf), SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "invalid service name from repository: {}",
            python_str(service)
        )));
    }
    check_restore_space(config, service, snapshot, allow_low_space)?;
    let restore_root = ensure_private_directory(required_str(config, "restore_root")?, false)?;
    let service_root = ensure_private_directory(&format!("{restore_root}/{service}"), false)?;
    let root = format!("{service_root}/{}", next_restore_id());
    let root = ensure_private_directory(&root, false)?;
    let host = required_str(config, "host_id")?;
    invoke(
        &[
            "restic".to_owned(),
            "restore".to_owned(),
            snapshot.to_owned(),
            "--host".to_owned(),
            host.to_owned(),
            "--tag".to_owned(),
            format!("service:{service}"),
            "--target".to_owned(),
            root.clone(),
        ],
        false,
        config,
    )?;
    let manifest = prepare_restored_manifest(config, service, Path::new(&root), policy)?;
    println!("Restored snapshot for {}: {root}", service_label(&manifest));
    Ok((manifest, PathBuf::from(root)))
}

fn prepare_restored_manifest(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    root: &Path,
    policy: &str,
) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "invalid service name from repository: {}",
            python_str(service)
        )));
    }
    let source = root.join("_meta").join("backup.yaml");
    let services_root = lexical_absolute(required_str(config, "services_root")?)
        .map_err(|error| SecurityError::invalid(error.message))?;
    let (service_dir, relative_dir) = if let Some(local) =
        find_local_manifest(config, service, true)?
    {
        let directory = local
            .get("_dir")
            .and_then(Yaml::as_str)
            .ok_or_else(|| SecurityError::invalid("manifest is missing _dir"))?;
        let relative = local
            .get("_relative_dir")
            .and_then(Yaml::as_str)
            .unwrap_or(service);
        (
            lexical_absolute(directory).map_err(|error| SecurityError::invalid(error.message))?,
            relative.to_owned(),
        )
    } else {
        let relative = snapshot_service_relative_directory(root, service)?;
        let joined = if services_root.ends_with('/') {
            format!("{services_root}{relative}")
        } else {
            format!("{services_root}/{relative}")
        };
        (
            lexical_absolute(&joined).map_err(|error| SecurityError::invalid(error.message))?,
            relative,
        )
    };
    if !path_contains(&services_root, &service_dir)
        .map_err(|error| SecurityError::invalid(error.message))?
    {
        return Err(SecurityError::invalid(format!(
            "restored service directory escapes services_root: {service_dir}"
        )));
    }
    let target = format!("{service_dir}/backup.yaml");
    let target_exists = std::fs::metadata(&target).is_ok();
    let restore_it = if !target_exists || policy == "restore" {
        true
    } else if policy == "keep" {
        false
    } else {
        prompt_yes(&format!(
            "{target} already exists. Restore backup.yaml from the snapshot?"
        ))
        .map_err(|error| SecurityError::runtime(error.to_string()))?
    };
    let manifest_source = if restore_it {
        source.clone()
    } else {
        PathBuf::from(&target)
    };
    let text = match crate::restore_inventory::read_unprotected_text(
        &manifest_source.display().to_string(),
    ) {
        Ok(text) => text,
        Err(error) if error.is_missing_path() => {
            return Err(SecurityError::runtime(format!(
                "restored snapshot does not contain {}; cannot reconstruct backup manifest",
                manifest_source.display()
            )));
        }
        Err(error) => return Err(error),
    };
    let parsed =
        parse_yaml_mapping(&manifest_source.display().to_string(), &text).map_err(|error| {
            if error.message.contains("must be a mapping") {
                error
            } else {
                SecurityError::invalid(format!(
                    "{}: invalid manifest YAML: {}",
                    manifest_source.display(),
                    error.message
                ))
            }
        })?;
    let mut manifest = match parsed {
        Yaml::Map(map) => map,
        Yaml::Null => BTreeMap::new(),
        _ => {
            return Err(SecurityError::invalid(format!(
                "{}: manifest must be a mapping",
                manifest_source.display()
            )));
        }
    };
    manifest.insert("_path".to_owned(), Yaml::string(&target));
    manifest.insert("_dir".to_owned(), Yaml::string(&service_dir));
    manifest.insert("_relative_dir".to_owned(), Yaml::string(&relative_dir));
    if manifest.get("service").and_then(Yaml::as_str) != Some(service) {
        return Err(SecurityError::invalid(format!(
            "{target}: service is {}, expected {}",
            python_maybe(manifest.get("service").and_then(Yaml::as_str)),
            python_str(service)
        )));
    }
    validate_manifest(&Yaml::Map(manifest.clone()))
        .map_err(|error| SecurityError::invalid(error.message))?;
    manifest.insert(
        "_snapshot_manifest".to_owned(),
        Yaml::string(source.display().to_string()),
    );
    manifest.insert(
        "_restore_manifest_requested".to_owned(),
        Yaml::Bool(restore_it),
    );
    if !restore_it {
        println!("Keeping local manifest: {target}");
    }
    Ok(manifest)
}

fn snapshot_service_relative_directory(
    root: &Path,
    service: &str,
) -> Result<String, SecurityError> {
    let inventory = load_restore_inventory(root, Some(service))?;
    let mut raw = inventory.to_snapshot_dict();
    if let Some(object) = raw.as_object_mut() {
        if inventory.relative_directory_set {
            object.insert(
                "service_relative_directory".to_owned(),
                inventory
                    .service_relative_directory
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
        } else {
            object.remove("service_relative_directory");
        }
    }
    restore_inventory_service_directory(&raw, service)
}

fn validated_apply_workspace(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    value: &str,
) -> Result<PathBuf, SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "invalid service name: {}",
            python_str(service)
        )));
    }
    let restore_root = lexical_absolute(required_str(config, "restore_root")?).map_err(invalid)?;
    let candidate = lexical_absolute(value).map_err(invalid)?;
    let restore_id = candidate
        .rsplit_once('/')
        .map(|(_, name)| name)
        .unwrap_or(&candidate);
    if !is_restore_id(restore_id) {
        return Err(SecurityError::invalid(format!(
            "invalid restore directory {}; expected {restore_root}/{service}/RESTORE_ID",
            python_str(value)
        )));
    }
    let expected = format!("{restore_root}/{service}/{restore_id}");
    if candidate != expected {
        return Err(SecurityError::invalid(format!(
            "restore directory must be under configured restore root: {expected}"
        )));
    }
    validate_live_control_root(&restore_root, false)?;
    validate_live_control_root(&format!("{restore_root}/{service}"), false)?;
    validate_live_control_root(&candidate, false)?;
    Ok(PathBuf::from(candidate))
}

fn find_local_manifest(
    config: &BTreeMap<String, Yaml>,
    service: &str,
    include_disabled: bool,
) -> Result<Option<BTreeMap<String, Yaml>>, SecurityError> {
    if !valid_service_name(service) || config_str(config, "services_root").is_none() {
        return Ok(None);
    }
    let (ready, failures) = select_every_manifest(config)?;
    if let Some(failure) = failures.iter().find(|failure| {
        failure
            .summary
            .contains(&format!("duplicate service ID '{service}'"))
    }) {
        let message = failure
            .summary
            .trim_start_matches("manifest loading failed: ")
            .to_owned();
        return Err(SecurityError::invalid(message));
    }
    Ok(ready.into_iter().find_map(|item| {
        let matches = item.manifest.get("service").and_then(Yaml::as_str) == Some(service);
        let disabled = item.manifest.get("enabled").and_then(Yaml::as_bool) == Some(false);
        if matches && (include_disabled || !disabled) {
            Some(item.manifest)
        } else {
            None
        }
    }))
}

fn local_manifest_enabled(
    config: &BTreeMap<String, Yaml>,
    service: &str,
) -> Result<bool, SecurityError> {
    Ok(find_local_manifest(config, service, false)?.is_some())
}

fn enabled_manifest(
    config: &BTreeMap<String, Yaml>,
    service: &str,
) -> Result<BTreeMap<String, Yaml>, SecurityError> {
    if !valid_service_name(service) {
        return Err(SecurityError::invalid(format!(
            "unknown or disabled service: {service}"
        )));
    }
    find_local_manifest(config, service, false)?
        .ok_or_else(|| SecurityError::invalid(format!("unknown or disabled service: {service}")))
}

fn invoke(
    argv: &[String],
    capture: bool,
    config: &BTreeMap<String, Yaml>,
) -> Result<String, SecurityError> {
    HOOKS.with(|hooks| hooks.borrow_mut().calls.push(argv.to_vec()));
    println!(
        "+ {}",
        argv.iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    );
    let hooked = HOOKS.with(|hooks| hooks.borrow().run.is_some());
    if hooked {
        return HOOKS.with(|hooks| hooks.borrow_mut().run.as_mut().expect("run hook")(argv));
    }
    let flat = files::flat_strings(config);
    let environment = restic_env(&flat)?;
    let program = argv.first().map(String::as_str).unwrap_or("restic");
    let args = if argv.is_empty() { &[] } else { &argv[1..] };
    files::run_restic(program, args, None, &environment, capture)
}

fn free_bytes(path: &str) -> Result<u64, SecurityError> {
    if let Some(value) = HOOKS.with(|hooks| hooks.borrow().free_bytes) {
        return Ok(value);
    }
    let stat =
        rustix::fs::statvfs(path).map_err(|error| SecurityError::runtime(error.to_string()))?;
    Ok(stat.f_bavail.saturating_mul(stat.f_frsize))
}

fn next_restore_id() -> String {
    if let Some(value) = HOOKS.with(|hooks| hooks.borrow().restore_id.clone()) {
        return value;
    }
    let now = chrono::Local::now();
    format!(
        "{}-{:09}",
        now.format("%Y%m%d-%H%M%S"),
        now.timestamp_subsec_nanos()
    )
}

fn prompt_yes(question: &str) -> Result<bool, CommandError> {
    if !stdin_tty() {
        return Ok(false);
    }
    if let Some(answer) = HOOKS.with(|hooks| hooks.borrow_mut().prompts.pop_front()) {
        println!("{question} [y/N] ");
        return Ok(answer);
    }
    let mut input = io::stdin().lock();
    let mut line = String::new();
    loop {
        print!("{question} [y/N] ");
        let _ = io::stdout().flush();
        line.clear();
        if input
            .read_line(&mut line)
            .map_err(|error| plain_message(error.to_string()))?
            == 0
        {
            return Ok(false);
        }
        match line.trim().to_ascii_lowercase().as_str() {
            "" | "n" | "no" => return Ok(false),
            "y" | "yes" => return Ok(true),
            _ => println!("Please answer y or n."),
        }
    }
}

fn stdin_tty() -> bool {
    HOOKS
        .with(|hooks| hooks.borrow().tty)
        .unwrap_or_else(|| io::stdin().is_terminal())
}

fn stdout_tty() -> bool {
    HOOKS
        .with(|hooks| hooks.borrow().tty)
        .unwrap_or_else(|| io::stdout().is_terminal())
}

fn record_failure(failures: &mut Vec<(String, String)>, service: &str, error: &SecurityError) {
    if let Some(code) = error
        .message
        .strip_prefix("command failed with exit code ")
        .and_then(|value| value.parse::<i32>().ok())
    {
        eprintln!("ERROR: Restore failed for {service}");
        eprintln!("ERROR: command exited with status {code}");
    } else {
        eprintln!("ERROR: restore failed for {service}: {}", error.message);
    }
    failures.push((service.to_owned(), error.message.clone()));
}

fn is_snapshot_id(value: &str) -> bool {
    (8..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_restore_id(value: &str) -> bool {
    let mut parts = value.split('-');
    let Some(date) = parts.next() else {
        return false;
    };
    let Some(time) = parts.next() else {
        return false;
    };
    let Some(nanos) = parts.next() else {
        return false;
    };
    parts.next().is_none()
        && date.len() == 8
        && date.bytes().all(|byte| byte.is_ascii_digit())
        && time.len() == 6
        && time.bytes().all(|byte| byte.is_ascii_digit())
        && nanos.len() == 9
        && nanos.bytes().all(|byte| byte.is_ascii_digit())
}

fn config_str<'a>(config: &'a BTreeMap<String, Yaml>, key: &str) -> Option<&'a str> {
    config
        .get(key)
        .and_then(Yaml::as_str)
        .filter(|value| !value.is_empty())
}

fn required_str<'a>(
    config: &'a BTreeMap<String, Yaml>,
    key: &str,
) -> Result<&'a str, SecurityError> {
    config_str(config, key).ok_or_else(|| SecurityError::runtime(format!("missing {key}")))
}

fn python_maybe(value: Option<&str>) -> String {
    match value {
        Some(value) => python_str(value),
        None => "None".to_owned(),
    }
}

fn invalid(error: homelab_backup_domain::error::PolicyError) -> SecurityError {
    SecurityError::invalid(error.message)
}

fn plain(error: SecurityError) -> CommandError {
    CommandError::Text(error.message)
}

fn plain_message(message: impl Into<String>) -> CommandError {
    CommandError::Text(message.into())
}

fn uncaught(error: SecurityError) -> CommandError {
    let kind = if error.is_runtime() {
        "RuntimeError"
    } else {
        "ValueError"
    };
    let mut message = format!("{kind}: {}", error.message);
    if let Some(command) = &error.suggested_command {
        message.push_str("\n    suggested fix: ");
        message.push_str(
            &command
                .iter()
                .map(|part| shell_quote(part))
                .collect::<Vec<_>>()
                .join(" "),
        );
    } else if let Some(action) = &error.suggested_action {
        message.push_str("\n    suggested action: ");
        message.push_str(action);
    }
    message.push_str("\nRun with BACKUPCTL_DEBUG=1 to display a Python traceback.");
    CommandError::Text(message)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static TEMP: AtomicU64 = AtomicU64::new(0);

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "backupctl-restore-{label}-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).unwrap();
        mode(&path, 0o755);
        path
    }

    fn yaml_config(pairs: &[(&str, &str)]) -> BTreeMap<String, Yaml> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), Yaml::string(*value)))
            .collect()
    }

    fn text_error(error: CommandError) -> String {
        match error {
            CommandError::Text(message) => message,
            CommandError::Reported => "reported".to_owned(),
            CommandError::Exit(code) => format!("exit {code}"),
        }
    }

    fn manifest_text() -> &'static str {
        "version: 1\nservice: advent-plus\nname: \"Advent Plus\"\nschedule:\n  cron: \"0 0 * * *\"\nretention:\n  keep_last: 1\nconsistency:\n  mode: external\nsources:\n  paths: []\n  volumes: []\n"
    }

    fn write_inventory(root: &Path, service: &str, relative: Option<&str>) {
        let meta = root.join("_meta");
        std::fs::create_dir_all(&meta).unwrap();
        let mut inventory = serde_json::json!({
            "version": 1,
            "service": service,
            "paths": [],
            "volumes": [],
            "compose": {
                "project_name": service,
                "services": [],
                "compose_files": ["compose.yaml"],
                "volumes": []
            }
        });
        if let Some(relative) = relative {
            inventory.as_object_mut().unwrap().insert(
                "service_relative_directory".to_owned(),
                Value::String(relative.to_owned()),
            );
        }
        std::fs::write(
            meta.join("inventory.json"),
            serde_json::to_vec(&inventory).unwrap(),
        )
        .unwrap();
    }

    fn mode(path: &Path, bits: u32) {
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(bits));
    }

    #[test]
    fn selector_starts_selected_and_confirms_the_toggled_subset() {
        let mut selector = ServiceSelector::new(&["one".to_owned(), "two".to_owned()]);
        assert_eq!(
            selector.apply_key(SelectorKey::Space),
            SelectorAction::Continue
        );
        assert_eq!(
            selector.apply_key(SelectorKey::Down),
            SelectorAction::Continue
        );
        assert_eq!(
            selector.apply_key(SelectorKey::Enter),
            SelectorAction::Confirm(vec!["two".to_owned()])
        );
        assert_eq!(
            selector.apply_key(SelectorKey::All),
            SelectorAction::Continue
        );
        assert!(selector.selected.iter().all(|item| *item));
        assert_eq!(
            selector.apply_key(SelectorKey::All),
            SelectorAction::Continue
        );
        assert!(selector.selected.iter().all(|item| !*item));
        assert_eq!(
            selector.apply_key(SelectorKey::Quit),
            SelectorAction::Cancel
        );
    }

    #[test]
    fn empty_selector_does_not_require_a_tty() {
        let selected = select_services_interactively(&[]).unwrap();
        assert_eq!(selected, Some(Vec::new()));
    }

    #[test]
    fn interactive_selection_without_a_tty_names_the_flags() {
        let error = select_services_interactively(&["demo".to_owned()]).unwrap_err();
        assert!(text_error(error).contains("interactive service selection requires a TTY"));
    }

    #[test]
    fn invalid_service_tag_is_rejected() {
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(
                |_| Ok(r#"[{"tags":["service:/etc"]}]"#.to_owned()),
            )),
            ..Default::default()
        });
        let error = repository_services(&yaml_config(&[("host_id", "host")])).unwrap_err();
        assert!(error.message.contains("invalid service tag"), "{error}");
    }

    #[test]
    fn explicit_snapshot_is_resolved_for_the_requested_service() {
        let snapshot = "a".repeat(64);
        let payload = serde_json::json!([{
            "id": snapshot,
            "hostname": "host",
            "tags": ["service:demo"]
        }]);
        let body = payload.to_string();
        let seen = std::rc::Rc::new(std::cell::RefCell::new(Vec::<Vec<String>>::new()));
        let captured = std::rc::Rc::clone(&seen);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(move |argv| {
                captured.borrow_mut().push(argv.to_vec());
                Ok(body.clone())
            })),
            ..Default::default()
        });
        let resolved =
            resolve_explicit_snapshot(&yaml_config(&[("host_id", "host")]), "demo", &"a".repeat(8))
                .unwrap();
        assert_eq!(resolved, snapshot);
        let calls = seen.borrow().clone();
        assert!(calls[0].contains(&"service:demo".to_owned()), "{calls:?}");
    }

    #[test]
    fn explicit_snapshot_from_another_service_is_rejected() {
        let payload = serde_json::json!([{
            "id": "a".repeat(64),
            "hostname": "host",
            "tags": ["service:other"]
        }]);
        let body = payload.to_string();
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(move |_| Ok(body.clone()))),
            ..Default::default()
        });
        let error = text_error(
            resolve_explicit_snapshot(&yaml_config(&[("host_id", "host")]), "demo", "aaaaaaaa")
                .unwrap_err(),
        );
        assert!(error.contains("does not belong to service"), "{error}");
    }

    #[test]
    fn invalid_explicit_snapshot_id_is_rejected_before_repository_access() {
        let seen = std::rc::Rc::new(std::cell::RefCell::new(0u32));
        let count = std::rc::Rc::clone(&seen);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(move |_| {
                *count.borrow_mut() += 1;
                Ok("[]".to_owned())
            })),
            ..Default::default()
        });
        let error = resolve_explicit_snapshot(
            &yaml_config(&[("host_id", "host")]),
            "demo",
            "latest:subfolder",
        )
        .unwrap_err();
        assert!(text_error(error).contains("lowercase hexadecimal"));
        assert_eq!(*seen.borrow(), 0);
    }

    #[test]
    fn restore_size_is_scoped_to_service_snapshot_and_host() {
        let seen = std::rc::Rc::new(std::cell::RefCell::new(Vec::<Vec<String>>::new()));
        let captured = std::rc::Rc::clone(&seen);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(move |argv| {
                captured.borrow_mut().push(argv.to_vec());
                Ok(r#"{"total_size":123456,"total_file_count":7}"#.to_owned())
            })),
            ..Default::default()
        });
        let size = estimate_restore_size(&yaml_config(&[("host_id", "host")]), "demo", "01234567")
            .unwrap();
        assert_eq!(size, 123456);
        let calls = seen.borrow().clone();
        assert_eq!(
            calls[0],
            vec![
                "restic",
                "stats",
                "--json",
                "--mode",
                "restore-size",
                "--host",
                "host",
                "--tag",
                "service:demo",
                "01234567",
            ]
        );
    }

    #[test]
    fn invalid_restore_size_output_is_rejected() {
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(|_| Ok(r#"{"total_size":-1}"#.to_owned()))),
            ..Default::default()
        });
        let error = estimate_restore_size(&yaml_config(&[("host_id", "host")]), "demo", "latest")
            .unwrap_err();
        assert!(error.message.contains("restore size"), "{error}");
    }

    #[test]
    fn restored_manifest_uses_recorded_relative_directory_and_local_precedence() {
        let root = temp_dir("manifest");
        let services = root.join("services");
        std::fs::create_dir_all(&services).unwrap();
        mode(&services, 0o755);
        let restored = root.join("restored");
        std::fs::create_dir_all(restored.join("_meta")).unwrap();
        std::fs::write(restored.join("_meta").join("backup.yaml"), manifest_text()).unwrap();
        write_inventory(&restored, "advent-plus", Some("Minecraft/Advent Plus"));
        let config = yaml_config(&[("services_root", &services.display().to_string())]);
        let value =
            prepare_restored_manifest(&config, "advent-plus", &restored, "restore").unwrap();
        assert_eq!(
            value.get("_relative_dir").and_then(Yaml::as_str),
            Some("Minecraft/Advent Plus")
        );
        assert!(
            value
                .get("_path")
                .and_then(Yaml::as_str)
                .unwrap()
                .ends_with("Minecraft/Advent Plus/backup.yaml")
        );

        let legacy = root.join("legacy");
        std::fs::create_dir_all(legacy.join("_meta")).unwrap();
        std::fs::write(legacy.join("_meta").join("backup.yaml"), manifest_text()).unwrap();
        write_inventory(&legacy, "advent-plus", None);
        let legacy_value =
            prepare_restored_manifest(&config, "advent-plus", &legacy, "restore").unwrap();
        assert_eq!(
            legacy_value.get("_relative_dir").and_then(Yaml::as_str),
            Some("advent-plus")
        );

        let local_dir = services.join("Current Location").join("Server");
        std::fs::create_dir_all(&local_dir).unwrap();
        mode(&services.join("Current Location"), 0o755);
        mode(&local_dir, 0o755);
        let local = local_dir.join("backup.yaml");
        std::fs::write(&local, manifest_text()).unwrap();
        mode(&local, 0o600);
        let kept = prepare_restored_manifest(&config, "advent-plus", &restored, "keep").unwrap();
        assert_eq!(
            kept.get("_relative_dir").and_then(Yaml::as_str),
            Some("Current Location/Server")
        );
        assert_eq!(
            kept.get("_path").and_then(Yaml::as_str),
            Some(local.to_str().unwrap())
        );
    }

    #[test]
    fn unsafe_relative_directory_and_manifest_symlink_are_rejected() {
        let root = temp_dir("unsafe");
        let services = root.join("services");
        std::fs::create_dir_all(&services).unwrap();
        mode(&services, 0o755);
        let restored = root.join("restored");
        std::fs::create_dir_all(restored.join("_meta")).unwrap();
        std::fs::write(restored.join("_meta").join("backup.yaml"), manifest_text()).unwrap();
        write_inventory(&restored, "advent-plus", Some("../outside"));
        let config = yaml_config(&[("services_root", &services.display().to_string())]);
        let error =
            prepare_restored_manifest(&config, "advent-plus", &restored, "restore").unwrap_err();
        assert!(!error.is_runtime(), "{error}");
        assert!(!root.join("outside").exists());

        let linked = root.join("linked");
        std::fs::create_dir_all(linked.join("_meta")).unwrap();
        write_inventory(&linked, "demo", None);
        let outside = root.join("outside.yaml");
        std::fs::write(&outside, "version: 1\nservice: demo\n").unwrap();
        symlink(&outside, linked.join("_meta").join("backup.yaml")).unwrap();
        let error = prepare_restored_manifest(&config, "demo", &linked, "restore").unwrap_err();
        assert!(error.message.contains("regular file"), "{error}");
    }

    #[test]
    fn invalid_snapshot_manifest_does_not_replace_local_manifest() {
        let root = temp_dir("invalid-manifest");
        let restored = root.join("restored");
        std::fs::create_dir_all(restored.join("_meta")).unwrap();
        std::fs::write(
            restored.join("_meta").join("backup.yaml"),
            "version: 1\nservice: wrong\n",
        )
        .unwrap();
        write_inventory(&restored, "demo", None);
        let service_dir = root.join("services").join("demo");
        std::fs::create_dir_all(&service_dir).unwrap();
        mode(&root.join("services"), 0o755);
        mode(&service_dir, 0o755);
        let target = service_dir.join("backup.yaml");
        std::fs::write(&target, "original local manifest\n").unwrap();
        mode(&target, 0o600);
        let config = yaml_config(&[(
            "services_root",
            &root.join("services").display().to_string(),
        )]);
        let error = prepare_restored_manifest(&config, "demo", &restored, "restore").unwrap_err();
        assert!(!error.is_runtime(), "{error}");
        assert_eq!(
            std::fs::read_to_string(target).unwrap(),
            "original local manifest\n"
        );
    }

    #[test]
    fn low_space_restore_requires_explicit_override_without_tty() {
        let root = temp_dir("space");
        let restore_root = root.join("restores");
        let config = yaml_config(&[
            ("host_id", "host"),
            ("restore_root", &restore_root.display().to_string()),
        ]);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(|_| {
                Ok(format!(r#"{{"total_size":{}}}"#, 2 * MIN_FREE_BYTES))
            })),
            free_bytes: Some(3 * MIN_FREE_BYTES - 1),
            tty: Some(false),
            ..Default::default()
        });
        let error = check_restore_space(&config, "demo", "latest", false).unwrap_err();
        assert!(error.message.contains("--allow-low-space"), "{error}");
        check_restore_space(&config, "demo", "latest", true).unwrap();
        let _enough = install_cmd(CmdInstall {
            run: Some(Box::new(|_| {
                Ok(format!(r#"{{"total_size":{}}}"#, 2 * MIN_FREE_BYTES))
            })),
            free_bytes: Some(3 * MIN_FREE_BYTES),
            tty: Some(false),
            ..Default::default()
        });
        check_restore_space(&config, "demo", "latest", false).unwrap();
    }

    #[test]
    fn restore_command_guards_run_before_download() {
        let error = run_restore_loaded(
            &yaml_config(&[]),
            &["demo".to_owned()],
            true,
            "latest",
            false,
            false,
            true,
            false,
            false,
            true,
        )
        .unwrap_err();
        assert!(text_error(error).contains("specify services or --all"));

        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(|_| {
                Ok(r#"[{"id":"aaaaaaaaaaaaaaaa","hostname":"host","tags":["service:one"]},{"id":"bbbbbbbbbbbbbbbb","hostname":"host","tags":["service:two"]}]"#.to_owned())
            })),
            ..Default::default()
        });
        let error = run_restore_loaded(
            &yaml_config(&[("host_id", "host")]),
            &["one".to_owned(), "two".to_owned()],
            false,
            &"a".repeat(8),
            false,
            false,
            true,
            false,
            false,
            true,
        )
        .unwrap_err();
        assert!(text_error(error).contains("exactly one service"));
        assert!(HOOKS.with(|hooks| hooks.borrow().restores.is_empty()));

        let error = run_restore_loaded(
            &yaml_config(&[("host_id", "host")]),
            &["one".to_owned()],
            false,
            "latest",
            true,
            false,
            false,
            false,
            false,
            false,
        )
        .unwrap_err();
        assert!(text_error(error).contains("non-interactive restore requires --yes"));
    }

    #[test]
    fn one_service_failure_does_not_skip_later_services() {
        let root = temp_dir("partial");
        let lock = root.join("lock");
        let config = yaml_config(&[
            ("host_id", "host"),
            ("lock_file", &lock.display().to_string()),
        ]);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(|_| {
                Ok(r#"[{"tags":["service:one"]},{"tags":["service:two"]}]"#.to_owned())
            })),
            restore_one: Some(Box::new(|_config, service, _snapshot, _policy, _allow| {
                if service == "one" {
                    return Err(SecurityError::runtime("first failed"));
                }
                Ok((BTreeMap::new(), PathBuf::from(service)))
            })),
            ..Default::default()
        });
        let error = run_restore_loaded(
            &config,
            &[],
            true,
            "latest",
            false,
            false,
            true,
            false,
            false,
            true,
        )
        .unwrap_err();
        assert!(matches!(error, CommandError::Reported));
        let restores = HOOKS.with(|hooks| hooks.borrow().restores.clone());
        assert_eq!(
            restores
                .iter()
                .map(|item| item.0.as_str())
                .collect::<Vec<_>>(),
            vec!["one", "two"]
        );
    }

    #[test]
    fn explicit_snapshot_is_resolved_before_download_and_low_space_is_forwarded() {
        let root = temp_dir("forward");
        let lock = root.join("lock");
        let snapshot = "a".repeat(64);
        let payload =
            format!(r#"[{{"id":"{snapshot}","hostname":"host","tags":["service:demo"]}}]"#);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(move |_| Ok(payload.clone()))),
            restore_one: Some(Box::new(|_config, _service, _snapshot, _policy, _allow| {
                Ok((BTreeMap::new(), PathBuf::from("/restore/demo")))
            })),
            ..Default::default()
        });
        let config = yaml_config(&[
            ("host_id", "host"),
            ("lock_file", &lock.display().to_string()),
        ]);
        run_restore_loaded(
            &config,
            &["demo".to_owned()],
            false,
            &"a".repeat(8),
            false,
            false,
            true,
            true,
            false,
            true,
        )
        .unwrap();
        let restores = HOOKS.with(|hooks| hooks.borrow().restores.clone());
        assert_eq!(restores[0].1, snapshot);
        assert!(restores[0].3);
        assert_eq!(restores[0].2, "keep");
    }

    #[test]
    fn missing_local_manifest_rejects_combined_apply_before_download() {
        let root = temp_dir("missing-local");
        let services = root.join("services");
        let second = services.join("two");
        std::fs::create_dir_all(&second).unwrap();
        mode(&services, 0o755);
        mode(&second, 0o755);
        std::fs::write(second.join("backup.yaml"), "service: two\n").unwrap();
        mode(&second.join("backup.yaml"), 0o600);
        let config = yaml_config(&[
            ("host_id", "host"),
            ("lock_file", &root.join("lock").display().to_string()),
            ("services_root", &services.display().to_string()),
        ]);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(|_| {
                Ok(r#"[{"tags":["service:one"]},{"tags":["service:two"]}]"#.to_owned())
            })),
            restore_one: Some(Box::new(|_c, _s, _snap, _p, _a| {
                Ok((BTreeMap::new(), PathBuf::from("/unused")))
            })),
            ..Default::default()
        });
        let error = run_restore_loaded(
            &config,
            &[],
            true,
            "latest",
            true,
            false,
            true,
            false,
            false,
            true,
        )
        .unwrap_err();
        assert!(text_error(error).contains("protected local manifest"));
        assert!(HOOKS.with(|hooks| hooks.borrow().restores.is_empty()));
    }

    #[test]
    fn successful_apply_removes_downloaded_restore() {
        let root = temp_dir("cleanup-download");
        let restored = root
            .join("restores")
            .join("demo")
            .join("20260717-120000-000000001");
        std::fs::create_dir_all(&restored).unwrap();
        for directory in [
            root.join("restores"),
            restored.parent().unwrap().to_path_buf(),
            restored.clone(),
        ] {
            mode(&directory, 0o700);
        }
        std::fs::write(restored.join("payload"), "restored").unwrap();
        let mut manifest_for_hook = BTreeMap::new();
        manifest_for_hook.insert("service".to_owned(), Yaml::string("demo"));
        manifest_for_hook.insert(
            "consistency".to_owned(),
            Yaml::map([("mode", Yaml::string("files"))]),
        );
        let restored_for_hook = restored.clone();
        let config = yaml_config(&[
            ("host_id", "host"),
            ("lock_file", &root.join("lock").display().to_string()),
        ]);
        let _guard = install_cmd(CmdInstall {
            run: Some(Box::new(
                |_| Ok(r#"[{"tags":["service:demo"]}]"#.to_owned()),
            )),
            restore_one: Some(Box::new(move |_c, _s, _snap, _p, _a| {
                Ok((manifest_for_hook.clone(), restored_for_hook.clone()))
            })),
            apply_one: Some(Box::new(|_c, _m, _root, _start| Ok(()))),
            ..Default::default()
        });
        run_restore_loaded(
            &config,
            &["demo".to_owned()],
            false,
            "latest",
            true,
            false,
            true,
            false,
            false,
            true,
        )
        .unwrap();
        assert!(!restored.exists());
    }

    #[test]
    fn apply_rejects_bad_workspace_and_requires_yes() {
        let error =
            run_apply("/no/such/config.yaml", "demo", "/tmp/restore", false, false).unwrap_err();
        assert_eq!(text_error(error), "apply requires --yes");
        let root = temp_dir("apply-workspace");
        let restore_root = root.join("restores");
        let workspace = restore_root.join("demo").join("latest");
        std::fs::create_dir_all(&workspace).unwrap();
        let config = yaml_config(&[("restore_root", &restore_root.display().to_string())]);
        let error = validated_apply_workspace(&config, "demo", &workspace.display().to_string())
            .unwrap_err();
        assert!(
            error.message.contains("invalid restore directory"),
            "{error}"
        );

        let outside = root.join("outside").join("20260717-120000-000000001");
        std::fs::create_dir_all(&outside).unwrap();
        mode(&restore_root, 0o700);
        let error =
            validated_apply_workspace(&config, "demo", &outside.display().to_string()).unwrap_err();
        assert!(error.message.contains("configured restore root"), "{error}");

        let service_root = restore_root.join("demo");
        std::fs::create_dir_all(&service_root).unwrap();
        mode(&service_root, 0o700);
        let link = service_root.join("20260717-120000-000000001");
        symlink(&outside, &link).unwrap();
        let error =
            validated_apply_workspace(&config, "demo", &link.display().to_string()).unwrap_err();
        assert!(error.message.contains("not a real directory"), "{error}");
    }
}
