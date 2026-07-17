import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from .common import CommandError, GlobalLock, _print_command_failure, die, restic_env, run
from .config import RETENTION_FLAGS, compose_cmd, compose_model, manifest, manifests, source_path, validate_manifest
from .schedule import cron_next, cron_previous, local_now, parse_cron, parse_duration
from .security import (
    atomic_copy_file, atomic_write_json, ensure_control_directory,
    ensure_private_directory, paths_overlap, validate_control_root,
    validate_trusted_roots,
)
from .storage import (
    compose_identity, hooks, resolved_volume_sources, running_services,
    sync_paths, sync_volumes, validate_docker_bind_probe,
    validate_docker_environment,
    validate_no_docker_writers, validate_path_payloads, validate_runtime_sources,
)

def stage_service(c, m):
    validate_manifest(m)
    validate_trusted_roots(c['trusted_data_roots'])
    staging_root = Path(c['staging_root'])
    stage = staging_root / m['service']
    protected = [Path(m['_dir'])]
    protected.extend(source_path(m, source) for source in (m.get('sources') or {}).get('paths', []))
    for target in protected:
        if paths_overlap(stage, target):
            raise ValueError(f'staging directory {stage} overlaps protected path {target}')
    ensure_private_directory(staging_root)
    ensure_private_directory(stage, replace=True)
    validate_docker_environment()
    validate_docker_bind_probe(c)
    model = compose_model(m)
    mode = (m.get('consistency') or {}).get('mode', 'stop')
    validate_runtime_sources(c, m, model, allow_missing_paths=mode == 'hooks')
    resolved_volumes = resolved_volume_sources(m, model=model)
    identity = compose_identity(m, model=model, resolved=resolved_volumes)
    if mode == 'hooks':
        try:
            hooks(m, 'before')
            validate_no_docker_writers(
                m, identity, resolved_volumes, project_must_be_stopped=False,
            )
            validate_path_payloads(c, m)
            path_inventory = sync_paths(c, m, stage)
            volume_inventory = list(sync_volumes(c, m, stage, resolved=resolved_volumes) or [])
        finally:
            hooks(m, 'after')
    elif mode == 'stop':
        running = running_services(m)
        targets = running
        try:
            if targets:
                run(compose_cmd(m) + ['stop', '-t', str((m.get('consistency') or {}).get('timeout', 120))] + targets, cwd=m['_dir'])
            validate_no_docker_writers(
                m, identity, resolved_volumes, project_must_be_stopped=True,
            )
            validate_path_payloads(c, m)
            path_inventory = sync_paths(c, m, stage)
            volume_inventory = list(sync_volumes(c, m, stage, resolved=resolved_volumes) or [])
        finally:
            if targets:
                run(compose_cmd(m) + ['start'] + targets, cwd=m['_dir'])
    else:
        validate_no_docker_writers(
            m, identity, resolved_volumes, project_must_be_stopped=False,
        )
        validate_path_payloads(c, m)
        path_inventory = sync_paths(c, m, stage)
        volume_inventory = list(sync_volumes(c, m, stage, resolved=resolved_volumes) or [])
    meta = stage / '_meta'
    ensure_private_directory(meta)
    # backup.yaml is always included as recovery metadata, even when it is not listed in sources.paths.
    atomic_copy_file(m['_path'], meta / 'backup.yaml')
    inventory = {
        'version': 1,
        'service': m['service'],
        'service_directory': m['_dir'],
        'paths': path_inventory,
        'volumes': volume_inventory,
        'compose': identity,
    }
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


def due_status(c, m, now=None):
    now = now or local_now()
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
            # Boundary: completion is the schedule watermark. Cron occurrences
            # reached while this backup was running are skipped, not queued.
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
    now = local_now()
    for m in manifests(c):
        due, reason, _ = due_status(c, m, now)
        schedule = m['schedule']
        schedule_text = f"cron {schedule['cron']}"
        print(f"{m['service']}: {schedule_text}; {'DUE' if due else reason}; {m['_path']}")


def cmd_status(c, args):
    now = local_now()
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
    try:
        validate_trusted_roots(c['trusted_data_roots'])
    except (OSError, ValueError, RuntimeError) as err:
        errors.append(f'trusted_data_roots are unsupported: {err}')
    for key in ('staging_root', 'restore_root'):
        try:
            validate_control_root(c[key])
        except (OSError, ValueError, RuntimeError) as err:
            errors.append(f'{key} is unsupported: {err}')
    for command in ['docker', 'restic', 'rclone', 'rsync']:
        if not shutil.which(command):
            errors.append(f'missing command: {command}')
    if shutil.which('docker'):
        try:
            validate_docker_environment()
            validate_docker_bind_probe(c)
        except (CommandError, OSError, ValueError, RuntimeError) as err:
            errors.append(f'Docker environment is unsupported: {err}')
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
    def record_manifest_error(path, err):
        print(f'ERROR: {path}: {err}', file=sys.stderr)
        errors.append(f'{path}: {err}')

    ms = manifests(
        c,
        include_disabled=True,
        on_error=record_manifest_error,
    )
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
            validate_runtime_sources(
                c, m, model,
                allow_missing_paths=(m.get('consistency') or {}).get('mode') == 'hooks',
            )
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
        now = local_now()
        due = []
        failures = []

        def record_manifest_error(path, err):
            failures.append((str(path), f'manifest loading failed: {err}'))
            print(f'ERROR: manifest loading failed for {path}: {err}', file=sys.stderr)

        for m in manifests(c, on_error=record_manifest_error):
            service = m.get('service', '<unnamed>')
            try:
                is_due, reason, _ = due_status(c, m, now)
            except Exception as err:
                failures.append((service, f'schedule planning failed: {err}'))
                print(
                    f'ERROR: schedule planning failed for {service}: {err}',
                    file=sys.stderr,
                )
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
                continue
            print(f"{m['service']}: {'DUE' if is_due else 'skip'} ({reason})")
            if is_due:
                due.append(m)
        if not due:
            print('No backups are due.')
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
        failures = []

        def record_manifest_error(path, err):
            failures.append((str(path), f'manifest loading failed: {err}'))
            print(f'ERROR: manifest loading failed for {path}: {err}', file=sys.stderr)

        for m in manifests(c, on_error=record_manifest_error):
            service = m.get('service', '<unnamed>')
            print(f"\n== Retention: {service} ==")
            try:
                run(retention_cmd(c, m, dry_run=args.dry_run), env=restic_env(c))
            except Exception as err:
                if isinstance(err, CommandError):
                    _print_command_failure(
                        err,
                        context=f'Retention failed for {service}',
                    )
                else:
                    print(f'ERROR: retention failed for {service}: {err}', file=sys.stderr)
                failures.append((service, str(err)))
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
        if not args.dry_run:
            try:
                run(['restic', 'prune'], env=restic_env(c))
            except Exception as err:
                if isinstance(err, CommandError):
                    _print_command_failure(err, context='Repository prune failed')
                else:
                    print(f'ERROR: repository prune failed: {err}', file=sys.stderr)
                failures.append(('repository prune', str(err)))
                if os.environ.get('BACKUPCTL_DEBUG') == '1':
                    raise
        if failures:
            print('\nMAINTENANCE FAILURES', file=sys.stderr)
            for operation, error in failures:
                print(f'  - {operation}: {error}', file=sys.stderr)
            raise SystemExit(1)


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
