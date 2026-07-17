import sys
from pathlib import Path

from .common import die, run
from .manifest import compose_cmd, source_path
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
    atomic_copy_file, clear_control_leaf, ensure_control_parent,
    validate_control_directory, validate_managed_leaf, validate_payload,
)
from .storage import (
    create_restore_volume, docker_mount_conflicts, docker_project_containers,
    docker_volume_exists, rsync, sync_volumes, validate_volume_identity,
)


def compose_files_exist(m):
    return all(
        (Path(m['_dir']) / item).exists()
        for item in m.get('compose', {}).get('files', ['compose.yaml'])
    )


def normalize_restore_target(target, source_type):
    target = Path(target)
    if target.is_symlink():
        clear_control_leaf(target)
        return
    if not target.exists():
        return
    if source_type in ('file', 'symlink') and target.is_dir():
        clear_control_leaf(target)
    elif source_type == 'directory' and target.is_file():
        clear_control_leaf(target)
    elif not (target.is_file() or target.is_dir()):
        raise RuntimeError(f'unsupported live target type: {target}')


def restore_path_source(m, root, source, inventory, *, c=None, rebuild=False):
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
    validate_managed_leaf(
        target, c['trusted_data_roots'],
        allow_missing=rebuild or direct_library_call,
    )
    validate_payload(restored)
    if rebuild:
        ensure_control_parent(target.parent, c['trusted_data_roots'])
        validate_control_directory(target.parent)
    if source_type == 'file':
        normalize_restore_target(target, source_type)
        run(['rsync', '-aHAX', '--numeric-ids', str(restored), str(target)])
        return

    if source_type == 'symlink':
        normalize_restore_target(target, source_type)
        run(['rsync', '-aHAX', '--numeric-ids', str(restored), str(target)])
        return

    if restored.is_dir() and not restored.is_symlink():
        normalize_restore_target(target, source_type)
        rsync(restored, target, source.get('exclude'))
    else:
        raise RuntimeError(f'restored directory artifact is missing: {restored}')


def apply_one(c, m, root, *, start_services=False):
    plan = prepare_restore_plan(c, m, root)
    targets = list(plan.running_services)
    if targets:
        run(
            compose_cmd(plan.manifest) + ['stop', '-t', str((plan.manifest.get('consistency') or {}).get('timeout', 120))] + targets,
            cwd=plan.manifest['_dir'],
        )
    mutation_started = False
    changed_targets = []
    try:
        path_targets = [source_path(plan.manifest, source) for source in (plan.manifest.get('sources') or {}).get('paths', [])]
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
            validate_managed_leaf(
                target, c.get('trusted_data_roots') or [str(Path(target).parent)],
                allow_missing=plan.mode == 'rebuild',
            )
            entry = next(
                item for item in plan.inventory['paths'] if item['id'] == source['id']
            )
            exists = target.exists() or target.is_symlink()
            if plan.mode == 'rebuild':
                if exists:
                    raise RuntimeError(f'rebuild target appeared during preflight: {target}')
            elif entry['present'] and not exists:
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
        mutation_started = True
        if plan.mode == 'rebuild':
            for source, name in plan.volumes:
                changed_targets.append(f'volume:{name}')
                create_restore_volume(
                    name, service=plan.manifest['service'], source=source,
                    project_name=plan.project_name,
                )
        for source in (plan.manifest.get('sources') or {}).get('paths', []):
            if source['id'] not in plan.deferred_sources:
                changed_targets.append(str(source_path(plan.manifest, source)))
                restore_path_source(
                    plan.manifest, plan.root, source, plan.inventory,
                    c=c, rebuild=plan.mode == 'rebuild',
                )
        changed_targets.extend(f'volume:{name}' for _source, name in plan.volumes)
        sync_volumes(c, plan.manifest, plan.root, restore=True, resolved=plan.volumes)
        for source in (plan.manifest.get('sources') or {}).get('paths', []):
            if source['id'] in plan.deferred_sources:
                source_type, restored, target = restored_path_details(
                    plan.manifest, plan.root, source, plan.inventory,
                )
                if source_type != 'file':
                    raise RuntimeError(f'Compose source must be a regular file: {target}')
                if plan.mode == 'rebuild':
                    ensure_control_parent(target.parent, c['trusted_data_roots'])
                changed_targets.append(str(target))
                atomic_copy_file(restored, target, require_absent=plan.mode == 'rebuild')
        if m.get('_restore_manifest_requested'):
            target = Path(m['_path'])
            if plan.mode == 'rebuild':
                ensure_control_parent(target.parent, c['trusted_data_roots'])
            changed_targets.append(str(target))
            atomic_copy_file(
                m['_snapshot_manifest'], target,
                require_absent=plan.mode == 'rebuild',
            )
    except Exception:
        if not mutation_started and targets:
            run(
                compose_cmd(plan.manifest) + ['up', '-d', '--no-deps'] + targets,
                cwd=plan.manifest['_dir'],
            )
        elif mutation_started:
            print(
                'ERROR: restore failed after live mutation; services remain stopped',
                file=sys.stderr,
            )
            for target in dict.fromkeys(changed_targets):
                print(f'  - possibly modified: {target}', file=sys.stderr)
        raise
    if start_services:
        if not compose_files_exist(plan.manifest):
            die(f"cannot start {plan.manifest['service']}: Compose files were not restored")
        run(compose_cmd(plan.manifest) + ['up', '-d'], cwd=plan.manifest['_dir'])
    elif targets:
        run(compose_cmd(plan.manifest) + ['up', '-d', '--no-deps'] + targets, cwd=plan.manifest['_dir'])
