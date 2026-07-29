import type { GlobalConfig, ServiceManifest } from './types'

export function createGlobalConfig(): GlobalConfig {
  return {
    version: 1,
    host_id: 'server-a',
    services_root: '/srv/stacks',
    repository: 'rclone:remote:Backups/restic/server-a',
    password_file: '/etc/homelab-backup/restic-password',
    rclone_config: '/etc/homelab-backup/rclone/rclone.conf',
    staging_root: '/var/lib/homelab-backup/staging',
    restore_root: '/var/lib/homelab-backup/restores',
    state_root: '/var/lib/homelab-backup/state',
    cache_root: '/var/cache/homelab-backup/restic',
    lock_file: '/run/homelab-backup/backupctl.lock',
    volume_helper_image: 'homelab/volume-rsync:1',
    rclone: { bwlimit: '' },
    check: { read_data_subset: '5%' },
  }
}

export function createServiceManifest(): ServiceManifest {
  return {
    version: 1,
    service: 'my-service',
    enabled: true,
    create_snapshot_if_unchanged: false,
    schedule: {
      cron: '0 */6 * * *',
      retry_after: '30m',
      max_lateness: '2h',
    },
    retention: {
      keep_last: 8,
      keep_daily: 14,
      keep_weekly: 8,
      keep_monthly: 12,
    },
    compose: {
      files: ['compose.yaml'],
      env_file: '.env',
    },
    consistency: {
      mode: 'stop',
      timeout: 120,
      before: [],
      after: [],
    },
    actions: {
      before: [],
      finally: [],
      on_success: [],
      on_failure: [],
    },
    sources: {
      paths: [{ id: 'compose', path: 'compose.yaml' }],
      volumes: [],
    },
  }
}
