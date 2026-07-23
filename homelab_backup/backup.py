import datetime as dt
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import backup_state as _backup_state
from .actions import (
    action_executable, run_failure_actions, run_success_actions,
)
from .backup_state import load_state, save_state
from .common import (
    CommandError, FailureSummary, GlobalLock, _print_command_failure, die,
    restic_env, run, run_cleanup,
)
from .manifest import (
    RETENTION_FLAGS, compose_model, manifest, manifests, service_label,
    validate_manifest,
)
from .identity import account, action_user
from .security import (
    clear_control_leaf, validate_control_file, validate_control_root,
    validate_trusted_roots,
)
from .storage import (
    validate_docker_bind_probe, validate_docker_environment,
    validate_runtime_sources,
)
from .staging import _check_backup_space, stage_service
from .types import GlobalConfig, ServiceManifest


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


@dataclass(frozen=True)
class BackupDependencies:
    now: Callable
    load_state: Callable
    save_state: Callable
    stage: Callable
    check_space: Callable
    run_command: Callable
    restic_environment: Callable
    success_actions: Callable
    failure_actions: Callable
    cleanup: Callable
    remove_stage: Callable
    print_command_failure: Callable

    @classmethod
    def production(cls):
        # Resolve module globals at call time so compatibility callers that
        # replace a low-level adapter still affect the production workflow.
        return cls(
            now=lambda: dt.datetime.now().astimezone(),
            load_state=load_state,
            save_state=save_state,
            stage=stage_service,
            check_space=_check_backup_space,
            run_command=run,
            restic_environment=restic_env,
            success_actions=run_success_actions,
            failure_actions=run_failure_actions,
            cleanup=run_cleanup,
            remove_stage=clear_control_leaf,
            print_command_failure=_print_command_failure,
        )


