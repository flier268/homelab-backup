import datetime as dt
import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError

DURATION_RE = re.compile(r'^(\d+)(m|h|d|w)$')
LOCALTIME_PATH = Path('/etc/localtime')


@lru_cache(maxsize=1)
def local_timezone():
    try:
        with LOCALTIME_PATH.open('rb') as timezone_file:
            return ZoneInfo.from_file(timezone_file)
    except (OSError, ValueError):
        return dt.datetime.now().astimezone().tzinfo


def local_now():
    return dt.datetime.now(local_timezone())

def parse_duration(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a duration string such as 30m, 6h, or 1d')
    match = DURATION_RE.fullmatch(value.strip().lower())
    if not match:
        raise ValueError(f'{field} must match <number><m|h|d|w>, for example 30m, 6h, 1d')
    number = int(match.group(1))
    if number <= 0:
        raise ValueError(f'{field} must be greater than zero')
    unit = match.group(2)
    return number * {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}[unit]


def parse_cron(expression, field='schedule.cron'):
    if not isinstance(expression, str):
        raise ValueError(f'{field} must be a standard 5-field cron string')
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(
            f'{field} must contain exactly 5 fields: minute hour day-of-month month day-of-week'
        )
    # Preserve the existing standard-cron spelling where Sunday may terminate
    # a weekday range. CronSim represents Sunday as 0 internally, so FRI-SUN
    # would otherwise look like a descending range; 7 is cron's equivalent.
    parts[4] = re.sub(r'(?i)-sun(?=($|[,/]))', '-7', parts[4])
    expression = ' '.join(parts)
    try:
        CronSim(expression, dt.datetime(2000, 1, 1))
    except CronSimError as err:
        raise ValueError(
            f'{field} is invalid or never matches a real calendar date: {err}'
        ) from err
    return expression


def cron_has_occurrence(spec):
    try:
        next(CronSim(spec, dt.datetime(2000, 1, 1)))
    except (CronSimError, StopIteration):
        return False
    return True


def _absolute(value):
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(dt.timezone.utc)


def _elapsed_add(value, delta):
    """Add elapsed time without resetting an aware datetime's PEP 495 fold."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value + delta
    return (value.astimezone(dt.timezone.utc) + delta).astimezone(value.tzinfo)


def cron_previous(spec, now, search_minutes=366 * 24 * 60 * 5):
    current = now.replace(second=0, microsecond=0)
    # CronSim reverse iteration is inclusive via a one-second offset. Use
    # elapsed-time arithmetic so the second DST fall-back fold stays selected.
    start = _elapsed_add(current, dt.timedelta(seconds=1))
    try:
        candidate = next(CronSim(spec, start, reverse=True))
    except StopIteration:
        return None
    boundary = _absolute(current) - dt.timedelta(minutes=search_minutes)
    return candidate if _absolute(candidate) >= boundary else None


def cron_next(spec, now, search_minutes=366 * 24 * 60 * 5):
    current = now.replace(second=0, microsecond=0)
    try:
        candidate = next(CronSim(spec, current))
    except StopIteration:
        return None
    boundary = _absolute(current) + dt.timedelta(minutes=search_minutes)
    return candidate if _absolute(candidate) <= boundary else None


def validate_schedule(m):
    path = m.get('_path', '<manifest>')
    schedule = m.get('schedule')
    if not isinstance(schedule, dict):
        raise ValueError(f'{path}: schedule must be a mapping')
    allowed = {'cron', 'retry_after', 'max_lateness', 'enabled'}
    unknown = sorted(set(schedule) - allowed)
    if unknown:
        raise ValueError(f'{path}: unsupported schedule fields: {unknown}; use cron only')
    if not isinstance(schedule.get('enabled', True), bool):
        raise ValueError(f'{path}: schedule.enabled must be boolean')
    if schedule.get('enabled', True) is False:
        return
    if not schedule.get('cron'):
        raise ValueError(f'{path}: schedule.cron is required')
    spec = parse_cron(schedule['cron'], f'{path}: schedule.cron')
    if not cron_has_occurrence(spec):
        raise ValueError(f'{path}: schedule.cron never matches a real calendar date')
    parse_duration(schedule.get('retry_after', '30m'), f'{path}: schedule.retry_after')
    if schedule.get('max_lateness') is not None:
        parse_duration(schedule['max_lateness'], f'{path}: schedule.max_lateness')
