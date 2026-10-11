//! Staging copies, space checks, and the Restic command adapter.

mod estimate;
mod policy;
mod restic;
mod space;
mod stage;
mod sync;

#[allow(unused_imports)]
pub use estimate::estimate_path_sources;
#[allow(unused_imports)]
pub use policy::{build_path_filter_args, validate_path_source_policy};
#[allow(unused_imports)]
pub use restic::{parse_repository_id, retention_command, snapshots_command};
#[allow(unused_imports)]
pub use space::{MIN_FREE_BYTES, check_backup_space, format_bytes};
#[allow(unused_imports)]
pub use stage::prepare_stage;
#[allow(unused_imports)]
pub use sync::{PathInventory, SyncOptions, sync_paths};

#[cfg(test)]
mod tests;
