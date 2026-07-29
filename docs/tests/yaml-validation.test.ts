import { describe, expect, it } from 'vitest'
import { createGlobalConfig, createServiceManifest } from '../src/domain/defaults'
import { validateDocument } from '../src/domain/validation'
import { parseEditorDocument, serializeEditorDocument } from '../src/domain/yaml'
import type { GlobalConfig } from '../src/domain/types'

describe('YAML document codec', () => {
  it('round-trips a service manifest and detects its kind', () => {
    const original = {
      kind: 'service' as const,
      value: createServiceManifest(),
    }
    const parsed = parseEditorDocument(serializeEditorDocument(original))
    expect(parsed).toEqual(original)
  })

  it('round-trips a global config and detects its kind', () => {
    const original = {
      kind: 'global' as const,
      value: createGlobalConfig(),
    }
    const parsed = parseEditorDocument(serializeEditorDocument(original))
    expect(parsed).toEqual(original)
  })

  it('rejects unknown top-level fields instead of silently dropping them', () => {
    expect(() => parseEditorDocument(`
version: 1
service: demo
unexpected: true
    `)).toThrow(/不支援的欄位/)
  })

  it('rejects unknown nested fields instead of silently dropping them', () => {
    expect(() => parseEditorDocument(`
version: 1
service: demo
schedule:
  cron: "0 0 * * *"
  cronn: "typo"
    `)).toThrow(/schedule 不支援的欄位/)
  })

  it('normalizes sparse service sections accepted by the runtime', () => {
    const parsed = parseEditorDocument(`
version: 1
service: sparse
schedule:
  enabled: false
retention:
  keep_last: 1
compose: {}
sources:
  volumes:
    - id: data
      name: demo_data
`)

    expect(parsed).toEqual({
      kind: 'service',
      value: expect.objectContaining({
        compose: { files: ['compose.yaml'] },
        sources: {
          paths: [],
          volumes: [{ id: 'data', name: 'demo_data' }],
        },
      }),
    })
  })

  it('normalizes null optional service sections accepted by the runtime', () => {
    const parsed = parseEditorDocument(`
version: 1
service: nullable
schedule:
  enabled: false
retention:
  keep_last: 1
compose: null
consistency: null
actions: null
sources: null
`)

    expect(parsed).toEqual({
      kind: 'service',
      value: expect.objectContaining({
        compose: { files: ['compose.yaml'] },
        consistency: { mode: 'stop', before: [], after: [] },
        actions: {
          before: [],
          finally: [],
          on_success: [],
          on_failure: [],
        },
        sources: { paths: [], volumes: [] },
      }),
    })
  })

  it('preserves an explicit files mode without adding Compose defaults', () => {
    const parsed = parseEditorDocument(`
version: 1
service: host-files
schedule:
  enabled: false
retention:
  keep_last: 1
consistency:
  mode: files
sources:
  paths:
    - id: data
      path: /srv/data
  volumes: []
`)

    expect(parsed.kind).toBe('service')
    if (parsed.kind !== 'service') throw new Error('expected service manifest')
    expect(parsed.value.compose).toBeUndefined()
    expect(validateDocument(parsed)).toEqual([])
  })

  it('rejects nested values whose types would crash an editor control', () => {
    expect(() => parseEditorDocument(`
version: 1
service: demo
actions:
  before:
    - name: invalid
      command: /usr/bin/true
      run_as: root
`)).toThrow(/command.*陣列/)
  })

  it('reports missing required values without crashing validation', () => {
    const global = parseEditorDocument(`
version: 1
host_id: incomplete
`)
    expect(() => validateDocument(global)).not.toThrow()
    expect(validateDocument(global)).toContainEqual(expect.objectContaining({
      path: 'services_root',
    }))

    const service = parseEditorDocument(`
version: 1
service: incomplete
schedule:
  enabled: false
retention:
  keep_last: 1
sources:
  paths:
    - id: missing-path
    - id: valid-path
      path: data
`)
    expect(() => validateDocument(service)).not.toThrow()
    expect(validateDocument(service)).toContainEqual(expect.objectContaining({
      path: 'sources.paths[0].path',
    }))
  })
})

