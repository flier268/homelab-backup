//! Staging must keep a 1 GiB reserve after the estimated copy.

pub const MIN_FREE_BYTES: u64 = 1024 * 1024 * 1024;

pub fn format_bytes(value: u64) -> String {
    let mut amount = value as f64;
    for unit in ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] {
        if amount < 1024.0 || unit == "PiB" {
            return format!("{amount:.2} {unit}");
        }
        amount /= 1024.0;
    }
    format!("{amount:.2} PiB")
}

pub fn check_backup_space(
    label: &str,
    stage: &str,
    estimated_size: u64,
    free: u64,
    allow_low_space: bool,
) -> Result<Vec<String>, (Vec<String>, String)> {
    let required = estimated_size.saturating_add(MIN_FREE_BYTES);
    if free >= required {
        return Ok(Vec::new());
    }
    let shortfall = required - free;
    let mut warnings = vec![format!(
        "WARNING: backup for {label} is estimated at {}, but only {} is free on {stage}; the required 1.00 GiB reserve would be short by {}.",
        format_bytes(estimated_size),
        format_bytes(free),
        format_bytes(shortfall),
    )];
    if allow_low_space {
        warnings.push("WARNING: continuing because --allow-low-space was specified.".to_owned());
        return Ok(warnings);
    }
    Err((
        warnings,
        "insufficient backup staging space; free additional space or explicitly use --allow-low-space"
            .to_owned(),
    ))
}
