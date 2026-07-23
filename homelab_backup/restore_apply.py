import errno
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .common import run, run_cleanup
from .manifest import compose_run, source_path
from .restore_plan import (
    RestorePlan,
    compose_authorization_projection,
    compose_targets,
    deferred_compose_sources,
    inventory_volumes,
    load_restore_inventory,
    prepare_restore_plan,
    restored_path_details,
    restore_authorization_projection,
    validate_restore_inventory,
    validate_restore_path_separation,
    validate_restore_sources,
)
from .security import (
    atomic_copy_file, clear_control_leaf, data_object_metadata_state,
    data_object_state, ensure_control_parent, open_data_parent, open_data_path,
    remove_data_entry, validate_data_parent, validate_data_path, validate_payload,
)
from .storage import (
    build_path_filter_args, create_restore_volume, docker_mount_conflicts,
    docker_project_containers, docker_volume_exists, sync_volumes,
    validate_volume_identity, volume_owned_by_operation,
)
from .types import GlobalConfig, ServiceManifest


def compose_files_exist(m):
    return all(
        (Path(m['_dir']) / item).exists()
        for item in m.get('compose', {}).get('files', ['compose.yaml'])
    )


def normalize_restore_target(parent_fd, name, source_type):
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    compatible = (
        source_type == 'directory' and stat.S_ISDIR(metadata.st_mode)
        or source_type == 'file' and stat.S_ISREG(metadata.st_mode)
        or source_type == 'symlink' and stat.S_ISLNK(metadata.st_mode)
    )
    if not compatible:
        remove_data_entry(parent_fd, name)


