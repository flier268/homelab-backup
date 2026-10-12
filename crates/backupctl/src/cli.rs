//! Command line matching the existing `backupctl` subcommands.
//! Handlers land with the modules they belong to. Until then they fail closed.

use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "backupctl", version, about = "Homelab Docker backup")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// List configured services.
    List,
    /// Report backup freshness and alert when a success is too old.
    Status {
        /// Alert when the last success is older than this (e.g. 36h).
        #[arg(long, value_name = "DURATION")]
        max_age: Option<String>,
        /// Send the result to alert.notify_url_file (ntfy/healthchecks).
        #[arg(long)]
        notify: bool,
    },
    /// Validate the global config and every service manifest.
    Validate,
    /// Create a new restic repository.
    Init,
    /// Back up the selected services, or every due-independent selection.
    Backup {
        services: Vec<String>,
        /// Continue even if staging would leave less than 1 GiB free.
        #[arg(long)]
        allow_low_space: bool,
    },
    /// Back up services whose schedule is due.
    RunDue,
    /// List restic snapshots.
    Snapshots { service: Option<String> },
    /// Delete one snapshot.
    DeleteSnapshot {
        service: String,
        /// 8-64 character snapshot ID or prefix.
        snapshot: String,
        /// Immediately reclaim repository space after deleting the snapshot.
        #[arg(long)]
        prune: bool,
        /// Confirm deletion; required whenever stdin is not a TTY.
        #[arg(long)]
        yes: bool,
    },
    /// Download a snapshot into the private restore workspace.
    Restore {
        services: Vec<String>,
        /// Restore all services without the selector.
        #[arg(long)]
        all: bool,
        /// latest, or one snapshot ID when restoring exactly one service.
        #[arg(long, default_value = "latest")]
        snapshot: String,
        /// Apply restored data after downloading it.
        #[arg(long)]
        apply: bool,
        /// Start selected Compose stacks after apply.
        #[arg(long)]
        start: bool,
        /// Confirm restore; required whenever stdin is not a TTY.
        #[arg(long)]
        yes: bool,
        /// Continue even if the restore would leave less than 1 GiB free.
        #[arg(long)]
        allow_low_space: bool,
        /// Replace local backup.yaml from snapshot.
        #[arg(long, conflicts_with = "keep_manifest")]
        restore_manifest: bool,
        /// Keep an existing local backup.yaml.
        #[arg(long)]
        keep_manifest: bool,
    },
    /// Remove downloaded restore workspaces.
    CleanupRestores {
        /// SERVICE/RESTORE_ID
        targets: Vec<String>,
        /// Remove every downloaded restore.
        #[arg(long)]
        all: bool,
        /// Confirm deletion.
        #[arg(long)]
        yes: bool,
    },
    /// Apply one already downloaded restore.
    Apply {
        service: String,
        restore_dir: String,
        #[arg(long)]
        start: bool,
        #[arg(long)]
        yes: bool,
    },
    /// Prune and compact the repository.
    Maintenance {
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        no_wait: bool,
    },
    /// Check the restic repository.
    Check {
        #[arg(long)]
        no_wait: bool,
    },
    /// Remove a stale restic lock.
    Unlock,
    /// Send a failure notification for a systemd unit (OnFailure=).
    NotifyFailure { unit: String },
    /// Open the offline guide shipped with this release.
    Docs,
    /// Upgrade an installed release from GitHub.
    Upgrade,
}

#[derive(Debug, thiserror::Error)]
pub enum CliError {
    #[error("{command} is not ported yet")]
    NotPorted { command: &'static str },
    #[error("{0}")]
    Failed(String),
    #[error("")]
    Reported,
    /// Stale or stalled status. The alert lines are already printed.
    #[error("")]
    Stale,
}

impl CliError {
    pub fn code(&self) -> u8 {
        match self {
            Self::Stale => 2,
            _ => 1,
        }
    }
}

pub fn run(cli: Cli) -> Result<(), CliError> {
    if let Command::Backup {
        services,
        allow_low_space,
    } = &cli.command
    {
        return crate::files::run_backup(
            crate::documents::CONFIG_PATH,
            services,
            *allow_low_space,
            "restic",
        )
        .map_err(map_command);
    }
    let result = match &cli.command {
        Command::List => crate::ops::run_list(crate::documents::CONFIG_PATH),
        Command::Init => crate::ops::run_init(crate::documents::CONFIG_PATH),
        Command::Snapshots { service } => {
            crate::ops::run_snapshots(crate::documents::CONFIG_PATH, service.as_deref())
        }
        Command::Check { no_wait } => {
            crate::ops::run_check(crate::documents::CONFIG_PATH, *no_wait)
        }
        Command::Unlock => crate::ops::run_unlock(crate::documents::CONFIG_PATH),
        Command::Maintenance { dry_run, no_wait } => {
            crate::ops::run_maintenance(crate::documents::CONFIG_PATH, *dry_run, *no_wait)
        }
        Command::Validate => crate::commands::run_validate(crate::documents::CONFIG_PATH),
        Command::Docs => crate::commands::run_docs(),
        Command::DeleteSnapshot {
            service,
            snapshot,
            prune,
            yes,
        } => crate::commands::run_delete_snapshot(
            crate::documents::CONFIG_PATH,
            service,
            snapshot,
            *yes,
            *prune,
        ),
        Command::CleanupRestores { targets, all, yes } => crate::commands::run_cleanup_restores(
            crate::documents::CONFIG_PATH,
            targets,
            *all,
            *yes,
        ),
        Command::RunDue => crate::ops::run_due(crate::documents::CONFIG_PATH),
        Command::Status { max_age, notify } => {
            crate::ops::run_status(crate::documents::CONFIG_PATH, max_age.as_deref(), *notify)
        }
        Command::NotifyFailure { unit } => {
            crate::alerts::run_notify_failure(crate::documents::CONFIG_PATH, unit)
        }
        Command::Upgrade => {
            return crate::upgrade::run_upgrade()
                .map_err(|error| CliError::Failed(error.to_string()));
        }
        _ => return not_ported(&cli.command),
    };
    result.map_err(map_command)
}

fn map_command(error: crate::files::CommandError) -> CliError {
    match error {
        crate::files::CommandError::Text(message) => CliError::Failed(message),
        crate::files::CommandError::Reported => CliError::Reported,
        crate::files::CommandError::Exit(2) => CliError::Stale,
        crate::files::CommandError::Exit(_) => CliError::Reported,
    }
}

fn not_ported(command: &Command) -> Result<(), CliError> {
    let name = match command {
        Command::List => "list",
        Command::Status { .. } => "status",
        Command::Validate => "validate",
        Command::Init => "init",
        Command::Backup { .. } => "backup",
        Command::RunDue => "run-due",
        Command::Snapshots { .. } => "snapshots",
        Command::DeleteSnapshot { .. } => "delete-snapshot",
        Command::Restore { .. } => "restore",
        Command::CleanupRestores { .. } => "cleanup-restores",
        Command::Apply { .. } => "apply",
        Command::Maintenance { .. } => "maintenance",
        Command::Check { .. } => "check",
        Command::Unlock => "unlock",
        Command::NotifyFailure { .. } => "notify-failure",
        Command::Docs => "docs",
        Command::Upgrade => "upgrade",
    };
    Err(CliError::NotPorted { command: name })
}
