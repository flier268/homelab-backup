//! Five-field cron policy on top of `croner`.
//!
//! The project accepts exactly five fields. Sunday may end a weekday range
//! (`fri-sun`, `5-7`). Occurrence search matches the previous cronsim calls:
//! the previous hit includes the truncated minute, and the next hit does not.

use std::collections::BTreeMap;

use chrono::{DateTime, TimeZone, Timelike};
use croner::Cron;
use croner::parser::CronParser;
use thiserror::Error;

use crate::duration::parse_duration;
use crate::error::PolicyError;
use crate::yaml::{Yaml, python_string_list, unknown_keys};

const SEARCH_MINUTES: i64 = 366 * 24 * 60 * 5;

#[derive(Debug, Clone)]
pub struct CronSpec {
    cron: Cron,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CronError {
    #[error("{field} must be a standard 5-field cron string")]
    NotAString { field: String },
    #[error("{field} must contain exactly 5 fields: minute hour day-of-month month day-of-week")]
    FieldCount { field: String },
    #[error("{field} is invalid or never matches a real calendar date: {detail}")]
    Invalid { field: String, detail: String },
}

pub fn parse_cron(expression: &str, field: &str) -> Result<CronSpec, CronError> {
    let parts: Vec<&str> = expression.split_whitespace().collect();
    if parts.len() != 5 {
        return Err(CronError::FieldCount {
            field: field.to_owned(),
        });
    }
    let fields = [
        wildcard_step_to_star(parts[0]),
        wildcard_step_to_star(parts[1]),
        wildcard_step_to_star(parts[2]),
        wildcard_step_to_star(parts[3]),
        wildcard_step_to_star(&replace_sunday_range_end(parts[4])),
    ];
    let normalized = fields.join(" ");
    // `5/10` is the Debian "start here, step to the field maximum" form.
    // croner accepts it when sloppy ranges are enabled.
    let cron = CronParser::builder()
        .sloppy_ranges(true)
        .build()
        .parse(&normalized)
        .map_err(|err| CronError::Invalid {
            field: field.to_owned(),
            detail: err.to_string(),
        })?;
    Ok(CronSpec { cron })
}

/// `*/1` is every legal value, and a day field that begins with `*` keeps
/// wildcard DOM/DOW semantics. croner only treats a bare `*` as that wildcard.
fn wildcard_step_to_star(field: &str) -> String {
    if field.eq_ignore_ascii_case("*/1") {
        "*".to_owned()
    } else {
        field.to_owned()
    }
}

pub fn cron_has_occurrence(spec: &CronSpec) -> bool {
    let start = chrono::NaiveDate::from_ymd_opt(2000, 1, 1)
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .expect("year 2000 is a valid civil time");
    spec.cron.find_next_occurrence(&start, true).is_ok()
}

pub fn cron_next<Tz>(spec: &CronSpec, now: &DateTime<Tz>) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
{
    let current = truncate_to_minute(now)?;
    let candidate = spec.cron.find_next_occurrence(&current, false).ok()?;
    within_search_window(&current, &candidate)
}

pub fn cron_previous<Tz>(spec: &CronSpec, now: &DateTime<Tz>) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
{
    let current = truncate_to_minute(now)?;
    let candidate = spec.cron.find_previous_occurrence(&current, true).ok()?;
    within_search_window(&current, &candidate)
}

fn truncate_to_minute<Tz>(now: &DateTime<Tz>) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
{
    // Subtract elapsed seconds so an ambiguous civil time keeps its offset.
    // Rebuilding the civil clock would pick the earliest fold.
    let seconds = i64::from(now.second());
    let nanos = i64::from(now.nanosecond());
    Some(now.clone() - chrono::Duration::seconds(seconds) - chrono::Duration::nanoseconds(nanos))
}

fn within_search_window<Tz>(
    current: &DateTime<Tz>,
    candidate: &DateTime<Tz>,
) -> Option<DateTime<Tz>>
where
    Tz: TimeZone,
{
    let limit = chrono::Duration::minutes(SEARCH_MINUTES);
    let delta = candidate.clone().signed_duration_since(current);
    if delta.num_minutes().abs() > limit.num_minutes() {
        None
    } else {
        Some(candidate.clone())
    }
}

/// `fri-sun` is a descending range when Sunday is only 0. Croner's Sunday is
/// also 7, and the existing manifests spell the end of the week as `sun`.
pub fn validate_schedule_value(path: &str, schedule: Option<&Yaml>) -> Result<(), PolicyError> {
    let Some(Yaml::Map(schedule)) = schedule else {
        return Err(PolicyError::new(format!(
            "{path}: schedule must be a mapping"
        )));
    };
    validate_schedule_mapping(path, schedule)
}

fn validate_schedule_mapping(
    path: &str,
    schedule: &BTreeMap<String, Yaml>,
) -> Result<(), PolicyError> {
    let allowed = ["cron", "retry_after", "max_lateness", "enabled"];
    let unknown = unknown_keys(schedule, &allowed);
    if !unknown.is_empty() {
        return Err(PolicyError::new(format!(
            "{path}: unsupported schedule fields: {}; use cron only",
            python_string_list(unknown)
        )));
    }
    match schedule.get("enabled") {
        None => {}
        Some(Yaml::Bool(_)) => {}
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{path}: schedule.enabled must be boolean"
            )));
        }
    }
    if schedule.get("enabled") == Some(&Yaml::Bool(false)) {
        return Ok(());
    }
    let field = format!("{path}: schedule.cron");
    let cron = match schedule.get("cron") {
        Some(value) if !value.is_truthy() => {
            return Err(PolicyError::new(format!(
                "{path}: schedule.cron is required"
            )));
        }
        None => {
            return Err(PolicyError::new(format!(
                "{path}: schedule.cron is required"
            )));
        }
        Some(Yaml::String(value)) => value.as_str(),
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{field} must be a standard 5-field cron string"
            )));
        }
    };
    let spec = parse_cron(cron, &field).map_err(|err| PolicyError::new(err.to_string()))?;
    if !cron_has_occurrence(&spec) {
        return Err(PolicyError::new(format!(
            "{path}: schedule.cron never matches a real calendar date"
        )));
    }
    require_duration(
        schedule.get("retry_after"),
        Some("30m"),
        &format!("{path}: schedule.retry_after"),
    )?;
    if schedule.contains_key("max_lateness") {
        require_duration(
            schedule.get("max_lateness"),
            None,
            &format!("{path}: schedule.max_lateness"),
        )?;
    }
    Ok(())
}

