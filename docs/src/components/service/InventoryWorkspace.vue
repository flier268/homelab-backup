<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  buildPathSources,
  INVENTORY_COMMAND,
  parseInventory,
} from '../../domain/inventory'
import type {
  InventoryChoice,
  InventoryEntry,
  PathSource,
  SourceGroup,
} from '../../domain/types'

const emit = defineEmits<{
  apply: [sources: PathSource[]]
}>()

interface TreeNode extends InventoryEntry {
  label: string
  children?: TreeNode[]
}

const inventoryText = ref('')
const entries = ref<InventoryEntry[]>([])
const groups = ref<SourceGroup[]>([])
const choices = ref<Record<string, InventoryChoice>>({})
const parseError = ref('')
const fileInput = ref<HTMLInputElement>()

function createTree(items: InventoryEntry[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>()
  const roots: TreeNode[] = []
  items.forEach((entry) => {
    nodes.set(entry.path, { ...entry, label: entry.path.split('/').at(-1) ?? entry.path })
  })
  items.forEach((entry) => {
    const node = nodes.get(entry.path)!
    const separator = entry.path.lastIndexOf('/')
    const parentPath = separator === -1 ? '' : entry.path.slice(0, separator)
    const parent = nodes.get(parentPath)
    if (parent) {
      parent.children ??= []
      parent.children.push(node)
    } else roots.push(node)
  })
  return roots
}

const tree = computed(() => createTree(entries.value))

async function copyCommand(): Promise<void> {
  try {
    await navigator.clipboard.writeText(INVENTORY_COMMAND)
    ElMessage.success('清單命令已複製')
  } catch {
    ElMessage.warning('瀏覽器不允許剪貼簿存取，請手動選取命令')
  }
}

function parse(): void {
  parseError.value = ''
  try {
    entries.value = parseInventory(inventoryText.value)
    choices.value = Object.fromEntries(
      entries.value.map((entry) => [
        entry.path,
        choices.value[entry.path] ?? { state: 'unassigned' },
      ]),
    )
    ElMessage.success(`已載入 ${entries.value.length} 個項目`)
  } catch (error) {
    parseError.value = error instanceof Error ? error.message : String(error)
  }
}

async function importFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  inventoryText.value = await file.text()
  parse()
  input.value = ''
}

function addGroup(): void {
  const ordinal = groups.value.length + 1
  groups.value.push({
    key: crypto.randomUUID(),
    id: `data-${ordinal}`,
    root: '.',
    required: true,
  })
}

function removeGroup(key: string): void {
  groups.value = groups.value.filter((group) => group.key !== key)
  Object.values(choices.value).forEach((choice) => {
    if (choice.groupKey === key) {
      choice.groupKey = undefined
      choice.state = 'unassigned'
    }
  })
}

function updateChoice(path: string, state: InventoryChoice['state']): void {
  const choice = choices.value[path] ?? { state: 'unassigned' }
  choice.state = state
  if (state === 'unassigned') choice.groupKey = undefined
  choices.value[path] = choice
}

function apply(): void {
  if (!groups.value.length) {
    ElMessage.warning('請先建立至少一個來源群組')
    return
  }
  const missingGroup = Object.values(choices.value).some(
    (choice) => choice.state !== 'unassigned' && !choice.groupKey,
  )
  if (missingGroup) {
    ElMessage.warning('每個備份或排除項目都必須指定群組')
    return
  }
  const incompatible = Object.entries(choices.value).some(([path, choice]) => {
    if (choice.state === 'unassigned' || !choice.groupKey) return false
    const group = groups.value.find((item) => item.key === choice.groupKey)
    return !group || (
      group.root !== '.'
      && path !== group.root
      && !path.startsWith(`${group.root}/`)
    )
  })
  if (incompatible) {
    ElMessage.warning('項目必須位於所選群組的來源 root 之下')
    return
  }
  const groupWithoutInclude = groups.value.some((group) => (
    !Object.values(choices.value).some(
      (choice) => choice.groupKey === group.key && choice.state === 'include',
    )
  ))
  if (groupWithoutInclude) {
    ElMessage.warning('每個群組至少要指定一個備份項目；選取群組 root 代表全部備份')
    return
  }
  emit('apply', buildPathSources(groups.value, entries.value, choices.value))
  ElMessage.success('已將分組套用至 path sources')
}
</script>

