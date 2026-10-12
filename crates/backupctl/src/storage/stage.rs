//! A service stage is a private directory that does not cover live data.

use homelab_backup_domain::paths::{lexical_absolute, paths_overlap};

use crate::unix::control::ensure_private_directory;
use crate::unix::error::SecurityError;

pub fn prepare_stage(
    staging_root: &str,
    service: &str,
    protected: &[String],
) -> Result<String, SecurityError> {
    let root =
        lexical_absolute(staging_root).map_err(|error| SecurityError::invalid(error.message))?;
    let stage = if root == "/" {
        format!("/{service}")
    } else {
        format!("{root}/{service}")
    };
    for target in protected {
        let target =
            lexical_absolute(target).map_err(|error| SecurityError::invalid(error.message))?;
        if paths_overlap(&stage, &target).unwrap_or(false) {
            return Err(SecurityError::invalid(format!(
                "staging directory {stage} overlaps protected path {target}"
            )));
        }
    }
    ensure_private_directory(&root, false)?;
    ensure_private_directory(&stage, true)?;
    Ok(stage)
}
