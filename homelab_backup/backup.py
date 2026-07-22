import datetime as dt
import shutil
import sys
from pathlib import Path

from . import backup_state as _backup_state
from .actions import (
    action_executable, run_before_actions, run_finally_actions,
    run_failure_actions, run_success_actions,
)
from .backup_state import load_state, save_state
from .btrfs_snapshot import (
    SnapshotTransaction, cleanup_snapshot_state,
)
from .common import (
    MIN_FREE_BYTES, CommandError, FailureSummary, GlobalLock,
    _print_command_failure, die, format_bytes, restic_env, run, run_cleanup,
)
from .manifest import (
    RETENTION_FLAGS, compose_model, compose_run, manifest, manifests,
    source_path, validate_manifest,
)
from .identity import account, action_user
from .security import (
    atomic_copy_file, atomic_write_json, clear_control_leaf,
    ensure_private_directory, paths_overlap, validate_control_root,
    validate_control_file, validate_trusted_roots,
)
from .storage import (
    compose_identity, docker_mount_conflicts, docker_writer_maps,
    estimate_backup_size, hooks, resolved_volume_sources,
    running_services,
    sync_paths, sync_volumes, validate_docker_bind_probe,
    validate_docker_environment,
    validate_no_docker_writers, validate_path_payloads, validate_runtime_sources,
)
from .types import GlobalConfig, ServiceManifest


def _check_backup_space(
        c, m, stage, estimated_size, *, allow_low_space=False,
):
    free = shutil.disk_usage(stage).free
    required = estimated_size + MIN_FREE_BYTES
    if free >= required:
        return
    shortfall = required - free
    print(
        f"WARNING: backup for {m['service']} is estimated at "
        f'{format_bytes(estimated_size)}, but only {format_bytes(free)} is free '
        f'on {stage}; the required 1.00 GiB reserve would be short by '
        f'{format_bytes(shortfall)}.',
        file=sys.stderr,
    )
    if allow_low_space:
        print(
            'WARNING: continuing because --allow-low-space was specified.',
            file=sys.stderr,
        )
        return
    raise RuntimeError(
        'insufficient backup staging space; free additional space or explicitly '
        'use --allow-low-space'
    )


