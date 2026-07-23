import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator


CaptureMethod = Literal['btrfs-snapshot', 'quiesced-copy', 'best-effort']


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')


def _unique_nonempty(values, label):
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError(f'{label} must contain unique non-empty strings')
    return values


class InventoryAncestorModel(_SnapshotModel):
    path: str
    uid: int
    gid: int
    mode: int

    @model_validator(mode='after')
    def validate_values(self):
        candidate = Path(self.path)
        valid_path = (
            bool(self.path)
            and not candidate.is_absolute()
            and bool(candidate.parts)
            and all(part not in ('', '.', '..') for part in candidate.parts)
        )
        if not valid_path or self.uid < 0 or self.gid < 0:
            raise ValueError('ancestor path and ownership must be valid')
        if not 0 <= self.mode <= 0o7777:
            raise ValueError('ancestor mode must be between 0 and 07777')
        return self


class InventoryPathModel(_SnapshotModel):
    id: str
    path: str
    type: Literal['file', 'directory', 'symlink'] | None = None
    present: bool
    ancestors: tuple[InventoryAncestorModel, ...] = ()
    capture_method: CaptureMethod | None = None
    writers: tuple[str, ...] = ()

    @field_validator('writers')
    @classmethod
    def validate_writers(cls, values):
        return _unique_nonempty(values, 'path writers')

    @model_validator(mode='after')
    def validate_presence_and_ancestors(self):
        if self.present != (self.type is not None):
            raise ValueError('present path entries require a type and absent entries forbid it')
        previous = None
        for ancestor in self.ancestors:
            candidate = Path(ancestor.path)
            if previous is not None and candidate.parent.as_posix() != previous:
                raise ValueError('ancestor entries must form one ordered chain')
            previous = candidate.as_posix()
        return self


class InventoryVolumeModel(_SnapshotModel):
    id: str
    name: str | None = None
    compose_volume: str | None = None
    actual_name: str
    present: bool
    capture_method: CaptureMethod | None = None
    writers: tuple[str, ...] = ()

    @field_validator('writers')
    @classmethod
    def validate_writers(cls, values):
        return _unique_nonempty(values, 'volume writers')


class ComposeVolumeIdentityModel(_SnapshotModel):
    id: str
    logical_name: str | None = None
    actual_name: str


class ComposeIdentityModel(_SnapshotModel):
    project_name: str
    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    volumes: tuple[ComposeVolumeIdentityModel, ...]

    @field_validator('services')
    @classmethod
    def validate_services(cls, values):
        _unique_nonempty(values, 'Compose services')
        if values != tuple(sorted(values)):
            raise ValueError('Compose services must be sorted')
        return values

    @model_validator(mode='after')
    def validate_volume_ids(self):
        ids = tuple(item.id for item in self.volumes)
        _unique_nonempty(ids, 'Compose volume IDs')
        return self


class OptionalActionFailureModel(_SnapshotModel):
    phase: Literal['before', 'finally', 'on_success', 'on_failure']
    name: str
    result: Literal['failed', 'timeout']

    @field_validator('name')
    @classmethod
    def validate_name(cls, value):
        if not value:
            raise ValueError('action failure name must not be empty')
        return value


class ConsistencyMetadataModel(_SnapshotModel):
    mode: Literal['stop', 'hooks', 'external', 'live', 'snapshot']
    guarantee: CaptureMethod
    optional_action_failures: tuple[OptionalActionFailureModel, ...]
    writers: tuple[str, ...]

    @field_validator('writers')
    @classmethod
    def validate_writers(cls, values):
        _unique_nonempty(values, 'consistency writers')
        if values != tuple(sorted(values)):
            raise ValueError('consistency writers must be sorted')
        return values


class RestoreInventoryModel(_SnapshotModel):
    version: Literal[1]
    service: str
    service_directory: str | None = None
    service_relative_directory: str | None = None
    paths: tuple[InventoryPathModel, ...]
    volumes: tuple[InventoryVolumeModel, ...]
    compose: ComposeIdentityModel
    consistency: ConsistencyMetadataModel | None = None

    @field_validator('version', mode='before')
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError('version must be an integer')
        return value

    @model_validator(mode='after')
    def validate_source_ids(self):
        path_ids = tuple(item.id for item in self.paths)
        volume_ids = tuple(item.id for item in self.volumes)
        _unique_nonempty(path_ids, 'path source IDs')
        _unique_nonempty(volume_ids, 'volume source IDs')
        return self

    @property
    def paths_by_id(self):
        return {item.id: item for item in self.paths}

    @property
    def volumes_by_id(self):
        return {item.id: item for item in self.volumes}

    @property
    def compose_volumes_by_id(self):
        return {item.id: item for item in self.compose.volumes}

    @classmethod
    def from_snapshot_data(cls, data):
        if isinstance(data, cls):
            return data
        return cls.model_validate_json(json.dumps(data))

    def to_snapshot_dict(self):
        return self.model_dump(
            mode='json',
            exclude_none=True,
            exclude_defaults=True,
        )


__all__ = [
    'RestoreInventoryModel',
    'ValidationError',
]
