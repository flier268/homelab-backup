export type DocumentKind = 'global' | 'service'
export type ConsistencyMode = 'stop' | 'hooks' | 'external' | 'live' | 'snapshot' | 'files'
export type ActionPhase = 'before' | 'finally' | 'on_success' | 'on_failure'
export type InventoryEntryType = 'file' | 'directory' | 'symlink'
export type SelectionState = 'unassigned' | 'include' | 'exclude'

export interface GlobalConfig {
  version: 1
  host_id: string
  services_root: string
  trusted_data_roots: string[]
  repository: string
  password_file: string
  rclone_config: string
  staging_root: string
  restore_root: string
  state_root: string
  cache_root: string
  lock_file: string
  volume_helper_image: string
  rclone?: { bwlimit?: string }
  check?: { read_data_subset?: string }
}

export interface ScheduleConfig {
  cron: string
  enabled?: boolean
  retry_after?: string
  max_lateness?: string
}

export interface RetentionConfig {
  keep_last?: number
  keep_hourly?: number
  keep_daily?: number
  keep_weekly?: number
  keep_monthly?: number
  keep_yearly?: number
}

export interface ComposeConfig {
  files: string[]
  env_file?: string
}

export interface ConsistencyConfig {
  mode: ConsistencyMode
  timeout?: number
  before?: string[]
  after?: string[]
}

export interface ActionDefinition {
  name: string
  command: string[]
  timeout?: number
  required?: boolean
  run_as: string
}

export type ActionsConfig = Partial<Record<ActionPhase, ActionDefinition[]>>

export interface PathSource {
  id: string
  path: string
  required?: boolean
  include?: string[]
  exclude?: string[]
}

export interface VolumeSource {
  id: string
  name?: string
  compose_volume?: string
  required?: boolean
  exclude?: string[]
}

export interface SourcesConfig {
  paths: PathSource[]
  volumes: VolumeSource[]
}

export interface ServiceManifest {
  version: 1
  service: string
  name?: string
  enabled?: boolean
  create_snapshot_if_unchanged?: boolean
  schedule: ScheduleConfig
  retention: RetentionConfig
  compose?: ComposeConfig
  consistency?: ConsistencyConfig
  actions?: ActionsConfig
  sources?: SourcesConfig
}

export interface InventoryHeader {
  format: 'homelab-backup-inventory'
  version: 1
  root: '.'
}

export interface InventoryEntry {
  path: string
  type: InventoryEntryType
}

export interface SourceGroup {
  key: string
  id: string
  root: string
  required: boolean
}

export interface InventoryChoice {
  state: SelectionState
  groupKey?: string
}

export interface ValidationIssue {
  path: string
  message: string
}

export type EditorDocument =
  | { kind: 'global'; value: GlobalConfig }
  | { kind: 'service'; value: ServiceManifest }