describe('schema validation', () => {
  it('accepts both new-document templates', () => {
    expect(validateDocument({
      kind: 'global',
      value: createGlobalConfig(),
    })).toEqual([])
    expect(validateDocument({
      kind: 'service',
      value: createServiceManifest(),
    })).toEqual([])
  })

  it('accepts files mode and rejects Docker-only sections', () => {
    const value = createServiceManifest()
    value.consistency = { mode: 'files', before: [], after: [] }
    delete value.compose
    value.sources = {
      paths: [{ id: 'data', path: '/srv/data', required: true }],
      volumes: [],
    }
    expect(validateDocument({ kind: 'service', value })).toEqual([])

    value.compose = { files: ['compose.yaml'] }
    expect(validateDocument({ kind: 'service', value })).toContainEqual(
      expect.objectContaining({ path: 'compose' }),
    )
    delete value.compose
    value.sources.volumes = [{ id: 'data-volume', name: 'demo_data' }]
    expect(validateDocument({ kind: 'service', value })).toContainEqual(
      expect.objectContaining({ path: 'sources.volumes' }),
    )
  })

  it('rejects overlapping path sources and duplicate action names', () => {
    const value = createServiceManifest()
    value.sources!.paths = [
      { id: 'data', path: 'data' },
      { id: 'child', path: 'data/child' },
    ]
    value.actions = {
      before: [{
        name: 'notify',
        command: ['/usr/bin/true'],
        run_as: 'root',
      }],
      on_success: [{
        name: 'notify',
        command: ['/usr/bin/true'],
        run_as: 'root',
      }],
    }
    const issues = validateDocument({ kind: 'service', value })
    expect(issues.some(({ message }) => message.includes('互相重疊'))).toBe(true)
    expect(issues.some(({ message }) => message.includes('跨 phase 唯一'))).toBe(true)
  })

  it('rejects data paths covering protected controls but allows exact Compose controls', () => {
    const value = createServiceManifest()
    value.compose = {
      files: ['config/compose.yaml'],
      env_file: 'config/compose.env',
    }
    value.sources!.paths = [{ id: 'all', path: '.' }]
    expect(validateDocument({ kind: 'service', value }))
      .toContainEqual(expect.objectContaining({
        path: 'sources.paths[0]',
        message: expect.stringContaining('backup.yaml'),
      }))

    value.sources!.paths = [{ id: 'config', path: 'config' }]
    expect(validateDocument({ kind: 'service', value }))
      .toContainEqual(expect.objectContaining({
        path: 'sources.paths[0]',
        message: expect.stringContaining('Compose 控制檔'),
      }))

    value.sources!.paths = [
      { id: 'compose', path: 'config/compose.yaml' },
      { id: 'env', path: 'config/compose.env' },
    ]
    expect(validateDocument({ kind: 'service', value })).toEqual([])
  })

  it('allows Bash hooks only in hooks mode', () => {
    const value = createServiceManifest()
    value.consistency = {
      mode: 'live',
      before: ['echo freeze'],
      after: [],
    }
    expect(validateDocument({ kind: 'service', value }))
      .toContainEqual(expect.objectContaining({ path: 'consistency' }))
  })

  it('matches runtime action identity and executable rules', () => {
    const value = createServiceManifest()
    value.actions = {
      before: [{
        name: 'bad-action',
        command: ['./relative-script'],
        run_as: 'UppercaseUser',
      }],
    }
    const issues = validateDocument({ kind: 'service', value })
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'actions.before[0].run_as',
    }))
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'actions.before[0].command[0]',
    }))
  })

  it('rejects cron fields outside the runtime-supported range', () => {
    const value = createServiceManifest()
    value.schedule.cron = '99 99 * * *'

    expect(validateDocument({ kind: 'service', value }))
      .toContainEqual(expect.objectContaining({ path: 'schedule.cron' }))
  })

  it('accepts Sunday as the end of a weekday range like the runtime', () => {
    const value = createServiceManifest()
    value.schedule.cron = '0 4 * * FRI-SUN'

    expect(validateDocument({ kind: 'service', value })).toEqual([])
  })

  it('rejects non-string optional global settings', () => {
    const value = createGlobalConfig() as unknown as {
      rclone: { bwlimit: unknown }
      check: { read_data_subset: unknown }
    }
    value.rclone.bwlimit = 10
    value.check.read_data_subset = 5

    const issues = validateDocument({
      kind: 'global',
      value: value as GlobalConfig,
    })
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'rclone.bwlimit',
    }))
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'check.read_data_subset',
    }))
  })

  it('accepts but removes the deprecated trusted data roots field', () => {
    const parsed = parseEditorDocument(`
version: 1
host_id: server-a
trusted_data_roots:
  - /srv/data
`)

    expect(serializeEditorDocument(parsed)).not.toContain('trusted_data_roots')
  })

  it('rejects empty compose env files and hook commands', () => {
    const value = createServiceManifest()
    value.compose!.env_file = ''
    value.consistency = {
      mode: 'hooks',
      before: [''],
      after: [],
    }

    const issues = validateDocument({ kind: 'service', value })
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'compose.env_file',
    }))
    expect(issues).toContainEqual(expect.objectContaining({
      path: 'consistency.before',
    }))
  })

  it('rejects duplicate Docker volume targets across source IDs', () => {
    const value = createServiceManifest()
    value.sources!.volumes = [
      { id: 'primary', name: 'shared_data' },
      { id: 'secondary', name: 'shared_data' },
    ]

    expect(validateDocument({ kind: 'service', value }))
      .toContainEqual(expect.objectContaining({
        path: 'sources.volumes[1].name',
      }))
  })
})
