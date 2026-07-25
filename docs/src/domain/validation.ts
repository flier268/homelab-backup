import { CronExpressionParser } from 'cron-parser'
import type {
  ActionDefinition,
  EditorDocument,
  GlobalConfig,
  PathSource,
  ServiceManifest,
  ValidationIssue,
} from './types'

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
const DISPLAY_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*(?: [A-Za-z0-9][A-Za-z0-9_.-]*)*$/
const DURATION = /^[1-9]\d*[mhdw]$/
const VOLUME_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
const USER_NAME = /^[a-z_][a-z0-9_-]{0,31}$/
const NUMERIC_IDENTITY = /^(\d+):(\d+)$/
const RETENTION_KEYS = [
  'keep_last', 'keep_hourly', 'keep_daily',
  'keep_weekly', 'keep_monthly', 'keep_yearly',
] as const

function issue(path: string, message: string): ValidationIssue {
  return { path, message }
}

function requiredString(
  issues: ValidationIssue[],
  value: unknown,
  path: string,
): value is string {
  if (typeof value !== 'string' || value.length === 0) {
    issues.push(issue(path, '必須是非空字串'))
    return false
  }
  return true
}

function normalizePath(path: string): string {
  const absolute = path.startsWith('/')
  const parts = path.split('/').filter((part) => part && part !== '.')
  const stack: string[] = []
  for (const part of parts) {
    if (part === '..') stack.pop()
    else stack.push(part)
  }
  return `${absolute ? '/' : ''}${stack.join('/')}` || (absolute ? '/' : '.')
}

