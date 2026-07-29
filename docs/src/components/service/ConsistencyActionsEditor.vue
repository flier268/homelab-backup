<script setup lang="ts">
import { computed } from 'vue'
import type {
  ActionDefinition,
  ActionOperation,
  ActionPhase,
  ConsistencyMode,
  ServiceManifest,
} from '../../domain/types'
import ActionPhaseEditor from './ActionPhaseEditor.vue'

const model = defineModel<ServiceManifest>({ required: true })

const modes: {
  id: ConsistencyMode
  title: string
  badge: string
  description: string
  suitable: string
}[] = [
  {
    id: 'stop',
    title: 'Stop',
    badge: '建議預設',
    description: '停止原本運行的整個 Compose project，完成 staging 後恢復。',
    suitable: '持續寫入的遊戲伺服器、raw database volume。',
  },
  {
    id: 'hooks',
    title: 'Hooks',
    badge: '應用協調',
    description: '由 actions.backup 的 before／finally 協調應用一致性。',
    suitable: 'SQL logical dump、RCON save、應用內建 snapshot。',
  },
  {
    id: 'external',
    title: 'External',
    badge: '外部保證',
    description: '不控制服務，但偵測到 Docker writer 時拒絕備份。',
    suitable: '由其他編排器事先靜止的服務或唯讀資料。',
  },
  {
    id: 'live',
    title: 'Live',
    badge: 'Best effort',
    description: '不中斷 writer 直接複製；有 writer 時一致性降級。',
    suitable: '可接受 fuzzy copy、容易重建的非關鍵內容。',
  },
  {
    id: 'snapshot',
    title: 'Btrfs Snapshot',
    badge: '低停機',
    description: 'Btrfs subvolume 使用唯讀 snapshot，其餘來源直接複製。',
    suitable: '已正確規劃為 Btrfs subvolume 的檔案型服務。',
  },
  {
    id: 'files',
    title: '純檔案',
    badge: '無 Docker',
    description: '只備份 path sources，不載入 Compose、volume 或 container 狀態。',
    suitable: '主機設定、一般目錄，以及由 Actions 協調的非 Docker 服務。',
  },
]

const phases: ActionPhase[] = [
  'before', 'after', 'finally', 'on_success', 'on_failure',
]

const operations: {
  id: ActionOperation
  title: string
  descriptions: Record<ActionPhase, string>
}[] = [
  {
    id: 'backup',
    title: 'Backup',
    descriptions: {
      before: '建立 capture context 與擷取來源之前執行。',
      after: '來源擷取成功後、提交 Restic snapshot 前執行。',
      finally: '擷取或 after 成敗都會嘗試執行，適合解除 quiesce。',
      on_success: 'Restic snapshot 已提交且 staging 已清理後執行。',
      on_failure: '備份失敗後執行，並收到 BACKUPCTL_FAILURE_*。',
    },
  },
  {
    id: 'restore',
    title: 'Restore',
    descriptions: {
      before: '服務停止並通過 preflight 後、修改資料前執行。',
      after: 'paths、volumes 與控制檔還原後、服務重啟前執行。',
      finally: '還原或 after 成敗都會嘗試執行。',
      on_success: '服務狀態協調完成後執行。',
      on_failure: '失敗後、最終 rollback／服務協調前執行，並收到 BACKUPCTL_FAILURE_*。',
    },
  },
]

function ensureSections(): void {
  model.value.consistency ??= {
    mode: 'stop',
    timeout: 120,
  }
  model.value.actions ??= {
    backup: {},
    restore: {},
  }
  for (const operation of operations) {
    model.value.actions[operation.id] ??= {}
    for (const phase of phases) {
      model.value.actions[operation.id]![phase] ??= []
    }
  }
}

ensureSections()

const mode = computed({
  get: () => model.value.consistency!.mode,
  set: (value: ConsistencyMode) => {
    const previous = model.value.consistency!.mode
    if (value === 'files' && previous !== 'files') {
      const hasDockerSettings = model.value.compose !== undefined
        || (model.value.sources?.volumes.length ?? 0) > 0
      if (hasDockerSettings && !window.confirm(
        '切換到純檔案模式會移除 Compose 與 Docker volume 設定。確定要繼續嗎？',
      )) {
        return
      }
      delete model.value.compose
      if (model.value.sources) model.value.sources.volumes = []
    } else if (previous === 'files' && value !== 'files') {
      model.value.compose ??= { files: ['compose.yaml'] }
    }
    model.value.consistency!.mode = value
  },
})

const consistency = computed(() => model.value.consistency!)
const actions = computed(() => model.value.actions!)

function setPhase(
  operation: ActionOperation,
  phase: ActionPhase,
  value: ActionDefinition[],
): void {
  actions.value[operation]![phase] = value
}
</script>

<template>
  <div class="editor-stack">
    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">CONSISTENCY</p>
          <h2>選擇一致性模式</h2>
        </div>
        <el-form-item v-if="mode !== 'files'" label="逾時秒數">
          <el-input-number
            v-model="consistency.timeout"
            :min="1"
            :precision="0"
            controls-position="right"
          />
        </el-form-item>
      </div>
      <el-alert
        v-if="mode === 'files'"
        title="純檔案模式不會偵測 writer，也不會呼叫 Docker。若來源正在寫入，請用 before/finally Actions 自行協調。"
        type="warning"
        :closable="false"
        show-icon
      />
      <div class="mode-grid">
        <button
          v-for="item in modes"
          :key="item.id"
          class="mode-card"
          :class="{ 'mode-card--active': mode === item.id }"
          type="button"
          @click="mode = item.id"
        >
          <span class="mode-card__top">
            <strong>{{ item.title }}</strong>
            <el-tag
              size="small"
              :type="item.id === 'stop' ? 'success' : item.id === 'live' ? 'warning' : 'info'"
            >
              {{ item.badge }}
            </el-tag>
          </span>
          <span>{{ item.description }}</span>
          <small>{{ item.suitable }}</small>
        </button>
      </div>

      <div v-if="mode === 'hooks'" class="hooks-grid">
        <el-alert
          title="請在 actions.backup.before 建立 logical dump 或隔離 writer，並在 actions.backup.finally 做必要清理。"
          type="warning"
          :closable="false"
          show-icon
        />
      </div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">ACTIONS</p>
          <h2>Backup／Restore 生命週期命令</h2>
        </div>
      </div>
      <el-alert
        title="每個 Action 必須擇一使用安全 argv Command 或 inline Bash Script；名稱須跨操作與階段唯一。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-tabs>
        <el-tab-pane
          v-for="operation in operations"
          :key="operation.id"
          :label="operation.title"
          :name="operation.id"
        >
          <el-collapse class="actions-collapse" accordion>
            <el-collapse-item
              v-for="phase in phases"
              :key="phase"
              :name="`${operation.id}.${phase}`"
            >
              <template #title>
                <strong>{{ phase }}</strong>
                <el-tag class="phase-count" size="small" effect="plain">
                  {{ actions[operation.id]?.[phase]?.length ?? 0 }}
                </el-tag>
              </template>
              <ActionPhaseEditor
                :model-value="actions[operation.id]?.[phase] ?? []"
                :operation="operation.id"
                :phase="phase"
                :title="`${operation.title} · ${phase}`"
                :description="operation.descriptions[phase]"
                @update:model-value="setPhase(operation.id, phase, $event)"
              />
            </el-collapse-item>
          </el-collapse>
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>
</template>
