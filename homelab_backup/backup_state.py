import datetime as dt
import json
import math
import os
import re
import stat
from pathlib import Path

from .manifest import valid_service_name
from .schedule import cron_next, cron_previous, local_now, parse_cron, parse_duration
from .security import (
    atomic_write_json, ensure_control_directory, lexical_absolute, path_contains,
)
from .types import BackupState, GlobalConfig, ServiceManifest


def state_path(c, service):
    if not valid_service_name(service):
        raise ValueError(f'invalid service name for backup state: {service!r}')
    root = lexical_absolute(c['state_root'])
    path = root / f'{service}.json'
    if not path_contains(root, path):
        raise ValueError(f'backup state path escapes state_root: {path}')
    return path


def load_state(c: GlobalConfig, service: str) -> BackupState:
    path = state_path(c, service)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ValueError(f'{path}: could not open backup state: {err}') from err
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'{path}: backup state must be a regular file')
        with os.fdopen(fd, encoding='utf-8') as state_file:
            fd = -1
            data = json.load(state_file)
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f'{path}: could not load backup state: {err}') from err
    finally:
        if fd >= 0:
            os.close(fd)
    return validate_state(data, service=service, source=str(path))


def save_state(c: GlobalConfig, service: str, state: BackupState):
    path = state_path(c, service)
    validate_state(state, service=service, source=str(path))
    ensure_control_directory(c['state_root'])
    atomic_write_json(path, state)


def invalidate_snapshot_fingerprint(c: GlobalConfig, service: str):
    state = load_state(c, service)
    changed = False
    for field in ('last_snapshot_fingerprint', 'last_snapshot_context'):
        if state.pop(field, None) is not None:
            changed = True
    if changed:
        save_state(c, service, state)


def parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def validate_state(data, *, service, source='backup state') -> BackupState:
    if not isinstance(data, dict):
        raise ValueError(f'{source}: backup state must be a JSON mapping')
    allowed = {
        'service', 'first_seen_at', 'last_result', 'last_success_at',
        'last_attempt_at', 'last_finished_at', 'last_duration_seconds',
        'last_error', 'last_retention_error', 'last_snapshot_fingerprint',
        'last_snapshot_context',
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f'{source}: unsupported backup state fields: {unknown}')
    if 'service' in data and data['service'] != service:
        raise ValueError(f'{source}: service does not match {service!r}')
    if 'last_result' in data and data['last_result'] not in {
            'running', 'success', 'failed',
    }:
        raise ValueError(f'{source}: last_result is invalid')
    for field in (
            'first_seen_at', 'last_success_at', 'last_attempt_at',
            'last_finished_at',
    ):
        if field in data and parse_iso(data[field]) is None:
            raise ValueError(f'{source}: {field} must be a timezone-aware timestamp')
    if 'last_duration_seconds' in data:
        duration = data['last_duration_seconds']
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
                or not math.isfinite(duration) or duration < 0:
            raise ValueError(f'{source}: last_duration_seconds must be non-negative')
    for field in (
            'last_error', 'last_retention_error', 'last_snapshot_fingerprint',
            'last_snapshot_context',
    ):
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise ValueError(f'{source}: {field} must be a string or null')
    for field in ('last_snapshot_fingerprint', 'last_snapshot_context'):
        fingerprint = data.get(field)
        if fingerprint is not None and not re.fullmatch(
                r'sha256:[0-9a-f]{64}', fingerprint,
        ):
            raise ValueError(
                f'{source}: {field} must be a SHA-256 fingerprint'
            )
    return data