function pathsOverlap(left: string, right: string): boolean {
  const a = normalizePath(left).replace(/\/+$/, '')
  const b = normalizePath(right).replace(/\/+$/, '')
  return a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`)
}

function collectNamedPaths(
  entries: readonly (readonly [string, unknown])[],
): Array<[string, string]> {
  const paths: Array<[string, string]> = []
  entries.forEach(([name, value]) => {
    if (typeof value === 'string' && value.length) paths.push([name, value])
  })
  return paths
}

function isValidCron(value: string): boolean {
  const fields = value.trim().split(/\s+/)
  if (fields.length !== 5) return false
  fields[4] = fields[4].replace(/-sun(?=($|[,/]))/gi, '-7')
  try {
    CronExpressionParser.parse(fields.join(' ')).next()
    return true
  } catch {
    return false
  }
}

function validateGlobal(config: GlobalConfig): ValidationIssue[] {
  const issues: ValidationIssue[] = []
  if (config.version !== 1) issues.push(issue('version', '目前只支援 version: 1'))
  const strings: (keyof GlobalConfig)[] = [
    'host_id', 'services_root', 'repository', 'password_file',
    'rclone_config', 'staging_root', 'restore_root', 'state_root',
    'cache_root', 'lock_file', 'volume_helper_image',
  ]
  strings.forEach((key) => requiredString(issues, config[key], String(key)))
  if (!Array.isArray(config.trusted_data_roots) || !config.trusted_data_roots.length) {
    issues.push(issue('trusted_data_roots', '至少需要一個 trusted data root'))
  } else {
    const trustedRoots: Array<[string, string]> = []
    config.trusted_data_roots.forEach((root, index) => {
      if (!requiredString(issues, root, `trusted_data_roots[${index}]`)) return
      trustedRoots.push([`trusted_data_roots[${index}]`, root])
      if (!root.startsWith('/')) {
        issues.push(issue(`trusted_data_roots[${index}]`, '必須是絕對路徑'))
      }
    })
    for (let left = 0; left < trustedRoots.length; left += 1) {
      for (let right = left + 1; right < trustedRoots.length; right += 1) {
        if (pathsOverlap(trustedRoots[left][1], trustedRoots[right][1])) {
          issues.push(issue('trusted_data_roots', 'trusted roots 不可重複或互相重疊'))
        }
      }
    }
  }
  const separated = collectNamedPaths([
    ['services_root', config.services_root],
    ['staging_root', config.staging_root],
    ['restore_root', config.restore_root],
  ])
  for (let left = 0; left < separated.length; left += 1) {
    for (let right = left + 1; right < separated.length; right += 1) {
      if (pathsOverlap(separated[left][1], separated[right][1])) {
        issues.push(issue(
          separated[right][0],
          `${separated[left][0]} 與 ${separated[right][0]} 不可重疊`,
        ))
      }
    }
  }
  const protectedPaths = collectNamedPaths([
    ['password_file', config.password_file],
    ['rclone_config', config.rclone_config],
    ['cache_root', config.cache_root],
    ['state_root', config.state_root],
    ['lock_file', config.lock_file],
  ])
  const workspaceRoots = collectNamedPaths([
    ['staging_root', config.staging_root],
    ['restore_root', config.restore_root],
  ])
  workspaceRoots.forEach(([rootName, root]) => {
    protectedPaths.forEach(([pathName, path]) => {
      if (pathsOverlap(root, path)) {
        issues.push(issue(pathName, `${rootName} 與 ${pathName} 不可重疊`))
      }
    })
  })
  const forbiddenDataPaths = [...workspaceRoots, ...protectedPaths]
  if (Array.isArray(config.trusted_data_roots)) {
    config.trusted_data_roots.forEach((root, index) => {
      if (typeof root !== 'string' || !root) return
      forbiddenDataPaths.forEach(([pathName, path]) => {
        if (pathsOverlap(root, path)) {
          issues.push(issue(
            `trusted_data_roots[${index}]`,
            `trusted data root 不可與 ${pathName} 重疊`,
          ))
        }
      })
    })
  }
  if (config.rclone?.bwlimit !== undefined
      && typeof config.rclone.bwlimit !== 'string') {
    issues.push(issue('rclone.bwlimit', '必須是字串'))
  }
  if (config.check?.read_data_subset !== undefined
      && (typeof config.check.read_data_subset !== 'string'
        || config.check.read_data_subset === '')) {
    issues.push(issue('check.read_data_subset', '若設定此欄位，必須是非空字串'))
  }
  return issues
}

function validateAction(
  action: ActionDefinition,
  path: string,
  issues: ValidationIssue[],
): void {
  requiredString(issues, action.name, `${path}.name`)
  if (requiredString(issues, action.run_as, `${path}.run_as`)) {
    const numeric = NUMERIC_IDENTITY.exec(action.run_as)
    const numericIsSafe = numeric !== null
      && Number(numeric[1]) <= 2 ** 32 - 2
      && Number(numeric[2]) <= 2 ** 32 - 2
    if (!USER_NAME.test(action.run_as) && !numericIsSafe) {
      issues.push(issue(
        `${path}.run_as`,
        '必須是本機帳號名稱或 UID:GID，數值不可超過 2^32-2',
      ))
    }
  }
  if (!Array.isArray(action.command) || !action.command.length
      || action.command.some((part) => typeof part !== 'string' || !part)) {
    issues.push(issue(`${path}.command`, '至少需要一個非空 argv 參數'))
  } else if (action.command[0].includes('/') && !action.command[0].startsWith('/')) {
    issues.push(issue(
      `${path}.command[0]`,
      '含有 / 的 executable 必須使用絕對路徑',
    ))
  }
  if (action.timeout !== undefined
      && (!Number.isInteger(action.timeout) || action.timeout <= 0)) {
    issues.push(issue(`${path}.timeout`, '必須是大於 0 的整數'))
  }
  if (action.required !== undefined && typeof action.required !== 'boolean') {
    issues.push(issue(`${path}.required`, '必須是布林值'))
  }
}

function validatePathSources(
  service: ServiceManifest,
  sources: PathSource[],
  issues: ValidationIssue[],
  allIds: Set<string>,
): void {
  sources.forEach((source, index) => {
    const path = `sources.paths[${index}]`
    if (!requiredString(issues, source.id, `${path}.id`) || !SAFE_ID.test(source.id)) {
      issues.push(issue(`${path}.id`, '只能使用英數、.、_、-，且不能是路徑'))
    } else if (allIds.has(source.id)) {
      issues.push(issue(`${path}.id`, 'source id 不可重複'))
    } else allIds.add(source.id)
    const hasPath = requiredString(issues, source.path, `${path}.path`)
    if (source.include !== undefined
        && (!Array.isArray(source.include) || !source.include.length
          || source.include.some((item) => typeof item !== 'string' || !item))) {
      issues.push(issue(`${path}.include`, '必須是非空字串陣列'))
    }
    if (source.exclude !== undefined
        && (!Array.isArray(source.exclude)
          || source.exclude.some((item) => typeof item !== 'string'))) {
      issues.push(issue(`${path}.exclude`, '必須是字串陣列'))
    }
    if (!hasPath) return
    for (let previous = 0; previous < index; previous += 1) {
      const previousSourcePath = sources[previous].path
      if (typeof previousSourcePath !== 'string' || !previousSourcePath) continue
      const base = '/service-root'
      const currentPath = source.path.startsWith('/') ? source.path : `${base}/${source.path}`
      const previousPath = previousSourcePath.startsWith('/')
        ? previousSourcePath : `${base}/${previousSourcePath}`
      if (pathsOverlap(currentPath, previousPath)) {
        issues.push(issue(path, 'path source 不可重複或互相重疊'))
      }
    }
  })
}

function validateService(service: ServiceManifest): ValidationIssue[] {
  const issues: ValidationIssue[] = []
  if (service.version !== 1) issues.push(issue('version', '目前只支援 version: 1'))
  if (!requiredString(issues, service.service, 'service') || !SAFE_ID.test(service.service)) {
    issues.push(issue('service', '只能使用英數、.、_、-'))
  }
  if (service.name !== undefined && !DISPLAY_NAME.test(service.name)) {
    issues.push(issue('name', '只能使用安全字元，單字間限一個半形空白'))
  }
  if (service.enabled !== undefined && typeof service.enabled !== 'boolean') {
    issues.push(issue('enabled', '必須是布林值'))
  }
  if (service.create_snapshot_if_unchanged !== undefined
      && typeof service.create_snapshot_if_unchanged !== 'boolean') {
    issues.push(issue('create_snapshot_if_unchanged', '必須是布林值'))
  }
  if (!service.schedule || typeof service.schedule !== 'object') {
    issues.push(issue('schedule', '必須提供排程設定'))
  } else if (service.schedule.enabled !== false) {
    const cron = service.schedule.cron
    if (!requiredString(issues, cron, 'schedule.cron')
        || !isValidCron(cron)) {
      issues.push(issue('schedule.cron', '必須是可執行的標準五欄 Cron'))
    }
    for (const key of ['retry_after', 'max_lateness'] as const) {
      const value = service.schedule[key]
      if (value !== undefined && !DURATION.test(value)) {
        issues.push(issue(`schedule.${key}`, '格式必須為正整數加 m、h、d 或 w'))
      }
    }
  }
  if (!service.retention || typeof service.retention !== 'object') {
    issues.push(issue('retention', '至少需要一項保留規則'))
  } else {
    let positive = false
    RETENTION_KEYS.forEach((key) => {
      const value = service.retention[key]
      if (value === undefined) return
      if (!Number.isInteger(value) || value < 0) {
        issues.push(issue(`retention.${key}`, '必須是大於等於 0 的整數'))
      } else if (value > 0) positive = true
    })
    if (!positive) issues.push(issue('retention', '至少一項保留數量必須大於 0'))
  }
  const consistency = service.consistency ?? { mode: 'stop' as const }
  if (consistency.mode === 'files' && service.compose !== undefined) {
    issues.push(issue('compose', '純檔案模式不可設定 Docker Compose'))
  } else if (service.compose) {
    if (!Array.isArray(service.compose.files) || !service.compose.files.length
        || service.compose.files.some((file) => typeof file !== 'string' || !file)) {
      issues.push(issue('compose.files', '至少需要一個非空 Compose 檔名'))
    }
    if (service.compose.env_file !== undefined) {
      requiredString(issues, service.compose.env_file, 'compose.env_file')
    }
  }
  if (!['stop', 'hooks', 'external', 'live', 'snapshot', 'files'].includes(consistency.mode)) {
    issues.push(issue('consistency.mode', '不支援的備份模式'))
  }
  if (consistency.timeout !== undefined
      && (!Number.isInteger(consistency.timeout) || consistency.timeout <= 0)) {
    issues.push(issue('consistency.timeout', '必須是大於 0 的整數'))
  }
  for (const key of ['before', 'after'] as const) {
    const hooks = consistency[key]
    if (hooks !== undefined
        && (!Array.isArray(hooks)
          || hooks.some((hook) => typeof hook !== 'string' || !hook))) {
      issues.push(issue(`consistency.${key}`, '必須是非空字串陣列'))
    }
  }
  if (consistency.mode !== 'hooks'
      && ((consistency.before?.length ?? 0) || (consistency.after?.length ?? 0))) {
    issues.push(issue('consistency', 'before/after hooks 只能用於 hooks 模式'))
  }
  const actionNames = new Set<string>()
  for (const phase of ['before', 'finally', 'on_success', 'on_failure'] as const) {
    ;(service.actions?.[phase] ?? []).forEach((action, index) => {
      validateAction(action, `actions.${phase}[${index}]`, issues)
      if (actionNames.has(action.name)) {
        issues.push(issue(`actions.${phase}[${index}].name`, 'action name 必須跨 phase 唯一'))
      }
      actionNames.add(action.name)
    })
  }
  const allIds = new Set<string>()
  validatePathSources(service, service.sources?.paths ?? [], issues, allIds)
  if (consistency.mode === 'files' && (service.sources?.volumes.length ?? 0) > 0) {
    issues.push(issue('sources.volumes', '純檔案模式不可設定 Docker volumes'))
  }
  const volumeTargets = new Set<string>()
  ;(service.sources?.volumes ?? []).forEach((source, index) => {
    const path = `sources.volumes[${index}]`
    if (!requiredString(issues, source.id, `${path}.id`) || !SAFE_ID.test(source.id)) {
      issues.push(issue(`${path}.id`, '只能使用英數、.、_、-'))
    } else if (allIds.has(source.id)) {
      issues.push(issue(`${path}.id`, 'source id 不可重複'))
    } else allIds.add(source.id)
    const targets = [source.name, source.compose_volume].filter(Boolean)
    if (targets.length !== 1) {
      issues.push(issue(path, 'name 與 compose_volume 必須擇一設定'))
    } else {
      const field = source.name ? 'name' : 'compose_volume'
      const value = source[field]!
      const target = `${field}\0${value}`
      if (volumeTargets.has(target)) {
        issues.push(issue(`${path}.${field}`, 'Docker volume target 不可重複'))
      } else {
        volumeTargets.add(target)
      }
    }
    if (source.name && !VOLUME_NAME.test(source.name)) {
      issues.push(issue(`${path}.name`, '不是安全的 Docker volume 名稱'))
    }
  })
  return issues
}

export function validateDocument(document: EditorDocument): ValidationIssue[] {
  return document.kind === 'global'
    ? validateGlobal(document.value)
    : validateService(document.value)
}
