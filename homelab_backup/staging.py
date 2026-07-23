import sys
from pathlib import Path

from . import capture as _capture
from .actions import run_before_actions, run_finally_actions
from .btrfs_snapshot import cleanup_snapshot_state
from .capture import _check_backup_space
from .common import run_cleanup
from .inventory_models import RestoreInventoryModel
from .manifest import compose_model, source_path, validate_manifest
from .security import (
    atomic_copy_file, atomic_write_json, ensure_private_directory,
    paths_overlap, validate_trusted_roots,
)
from .storage import (
    compose_identity, resolved_volume_sources, validate_docker_bind_probe,
    validate_docker_environment, validate_runtime_sources,
)
from .types import GlobalConfig, ServiceManifest


def _prepare_stage(c, m):
    validate_manifest(m)
    validate_trusted_roots(c['trusted_data_roots'])
    # Recovery precedes every action even if the consistency mode changed.
    cleanup_snapshot_state(c, m['service'])
    staging_root = Path(c['staging_root'])
    stage = staging_root / m['service']
    protected = [Path(m['_dir'])]
    protected.extend(
        source_path(m, source)
        for source in (m.get('sources') or {}).get('paths', [])
    )
    for target in protected:
        if paths_overlap(stage, target):
            raise ValueError(
                f'staging directory {stage} overlaps protected path {target}'
            )
    ensure_private_directory(staging_root)
    ensure_private_directory(stage, replace=True)
    mode = (m.get('consistency') or {}).get('mode', 'stop')
    return stage, mode


def _build_stage_context(c, m, stage, mode, allow_low_space):
    validate_docker_environment()
    validate_docker_bind_probe(c)
    model = compose_model(m)
    validate_runtime_sources(
        c, m, model, allow_missing_paths=mode == 'hooks',
    )
    resolved_volumes = resolved_volume_sources(m, model=model)
    return _capture._StageContext(
        config=c,
        manifest=m,
        stage=stage,
        mode=mode,
        resolved_volumes=resolved_volumes,
        identity=compose_identity(m, model=model, resolved=resolved_volumes),
        allow_low_space=allow_low_space,
    )


def _finish_actions(m, optional_action_failures, *, primary_failed):
    def execute():
        optional_action_failures.extend(run_finally_actions(m))

    if primary_failed:
        run_cleanup(execute, 'finally actions')
    else:
        execute()


def _write_stage_metadata(context, captured, optional_action_failures):
    source_entries = [
        item for item in (*captured.paths, *captured.volumes)
        if item.get('present', True)
    ]
    capture_methods = [
        item.get('capture_method', 'quiesced-copy')
        for item in source_entries
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
    m = context.manifest
    meta = context.stage / '_meta'
    ensure_private_directory(meta)
    # backup.yaml is always included as recovery metadata, even when it is not
    # listed in sources.paths.
    atomic_copy_file(m['_path'], meta / 'backup.yaml')
    inventory = RestoreInventoryModel.from_snapshot_data({
        'version': 1,
        'service': m['service'],
        'service_directory': m['_dir'],
        'service_relative_directory': m.get(
            '_relative_dir', Path(m['_dir']).name,
        ),
        'paths': captured.paths,
        'volumes': captured.volumes,
        'compose': context.identity,
        'consistency': {
            'mode': context.mode,
            'guarantee': overall_capture,
            'optional_action_failures': optional_action_failures,
            'writers': all_writers,
        },
    })
    atomic_write_json(meta / 'inventory.json', inventory.to_snapshot_dict())
    return context.stage


def stage_service(
        c: GlobalConfig, m: ServiceManifest, *, allow_low_space=False,
):
    stage, mode = _prepare_stage(c, m)
    optional_action_failures = []
    try:
        optional_action_failures.extend(run_before_actions(m))
        context = _build_stage_context(
            c, m, stage, mode, allow_low_space,
        )
        captured = _capture._capture_stage(context)
    finally:
        _finish_actions(
            m, optional_action_failures,
            primary_failed=sys.exc_info()[1] is not None,
        )
    return _write_stage_metadata(
        context, captured, optional_action_failures,
    )
