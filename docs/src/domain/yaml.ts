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
const ACTION_OPERATIONS = ['backup', 'restore'] as const
const ACTION_PHASES = [
  'before', 'after', 'finally', 'on_success', 'on_failure',
] as const
const LEGACY_ACTION_PHASES = [
  'before', 'finally', 'on_success', 'on_failure',
] as const

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

function uniqueLegacyName(used: Set<string>, base: string): string {
  let candidate = base
  let suffix = 2
  while (used.has(candidate)) {
    candidate = `${base}-${suffix}`
    suffix += 1
  }
  used.add(candidate)
  return candidate
}

function optionalLegacyArray(
  value: Record<string, unknown>,
  key: string,
  path: string,
): unknown[] {
  const entries = value[key]
  if (entries === undefined) return []
  if (!Array.isArray(entries)) throw new Error(`${path} 必須是陣列。`)
  return entries
}

function migrateLegacyServiceActions(value: Record<string, unknown>): boolean {
  const consistency = value.consistency === undefined
    ? {}
    : assertMapping(value.consistency, 'consistency')
  const actions = value.actions === undefined
    ? {}
    : assertMapping(value.actions, 'actions')
  const nested = ACTION_OPERATIONS.some((key) => key in actions)
  const legacyFlat = LEGACY_ACTION_PHASES.some((key) => key in actions)
  const legacyHooks = 'before' in consistency || 'after' in consistency
  if (nested && (legacyFlat || legacyHooks)) {
    throw new Error('巢狀 actions 不可與舊版 flat actions 或 consistency hooks 混用。')
  }
  if (nested) return false
  if (!legacyFlat && !legacyHooks) {
    value.actions = {
      backup: Object.fromEntries(ACTION_PHASES.map((phase) => [phase, []])),
      restore: Object.fromEntries(ACTION_PHASES.map((phase) => [phase, []])),
    }
    return false
  }

  const used = new Set<string>()
  const flatEntries = Object.fromEntries(
    LEGACY_ACTION_PHASES.map((phase) => [
      phase,
      optionalLegacyArray(actions, phase, `actions.${phase}`),
    ]),
  ) as Record<(typeof LEGACY_ACTION_PHASES)[number], unknown[]>
  for (const phase of LEGACY_ACTION_PHASES) {
    flatEntries[phase].forEach((entry) => {
      if (isRecord(entry) && typeof entry.name === 'string') used.add(entry.name)
    })
  }
  const beforeHooks = consistency.before === undefined ? [] : consistency.before
  const afterHooks = consistency.after === undefined ? [] : consistency.after
  assertStringArray(beforeHooks, 'consistency.before')
  assertStringArray(afterHooks, 'consistency.after')
  const timeout = typeof consistency.timeout === 'number'
    ? consistency.timeout
    : 120
  const scriptAction = (script: string, phase: 'before' | 'finally', index: number) => ({
    name: uniqueLegacyName(
      used,
      `legacy-consistency-${phase}-${index + 1}`,
    ),
    script,
    timeout,
    required: true,
    run_as: 'root',
  })
  value.actions = {
    backup: {
      before: [
        ...flatEntries.before,
        ...beforeHooks.map((script, index) => scriptAction(script, 'before', index)),
      ],
      after: [],
      finally: [
        ...afterHooks.map((script, index) => scriptAction(script, 'finally', index)),
        ...flatEntries.finally,
      ],
      on_success: flatEntries.on_success,
      on_failure: flatEntries.on_failure,
    },
    restore: Object.fromEntries(ACTION_PHASES.map((phase) => [phase, []])),
  }
  delete consistency.before
  delete consistency.after
  return true
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
    assertAllowed(actions, ACTION_OPERATIONS, 'actions')
    ACTION_OPERATIONS.forEach((operation) => {
      if (actions[operation] === undefined) return
      const section = assertMapping(actions[operation], `actions.${operation}`)
      assertAllowed(section, ACTION_PHASES, `actions.${operation}`)
      ACTION_PHASES.forEach((phase) => {
        const entries = section[phase]
        if (entries === undefined) return
        if (!Array.isArray(entries)) {
          throw new Error(`actions.${operation}.${phase} 必須是陣列。`)
        }
        entries.forEach((entry, index) => {
          const path = `actions.${operation}.${phase}[${index}]`
          const action = assertMapping(entry, path)
          assertAllowed(
            action,
            ['name', 'command', 'script', 'timeout', 'required', 'run_as'],
            path,
          )
          assertOptionalType(action, 'name', 'string', path)
          assertOptionalType(action, 'run_as', 'string', path)
          const hasCommand = action.command !== undefined
          const hasScript = action.script !== undefined
          if (hasCommand === hasScript) {
            throw new Error(`${path} 的 command 與 script 必須擇一設定。`)
          }
          if (hasCommand) assertStringArray(action.command, `${path}.command`)
          if (hasScript) assertOptionalType(action, 'script', 'string', path)
          assertOptionalType(action, 'timeout', 'number', path)
          assertOptionalType(action, 'required', 'boolean', path)
        })
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
    delete value.trusted_data_roots
    return
  }

  const service = value as unknown as ServiceManifest
  if (service.schedule) service.schedule.cron ??= ''
  if (service.compose) service.compose.files ??= ['compose.yaml']
  if (service.consistency) {
    service.consistency.mode ??= 'stop'
  }
  if (service.actions) {
    for (const operation of ACTION_OPERATIONS) {
      service.actions[operation] ??= {}
      for (const phase of ACTION_PHASES) {
        service.actions[operation]![phase] ??= []
      }
    }
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
  if (kind === 'service') {
    migrateLegacyServiceActions(value as Record<string, unknown>)
  }
  validateNestedShape(kind, value as Record<string, unknown>)
  normalizeDocument(kind, value as Record<string, unknown>)
  return kind === 'global'
    ? { kind, value: value as GlobalConfig }
    : { kind, value: value as ServiceManifest }
}

export function hasLegacyActionSyntax(source: string): boolean {
  try {
    const value = parse(source)
    if (!isRecord(value) || !('service' in value)) return false
    const actions = isRecord(value.actions) ? value.actions : {}
    const consistency = isRecord(value.consistency) ? value.consistency : {}
    return LEGACY_ACTION_PHASES.some((key) => key in actions)
      || 'before' in consistency
      || 'after' in consistency
  } catch {
    return false
  }
}

export function serializeEditorDocument(document: EditorDocument): string {
  return stringify(document.value, {
    lineWidth: 0,
    defaultStringType: 'PLAIN',
    defaultKeyType: 'PLAIN',
  })
}
