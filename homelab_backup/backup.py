import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from .common import CommandError, GlobalLock, _print_command_failure, atomic_write_json, die, paths_overlap, resolved_path, restic_env, run
from .config import RETENTION_FLAGS, compose_cmd, compose_model, manifest, manifests, source_path, validate_manifest
from .schedule import cron_next, cron_previous, parse_cron, parse_duration
from .storage import hooks, running_services, sync_paths, sync_volumes, validate_runtime_sources

def stage_service(c, m):
    validate_manifest(m)
    stage = resolved_path(Path(c['staging_root']) / m['service'])
    protected = [resolved_path(m['_dir'])]
    protected.extend(
        resolved_path(source_path(m, source))
        for source in (m.get('sources') or {}).get('paths', [])
    )
    for target in protected:
        if paths_overlap(stage, target):
            raise ValueError(f'staging directory {stage} overlaps protected path {target}')
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    mode = (m.get('consistency') or {}).get('mode', 'stop')
    selected = (m.get('consistency') or {}).get('services', [])
    if mode == 'hooks':
        try:
            hooks(m, 'before')
            sync_paths(m, stage)
            sync_volumes(c, m, stage)
        finally:
            hooks(m, 'after')
    elif mode == 'stop':
        sync_paths(m, stage)
        running = running_services(m)
        targets = [x for x in selected if x in running] if selected else running
        try:
            if targets:
                run(compose_cmd(m) + ['stop', '-t', str((m.get('consistency') or {}).get('timeout', 120))] + targets, cwd=m['_dir'])
            sync_paths(m, stage)
            sync_volumes(c, m, stage)
        finally:
            if targets:
                run(compose_cmd(m) + ['up', '-d'] + targets, cwd=m['_dir'])
    else:
        sync_paths(m, stage)
        sync_volumes(c, m, stage)
    meta = stage / '_meta'
    meta.mkdir(exist_ok=True)
    # backup.yaml is always included as recovery metadata, even when it is not listed in sources.paths.
    shutil.copy2(m['_path'], meta / 'backup.yaml')
    inventory = {
        'service': m['service'],
        'service_directory': m['_dir'],
        'paths': [],
        'volumes': [],
    }
    for source in (m.get('sources') or {}).get('paths', []):
        source_path = Path(source['path'])
        source_path = source_path if source_path.is_absolute() else Path(m['_dir']) / source_path
        inventory['paths'].append({
            'id': source['id'],
            'path': source['path'],
            'type': 'directory' if source_path.is_dir() else 'file',
        })
    for source in (m.get('sources') or {}).get('volumes', []):
        inventory['volumes'].append({
            'id': source['id'],
            'compose_volume': source.get('compose_volume'),
            'name': source.get('name'),
        })
    atomic_write_json(meta / 'inventory.json', inventory)
    return stage


def state_path(c, service):
    return Path(c['state_root']) / f'{service}.json'