<template>
  <div class="inventory-workspace">
    <el-alert
      title="清單只包含路徑與類型，不會讀取或上傳檔案內容。命令不跟隨 symlink。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="command-card">
      <div class="command-card__heading">
        <div>
          <strong>1. 在服務根目錄產生清單</strong>
          <p>使用版本化 JSON Lines，避免 ls 輸出受語系或特殊檔名影響。</p>
        </div>
        <el-button type="primary" plain @click="copyCommand">複製命令</el-button>
      </div>
      <pre>{{ INVENTORY_COMMAND }}</pre>
    </div>

    <div class="split-grid">
      <div>
        <div class="subheading">
          <strong>2. 貼上或匯入清單</strong>
          <div>
            <input
              ref="fileInput"
              class="visually-hidden"
              type="file"
              accept=".jsonl,.txt,application/json,text/plain"
              @change="importFile"
            />
            <el-button @click="fileInput?.click()">選取清單檔</el-button>
          </div>
        </div>
        <el-input
          v-model="inventoryText"
          type="textarea"
          :rows="9"
          placeholder="貼上 homelab-backup inventory JSON Lines"
        />
        <el-button class="top-gap" type="primary" @click="parse">解析清單</el-button>
        <el-alert
          v-if="parseError"
          class="top-gap"
          :title="parseError"
          type="error"
          :closable="false"
          show-icon
        />
      </div>

      <div>
        <div class="subheading">
          <strong>3. 建立來源群組</strong>
          <el-button type="primary" plain @click="addGroup">新增群組</el-button>
        </div>
        <el-empty v-if="!groups.length" description="尚未建立群組" :image-size="64" />
        <div v-for="group in groups" :key="group.key" class="group-card">
          <div class="form-grid form-grid--compact">
            <el-form-item label="Source ID">
              <el-input v-model="group.id" />
            </el-form-item>
            <el-form-item label="來源 root">
              <el-input v-model="group.root" placeholder="data" />
            </el-form-item>
          </div>
          <div class="inline-row inline-row--between">
            <el-checkbox v-model="group.required">必要來源</el-checkbox>
            <el-button type="danger" text @click="removeGroup(group.key)">刪除群組</el-button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="entries.length" class="tree-card">
      <div class="subheading">
        <div>
          <strong>4. 指定備份、排除或不納入</strong>
          <p>目錄會輸出為 <code>目錄/**</code>；exclude 永遠優先。</p>
        </div>
        <el-button type="primary" @click="apply">套用至來源</el-button>
      </div>
      <el-tree
        :data="tree"
        node-key="path"
        default-expand-all
        :expand-on-click-node="false"
      >
        <template #default="{ data }">
          <div class="tree-node">
            <span class="tree-node__label">
              <el-tag size="small" effect="plain">{{ data.type }}</el-tag>
              {{ data.label }}
            </span>
            <span class="tree-node__controls" @click.stop>
              <el-select
                :model-value="choices[data.path]?.state ?? 'unassigned'"
                size="small"
                @update:model-value="updateChoice(data.path, $event)"
              >
                <el-option label="未納入" value="unassigned" />
                <el-option label="備份" value="include" />
                <el-option label="排除" value="exclude" />
              </el-select>
              <el-select
                v-if="choices[data.path]?.state !== 'unassigned'"
                v-model="choices[data.path].groupKey"
                size="small"
                placeholder="選擇群組"
              >
                <el-option
                  v-for="group in groups"
                  :key="group.key"
                  :label="group.id"
                  :value="group.key"
                />
              </el-select>
            </span>
          </div>
        </template>
      </el-tree>
    </div>
  </div>
</template>
