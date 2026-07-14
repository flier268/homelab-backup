import datetime as dt
import re

DURATION_RE = re.compile(r'^(\d+)(m|h|d|w)$')

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


def _cron_atom(value, names, field):
    token = value.strip().lower()
    if names and token in names:
        return names[token]
    try:
        return int(token)
    except ValueError as err:
        raise ValueError(f'{field}: invalid cron value {value!r}') from err


def _parse_cron_field(expression, minimum, maximum, field, names=None, sunday_7=False):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError(f'{field}: cron field must not be empty')
    values = set()
    for item in expression.split(','):
        item = item.strip()
        if not item:
            raise ValueError(f'{field}: empty cron list item')
        if '/' in item:
            base, step_text = item.split('/', 1)
            try:
                step = int(step_text)
            except ValueError as err:
                raise ValueError(f'{field}: invalid cron step {step_text!r}') from err
            if step <= 0:
                raise ValueError(f'{field}: cron step must be greater than zero')
        else:
            base, step = item, 1
        if base == '*':
            first, last = minimum, maximum
        elif '-' in base:
            left, right = base.split('-', 1)
            first = _cron_atom(left, names, field)
            last = _cron_atom(right, names, field)
            if first > last:
                raise ValueError(f'{field}: descending cron ranges are not supported: {base!r}')
        else:
            first = _cron_atom(base, names, field)
            last = maximum if '/' in item else first
        if first < minimum or first > maximum or last < minimum or last > maximum:
            raise ValueError(f'{field}: cron value outside {minimum}-{maximum}: {base!r}')
        values.update(range(first, last + 1, step))
    if sunday_7 and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def parse_cron(expression, field='schedule.cron'):
    if not isinstance(expression, str):
        raise ValueError(f'{field} must be a standard 5-field cron string')
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(
            f'{field} must contain exactly 5 fields: minute hour day-of-month month day-of-week'
        )
    month_names = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    }
    weekday_names = {'sun': 7, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6}
    return {
        'expression': expression,
        'minutes': _parse_cron_field(parts[0], 0, 59, f'{field} minute'),
        'hours': _parse_cron_field(parts[1], 0, 23, f'{field} hour'),
        'days': _parse_cron_field(parts[2], 1, 31, f'{field} day-of-month'),
        'months': _parse_cron_field(parts[3], 1, 12, f'{field} month', month_names),
        'weekdays': _parse_cron_field(parts[4], 0, 7, f'{field} day-of-week', weekday_names, sunday_7=True),
        'day_wildcard': parts[2].startswith('*'),
        'weekday_wildcard': parts[4].startswith('*'),
    }


def cron_matches(spec, value):
    if value.minute not in spec['minutes'] or value.hour not in spec['hours']:
        return False
    return cron_date_matches(spec, value)


def cron_date_matches(spec, value):
    if value.month not in spec['months']:
        return False
    day_match = value.day in spec['days']
    cron_weekday = (value.weekday() + 1) % 7
    weekday_match = cron_weekday in spec['weekdays']
    if spec['day_wildcard'] and spec['weekday_wildcard']:
        return True
    if spec['day_wildcard']:
        return weekday_match
    if spec['weekday_wildcard']:
        return day_match
    # Traditional cron semantics: when both fields are restricted, either may match.
    return day_match or weekday_match


def cron_has_occurrence(spec):
    # A complete Gregorian calendar cycle proves whether the date fields can match.
    candidate = dt.date(2000, 1, 1)
    end = dt.date(2400, 1, 1)
    while candidate < end:
        if cron_date_matches(spec, candidate):
            return True
        candidate += dt.timedelta(days=1)
    return False


def cron_previous(spec, now, search_minutes=366 * 24 * 60 * 5):
    current = now.replace(second=0, microsecond=0)
    boundary = current - dt.timedelta(minutes=search_minutes)
    candidate_date = current.date()
    while candidate_date >= boundary.date():
        if cron_date_matches(spec, candidate_date):
            for hour in sorted(spec['hours'], reverse=True):
                for minute in sorted(spec['minutes'], reverse=True):
                    candidate = dt.datetime.combine(
                        candidate_date, dt.time(hour, minute), tzinfo=current.tzinfo,
                    )
                    if boundary <= candidate <= current:
                        return candidate
        candidate_date -= dt.timedelta(days=1)
    return None


def cron_next(spec, now, search_minutes=366 * 24 * 60 * 5):
    current = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    boundary = current + dt.timedelta(minutes=search_minutes - 1)
    candidate_date = current.date()
    while candidate_date <= boundary.date():
        if cron_date_matches(spec, candidate_date):
            for hour in sorted(spec['hours']):
                for minute in sorted(spec['minutes']):
                    candidate = dt.datetime.combine(
                        candidate_date, dt.time(hour, minute), tzinfo=current.tzinfo,
                    )
                    if current <= candidate <= boundary:
                        return candidate
        candidate_date += dt.timedelta(days=1)
    return None


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
