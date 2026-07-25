<script setup lang="ts">
import type { RetentionConfig, ServiceManifest } from '../../domain/types'
import StringListEditor from '../shared/StringListEditor.vue'

const model = defineModel<ServiceManifest>({ required: true })
model.value.schedule ??= { cron: '' }
model.value.retention ??= {}

const retentionFields: { key: keyof RetentionConfig; label: string }[] = [
  { key: 'keep_last', label: '最近幾份' },
  { key: 'keep_hourly', label: '每小時' },
  { key: 'keep_daily', label: '每日' },
  { key: 'keep_weekly', label: '每週' },
  { key: 'keep_monthly', label: '每月' },
  { key: 'keep_yearly', label: '每年' },
]

function ensureCompose(): void {
  model.value.compose ??= { files: ['compose.yaml'] }
}
</script>

<template>
  <div class="editor-stack">
    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SERVICE</p>
          <h2>服務識別</h2>
        </div>
        <el-switch
          v-model="model.enabled"
          inline-prompt
          active-text="啟用"
          inactive-text="停用"
        />
      </div>
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="穩定 Service ID">
            <el-input v-model="model.service" placeholder="minecraft" />
            <div class="field-help">用於 CLI、Restic tag 與狀態；移動目錄時不要更改。</div>
          </el-form-item>
          <el-form-item label="顯示名稱（選填）">
            <el-input v-model="model.name" placeholder="Minecraft Server" />
          </el-form-item>
        </div>
        <el-form-item>
          <el-checkbox v-model="model.create_snapshot_if_unchanged">
            內容未變時仍建立新的 Restic snapshot
          </el-checkbox>
        </el-form-item>
      </el-form>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SCHEDULE</p>
          <h2>排程與重試</h2>
        </div>
        <el-switch
          v-model="model.schedule.enabled"
          inline-prompt
          active-text="排程"
          inactive-text="停用"
        />
      </div>
      <el-form label-position="top">
        <el-form-item label="Cron（分鐘 小時 日 月 星期）">
          <el-input v-model="model.schedule.cron" placeholder="0 */6 * * *" />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="失敗後多久重試">
            <el-input v-model="model.schedule.retry_after" placeholder="30m" />
          </el-form-item>
          <el-form-item label="最多可延遲">
            <el-input v-model="model.schedule.max_lateness" placeholder="2h" />
          </el-form-item>
        </div>
      </el-form>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">RETENTION</p>
          <h2>快照保留策略</h2>
        </div>
      </div>
      <p class="section-copy">
        條件採聯集；符合任一規則就保留。若要嚴格只留固定份數，只設定「最近幾份」。
      </p>
      <div class="retention-grid">
        <el-form-item v-for="field in retentionFields" :key="field.key" :label="field.label">
          <el-input-number
            v-model="model.retention[field.key]"
            :min="0"
            :precision="0"
            controls-position="right"
          />
        </el-form-item>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">COMPOSE</p>
          <h2>Docker Compose</h2>
        </div>
      </div>
      <el-form v-if="model.compose" label-position="top">
        <el-form-item label="Compose files">
          <StringListEditor
            v-model="model.compose.files"
            placeholder="compose.yaml"
            :minimum="1"
          />
        </el-form-item>
        <el-form-item label="環境檔（選填）">
          <el-input v-model="model.compose.env_file" placeholder=".env" />
          <div class="field-help">未設定時 backupctl 會明確使用空環境檔。</div>
        </el-form-item>
      </el-form>
      <el-button v-else type="primary" plain @click="ensureCompose">
        加入 Compose 設定
      </el-button>
    </section>
  </div>
</template>