class BackupWorkflow:
    def __init__(
            self, c: GlobalConfig, m: ServiceManifest, *,
            dependencies: BackupDependencies, apply_retention=True,
            allow_low_space=False,
    ):
        self.c = c
        self.m = m
        self.dependencies = dependencies
        self.apply_retention = apply_retention
        self.allow_low_space = allow_low_space
        self.label = service_label(m)
        self.started = None
        self.state = None

    def execute(self):
        validate_manifest(self.m)
        self._mark_running()
        try:
            stage = self._prepare_stage()
        except Exception as err:
            self._record_precommit_failure(err, 'staging')
            raise
        try:
            self._commit_snapshot(stage)
        except Exception as err:
            self._record_precommit_failure(err, 'restic')
            raise
        postcommit_error = self._run_success_actions()
        retention_error = self._apply_retention()
        self._mark_success(retention_error)
        if postcommit_error is not None:
            raise postcommit_error
        return True

    def _mark_running(self):
        deps = self.dependencies
        self.started = deps.now()
        self.state = deps.load_state(self.c, self.m['service'])
        self.state.update({
            'service': self.m['service'],
            'last_attempt_at': self.started.isoformat(),
            'last_result': 'running',
            'last_error': None,
        })
        self.state.setdefault('first_seen_at', self.started.isoformat())
        try:
            deps.save_state(self.c, self.m['service'], self.state)
        except Exception as err:
            deps.cleanup(
                lambda: deps.failure_actions(
                    self.m, error=err, phase='state',
                ),
                'on_failure actions',
            )
            raise

    def _prepare_stage(self):
        deps = self.dependencies
        stage_options = (
            {'allow_low_space': True} if self.allow_low_space else {}
        )
        stage = deps.stage(self.c, self.m, **stage_options)
        deps.check_space(
            self.c, self.m, stage, 0,
            allow_low_space=self.allow_low_space,
        )
        return stage

    def _commit_snapshot(self, stage):
        deps = self.dependencies
        deps.run_command([
            'restic', 'backup', '.', '--host', self.c['host_id'],
            '--tag', f"service:{self.m['service']}",
        ], cwd=stage, env=deps.restic_environment(self.c))

    def _record_precommit_failure(self, err, failure_phase):
        deps = self.dependencies
        deps.cleanup(
            lambda: deps.failure_actions(
                self.m, error=err, phase=failure_phase,
            ),
            'on_failure actions',
        )
        partial_stage = (
            Path(self.c['staging_root']) / self.m['service']
            if self.c.get('staging_root') else None
        )
        if partial_stage is not None:
            def remove_partial_stage():
                if partial_stage.exists() or partial_stage.is_symlink():
                    deps.remove_stage(partial_stage)

            deps.cleanup(
                remove_partial_stage,
                f'remove partial staging for {self.label}',
            )
        finished = deps.now()
        self.state.update({
            'last_finished_at': finished.isoformat(),
            'last_result': 'failed',
            'last_duration_seconds': round(
                (finished - self.started).total_seconds(), 3,
            ),
            'last_error': f'{type(err).__name__}: {err}',
        })
        deps.cleanup(
            lambda: deps.save_state(
                self.c, self.m['service'], self.state,
            ),
            f'save failed backup state for {self.label}',
        )

    def _run_success_actions(self):
        deps = self.dependencies
        try:
            deps.success_actions(self.m)
        except Exception as err:
            print(
                f'ERROR: Restic snapshot for {self.label} was committed, but '
                f'on_success actions failed: {type(err).__name__}: {err}',
                file=sys.stderr,
            )
            deps.cleanup(
                lambda: deps.failure_actions(
                    self.m, error=err, phase='on_success',
                ),
                'on_failure actions',
            )
            return err
        return None

    def _apply_retention(self):
        deps = self.dependencies
        retention_error = self.state.get('last_retention_error')
        if not self.apply_retention:
            return retention_error
        try:
            deps.run_command(
                retention_cmd(self.c, self.m),
                env=deps.restic_environment(self.c),
            )
            return None
        except Exception as err:
            context = (
                f"Snapshot for {self.label} succeeded, but retention failed; "
                'maintenance will retry it later'
            )
            if isinstance(err, CommandError):
                deps.print_command_failure(err, context=context)
                return err.stderr.strip() or str(err)
            print(
                f'ERROR: {context}: {type(err).__name__}: {err}',
                file=sys.stderr,
            )
            return f'{type(err).__name__}: {err}'

    def _mark_success(self, retention_error):
        deps = self.dependencies
        finished = deps.now()
        self.state.update({
            # Completion is the schedule watermark. Cron occurrences reached
            # while this backup ran are skipped rather than queued.
            'last_success_at': finished.isoformat(),
            'last_finished_at': finished.isoformat(),
            'last_result': 'success',
            'last_duration_seconds': round(
                (finished - self.started).total_seconds(), 3,
            ),
            'last_error': None,
            'last_retention_error': retention_error,
        })
        try:
            deps.save_state(self.c, self.m['service'], self.state)
        except Exception as err:
            deps.cleanup(
                lambda: deps.failure_actions(
                    self.m, error=err, phase='state',
                ),
                'on_failure actions',
            )
            raise


def backup_one(
        c: GlobalConfig, m: ServiceManifest, *, apply_retention=True,
        allow_low_space=False, dependencies=None,
):
    workflow = BackupWorkflow(
        c, m,
        dependencies=dependencies or BackupDependencies.production(),
        apply_retention=apply_retention,
        allow_low_space=allow_low_space,
    )
    return workflow.execute()


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
        errors.append(
            f"no enabled backup.yaml manifests found under {c['services_root']}/"
        )
    for m in ms:
        label = f"{service_label(m)} ({m['_path']})"
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
            label = service_label(m)
            try:
                options = {}
                if getattr(args, 'allow_low_space', False) is True:
                    options['allow_low_space'] = True
                backup_one(c, m, **options)
            except Exception as err:
                failures.record_exception(
                    label, err,
                    message=f'ERROR: backup failed for {label}: {{error}}',
                    command_context=f'Backup failed for {label}',
                )
    failures.raise_if_any('BACKUP FAILURES')