fn require_duration(
    value: Option<&Yaml>,
    default: Option<&str>,
    field: &str,
) -> Result<(), PolicyError> {
    let text = match value {
        None => default.unwrap_or(""),
        Some(Yaml::String(value)) => value.as_str(),
        Some(_) => {
            return Err(PolicyError::new(format!(
                "{field} must be a duration string such as 30m, 6h, or 1d"
            )));
        }
    };
    parse_duration(text, field)
        .map(|_| ())
        .map_err(|err| PolicyError::new(err.to_string()))
}

fn replace_sunday_range_end(dow: &str) -> String {
    let mut out = String::with_capacity(dow.len());
    let bytes = dow.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'-' {
            let rest = &dow[index + 1..];
            let name_end = rest.find([',', '/']).unwrap_or(rest.len());
            let name = &rest[..name_end];
            if name.eq_ignore_ascii_case("sun") {
                out.push_str("-7");
                index += 1 + name_end;
                continue;
            }
        }
        out.push(bytes[index] as char);
        index += 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use chrono::{Offset, TimeZone};
    use chrono_tz::America::New_York;

    use super::*;

    fn naive(y: i32, m: u32, d: u32, h: u32, min: u32) -> chrono::NaiveDateTime {
        chrono::NaiveDate::from_ymd_opt(y, m, d)
            .and_then(|date| date.and_hms_opt(h, min, 0))
            .expect("fixed test timestamp")
    }

    fn utc(y: i32, m: u32, d: u32, h: u32, min: u32) -> chrono::DateTime<chrono::Utc> {
        chrono::Utc.from_utc_datetime(&naive(y, m, d, h, min))
    }

    #[test]
    fn previous_and_next_occurrences() {
        let cases = [
            (
                "*/15 * * * *",
                utc(2026, 7, 13, 12, 7),
                utc(2026, 7, 13, 12, 0),
                utc(2026, 7, 13, 12, 15),
            ),
            (
                "0 2,8,14,20 * * *",
                utc(2026, 7, 13, 12, 7),
                utc(2026, 7, 13, 8, 0),
                utc(2026, 7, 13, 14, 0),
            ),
            (
                "0 0 * * mon",
                utc(2026, 7, 15, 12, 7),
                utc(2026, 7, 13, 0, 0),
                utc(2026, 7, 20, 0, 0),
            ),
            (
                "0 0 29 2 *",
                utc(2024, 3, 1, 12, 7),
                utc(2024, 2, 29, 0, 0),
                utc(2028, 2, 29, 0, 0),
            ),
        ];
        for (expression, now, previous, following) in cases {
            let spec = parse_cron(expression, "schedule.cron").unwrap();
            assert_eq!(cron_previous(&spec, &now), Some(previous), "{expression}");
            assert_eq!(cron_next(&spec, &now), Some(following), "{expression}");
        }
    }

    #[test]
    fn sunday_ranges_accept_seven_and_names() {
        let now = utc(2026, 7, 13, 12, 0);
        let expected = utc(2026, 7, 17, 0, 0);
        for expression in ["0 0 * * 5-7", "0 0 * * fri-sun"] {
            let spec = parse_cron(expression, "schedule.cron").unwrap();
            assert_eq!(cron_next(&spec, &now), Some(expected), "{expression}");
        }
    }

    #[test]
    fn step_from_single_value_continues_to_field_maximum() {
        let spec = parse_cron("5/10 * * * *", "schedule.cron").unwrap();
        assert_eq!(
            cron_next(&spec, &utc(2026, 7, 14, 12, 6)),
            Some(utc(2026, 7, 14, 12, 15))
        );
    }

    #[test]
    fn star_step_keeps_day_field_wildcard_semantics() {
        let spec = parse_cron("0 0 */1 * mon", "schedule.cron").unwrap();
        let tuesday = utc(2026, 7, 14, 0, 0);
        assert_eq!(cron_next(&spec, &tuesday), Some(utc(2026, 7, 20, 0, 0)));
    }

    fn new_york_single(
        y: i32,
        m: u32,
        d: u32,
        h: u32,
        min: u32,
    ) -> chrono::DateTime<chrono_tz::Tz> {
        New_York
            .with_ymd_and_hms(y, m, d, h, min, 0)
            .single()
            .unwrap_or_else(|| panic!("{y}-{m}-{d} {h}:{min} is not a single civil time"))
    }

    fn new_york_fold(
        y: i32,
        m: u32,
        d: u32,
        h: u32,
        min: u32,
        fold: u8,
    ) -> chrono::DateTime<chrono_tz::Tz> {
        match New_York.with_ymd_and_hms(y, m, d, h, min, 0) {
            chrono::LocalResult::Single(value) => value,
            chrono::LocalResult::Ambiguous(earliest, latest) => {
                if fold == 0 {
                    earliest
                } else {
                    latest
                }
            }
            chrono::LocalResult::None => panic!("{y}-{m}-{d} {h}:{min} does not exist"),
        }
    }

    #[test]
    fn spring_forward_fixed_job_runs_after_skipped_interval() {
        let spec = parse_cron("30 2 * * *", "schedule.cron").unwrap();
        let before_jump = new_york_single(2026, 3, 8, 1, 55);
        let after_jump = new_york_single(2026, 3, 8, 3, 5);
        let expected = new_york_single(2026, 3, 8, 3, 0);
        assert_eq!(cron_next(&spec, &before_jump), Some(expected));
        assert_eq!(cron_previous(&spec, &after_jump), Some(expected));
    }

    #[test]
    fn fall_back_fixed_job_is_not_repeated() {
        let spec = parse_cron("30 1 * * *", "schedule.cron").unwrap();
        let second_hour = new_york_fold(2026, 11, 1, 1, 45, 1);
        let first_occurrence = new_york_fold(2026, 11, 1, 1, 30, 0);
        assert_eq!(cron_previous(&spec, &second_hour), Some(first_occurrence));
    }

    #[test]
    fn fall_back_frequent_job_keeps_second_fold() {
        let spec = parse_cron("*/5 * * * *", "schedule.cron").unwrap();
        let second_hour = new_york_fold(2026, 11, 1, 1, 45, 1);
        let previous = cron_previous(&spec, &second_hour).expect("previous");
        let following = cron_next(&spec, &second_hour).expect("next");
        assert_eq!(previous, second_hour);
        assert_eq!(
            previous.offset().fix().local_minus_utc(),
            second_hour.offset().fix().local_minus_utc()
        );
        assert_eq!(following, new_york_fold(2026, 11, 1, 1, 50, 1));
    }

    #[test]
    fn rejects_the_wrong_number_of_fields() {
        let error = parse_cron("0 0 * *", "schedule.cron").unwrap_err();
        assert!(matches!(error, CronError::FieldCount { .. }));
        assert!(error.to_string().contains("exactly 5 fields"));
    }
}
