//! When a service is due, and when a missing success is stale.
//! The clock is the caller's. Cron comes from the croner adapter.

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset, TimeZone, Utc};
use homelab_backup_domain::duration::parse_duration;
use homelab_backup_domain::schedule::{cron_next, cron_previous, parse_cron};
use homelab_backup_domain::yaml::Yaml;

const GRACE_SECONDS: i64 = 3600;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Due {
    pub due: bool,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Staleness {
    pub stale: bool,
    pub last_success_age_seconds: Option<i64>,
    pub max_success_age_seconds: Option<i64>,
}

pub fn due_status(
    manifest: &BTreeMap<String, Yaml>,
    state: &BTreeMap<String, serde_json::Value>,
    now: DateTime<FixedOffset>,
) -> Result<Due, String> {
    let schedule = manifest
        .get("schedule")
        .and_then(Yaml::as_map)
        .ok_or_else(|| "schedule must be a mapping".to_owned())?;
    if schedule.get("enabled").and_then(Yaml::as_bool) == Some(false) {
        return Ok(Due {
            due: false,
            reason: "schedule disabled".to_owned(),
        });
    }
    let cron = schedule
        .get("cron")
        .and_then(Yaml::as_str)
        .filter(|cron| !cron.is_empty())
        .ok_or_else(|| "schedule.cron is required".to_owned())?;
    let spec = parse_cron(cron, "schedule.cron").map_err(|error| error.to_string())?;
    let last_success = timestamp(state.get("last_success_at"));
    let last_attempt = timestamp(state.get("last_attempt_at"));
    let retry_after = duration_seconds(
        schedule
            .get("retry_after")
            .and_then(Yaml::as_str)
            .unwrap_or("30m"),
        "schedule.retry_after",
    )?;
    if state.get("last_result").and_then(|value| value.as_str()) == Some("failed")
        && let Some(last_attempt) = last_attempt
    {
        let retry_at = last_attempt + chrono::Duration::seconds(retry_after);
        if now < retry_at {
            return Ok(Due {
                due: false,
                reason: format!("retry at {}", iso_minutes(retry_at)),
            });
        }
    }
    let Some(occurrence) = cron_previous(&spec, &now) else {
        return Ok(Due {
            due: false,
            reason: "no cron occurrence found in search window".to_owned(),
        });
    };
    if state.get("last_result").and_then(|value| value.as_str()) == Some("running")
        && last_attempt.is_some_and(|attempt| to_utc(attempt) >= to_utc(occurrence))
    {
        let next = cron_next(&spec, &now);
        return Ok(Due {
            due: false,
            reason: format!(
                "attempted at {}; next at {}",
                last_attempt
                    .map(iso_minutes)
                    .unwrap_or_else(|| "unknown".to_owned()),
                next.map(iso_minutes)
                    .unwrap_or_else(|| "unknown".to_owned())
            ),
        });
    }
    if last_success.is_none_or(|success| to_utc(success) < to_utc(occurrence)) {
        if let Some(max_lateness) = schedule.get("max_lateness").and_then(Yaml::as_str) {
            let lateness = duration_seconds(max_lateness, "schedule.max_lateness")?;
            let deadline = to_utc(occurrence) + chrono::Duration::seconds(lateness);
            if to_utc(now) > deadline {
                let next = cron_next(&spec, &now);
                return Ok(Due {
                    due: false,
                    reason: format!(
                        "missed {}; next at {}",
                        iso_minutes(occurrence),
                        next.map(iso_minutes)
                            .unwrap_or_else(|| "unknown".to_owned())
                    ),
                });
            }
        }
        return Ok(Due {
            due: true,
            reason: format!("due since {}", iso_minutes(occurrence)),
        });
    }
    let next = cron_next(&spec, &now);
    Ok(Due {
        due: false,
        reason: format!(
            "next at {}",
            next.map(iso_minutes)
                .unwrap_or_else(|| "unknown".to_owned())
        ),
    })
}

pub fn success_staleness(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    state: &BTreeMap<String, serde_json::Value>,
    now: DateTime<FixedOffset>,
    max_age_seconds: Option<i64>,
) -> Result<Staleness, String> {
    let mut staleness = Staleness {
        stale: false,
        last_success_age_seconds: None,
        max_success_age_seconds: None,
    };
    if manifest
        .get("schedule")
        .and_then(Yaml::as_map)
        .and_then(|schedule| schedule.get("enabled"))
        .and_then(Yaml::as_bool)
        == Some(false)
    {
        return Ok(staleness);
    }
    let threshold = match max_age_seconds {
        Some(seconds) if seconds > 0 => Some(seconds),
        _ => success_age_threshold(config, manifest, now)?,
    };
    staleness.max_success_age_seconds = threshold;
    let Some(threshold) = threshold else {
        return Ok(staleness);
    };
    let now_utc = to_utc(now);
    let Some(reference) =
        timestamp(state.get("last_success_at")).or_else(|| timestamp(state.get("first_seen_at")))
    else {
        return Ok(staleness);
    };
    let age = (now_utc - to_utc(reference)).num_milliseconds() as f64 / 1000.0;
    if timestamp(state.get("last_success_at")).is_some() {
        staleness.last_success_age_seconds = Some(age.round() as i64);
    }
    staleness.stale = age > threshold as f64;
    Ok(staleness)
}

fn success_age_threshold(
    config: &BTreeMap<String, Yaml>,
    manifest: &BTreeMap<String, Yaml>,
    now: DateTime<FixedOffset>,
) -> Result<Option<i64>, String> {
    let configured = config
        .get("alert")
        .and_then(Yaml::as_map)
        .and_then(|alert| alert.get("max_success_age"))
        .and_then(Yaml::as_str)
        .map(|value| duration_seconds(value, "alert.max_success_age"))
        .transpose()?;
    let Some(cron) = manifest
        .get("schedule")
        .and_then(Yaml::as_map)
        .and_then(|schedule| schedule.get("cron"))
        .and_then(Yaml::as_str)
    else {
        return Ok(configured);
    };
    let spec = parse_cron(cron, "schedule.cron").map_err(|error| error.to_string())?;
    let Some(previous) = cron_previous(&spec, &now) else {
        return Ok(configured);
    };
    let Some(following) = cron_next(&spec, &previous) else {
        return Ok(configured);
    };
    let period = (to_utc(following) - to_utc(previous)).num_seconds();
    let derived = 2 * period + GRACE_SECONDS;
    Ok(Some(
        configured
            .map(|value| value.max(derived))
            .unwrap_or(derived),
    ))
}

fn duration_seconds(value: &str, field: &str) -> Result<i64, String> {
    parse_duration(value, field)
        .map(|duration| duration.as_secs() as i64)
        .map_err(|error| error.to_string())
}

fn timestamp(value: Option<&serde_json::Value>) -> Option<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value?.as_str()?).ok()
}