def load_state(c, service):
    path = state_path(c, service)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(c, service, state):
    atomic_write_json(state_path(c, service), state)


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def due_status(c, m, now=None):
    now = now or dt.datetime.now().astimezone()
    schedule = m['schedule']
    if schedule.get('enabled', True) is False:
        return False, 'schedule disabled', None
    spec = parse_cron(schedule['cron'], 'schedule.cron')
    state = load_state(c, m['service'])
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
            deadline = occurrence + dt.timedelta(
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


def retention_cmd(c, m, *, prune=False, dry_run=False):
    cmd = [
        'restic', 'forget', '--host', c['host_id'], '--tag', f"service:{m['service']}",
        '--group-by', 'host,tags',
    ]
    for key, flag in RETENTION_FLAGS:
        if key in m['retention']:
            cmd += [flag, str(m['retention'][key])]
    if prune:
        cmd.append('--prune')
    if dry_run:
        cmd.append('--dry-run')
    return cmd


def backup_one(c, m, *, apply_retention=True):
    started = dt.datetime.now().astimezone()
    state = load_state(c, m['service'])
    state.update({
        'service': m['service'],
        'last_attempt_at': started.isoformat(),
        'last_result': 'running',
        'last_error': None,
        'last_retention_error': None,
    })
    state.setdefault('first_seen_at', started.isoformat())
    save_state(c, m['service'], state)
    try:
        stage = stage_service(c, m)
        run([
            'restic', 'backup', '.', '--host', c['host_id'],
            '--tag', f"service:{m['service']}",
        ], cwd=stage, env=restic_env(c))
        retention_error = None
        if apply_retention:
            try:
                run(retention_cmd(c, m), env=restic_env(c))
            except CommandError as err:
                _print_command_failure(err, context=(
                    f"Snapshot for {m['service']} succeeded, but retention failed; "
                    'maintenance will retry it later'
                ))
                retention_error = err.stderr.strip() or str(err)
        finished = dt.datetime.now().astimezone()
        state.update({
            'last_success_at': finished.isoformat(),
            'last_finished_at': finished.isoformat(),
            'last_result': 'success',
            'last_duration_seconds': round((finished - started).total_seconds(), 3),
            'last_error': None,
            'last_retention_error': retention_error,
        })
        save_state(c, m['service'], state)
        return True
    except Exception as err:
        finished = dt.datetime.now().astimezone()
        state.update({
            'last_finished_at': finished.isoformat(),
            'last_result': 'failed',
            'last_duration_seconds': round((finished - started).total_seconds(), 3),
            'last_error': f'{type(err).__name__}: {err}',
        })
        save_state(c, m['service'], state)
        raise


def cmd_list(c, args):
    now = dt.datetime.now().astimezone()
    for m in manifests(c):
        due, reason, _ = due_status(c, m, now)
        schedule = m['schedule']
        schedule_text = f"cron {schedule['cron']}"
        print(f"{m['service']}: {schedule_text}; {'DUE' if due else reason}; {m['_path']}")


def cmd_status(c, args):
    now = dt.datetime.now().astimezone()
    for m in manifests(c):
        due, reason, _ = due_status(c, m, now)
        state = load_state(c, m['service'])
        print(json.dumps({
            'service': m['service'], 'due': due, 'schedule_status': reason,
            'last_result': state.get('last_result'),
            'last_success_at': state.get('last_success_at'),
            'last_duration_seconds': state.get('last_duration_seconds'),
            'last_error': state.get('last_error'),
            'last_retention_error': state.get('last_retention_error'),
        }, ensure_ascii=False))


def cmd_validate(c, args):
    errors = []
    for command in ['docker', 'restic', 'rclone', 'rsync']:
        if not shutil.which(command):
            errors.append(f'missing command: {command}')
    if shutil.which('docker'):
        try:
            run(['docker', 'compose', 'version'])
        except CommandError as err:
            _print_command_failure(err, context='Docker Compose is unavailable')
            errors.append('docker compose version failed')
        try:
            run(['docker', 'image', 'inspect', c['volume_helper_image']], capture=True)
        except CommandError:
            errors.append(f"missing Docker helper image: {c['volume_helper_image']}")
    for key in ('password_file', 'rclone_config'):
        if not Path(c[key]).is_file():
            errors.append(f'missing config file: {c[key]}')
    ms = manifests(c, include_disabled=True)
    if not any(m.get('enabled', True) for m in ms):
        errors.append(f"no enabled backup.yaml manifests found under {c['services_root']}/*/")
    seen = {}
    for m in ms:
        label = f"{m.get('service', '<unnamed>')} ({m['_path']})"
        print(f'\n== Validating {label} ==')
        try:
            validate_manifest(m)
            name = m['service']
            if name in seen:
                raise RuntimeError(f"duplicate service name '{name}' also used by {seen[name]}")
            seen[name] = m['_path']
            model = compose_model(m)
            validate_runtime_sources(c, m, model)
            print(f'OK: {label}')
        except CommandError:
            errors.append(f'{label}: Docker Compose configuration failed')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as err:
            print(f'ERROR: {label}: {err}', file=sys.stderr)
            errors.append(f'{label}: {err}')
    if errors:
        print('\nVALIDATION FAILED', file=sys.stderr)
        for item in errors:
            print(f'  - {item}', file=sys.stderr)
        raise SystemExit(1)
    print(f'\nOK: {len(ms)} service manifest(s) validated')


def cmd_init(c, args):
    run(['restic', 'init'], env=restic_env(c))


def cmd_backup(c, args):
    ms = [manifest(c, name) for name in args.services] if args.services else manifests(c)
    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for m in ms:
            backup_one(c, m)


def cmd_run_due(c, args):
    with GlobalLock(c['lock_file'], nonblocking=True) as acquired:
        if not acquired:
            print('SKIP: another backup or maintenance process is still running')
            return
        now = dt.datetime.now().astimezone()
        due = []
        for m in manifests(c):
            is_due, reason, _ = due_status(c, m, now)
            print(f"{m['service']}: {'DUE' if is_due else 'skip'} ({reason})")
            if is_due:
                due.append(m)
        if not due:
            print('No backups are due.')
            return
        failures = []
        for m in due:
            print(f"\n== Scheduled backup: {m['service']} ==")
            try:
                backup_one(c, m)
            except Exception as err:
                failures.append((m['service'], str(err)))
                print(f"ERROR: scheduled backup failed for {m['service']}: {err}", file=sys.stderr)
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
        if failures:
            print('\nSCHEDULED BACKUP FAILURES', file=sys.stderr)
            for service, error in failures:
                print(f'  - {service}: {error}', file=sys.stderr)
            raise SystemExit(1)


def cmd_snapshots(c, args):
    cmd = ['restic', 'snapshots', '--host', c['host_id']]
    if args.service:
        cmd += ['--tag', f'service:{args.service}']
    run(cmd, env=restic_env(c))


def cmd_maintenance(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        for m in manifests(c):
            print(f"\n== Retention: {m['service']} ==")
            run(retention_cmd(c, m, dry_run=args.dry_run), env=restic_env(c))
        if not args.dry_run:
            run(['restic', 'prune'], env=restic_env(c))


def cmd_check(c, args):
    with GlobalLock(c['lock_file'], nonblocking=args.no_wait) as acquired:
        if not acquired:
            print('SKIP: backup or maintenance is still running')
            return
        cmd = ['restic', 'check']
        subset = (c.get('check') or {}).get('read_data_subset')
        if subset:
            cmd += ['--read-data-subset', str(subset)]
        run(cmd, env=restic_env(c))


def cmd_unlock(c, args):
    with GlobalLock(c['lock_file'], nonblocking=True) as acquired:
        if not acquired:
            die('another backupctl process is running')
        run(['restic', 'unlock'], env=restic_env(c))
