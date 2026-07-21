import datetime as dt
import json
import math
import os
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
        'last_error', 'last_retention_error',
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
    for field in ('last_error', 'last_retention_error'):
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise ValueError(f'{source}: {field} must be a string or null')
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