fn to_utc<Tz>(value: DateTime<Tz>) -> DateTime<Utc>
where
    Tz: TimeZone,
{
    value.with_timezone(&Utc)
}

fn iso_minutes(value: DateTime<FixedOffset>) -> String {
    value.format("%Y-%m-%dT%H:%M%:z").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339("2026-01-02T12:00:00+00:00").unwrap()
    }

    fn daily() -> BTreeMap<String, Yaml> {
        BTreeMap::from([(
            "schedule".to_owned(),
            Yaml::map([("cron".to_owned(), Yaml::string("0 0 * * *"))]),
        )])
    }

    #[test]
    fn a_daily_service_without_a_success_is_due() {
        let due = due_status(&daily(), &BTreeMap::new(), now()).unwrap();
        assert!(due.due);
        assert_eq!(due.reason, "due since 2026-01-02T00:00+00:00");
    }

    #[test]
    fn a_recent_failure_waits_for_retry() {
        let mut schedule = daily();
        schedule.insert(
            "schedule".to_owned(),
            Yaml::map([
                ("cron".to_owned(), Yaml::string("0 0 * * *")),
                ("retry_after".to_owned(), Yaml::string("30m")),
            ]),
        );
        let state = BTreeMap::from([
            ("last_result".to_owned(), serde_json::json!("failed")),
            (
                "last_attempt_at".to_owned(),
                serde_json::json!("2026-01-02T11:45:00+00:00"),
            ),
        ]);
        let due = due_status(&schedule, &state, now()).unwrap();
        assert!(!due.due);
        assert_eq!(due.reason, "retry at 2026-01-02T12:15+00:00");
    }

    #[test]
    fn a_daily_service_is_stale_after_forty_nine_hours() {
        let config = BTreeMap::new();
        let state = BTreeMap::from([(
            "last_success_at".to_owned(),
            serde_json::json!("2025-12-30T00:00:00+00:00"),
        )]);
        let staleness = success_staleness(&config, &daily(), &state, now(), None).unwrap();
        assert_eq!(staleness.max_success_age_seconds, Some(2 * 86400 + 3600));
        assert!(staleness.stale);
    }
}