def stage_service(
        c: GlobalConfig, m: ServiceManifest, *, allow_low_space=False,
):
    validate_manifest(m)
    validate_trusted_roots(c['trusted_data_roots'])
    # Recover stale snapshot transactions before application actions.  A
    # service may have changed consistency mode since the interrupted run.
    cleanup_snapshot_state(c, m['service'])
    staging_root = Path(c['staging_root'])
    stage = staging_root / m['service']
    protected = [Path(m['_dir'])]
    protected.extend(source_path(m, source) for source in (m.get('sources') or {}).get('paths', []))
    for target in protected:
        if paths_overlap(stage, target):
            raise ValueError(f'staging directory {stage} overlaps protected path {target}')
    ensure_private_directory(staging_root)
    ensure_private_directory(stage, replace=True)
    mode = (m.get('consistency') or {}).get('mode', 'stop')

    optional_action_failures = []

    def finally_actions():
        def execute():
            optional_action_failures.extend(run_finally_actions(m))

        if sys.exc_info()[1] is None:
            execute()
        else:
            run_cleanup(execute, 'finally actions')

    try:
        optional_action_failures.extend(run_before_actions(m))
    except Exception:
        run_cleanup(lambda: run_finally_actions(m), 'finally actions')
        raise

    try:
        validate_docker_environment()
        validate_docker_bind_probe(c)
        model = compose_model(m)
        validate_runtime_sources(
            c, m, model, allow_missing_paths=mode == 'hooks',
        )
        resolved_volumes = resolved_volume_sources(m, model=model)
        identity = compose_identity(m, model=model, resolved=resolved_volumes)
    except Exception:
        run_cleanup(lambda: run_finally_actions(m), 'finally actions')
        raise

    def sync_all(
            *, source_overrides=None, path_methods=None, volume_methods=None,
            path_writers=None, volume_writers=None,
    ):
        estimated_size = estimate_backup_size(
            c, m, resolved=resolved_volumes,
            source_overrides=source_overrides,
        )
        _check_backup_space(
            c, m, stage, estimated_size, allow_low_space=allow_low_space,
        )

        def before_copy(_source):
            _check_backup_space(
                c, m, stage, 0, allow_low_space=allow_low_space,
            )

        path_result = sync_paths(
            c, m, stage, before_copy=before_copy,
            source_overrides=source_overrides,
            capture_methods=path_methods,
            writer_map=path_writers,
        )
        _check_backup_space(
            c, m, stage, 0, allow_low_space=allow_low_space,
        )
        volume_result = list(sync_volumes(
            c, m, stage, resolved=resolved_volumes,
            before_copy=before_copy,
            capture_methods=volume_methods,
            writer_map=volume_writers,
        ) or [])
        _check_backup_space(
            c, m, stage, 0, allow_low_space=allow_low_space,
        )
        return path_result, volume_result

    def quiesced_methods():
        return (
            {
                source['id']: 'quiesced-copy'
                for source in (m.get('sources') or {}).get('paths', [])
            },
            {
                source['id']: 'quiesced-copy'
                for source, _name in resolved_volumes
            },
        )

    def warn_writers(path_writers, volume_writers):
        for source_id, containers in sorted(path_writers.items()):
            print(
                f'WARNING: path source {source_id!r} is being written by '
                f'containers: {", ".join(containers)}; using best-effort copy',
                file=sys.stderr,
            )
        for source_id, containers in sorted(volume_writers.items()):
            print(
                f'WARNING: volume source {source_id!r} is being written by '
                f'containers: {", ".join(containers)}; using best-effort copy',
                file=sys.stderr,
            )

    if mode == 'hooks':
        try:
            hooks(m, 'before')
            validate_no_docker_writers(
                m, identity, resolved_volumes, project_must_be_stopped=False,
            )
            validate_path_payloads(c, m)
            path_methods, volume_methods = quiesced_methods()
            path_inventory, volume_inventory = sync_all(
                path_methods=path_methods, volume_methods=volume_methods,
            )
        finally:
            try:
                run_cleanup(lambda: hooks(m, 'after'), 'after hook')
            finally:
                finally_actions()
    elif mode == 'stop':
        targets = ()
        try:
            targets = running_services(m)
            if targets:
                compose_run(
                    m,
                    ['stop', '-t', str(
                        (m.get('consistency') or {}).get('timeout', 120)
                    )] + targets,
                    runner=run,
                )
            validate_no_docker_writers(
                m, identity, resolved_volumes, project_must_be_stopped=True,
            )
            validate_path_payloads(c, m)
            path_methods, volume_methods = quiesced_methods()
            path_inventory, volume_inventory = sync_all(
                path_methods=path_methods, volume_methods=volume_methods,
            )
        finally:
            try:
                if targets:
                    run_cleanup(
                        lambda: compose_run(m, ['start'] + targets, runner=run),
                        'service restart',
                    )
            finally:
                finally_actions()
    elif mode == 'external':
        try:
            validate_no_docker_writers(
                m, identity, resolved_volumes, project_must_be_stopped=False,
            )
            validate_path_payloads(c, m)
            path_methods, volume_methods = quiesced_methods()
            path_inventory, volume_inventory = sync_all(
                path_methods=path_methods, volume_methods=volume_methods,
            )
        finally:
            finally_actions()
    elif mode == 'live':
        try:
            validate_path_payloads(c, m)
            path_writers, volume_writers = docker_writer_maps(m, resolved_volumes)
            warn_writers(path_writers, volume_writers)
            path_methods = {
                source['id']: (
                    'best-effort' if source['id'] in path_writers
                    else 'quiesced-copy'
                )
                for source in (m.get('sources') or {}).get('paths', [])
            }
            volume_methods = {
                source['id']: (
                    'best-effort' if source['id'] in volume_writers
                    else 'quiesced-copy'
                )
                for source, _name in resolved_volumes
            }
            path_inventory, volume_inventory = sync_all(
                path_methods=path_methods, volume_methods=volume_methods,
                path_writers=path_writers, volume_writers=volume_writers,
            )
        finally:
            finally_actions()
    elif mode == 'snapshot':
        try:
            validate_path_payloads(c, m)
            transaction = SnapshotTransaction(
                c, m,
                lambda path: docker_mount_conflicts(
                    [path], [], writable_only=False,
                ),
            )
            try:
                source_overrides = transaction.create()
                path_writers, volume_writers = docker_writer_maps(
                    m, resolved_volumes,
                )
                warn_writers(
                    {
                        key: value for key, value in path_writers.items()
                        if key not in source_overrides
                    },
                    volume_writers,
                )
                path_methods = {
                    source['id']: (
                        'btrfs-snapshot'
                        if source['id'] in source_overrides
                        else 'best-effort'
                        if source['id'] in path_writers
                        else 'quiesced-copy'
                    )
                    for source in (m.get('sources') or {}).get('paths', [])
                }
                volume_methods = {
                    source['id']: (
                        'best-effort' if source['id'] in volume_writers
                        else 'quiesced-copy'
                    )
                    for source, _name in resolved_volumes
                }
                path_inventory, volume_inventory = sync_all(
                    source_overrides=source_overrides,
                    path_methods=path_methods,
                    volume_methods=volume_methods,
                    path_writers=path_writers,
                    volume_writers=volume_writers,
                )
            finally:
                run_cleanup(transaction.cleanup, 'Btrfs snapshot')
        finally:
            finally_actions()
    else:  # validate_manifest keeps this unreachable.
        raise RuntimeError(f'unsupported consistency mode: {mode}')

    source_entries = [
        item for item in (*path_inventory, *volume_inventory)
        if item.get('present', True)
    ]
    capture_methods = [
        item.get('capture_method', 'quiesced-copy') for item in source_entries
    ]
    if optional_action_failures or 'best-effort' in capture_methods:
        overall_capture = 'best-effort'
    elif capture_methods and all(
            item == 'btrfs-snapshot' for item in capture_methods
    ):
        overall_capture = 'btrfs-snapshot'
    else:
        overall_capture = 'quiesced-copy'
    all_writers = sorted({
        container
        for item in source_entries
        for container in item.get('writers', [])
    })
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
        'consistency': {
            'mode': mode,
            'guarantee': overall_capture,
            'optional_action_failures': optional_action_failures,
            'writers': all_writers,
        },
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
        allow_low_space=False,
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
    try:
        save_state(c, m['service'], state)
    except Exception as err:
        run_cleanup(
            lambda: run_failure_actions(m, error=err, phase='state'),
            'on_failure actions',
        )
        raise
    failure_phase = 'staging'
    try:
        stage_options = {}
        if allow_low_space:
            stage_options['allow_low_space'] = True
        stage = stage_service(c, m, **stage_options)
        _check_backup_space(
            c, m, stage, 0, allow_low_space=allow_low_space,
        )
        failure_phase = 'restic'
        run([
            'restic', 'backup', '.', '--host', c['host_id'],
            '--tag', f"service:{m['service']}",
        ], cwd=stage, env=restic_env(c))
    except Exception as err:
        run_cleanup(
            lambda: run_failure_actions(
                m, error=err, phase=failure_phase,
            ),
            'on_failure actions',
        )
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
        run_cleanup(
            lambda: save_state(c, m['service'], state),
            f"save failed backup state for {m['service']}",
        )
        raise

    # The restic command returning successfully is the durability boundary.
    # Failures after this point must not make the committed snapshot retryable
    # or remove its complete staging data.
    postcommit_error = None
    try:
        run_success_actions(m)
    except Exception as err:
        postcommit_error = err
        print(
            f'ERROR: Restic snapshot for {m["service"]} was committed, but '
            f'on_success actions failed: {type(err).__name__}: {err}',
            file=sys.stderr,
        )
        run_cleanup(
            lambda: run_failure_actions(
                m, error=err, phase='on_success',
            ),
            'on_failure actions',
        )

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
    try:
        save_state(c, m['service'], state)
    except Exception as err:
        run_cleanup(
            lambda: run_failure_actions(m, error=err, phase='state'),
            'on_failure actions',
        )
        raise
    if postcommit_error is not None:
        raise postcommit_error
    return True


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
        try:
            validate_control_file(c[key])
        except (OSError, ValueError) as err:
            errors.append(f'unsafe config file {c[key]}: {err}')
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
            for phase in ('before', 'finally', 'on_success', 'on_failure'):
                for action in (m.get('actions') or {}).get(phase, []):
                    executable = action['command'][0]
                    run_as = action_user(action)
                    account(run_as)
                    if action_executable(
                            action['command'], run_as=run_as,
                    ) is None:
                        raise RuntimeError(
                            f'action {action["name"]!r} executable is missing: '
                            f'{executable}'
                        )
            if (m.get('consistency') or {}).get('mode') == 'snapshot':
                for executable in ('btrfs', 'findmnt'):
                    if shutil.which(executable) is None:
                        raise RuntimeError(
                            f'snapshot mode requires command: {executable}'
                        )
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
                options = {}
                if getattr(args, 'allow_low_space', False) is True:
                    options['allow_low_space'] = True
                backup_one(c, m, **options)
            except Exception as err:
                failures.record_exception(
                    service, err,
                    message=f'ERROR: backup failed for {service}: {{error}}',
                    command_context=f'Backup failed for {service}',
                )
    failures.raise_if_any('BACKUP FAILURES')
