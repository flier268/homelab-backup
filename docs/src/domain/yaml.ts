import { parse, stringify } from 'yaml'
import type {
  DocumentKind,
  EditorDocument,
  GlobalConfig,
  ServiceManifest,
} from './types'

const GLOBAL_KEYS = new Set([
  'version', 'host_id', 'services_root', 'repository', 'password_file',
  'rclone_config', 'staging_root', 'restore_root', 'cache_root',
  'volume_helper_image', 'state_root', 'lock_file', 'trusted_data_roots',
  'rclone', 'check',
])

const SERVICE_KEYS = new Set([
  'version', 'service', 'name', 'enabled', 'create_snapshot_if_unchanged',
  'schedule', 'retention', 'compose', 'consistency', 'actions', 'sources',
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function assertMapping(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${path} 必須是 mapping。`)
  return value
}

function assertAllowed(
  value: Record<string, unknown>,
  allowed: readonly string[],
  path: string,
): void {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key))
  if (unknown.length) throw new Error(`${path} 不支援的欄位：${unknown.join(', ')}`)
}

function assertOptionalType(
  value: Record<string, unknown>,
  key: string,
  expected: 'string' | 'number' | 'boolean',
  path: string,
): void {
  if (value[key] !== undefined && typeof value[key] !== expected) {
    throw new Error(`${path}.${key} 必須是 ${expected}。`)
  }
}

function assertStringArray(value: unknown, path: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw new Error(`${path} 必須是字串陣列。`)
  }
}

function assertOptionalStringArray(
  value: Record<string, unknown>,
  key: string,
  path: string,
): void {
  if (value[key] !== undefined) assertStringArray(value[key], `${path}.${key}`)
}

function normalizeNullableSections(
  kind: DocumentKind,
  value: Record<string, unknown>,
): void {
  if (kind !== 'service') return
  for (const name of ['compose', 'consistency', 'actions', 'sources']) {
    if (value[name] === null) value[name] = {}
  }
}

function validateNestedShape(kind: DocumentKind, value: Record<string, unknown>): void {
  if (kind === 'global') {
    assertOptionalType(value, 'version', 'number', 'global')
    for (const key of [
      'host_id', 'services_root', 'repository', 'password_file',
      'rclone_config', 'staging_root', 'restore_root', 'cache_root',
      'volume_helper_image', 'state_root', 'lock_file',
    ]) {
      assertOptionalType(value, key, 'string', 'global')
    }
    assertOptionalStringArray(value, 'trusted_data_roots', 'global')
    for (const [name, fields] of [
      ['rclone', ['bwlimit']],
      ['check', ['read_data_subset']],
    ] as const) {
      if (value[name] === undefined) continue
      const section = assertMapping(value[name], name)
      assertAllowed(section, fields, name)
      assertOptionalType(section, fields[0], 'string', name)
    }
    return
  }

  assertOptionalType(value, 'version', 'number', 'service')
  assertOptionalType(value, 'service', 'string', 'service')
  assertOptionalType(value, 'name', 'string', 'service')
  assertOptionalType(value, 'enabled', 'boolean', 'service')
  assertOptionalType(value, 'create_snapshot_if_unchanged', 'boolean', 'service')

  const mappingSections = [
    ['schedule', ['cron', 'retry_after', 'max_lateness', 'enabled']],
    [
      'retention',
      [
        'keep_last', 'keep_hourly', 'keep_daily',
        'keep_weekly', 'keep_monthly', 'keep_yearly',
      ],
    ],
    ['compose', ['files', 'env_file']],
    ['consistency', ['mode', 'timeout', 'before', 'after']],
  ] as const
  for (const [name, fields] of mappingSections) {
    if (value[name] === undefined) continue
    assertAllowed(assertMapping(value[name], name), fields, name)
  }

  if (value.schedule !== undefined) {
    const schedule = assertMapping(value.schedule, 'schedule')
    for (const key of ['cron', 'retry_after', 'max_lateness']) {
      assertOptionalType(schedule, key, 'string', 'schedule')
    }
    assertOptionalType(schedule, 'enabled', 'boolean', 'schedule')
  }

  if (value.retention !== undefined) {
    const retention = assertMapping(value.retention, 'retention')
    for (const key of [
      'keep_last', 'keep_hourly', 'keep_daily',
      'keep_weekly', 'keep_monthly', 'keep_yearly',
    ]) {
      assertOptionalType(retention, key, 'number', 'retention')
    }
  }

  if (value.compose !== undefined) {
    const compose = assertMapping(value.compose, 'compose')
    assertOptionalStringArray(compose, 'files', 'compose')
    assertOptionalType(compose, 'env_file', 'string', 'compose')
  }

  if (value.consistency !== undefined) {
    const consistency = assertMapping(value.consistency, 'consistency')
    assertOptionalType(consistency, 'mode', 'string', 'consistency')
    assertOptionalType(consistency, 'timeout', 'number', 'consistency')
    assertOptionalStringArray(consistency, 'before', 'consistency')
    assertOptionalStringArray(consistency, 'after', 'consistency')
  }

  if (value.actions !== undefined) {
    const actions = assertMapping(value.actions, 'actions')
    const phases = ['before', 'finally', 'on_success', 'on_failure']
    assertAllowed(actions, phases, 'actions')
    phases.forEach((phase) => {
      const entries = actions[phase]
      if (entries === undefined) return
      if (!Array.isArray(entries)) throw new Error(`actions.${phase} 必須是陣列。`)
      entries.forEach((entry, index) => {
        const path = `actions.${phase}[${index}]`
        const action = assertMapping(entry, path)
        assertAllowed(
          action,
          ['name', 'command', 'timeout', 'required', 'run_as'],
          path,
        )
        assertOptionalType(action, 'name', 'string', path)
        assertOptionalType(action, 'run_as', 'string', path)
        if (action.command === undefined) {
          throw new Error(`${path}.command 必須是字串陣列。`)
        }
        assertStringArray(action.command, `${path}.command`)
        assertOptionalType(action, 'timeout', 'number', path)
        assertOptionalType(action, 'required', 'boolean', path)
      })
    })
  }

  if (value.sources !== undefined) {
    const sources = assertMapping(value.sources, 'sources')
    assertAllowed(sources, ['paths', 'volumes'], 'sources')
    for (const kindName of ['paths', 'volumes'] as const) {
      const entries = sources[kindName]
      if (entries === undefined) continue
      if (!Array.isArray(entries)) throw new Error(`sources.${kindName} 必須是陣列。`)
      entries.forEach((entry, index) => {
        const path = `sources.${kindName}[${index}]`
        const source = assertMapping(entry, path)
        assertAllowed(
          source,
          kindName === 'paths'
            ? ['id', 'path', 'required', 'include', 'exclude']
            : ['id', 'name', 'compose_volume', 'required', 'exclude'],
          path,
        )
        assertOptionalType(source, 'id', 'string', path)
        assertOptionalType(source, 'required', 'boolean', path)
        assertOptionalStringArray(source, 'exclude', path)
        if (kindName === 'paths') {
          assertOptionalType(source, 'path', 'string', path)
          assertOptionalStringArray(source, 'include', path)
        } else {
          assertOptionalType(source, 'name', 'string', path)
          assertOptionalType(source, 'compose_volume', 'string', path)
        }
      })
    }
  }
}

function normalizeDocument(kind: DocumentKind, value: Record<string, unknown>): void {
  if (kind === 'global') {
    value.trusted_data_roots ??= []
    return
  }

  const service = value as unknown as ServiceManifest
  if (service.schedule) service.schedule.cron ??= ''
  if (service.compose) service.compose.files ??= ['compose.yaml']
  if (service.consistency) {
    service.consistency.mode ??= 'stop'
    service.consistency.before ??= []
    service.consistency.after ??= []
  }
  if (service.actions) {
    service.actions.before ??= []
    service.actions.finally ??= []
    service.actions.on_success ??= []
    service.actions.on_failure ??= []
  }
  if (service.sources) {
    service.sources.paths ??= []
    service.sources.volumes ??= []
  }
}

export function detectDocumentKind(value: unknown): DocumentKind {
  if (!isRecord(value)) {
    throw new Error('YAML 最上層必須是 mapping。')
  }
  if ('host_id' in value && !('service' in value)) return 'global'
  if ('service' in value && !('host_id' in value)) return 'service'
  throw new Error('無法判斷設定種類：必須包含 host_id 或 service。')
}

export function parseEditorDocument(source: string): EditorDocument {
  let value: unknown
  try {
    value = parse(source)
  } catch (error) {
    throw new Error(`YAML 語法錯誤：${String(error)}`)
  }
  const kind = detectDocumentKind(value)
  const allowed = kind === 'global' ? GLOBAL_KEYS : SERVICE_KEYS
  const unknown = Object.keys(value as Record<string, unknown>)
    .filter((key) => !allowed.has(key))
  if (unknown.length) {
    throw new Error(`不支援的欄位：${unknown.join(', ')}`)
  }
  normalizeNullableSections(kind, value as Record<string, unknown>)
  validateNestedShape(kind, value as Record<string, unknown>)
  normalizeDocument(kind, value as Record<string, unknown>)
  return kind === 'global'
    ? { kind, value: value as GlobalConfig }
    : { kind, value: value as ServiceManifest }
}

export function serializeEditorDocument(document: EditorDocument): string {
  return stringify(document.value, {
    lineWidth: 0,
    defaultStringType: 'PLAIN',
    defaultKeyType: 'PLAIN',
  })
}
