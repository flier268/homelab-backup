<script setup lang="ts">
import { computed } from 'vue'
import type {
  ActionPhase,
  ConsistencyMode,
  ServiceManifest,
} from '../../domain/types'
import StringListEditor from '../shared/StringListEditor.vue'
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
    description: '先執行 before Bash hook，備份後執行 after hook。',
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
]

const phases: { id: ActionPhase; title: string; description: string }[] = [
  {
    id: 'before',
    title: '備份交易前',
    description: '任何 consistency mode 開始 staging 前執行；必要 action 失敗會中止。',
  },
  {
    id: 'finally',
    title: '交易清理後',
    description: '服務恢復、hook after 或 snapshot 清理之後執行；主要流程失敗也會嘗試。',
  },
  {
    id: 'on_success',
    title: '成功後',
    description: 'Restic snapshot 已提交，或內容未變而成功略過時執行。',
  },
  {
    id: 'on_failure',
    title: '失敗後',
    description: '收到 BACKUPCTL_FAILURE_* 環境變數，可用於安全通知與診斷。',
  },
]

function ensureSections(): void {
  model.value.consistency ??= {
    mode: 'stop',
    timeout: 120,
    before: [],
    after: [],
  }
  model.value.actions ??= {
    before: [],
    finally: [],
    on_success: [],
    on_failure: [],
  }
}

ensureSections()

const mode = computed({
  get: () => model.value.consistency!.mode,
  set: (value: ConsistencyMode) => {
    model.value.consistency!.mode = value
    if (value !== 'hooks') {
      model.value.consistency!.before = []
      model.value.consistency!.after = []
    }
  },
})

const consistency = computed(() => model.value.consistency!)
const actions = computed(() => model.value.actions!)
</script>

<template>
  <div class="editor-stack">
    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">CONSISTENCY</p>
          <h2>選擇一致性模式</h2>
        </div>
        <el-form-item label="逾時秒數">
          <el-input-number
            v-model="consistency.timeout"
            :min="1"
            :precision="0"
            controls-position="right"
          />
        </el-form-item>
      </div>
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
        <el-form-item label="Before hooks（Bash）">
          <StringListEditor
            :model-value="consistency.before ?? []"
            placeholder="./scripts/dump-database.sh"
            @update:model-value="consistency.before = $event"
          />
        </el-form-item>
        <el-form-item label="After hooks（Bash）">
          <StringListEditor
            :model-value="consistency.after ?? []"
            placeholder="./scripts/resume-writes.sh"
            @update:model-value="consistency.after = $event"
          />
        </el-form-item>
        <el-alert
          title="Hooks 由 /bin/bash -euo pipefail -c 執行。before 必須真的停止或隔離所有 writer；required volume 不能由 hook 動態建立。"
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
          <h2>備份生命週期命令</h2>
        </div>
      </div>
      <el-alert
        title="Action 使用 argv 直接執行、不經 shell；root 執行檔及其父目錄必須由 root 擁有、不可被其他人寫入，也不可是 symlink。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-collapse class="actions-collapse" accordion>
        <el-collapse-item
          v-for="phase in phases"
          :key="phase.id"
          :name="phase.id"
        >
          <template #title>
            <strong>{{ phase.title }}</strong>
            <el-tag class="phase-count" size="small" effect="plain">
              {{ actions[phase.id]?.length ?? 0 }}
            </el-tag>
          </template>
          <ActionPhaseEditor
            :model-value="actions[phase.id] ?? []"
            :phase="phase.id"
            :title="phase.title"
            :description="phase.description"
            @update:model-value="actions[phase.id] = $event"
          />
        </el-collapse-item>
      </el-collapse>
    </section>
  </div>
</template>
