from typing import Optional, TypedDict


class RcloneConfig(TypedDict, total=False):
    bwlimit: str


class CheckConfig(TypedDict, total=False):
    read_data_subset: str


class GlobalConfig(TypedDict, total=False):
    version: int
    host_id: str
    services_root: str
    repository: str
    password_file: str
    rclone_config: str
    staging_root: str
    restore_root: str
    cache_root: str
    volume_helper_image: str
    state_root: str
    lock_file: str
    trusted_data_roots: list[str]
    rclone: RcloneConfig
    check: CheckConfig


class ScheduleConfig(TypedDict, total=False):
    cron: str
    enabled: bool
    retry_after: str
    max_lateness: str


class RetentionConfig(TypedDict, total=False):
    keep_last: int
    keep_hourly: int
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    keep_yearly: int
    keep_within: str
    keep_within_hourly: str
    keep_within_daily: str
    keep_within_weekly: str
    keep_within_monthly: str
    keep_within_yearly: str


class ConsistencyConfig(TypedDict, total=False):
    mode: str
    timeout: int
    before: list[str]
    after: list[str]


class ComposeConfig(TypedDict, total=False):
    files: list[str]


class PathSource(TypedDict, total=False):
    id: str
    path: str
    required: bool
    exclude: list[str]


class VolumeSource(TypedDict, total=False):
    id: str
    name: str
    compose_volume: str
    required: bool
    exclude: list[str]


class SourcesConfig(TypedDict, total=False):
    paths: list[PathSource]
    volumes: list[VolumeSource]


class ServiceManifest(TypedDict, total=False):
    version: int
    service: str
    enabled: bool
    schedule: ScheduleConfig
    retention: RetentionConfig
    compose: ComposeConfig
    consistency: ConsistencyConfig
    sources: SourcesConfig
    _path: str
    _dir: str
    _snapshot_manifest: str
    _restore_manifest_requested: bool


class InventoryPath(TypedDict, total=False):
    id: str
    path: str
    type: Optional[str]
    present: bool


class InventoryVolume(TypedDict, total=False):
    id: str
    name: str
    compose_volume: str
    actual_name: str
    present: bool


class ComposeVolumeIdentity(TypedDict, total=False):
    id: str
    logical_name: str
    actual_name: str


class ComposeIdentity(TypedDict, total=False):
    project_name: str
    compose_files: list[str]
    services: list[str]
    volumes: list[ComposeVolumeIdentity]


class RestoreInventory(TypedDict, total=False):
    version: int
    service: str
    service_directory: str
    paths: list[InventoryPath]
    volumes: list[InventoryVolume]
    compose: ComposeIdentity


class BackupState(TypedDict, total=False):
    service: str
    first_seen_at: str
    last_result: str
    last_success_at: str
    last_attempt_at: str
    last_finished_at: str
    last_duration_seconds: float
    last_error: Optional[str]
    last_retention_error: Optional[str]
