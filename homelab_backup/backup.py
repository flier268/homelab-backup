import datetime as dt
import shutil
import sys
from pathlib import Path

from . import backup_state as _backup_state
from .backup_state import load_state, parse_iso, save_state, state_path
from .common import (
    CommandError, FailureSummary, GlobalLock, _print_command_failure, die,
    restic_env, run, run_cleanup,
)
from .manifest import (
    RETENTION_FLAGS, compose_cmd, compose_model, manifest, manifests,
    source_path, validate_manifest,
)
from .security import (
    atomic_copy_file, atomic_write_json, clear_control_leaf,
    ensure_private_directory, paths_overlap, validate_control_root,
    validate_trusted_roots,
)
from .storage import (
    compose_identity, hooks, resolved_volume_sources, running_services,
    sync_paths, sync_volumes, validate_docker_bind_probe,
    validate_docker_environment,
    validate_no_docker_writers, validate_path_payloads, validate_runtime_sources,
)
from .types import GlobalConfig, ServiceManifest


def stage_service(c: GlobalConfig, m: ServiceManifest):
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
            run_cleanup(lambda: hooks(m, 'after'), 'after hook')
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
                run_cleanup(
                    lambda: run(
                        compose_cmd(m) + ['start'] + targets, cwd=m['_dir'],
                    ),
                    'service restart',
                )
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


def due_status(c, m, now=None):
    return _backup_state.due_status(c, m, now, state_loader=load_state)


def retention_cmd(
        c: GlobalConfig, m: ServiceManifest, *, prune=False, dry_run=False,
):
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


def backup_one(
        c: GlobalConfig, m: ServiceManifest, *, apply_retention=True,
):
    validate_manifest(m)
    partial_stage = (
        Path(c['staging_root']) / m['service']
        if c.get('staging_root') else None
    )
    started = dt.datetime.now().astimezone()
    state = load_state(c, m['service'])
    state.update({
        'service': m['service'],
        'last_attempt_at': started.isoformat(),
        'last_result': 'running',
        'last_error': None,
    })
    state.setdefault('first_seen_at', started.isoformat())
    save_state(c, m['service'], state)
    try:
        stage = stage_service(c, m)
        run([
            'restic', 'backup', '.', '--host', c['host_id'],
            '--tag', f"service:{m['service']}",
        ], cwd=stage, env=restic_env(c))
    except Exception as err:
        if partial_stage is not None:
            def remove_partial_stage():
                if partial_stage.exists() or partial_stage.is_symlink():
                    clear_control_leaf(partial_stage)

            run_cleanup(
                remove_partial_stage,
                f"remove partial staging for {m['service']}",
            )
        finished = dt.datetime.now().astimezone()
        state.update({
            'last_finished_at': finished.isoformat(),
            'last_result': 'failed',
            'last_duration_seconds': round((finished - started).total_seconds(), 3),
            'last_error': f'{type(err).__name__}: {err}',
        })
        save_state(c, m['service'], state)
        raise

    # The restic command returning successfully is the durability boundary.
    # Failures after this point must not make the committed snapshot retryable
    # or remove its complete staging data.
    retention_error = state.get('last_retention_error')
    if apply_retention:
        try:
            run(retention_cmd(c, m), env=restic_env(c))
            retention_error = None
        except Exception as err:
            context = (
                f"Snapshot for {m['service']} succeeded, but retention failed; "
                'maintenance will retry it later'
            )
            if isinstance(err, CommandError):
                _print_command_failure(err, context=context)
                retention_error = err.stderr.strip() or str(err)
            else:
                print(
                    f'ERROR: {context}: {type(err).__name__}: {err}',
                    file=sys.stderr,
                )
                retention_error = f'{type(err).__name__}: {err}'
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


def cmd_list(c, args):
    from .maintenance import cmd_list as implementation
    return implementation(c, args)


def cmd_status(c, args):
    from .maintenance import cmd_status as implementation
    return implementation(c, args)


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
    failures = FailureSummary()
    ms = []
    if args.services:
        for name in dict.fromkeys(args.services):
            try:
                ms.append(manifest(c, name))
            except Exception as err:
                failures.record_exception(
                    name, err,
                    message=f'ERROR: cannot load service {name}: {{error}}',
                    summary_error=f'manifest loading failed: {err}',
                )
    else:
        def record_manifest_error(path, err):
            failures.record_exception(
                str(path), err,
                message=f'ERROR: cannot load manifest {path}: {{error}}',
                summary_error=f'manifest loading failed: {err}',
            )

        ms = manifests(c, on_error=record_manifest_error)

    with GlobalLock(c['lock_file']) as acquired:
        if not acquired:
            die('another backupctl process is running')
        for m in ms:
            service = m.get('service', '<unnamed>')
            try:
                backup_one(c, m)
            except Exception as err:
                failures.record_exception(
                    service, err,
                    message=f'ERROR: backup failed for {service}: {{error}}',
                    command_context=f'Backup failed for {service}',
                )
    failures.raise_if_any('BACKUP FAILURES')


def cmd_run_due(c, args):
    from .maintenance import cmd_run_due as implementation
    return implementation(c, args)


def cmd_snapshots(c, args):
    from .maintenance import cmd_snapshots as implementation
    return implementation(c, args)


def cmd_maintenance(c, args):
    from .maintenance import cmd_maintenance as implementation
    return implementation(c, args)


def cmd_check(c, args):
    from .maintenance import cmd_check as implementation
    return implementation(c, args)


def cmd_unlock(c, args):
    from .maintenance import cmd_unlock as implementation
    return implementation(c, args)
