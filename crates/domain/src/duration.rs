//! Durations written as `<number><m|h|d|w>`, matching the existing config grammar.
//! Conversion uses `humantime` after that grammar check so values such as
//! `1 hour` stay rejected.

use std::time::Duration;

use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DurationError {
    #[error("{field} must be a duration string such as 30m, 6h, or 1d")]
    NotAString { field: String },
    #[error("{field} must match <number><m|h|d|w>, for example 30m, 6h, 1d")]
    Invalid { field: String },
    #[error("{field} must be greater than zero")]
    NotPositive { field: String },
}

pub fn parse_duration(value: &str, field: &str) -> Result<Duration, DurationError> {
    let trimmed = value.trim().to_ascii_lowercase();
    let (number, unit) = split_duration(&trimmed).ok_or_else(|| DurationError::Invalid {
        field: field.to_owned(),
    })?;
    if number == 0 {
        return Err(DurationError::NotPositive {
            field: field.to_owned(),
        });
    }
    let spelled = format!("{number}{unit}");
    humantime::parse_duration(&spelled).map_err(|_| DurationError::Invalid {
        field: field.to_owned(),
    })
}

fn split_duration(value: &str) -> Option<(u64, &str)> {
    let unit_at = value.find(|ch: char| !ch.is_ascii_digit())?;
    let (digits, unit) = value.split_at(unit_at);
    if digits.is_empty() || !matches!(unit, "m" | "h" | "d" | "w") {
        return None;
    }
    let number = digits.parse().ok()?;
    Some((number, unit))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_the_existing_units() {
        assert_eq!(
            parse_duration("30m", "retry").unwrap(),
            Duration::from_secs(30 * 60)
        );
        assert_eq!(
            parse_duration(" 6H ", "retry").unwrap(),
            Duration::from_secs(6 * 3600)
        );
        assert_eq!(
            parse_duration("1d", "retry").unwrap(),
            Duration::from_secs(86400)
        );
        assert_eq!(
            parse_duration("2w", "retry").unwrap(),
            Duration::from_secs(2 * 604800)
        );
    }

    #[test]
    fn rejects_zero_words_and_unknown_units() {
        assert!(matches!(
            parse_duration("0m", "field"),
            Err(DurationError::NotPositive { .. })
        ));
        assert!(matches!(
            parse_duration("1 hour", "field"),
            Err(DurationError::Invalid { .. })
        ));
        assert!(matches!(
            parse_duration("15s", "field"),
            Err(DurationError::Invalid { .. })
        ));
    }
}
