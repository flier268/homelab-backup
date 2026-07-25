import { describe, expect, it } from 'vitest'
import {
  buildPathSources,
  INVENTORY_COMMAND,
  parseInventory,
} from '../src/domain/inventory'
import type {
  InventoryChoice,
  InventoryEntry,
  SourceGroup,
} from '../src/domain/types'

const header = JSON.stringify({
  format: 'homelab-backup-inventory',
  version: 1,
  root: '.',
})

describe('parseInventory', () => {
  it('generator excludes its own output and unsupported special files', () => {
    expect(INVENTORY_COMMAND).toContain('if relative in {OUTPUT, temporary_name}:')
    expect(INVENTORY_COMMAND).toContain('elif stat.S_ISREG(mode):')
    expect(INVENTORY_COMMAND).toMatch(/else:\n\s+continue/)
  })

  it('publishes output without following an existing destination symlink', () => {
    expect(INVENTORY_COMMAND).not.toContain('> homelab-backup-inventory.jsonl')
    expect(INVENTORY_COMMAND).toContain('tempfile.mkstemp(')
    expect(INVENTORY_COMMAND).toContain('os.replace(temporary_path, OUTPUT)')
  })

  it('parses versioned JSON Lines and preserves escaped special names', () => {
    const source = [
      header,
      JSON.stringify({ path: 'data', type: 'directory' }),
      JSON.stringify({ path: 'data/世界 name\tline\nbreak.txt', type: 'file' }),
      JSON.stringify({ path: 'current', type: 'symlink' }),
    ].join('\n')

    expect(parseInventory(source)).toEqual([
      { path: 'data', type: 'directory' },
      { path: 'data/世界 name\tline\nbreak.txt', type: 'file' },
      { path: 'current', type: 'symlink' },
    ])
  })

  it.each(['/etc/passwd', '../secret', 'data/../secret', './data'])(
    'rejects unsafe path %s',
    (path) => {
      const source = [
        header,
        JSON.stringify({ path, type: 'file' }),
      ].join('\n')
      expect(() => parseInventory(source)).toThrow(/不安全路徑/)
    },
  )

  it('rejects duplicate paths', () => {
    const line = JSON.stringify({ path: 'data/file', type: 'file' })
    expect(() => parseInventory([header, line, line].join('\n')))
      .toThrow(/路徑重複/)
  })
})

describe('buildPathSources', () => {
  it('creates deterministic include and exclude patterns by group', () => {
    const entries: InventoryEntry[] = [
      { path: 'data', type: 'directory' },
      { path: 'data/world', type: 'directory' },
      { path: 'data/server.properties', type: 'file' },
      { path: 'data/cache', type: 'directory' },
    ]
    const groups: SourceGroup[] = [{
      key: 'world-group',
      id: 'world',
      root: 'data',
      required: true,
    }]
    const choices: Record<string, InventoryChoice> = {
      'data/world': { state: 'include', groupKey: 'world-group' },
      'data/server.properties': { state: 'include', groupKey: 'world-group' },
      'data/cache': { state: 'exclude', groupKey: 'world-group' },
    }

    expect(buildPathSources(groups, entries, choices)).toEqual([{
      id: 'world',
      path: 'data',
      required: true,
      include: ['server.properties', 'world/**'],
      exclude: ['cache/**'],
    }])
  })

  it('uses a selected group root to mean backup everything below it', () => {
    const entries: InventoryEntry[] = [
      { path: 'data', type: 'directory' },
      { path: 'data/cache', type: 'directory' },
    ]
    const groups: SourceGroup[] = [{
      key: 'data-group',
      id: 'data',
      root: 'data',
      required: true,
    }]
    const choices: Record<string, InventoryChoice> = {
      data: { state: 'include', groupKey: 'data-group' },
      'data/cache': { state: 'exclude', groupKey: 'data-group' },
    }

    expect(buildPathSources(groups, entries, choices)).toEqual([{
      id: 'data',
      path: 'data',
      required: true,
      exclude: ['cache/**'],
    }])
  })
})