def _restore_non_directory(restored, parent_fd, name, *, on_publish=None):
    temporary = None
    temporary_fd = -1
    temporary_identity = None
    try:
        for _attempt in range(16):
            candidate = f'.backupctl-restore-{secrets.token_hex(8)}'
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            initial = os.stat(
                candidate, dir_fd=parent_fd, follow_symlinks=False,
            )
            temporary_fd = os.open(
                candidate,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            opened = os.fstat(temporary_fd)
            if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError(
                    'restore publication directory changed while being opened'
                )
            temporary = candidate
            temporary_identity = (opened.st_dev, opened.st_ino)
            break
        if temporary_fd < 0:
            raise RuntimeError('cannot reserve a restore publication directory')
        run([
            'rsync', '-aHAX', '--numeric-ids', str(restored),
            f'/proc/self/fd/{temporary_fd}/payload',
        ], pass_fds=(temporary_fd,))
        payload_fd = os.open(
            'payload', os.O_PATH | os.O_NOFOLLOW, dir_fd=temporary_fd,
        )
        published = os.fstat(payload_fd)
        os.replace(
            'payload', name,
            src_dir_fd=temporary_fd, dst_dir_fd=parent_fd,
        )
        identity = (published.st_dev, published.st_ino)
        try:
            if on_publish is not None:
                on_publish(identity, data_object_state(payload_fd))
        finally:
            os.close(payload_fd)
        os.fsync(parent_fd)
        return identity
    finally:
        if temporary_fd >= 0:
            try:
                remove_data_entry(temporary_fd, 'payload')
            finally:
                os.close(temporary_fd)
        if temporary is not None:
            try:
                metadata = os.stat(
                    temporary, dir_fd=parent_fd, follow_symlinks=False,
                )
                if stat.S_ISDIR(metadata.st_mode) and \
                        (metadata.st_dev, metadata.st_ino) == temporary_identity:
                    os.rmdir(temporary, dir_fd=parent_fd)
            except (FileNotFoundError, OSError):
                pass


def _restore_rebuild_directory(
        restored, parent_fd, name, *, on_publish=None,
):
    temporary = None
    temporary_fd = -1
    temporary_identity = None
    try:
        for _attempt in range(16):
            candidate = f'.backupctl-restore-{secrets.token_hex(8)}'
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            initial = os.stat(
                candidate, dir_fd=parent_fd, follow_symlinks=False,
            )
            temporary_fd = os.open(
                candidate,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            opened = os.fstat(temporary_fd)
            if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError(
                    'restore publication directory changed while being opened'
                )
            temporary = candidate
            temporary_identity = (opened.st_dev, opened.st_ino)
            break
        if temporary_fd < 0:
            raise RuntimeError('cannot reserve a restore publication directory')
        command = ['rsync', '-aHAX', '--numeric-ids', '--delete']
        command += [f'{restored}/', f'/proc/self/fd/{temporary_fd}/']
        run(command, pass_fds=(temporary_fd,))
        prepared_state = data_object_state(temporary_fd)
        os.replace(
            temporary, name,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        temporary = None
        published_state = (
            data_object_metadata_state(temporary_fd), prepared_state[1],
        )
        if on_publish is not None:
            on_publish(temporary_identity, published_state)
        os.fsync(parent_fd)
        return temporary_identity
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary is not None:
            try:
                metadata = os.stat(
                    temporary, dir_fd=parent_fd, follow_symlinks=False,
                )
                if stat.S_ISDIR(metadata.st_mode) and \
                        (metadata.st_dev, metadata.st_ino) == temporary_identity:
                    remove_data_entry(parent_fd, temporary)
            except (FileNotFoundError, OSError):
                pass


def restore_path_source(
        m, root, source, inventory, *, c=None, rebuild=False, on_publish=None,
):
    source_type, restored, target = restored_path_details(m, root, source, inventory)
    if source_type == 'missing':
        if source.get('required', True):
            raise RuntimeError(f'restored path source is missing: {restored}')
        return
    if source_type == 'file' and (not restored.is_file() or restored.is_symlink()):
        raise RuntimeError(f'restored file artifact is missing: {restored}')
    if source_type == 'symlink' and not restored.is_symlink():
        raise RuntimeError(f'restored symlink artifact is missing: {restored}')
    if source_type == 'directory' and (not restored.is_dir() or restored.is_symlink()):
        raise RuntimeError(f'restored directory artifact is missing: {restored}')
    direct_library_call = c is None
    c = c or {'trusted_data_roots': [str(Path(target).parent)]}
    if direct_library_call:
        try:
            validate_data_path(target, c['trusted_data_roots'])
        except FileNotFoundError:
            pass
    elif rebuild:
        validate_data_parent(target, c['trusted_data_roots'], allow_missing=True)
    else:
        validate_data_path(target, c['trusted_data_roots'])
    validate_payload(restored)
    if hasattr(inventory, 'paths_by_id'):
        entry = inventory.paths_by_id[source['id']]
        ancestor_metadata = [item.model_dump() for item in entry.ancestors]
    else:
        entry = next(
            item for item in inventory['paths']
            if item['id'] == source['id']
        )
        ancestor_metadata = entry.get('ancestors')
    parent_fd, name = open_data_parent(
        target, c['trusted_data_roots'],
        create_metadata=ancestor_metadata if rebuild else None,
    )
    try:
        normalize_restore_target(parent_fd, name, source_type)
        if source_type in ('file', 'symlink'):
            return _restore_non_directory(
                restored, parent_fd, name, on_publish=on_publish,
            )
        if rebuild:
            return _restore_rebuild_directory(
                restored, parent_fd, name, on_publish=on_publish,
            )
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        target_fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            command = ['rsync', '-aHAX', '--numeric-ids', '--delete']
            command += build_path_filter_args(
                source, protect_destination_dirs=True,
            )
            command += [f'{restored}/', f'/proc/self/fd/{target_fd}/']
            run(command, pass_fds=(target_fd,))
            metadata = os.fstat(target_fd)
            identity = (metadata.st_dev, metadata.st_ino)
            if on_publish is not None:
                on_publish(identity, data_object_state(target_fd))
            return identity
        finally:
            os.close(target_fd)
    finally:
        os.close(parent_fd)


def _stop_running_services(plan):
    targets = list(plan.running_services)
    if targets:
        compose_run(
            plan.manifest,
            ['stop', '-t', str(
                (plan.manifest.get('consistency') or {}).get('timeout', 120)
            )] + targets,
            runner=run,
        )
    return targets


def _dynamic_preflight(c, plan):
    path_targets = [
        source_path(plan.manifest, source)
        for source in (plan.manifest.get('sources') or {}).get('paths', [])
    ]
    conflicts = docker_mount_conflicts(
        path_targets,
        plan.all_volume_names if plan.mode == 'rebuild'
        else [name for _source, name in plan.volumes],
        include_stopped=plan.mode == 'rebuild',
        writable_only=False,
    )
    if conflicts:
        raise RuntimeError(f'restore targets are used by containers: {", ".join(conflicts)}')
    remaining = docker_project_containers(
        plan.project_name, include_stopped=plan.mode == 'rebuild',
    )
    if remaining:
        raise RuntimeError(
            'Compose project still has running containers after stop: '
            + ', '.join(remaining)
        )
    for source in (plan.manifest.get('sources') or {}).get('paths', []):
        target = source_path(plan.manifest, source)
        trusted_roots = c.get('trusted_data_roots') or [str(Path(target).parent)]
        if plan.mode == 'rebuild':
            validate_data_parent(target, trusted_roots, allow_missing=True)
        else:
            validate_data_path(target, trusted_roots)
        entry = _plan_path_entry(plan, source['id'])
        present = (
            entry.present if hasattr(entry, 'present')
            else entry['present']
        )
        exists = target.exists() or target.is_symlink()
        if plan.mode == 'rebuild':
            if exists:
                raise RuntimeError(f'rebuild target appeared during preflight: {target}')
        elif present and not exists:
            raise RuntimeError(f'existing target disappeared during preflight: {target}')
    if plan.mode == 'rebuild':
        for name in plan.all_volume_names:
            if docker_volume_exists(name):
                raise RuntimeError(f'rebuild volume appeared during preflight: {name}')
    for source, name in plan.volumes:
        exists = docker_volume_exists(name)
        if plan.mode == 'existing':
            if not exists:
                raise RuntimeError(f'existing volume disappeared during preflight: {name}')
            validate_volume_identity(
                name, project_name=plan.project_name,
                logical_name=source.get('compose_volume'),
            )
    if plan.mode == 'rebuild':
        manifest_target = Path(plan.manifest.get(
            '_path', Path(plan.manifest['_dir']) / 'backup.yaml',
        ))
        controls = (manifest_target, *compose_targets(plan.manifest))
        if any(path.exists() or path.is_symlink() for path in controls):
            raise RuntimeError('rebuild control target appeared during preflight')


def _path_identity(path):
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


@dataclass(frozen=True)
class RollbackDependencies:
    run_command: Callable
    cleanup: Callable
    volume_owned: Callable
    open_parent: Callable
    open_path: Callable
    object_state: Callable
    remove_entry: Callable
    clear_leaf: Callable

    @classmethod
    def production(cls):
        return cls(
            run, run_cleanup, volume_owned_by_operation, open_data_parent,
            open_data_path, data_object_state, remove_data_entry,
            clear_control_leaf,
        )


@dataclass
class VolumeClaim:
    name: str
    operation_id: str
    owned: bool = False

    @property
    def label(self):
        return self.name

    def rollback(self, deps):
        if not self.owned:
            return
        if deps.volume_owned(self.name, self.operation_id):
            deps.run_command(['docker', 'volume', 'rm', self.name])
        else:
            print(
                f'WARNING: preserving rebuild volume whose ownership changed: '
                f'{self.name}', file=sys.stderr,
            )


@dataclass
class DataAncestorClaim:
    path: Path
    identity: tuple[int, int]
    trusted_roots: tuple[str, ...]

    @property
    def label(self):
        return str(self.path)

    def rollback(self, deps):
        try:
            parent_fd, name = deps.open_parent(self.path, self.trusted_roots)
        except FileNotFoundError:
            return
        try:
            try:
                metadata = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                print(
                    f'WARNING: preserving rebuild ancestor whose ownership '
                    f'changed: {self.path}', file=sys.stderr,
                )
                return
            try:
                os.rmdir(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as err:
                if err.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                print(
                    f'WARNING: preserving non-empty rebuild ancestor: '
                    f'{self.path}', file=sys.stderr,
                )
        finally:
            os.close(parent_fd)


@dataclass
class DataPathClaim:
    path: Path
    trusted_roots: tuple[str, ...]
    identity: tuple[int, int] | None = None
    state: object = None
    owned: bool = False

    @property
    def label(self):
        return str(self.path)

    def rollback(self, deps):
        if not self.owned:
            return
        try:
            parent_fd, name = deps.open_parent(
                self.path, self.trusted_roots,
            )
        except FileNotFoundError:
            return
        try:
            try:
                metadata = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                print(
                    f'WARNING: preserving rebuild target whose ownership '
                    f'changed: {self.path}', file=sys.stderr,
                )
                return
            descriptor, _metadata = deps.open_path(
                self.path, self.trusted_roots,
            )
            try:
                current_state = deps.object_state(descriptor)
            finally:
                os.close(descriptor)
            if current_state != self.state:
                print(
                    f'WARNING: preserving rebuild target modified after '
                    f'publication: {self.path}', file=sys.stderr,
                )
                return
            deps.remove_entry(parent_fd, name)
        finally:
            os.close(parent_fd)


@dataclass
class ControlPathClaim:
    path: Path
    identity: tuple[int, int] | None = None
    owned: bool = False

    @property
    def label(self):
        return str(self.path)

    def rollback(self, deps):
        if not self.owned:
            return
        try:
            current_identity = _path_identity(self.path)
        except FileNotFoundError:
            return
        if current_identity != self.identity:
            print(
                f'WARNING: preserving rebuild target whose ownership changed: '
                f'{self.path}', file=sys.stderr,
            )
            return
        deps.clear_leaf(self.path)


@dataclass
class RollbackLedger:
    claims: list = field(default_factory=list)

    def add(self, claim):
        self.claims.append(claim)
        return claim

    def rollback(self, dependencies=None):
        deps = dependencies or RollbackDependencies.production()
        for claim in reversed(self.claims):
            deps.cleanup(
                lambda claim=claim: claim.rollback(deps),
                f'remove rebuild target {claim.label}',
            )


@dataclass
class RestoreChanges:
    rollback: RollbackLedger = field(default_factory=RollbackLedger)
    live_mutations: list[str] = field(default_factory=list)


def _claim_rebuild_path(c, target, source_type, ancestors, claim, ledger):
    def ancestor_created(path, identity):
        ledger.add(DataAncestorClaim(
            Path(path), identity, tuple(c['trusted_data_roots']),
        ))

    parent_fd, name = open_data_parent(
        target, c['trusted_data_roots'], create_metadata=ancestors,
        on_create=ancestor_created,
    )
    try:
        if source_type == 'directory':
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        else:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=parent_fd,
            )
        try:
            metadata = os.fstat(descriptor)
            claim.owned = True
            claim.identity = (metadata.st_dev, metadata.st_ino)
            claim.state = data_object_state(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _plan_path_entry(plan, source_id):
    if hasattr(plan.inventory, 'paths_by_id'):
        return plan.inventory.paths_by_id[source_id]
    return next(
        item for item in plan.inventory['paths']
        if item['id'] == source_id
    )


def _restore_data(c, plan, changes, operation_id):
    ledger = changes.rollback
    live_mutations = changes.live_mutations
    if plan.mode == 'rebuild':
        for source, name in plan.volumes:
            volume_claim = ledger.add(VolumeClaim(name, operation_id))
            create_restore_volume(
                name, service=plan.manifest['service'], source=source,
                project_name=plan.project_name,
                operation_id=operation_id,
                on_created=lambda _name, claim=volume_claim:
                setattr(claim, 'owned', True),
            )
    for source in (plan.manifest.get('sources') or {}).get('paths', []):
        if source['id'] in plan.deferred_sources:
            continue
        claim = None
        target = source_path(plan.manifest, source)
        if plan.mode == 'rebuild':
            source_type, _restored, target = restored_path_details(
                plan.manifest, plan.root, source, plan.inventory,
            )
            if source_type != 'missing':
                claim = DataPathClaim(
                    Path(target), tuple(c['trusted_data_roots']),
                )
                entry = _plan_path_entry(plan, source['id'])
                ancestors = (
                    [item.model_dump() for item in entry.ancestors]
                    if hasattr(entry, 'ancestors')
                    else entry.get('ancestors')
                )
                _claim_rebuild_path(
                    c, target, source_type, ancestors, claim, ledger,
                )
                ledger.add(claim)
        else:
            live_mutations.append(
                str(source_path(plan.manifest, source))
            )

        def path_published(identity, state=None, *, claim=claim, target=target):
            if claim is None:
                return
            if state is None:
                descriptor, _metadata = open_data_path(
                    target, claim.trusted_roots,
                )
                try:
                    state = data_object_state(descriptor)
                finally:
                    os.close(descriptor)
            claim.identity = identity
            claim.state = state

        restore_path_source(
            plan.manifest, plan.root, source, plan.inventory,
            c=c, rebuild=plan.mode == 'rebuild',
            on_publish=path_published if claim is not None else None,
        )
    if plan.mode != 'rebuild':
        live_mutations.extend(
            f'volume:{name}' for _source, name in plan.volumes
        )
    sync_volumes(
        c, plan.manifest, plan.root, restore=True, resolved=plan.volumes,
    )


def _rollback_rebuild(ledger):
    ledger.rollback()


def _publish_controls(c, requested_manifest, plan, changes):
    ledger = changes.rollback
    live_mutations = changes.live_mutations
    for source in (plan.manifest.get('sources') or {}).get('paths', []):
        if source['id'] in plan.deferred_sources:
            source_type, restored, target = restored_path_details(
                plan.manifest, plan.root, source, plan.inventory,
            )
            if source_type != 'file':
                raise RuntimeError(f'Compose source must be a regular file: {target}')
            if plan.mode == 'rebuild':
                ensure_control_parent(target.parent, c['trusted_data_roots'])
                claim = ledger.add(ControlPathClaim(Path(target)))

                def published(identity, claim=claim):
                    claim.identity = identity
                    claim.owned = True

                atomic_copy_file(
                    restored, target, require_absent=True,
                    on_publish=published,
                )
            else:
                live_mutations.append(str(target))
                atomic_copy_file(restored, target)
    if requested_manifest.get('_restore_manifest_requested'):
        target = Path(requested_manifest['_path'])
        if plan.mode == 'rebuild':
            ensure_control_parent(target.parent, c['trusted_data_roots'])
            claim = ledger.add(ControlPathClaim(target))

            def manifest_published(identity):
                claim.identity = identity
                claim.owned = True

            atomic_copy_file(
                requested_manifest['_snapshot_manifest'], target,
                require_absent=True,
                on_publish=manifest_published,
            )
        else:
            live_mutations.append(str(target))
            atomic_copy_file(requested_manifest['_snapshot_manifest'], target)


def _restart_services(plan, targets, start_services):
    if start_services:
        if not compose_files_exist(plan.manifest):
            raise RuntimeError(
                f"cannot start {plan.manifest['service']}: "
                'Compose files were not restored'
            )
        compose_run(plan.manifest, ['up', '-d'], runner=run)
    elif targets:
        compose_run(
            plan.manifest, ['up', '-d', '--no-deps'] + targets, runner=run,
        )


@dataclass(frozen=True)
class RestoreApplyDependencies:
    prepare_plan: Callable
    stop_services: Callable
    dynamic_preflight: Callable
    restore_data: Callable
    publish_controls: Callable
    rollback: Callable
    restart_services: Callable
    cleanup: Callable
    compose: Callable
    run_command: Callable
    operation_id: Callable

    @classmethod
    def production(cls):
        return cls(
            prepare_restore_plan, _stop_running_services, _dynamic_preflight,
            _restore_data, _publish_controls, _rollback_rebuild,
            _restart_services, run_cleanup, compose_run, run,
            lambda: secrets.token_hex(16),
        )


class RestoreApplyWorkflow:
    def __init__(self, dependencies):
        self.dependencies = dependencies

    def apply(self, c, m, root, *, start_services=False):
        deps = self.dependencies
        plan = deps.prepare_plan(c, m, root)
        targets = list(plan.running_services)
        try:
            deps.stop_services(plan)
        except Exception:
            if targets:
                deps.cleanup(
                    lambda: deps.compose(
                        plan.manifest,
                        ['up', '-d', '--no-deps'] + targets,
                        runner=deps.run_command,
                    ),
                    'service recovery after failed Compose stop',
                )
            raise
        mutation_started = False
        changes = RestoreChanges()
        try:
            deps.dynamic_preflight(c, plan)
            mutation_started = True
            deps.restore_data(c, plan, changes, deps.operation_id())
            deps.publish_controls(c, m, plan, changes)
        except Exception:
            if not mutation_started and targets:
                deps.cleanup(
                    lambda: deps.compose(
                        plan.manifest,
                        ['up', '-d', '--no-deps'] + targets,
                        runner=deps.run_command,
                    ),
                    'service recovery after restore preflight',
                )
            elif mutation_started and plan.mode == 'rebuild':
                deps.rollback(changes.rollback)
                print(
                    'ERROR: rebuild restore failed; rollback of new targets '
                    'was attempted',
                    file=sys.stderr,
                )
            elif mutation_started:
                print(
                    'ERROR: restore failed after live mutation; services remain '
                    'stopped',
                    file=sys.stderr,
                )
                for target in dict.fromkeys(changes.live_mutations):
                    print(
                        f'  - possibly modified: {target}',
                        file=sys.stderr,
                    )
            raise
        deps.restart_services(plan, targets, start_services)


def apply_one(
        c: GlobalConfig, m: ServiceManifest, root, *, start_services=False,
        dependencies=None,
):
    workflow = RestoreApplyWorkflow(
        dependencies or RestoreApplyDependencies.production(),
    )
    return workflow.apply(
        c, m, root, start_services=start_services,
    )
