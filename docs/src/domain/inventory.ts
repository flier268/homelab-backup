import type {
  InventoryChoice,
  InventoryEntry,
  InventoryEntryType,
  PathSource,
  SourceGroup,
} from './types'

export const INVENTORY_COMMAND = `python3 - <<'PY'
import json
import os
import stat
import tempfile

OUTPUT = "homelab-backup-inventory.jsonl"
descriptor, temporary_path = tempfile.mkstemp(
    prefix=".homelab-backup-inventory.", suffix=".tmp", dir="."
)
temporary_name = os.path.relpath(temporary_path, ".")
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        print(
            json.dumps(
                {"format": "homelab-backup-inventory", "version": 1, "root": "."}
            ),
            file=output,
        )
        for current, directories, files in os.walk(
            ".", topdown=True, followlinks=False
        ):
            names = sorted(directories + files)
            for name in names:
                path = os.path.join(current, name)
                relative = os.path.relpath(path, ".")
                if relative in {OUTPUT, temporary_name}:
                    continue
                mode = os.lstat(path).st_mode
                if stat.S_ISLNK(mode):
                    kind = "symlink"
                elif stat.S_ISDIR(mode):
                    kind = "directory"
                elif stat.S_ISREG(mode):
                    kind = "file"
                else:
                    continue
                print(
                    json.dumps(
                        {"path": relative, "type": kind}, ensure_ascii=False
                    ),
                    file=output,
                )
            directories[:] = [
                name for name in sorted(directories)
                if not os.path.islink(os.path.join(current, name))
            ]
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, OUTPUT)
except BaseException:
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
    raise
PY`

const ENTRY_TYPES = new Set<InventoryEntryType>(['file', 'directory', 'symlink'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function isSafeRelativePath(path: string): boolean {
  if (!path || path.startsWith('/') || path.includes('\0')) return false
  const parts = path.split('/')
  return parts.every((part) => part !== '' && part !== '.' && part !== '..')
}

export function parseInventory(source: string): InventoryEntry[] {
  const lines = source.split(/\r?\n/).filter((line) => line.trim())
  if (!lines.length) throw new Error('清單是空的。')
  let header: unknown
  try {
    header = JSON.parse(lines[0])
  } catch {
    throw new Error('第 1 行不是有效的 inventory JSON 標頭。')
  }
  if (!isRecord(header)
      || header.format !== 'homelab-backup-inventory'
      || header.version !== 1
      || header.root !== '.') {
    throw new Error('不支援的 inventory 格式、版本或 root。')
  }
  const seen = new Set<string>()
  return lines.slice(1).map((line, index) => {
    let entry: unknown
    try {
      entry = JSON.parse(line)
    } catch {
      throw new Error(`第 ${index + 2} 行不是有效 JSON。`)
    }
    if (!isRecord(entry)
        || typeof entry.path !== 'string'
        || !isSafeRelativePath(entry.path)
        || typeof entry.type !== 'string'
        || !ENTRY_TYPES.has(entry.type as InventoryEntryType)) {
      throw new Error(`第 ${index + 2} 行包含不安全路徑或未知類型。`)
    }
    if (seen.has(entry.path)) throw new Error(`路徑重複：${entry.path}`)
    seen.add(entry.path)
    return { path: entry.path, type: entry.type as InventoryEntryType }
  })
}

function relativeToRoot(path: string, root: string): string | null {
  if (root === '.') return path
  if (path === root) return ''
  return path.startsWith(`${root}/`) ? path.slice(root.length + 1) : null
}

function patternFor(entry: InventoryEntry, relative: string): string {
  return entry.type === 'directory' ? `${relative}/**` : relative
}

export function buildPathSources(
  groups: SourceGroup[],
  entries: InventoryEntry[],
  choices: Record<string, InventoryChoice>,
): PathSource[] {
  const entriesByPath = new Map(entries.map((entry) => [entry.path, entry]))
  return groups.map((group) => {
    const include: string[] = []
    const exclude: string[] = []
    let includesWholeRoot = false
    Object.entries(choices).forEach(([path, choice]) => {
      if (choice.groupKey !== group.key || choice.state === 'unassigned') return
      const entry = entriesByPath.get(path)
      const relative = relativeToRoot(path, group.root)
      if (!entry || relative === null) return
      if (relative === '') {
        if (choice.state === 'include') includesWholeRoot = true
        else exclude.push('**')
        return
      }
      const target = choice.state === 'include' ? include : exclude
      target.push(patternFor(entry, relative))
    })
    const source: PathSource = {
      id: group.id,
      path: group.root,
      required: group.required,
    }
    if (include.length && !includesWholeRoot) {
      source.include = [...new Set(include)].sort()
    }
    if (exclude.length) source.exclude = [...new Set(exclude)].sort()
    return source
  })
}