def due_status(
        c: GlobalConfig, m: ServiceManifest, now=None, *, state_loader=None,
):
    now = now or local_now()
    state_loader = state_loader or load_state
    schedule = m['schedule']
    if schedule.get('enabled', True) is False:
        return False, 'schedule disabled', None
    spec = parse_cron(schedule['cron'], 'schedule.cron')
    state = validate_state(
        state_loader(c, m['service']), service=m['service'], source='backup state',
    )
    last_success = parse_iso(state.get('last_success_at'))
    last_attempt = parse_iso(state.get('last_attempt_at'))
    retry_after = parse_duration(schedule.get('retry_after', '30m'), 'schedule.retry_after')
    if state.get('last_result') == 'failed' and last_attempt:
        retry_at = last_attempt + dt.timedelta(seconds=retry_after)
        if now < retry_at:
            return False, f'retry at {retry_at.isoformat(timespec="minutes")}', retry_at
    occurrence = cron_previous(spec, now)
    if occurrence is None:
        return False, 'no cron occurrence found in search window', None
    if (
            state.get('last_result') == 'running'
            and last_attempt is not None
            and last_attempt >= occurrence.astimezone(dt.timezone.utc)
    ):
        # A process may have committed its snapshot and then failed to persist
        # the final success state. Treat an indeterminate attempt as consuming
        # only its own cron occurrence; a later occurrence remains eligible.
        next_time = cron_next(spec, now)
        return False, (
            f'attempted at {last_attempt.isoformat(timespec="minutes")}; '
            f'next at {next_time.isoformat(timespec="minutes") if next_time else "unknown"}'
        ), next_time
    if last_success is None or last_success < occurrence:
        max_lateness = schedule.get('max_lateness')
        if max_lateness is not None:
            deadline = occurrence.astimezone(dt.timezone.utc) + dt.timedelta(
                seconds=parse_duration(max_lateness, 'schedule.max_lateness')
            )
            if now > deadline:
                next_time = cron_next(spec, now)
                return False, (
                    f'missed {occurrence.isoformat(timespec="minutes")}; '
                    f'next at {next_time.isoformat(timespec="minutes") if next_time else "unknown"}'
                ), next_time
        return True, f'due since {occurrence.isoformat(timespec="minutes")}', occurrence
    next_time = cron_next(spec, now)
    return False, (
        f'next at {next_time.isoformat(timespec="minutes") if next_time else "unknown"}'
    ), next_time


DEFAULT_SUCCESS_GRACE_SECONDS = 3600


def success_age_threshold(c: GlobalConfig, m: ServiceManifest, now=None):
    """Seconds after which a missing successful backup must alert.

    Two full schedule periods plus one hour of grace are allowed, so one
    failed or missed run is tolerated but a second one is not (daily -> 49h).
    ``alert.max_success_age`` in the global config can only raise that floor,
    so a global 36h never makes a weekly job alert between its runs. Without
    a usable schedule the global value (or None) applies.
    """
    alert = c.get('alert') or {}
    configured = (
        parse_duration(alert['max_success_age'], 'alert.max_success_age')
        if alert.get('max_success_age') else None
    )
    now = now or local_now()
    cron = (m.get('schedule') or {}).get('cron')
    if cron is None:
        return configured
    spec = parse_cron(cron, 'schedule.cron')
    previous = cron_previous(spec, now)
    following = cron_next(spec, previous) if previous is not None else None
    if previous is None or following is None:
        return configured
    period = (following - previous).total_seconds()
    derived = int(2 * period + DEFAULT_SUCCESS_GRACE_SECONDS)
    return derived if configured is None else max(configured, derived)


def success_staleness(
        c: GlobalConfig, m: ServiceManifest, state: BackupState, now=None,
        *, max_age_seconds=None,
):
    """Report whether a service has gone too long without a success."""
    now = now or local_now()
    result = {
        'stale': False,
        'last_success_age_seconds': None,
        'max_success_age_seconds': None,
    }
    if (m.get('schedule') or {}).get('enabled', True) is False:
        return result
    threshold = max_age_seconds or success_age_threshold(c, m, now)
    result['max_success_age_seconds'] = threshold
    if threshold is None:
        return result
    now_utc = now.astimezone(dt.timezone.utc)
    reference = parse_iso(state.get('last_success_at'))
    if reference is None:
        # Never succeeded: measure from when the service was first seen so a
        # brand-new service is not reported before it had a chance to run.
        first_seen = parse_iso(state.get('first_seen_at'))
        if first_seen is None:
            return result
        age = (now_utc - first_seen).total_seconds()
        result['stale'] = age > threshold
        return result
    age = (now_utc - reference).total_seconds()
    result['last_success_age_seconds'] = round(age)
    result['stale'] = age > threshold
    return result
