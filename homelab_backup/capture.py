import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .btrfs_snapshot import SnapshotTransaction
from .common import MIN_FREE_BYTES, format_bytes, run, run_cleanup
from .manifest import compose_run, service_label
from .storage import (
    docker_mount_conflicts, docker_writer_maps, estimate_backup_size, hooks,
    running_services, sync_paths, sync_volumes, validate_no_docker_writers,
    validate_path_payloads,
)
from .types import GlobalConfig, ServiceManifest


@dataclass(frozen=True)
class _StageContext:
    config: GlobalConfig
    manifest: ServiceManifest
    stage: Path
    mode: str
    resolved_volumes: list
    identity: dict
    allow_low_space: bool


@dataclass(frozen=True)
class _CaptureResult:
    paths: list[dict]
    volumes: list[dict]


def _check_backup_space(
        c, m, stage, estimated_size, *, allow_low_space=False,
):
    free = shutil.disk_usage(stage).free
    required = estimated_size + MIN_FREE_BYTES
    if free >= required:
        return
    shortfall = required - free
    print(
        f"WARNING: backup for {service_label(m)} is estimated at "
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


def _sync_stage(
        context, *, source_overrides=None, path_methods=None,
        volume_methods=None, path_writers=None, volume_writers=None,
):
    c = context.config
    m = context.manifest
    stage = context.stage
    estimated_size = estimate_backup_size(
        c, m, resolved=context.resolved_volumes,
        source_overrides=source_overrides,
    )
    _check_backup_space(
        c, m, stage, estimated_size,
        allow_low_space=context.allow_low_space,
    )

    def before_copy(_source):
        _check_backup_space(
            c, m, stage, 0,
            allow_low_space=context.allow_low_space,
        )

    path_result = sync_paths(
        c, m, stage, before_copy=before_copy,
        source_overrides=source_overrides,
        capture_methods=path_methods,
        writer_map=path_writers,
    )
    _check_backup_space(
        c, m, stage, 0, allow_low_space=context.allow_low_space,
    )
    volume_result = list(sync_volumes(
        c, m, stage, resolved=context.resolved_volumes,
        before_copy=before_copy,
        capture_methods=volume_methods,
        writer_map=volume_writers,
    ) or [])
    _check_backup_space(
        c, m, stage, 0, allow_low_space=context.allow_low_space,
    )
    return _CaptureResult(path_result, volume_result)


def _quiesced_methods(context):
    m = context.manifest
    return (
        {
            source['id']: 'quiesced-copy'
            for source in (m.get('sources') or {}).get('paths', [])
        },
        {
            source['id']: 'quiesced-copy'
            for source, _name in context.resolved_volumes
        },
    )


def _warn_writers(path_writers, volume_writers):
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


def _writer_methods(
        context, path_writers, volume_writers, *, source_overrides=None,
):
    source_overrides = source_overrides or {}
    m = context.manifest
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
        for source, _name in context.resolved_volumes
    }
    return path_methods, volume_methods


def _capture_quiesced(context, *, project_must_be_stopped):
    validate_no_docker_writers(
        context.manifest, context.identity, context.resolved_volumes,
        project_must_be_stopped=project_must_be_stopped,
    )
    validate_path_payloads(context.config, context.manifest)
    path_methods, volume_methods = _quiesced_methods(context)
    return _sync_stage(
        context,
        path_methods=path_methods,
        volume_methods=volume_methods,
    )


def _capture_hooks(context):
    m = context.manifest
    try:
        hooks(m, 'before')
        return _capture_quiesced(
            context, project_must_be_stopped=False,
        )
    finally:
        run_cleanup(lambda: hooks(m, 'after'), 'after hook')


def _capture_stop(context):
    m = context.manifest
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
        return _capture_quiesced(
            context, project_must_be_stopped=True,
        )
    finally:
        if targets:
            run_cleanup(
                lambda: compose_run(m, ['start'] + targets, runner=run),
                'service restart',
            )


def _capture_external(context):
    return _capture_quiesced(
        context, project_must_be_stopped=False,
    )


def _capture_live(context):
    m = context.manifest
    validate_path_payloads(context.config, m)
    path_writers, volume_writers = docker_writer_maps(
        m, context.resolved_volumes,
    )
    _warn_writers(path_writers, volume_writers)
    path_methods, volume_methods = _writer_methods(
        context, path_writers, volume_writers,
    )
    return _sync_stage(
        context,
        path_methods=path_methods,
        volume_methods=volume_methods,
        path_writers=path_writers,
        volume_writers=volume_writers,
    )


def _capture_snapshot(context):
    c = context.config
    m = context.manifest
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
            m, context.resolved_volumes,
        )
        _warn_writers(
            {
                key: value for key, value in path_writers.items()
                if key not in source_overrides
            },
            volume_writers,
        )
        path_methods, volume_methods = _writer_methods(
            context, path_writers, volume_writers,
            source_overrides=source_overrides,
        )
        return _sync_stage(
            context,
            source_overrides=source_overrides,
            path_methods=path_methods,
            volume_methods=volume_methods,
            path_writers=path_writers,
            volume_writers=volume_writers,
        )
    finally:
        run_cleanup(transaction.cleanup, 'Btrfs snapshot')


_MODE_HANDLERS = {
    'hooks': _capture_hooks,
    'stop': _capture_stop,
    'external': _capture_external,
    'live': _capture_live,
    'snapshot': _capture_snapshot,
}


def _capture_stage(context):
    try:
        handler = _MODE_HANDLERS[context.mode]
    except KeyError as err:
        raise RuntimeError(
            f'unsupported consistency mode: {context.mode}'
        ) from err
    return handler(context)
