import datetime as dt
import json
from pathlib import Path

from .schedule import cron_next, cron_previous, local_now, parse_cron, parse_duration
from .security import atomic_write_json, ensure_control_directory
from .types import BackupState, GlobalConfig, ServiceManifest


def state_path(c, service):
    return Path(c['state_root']) / f'{service}.json'


def load_state(c: GlobalConfig, service: str) -> BackupState:
    path = state_path(c, service)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(c: GlobalConfig, service: str, state: BackupState):
    ensure_control_directory(c['state_root'])
    atomic_write_json(state_path(c, service), state)


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


def due_status(
        c: GlobalConfig, m: ServiceManifest, now=None, *, state_loader=None,
):
    now = now or local_now()
    state_loader = state_loader or load_state
    schedule = m['schedule']
    if schedule.get('enabled', True) is False:
        return False, 'schedule disabled', None
    spec = parse_cron(schedule['cron'], 'schedule.cron')
    state = state_loader(c, m['service'])
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
