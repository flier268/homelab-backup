<script setup lang="ts">
import { ElMessage } from 'element-plus'
import type {
  DocumentKind,
  ValidationIssue,
} from '../../domain/types'

const props = defineProps<{
  kind: DocumentKind
  yaml: string
  issues: ValidationIssue[]
}>()

async function copyYaml(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.yaml)
    ElMessage.success('YAML 已複製')
  } catch {
    ElMessage.warning('瀏覽器不允許剪貼簿存取，請從預覽手動複製')
  }
}

function download(): void {
  if (props.issues.length) return
  const blob = new Blob([props.yaml], { type: 'application/yaml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = window.document.createElement('a')
  anchor.href = url
  anchor.download = props.kind === 'global' ? 'config.yaml' : 'backup.yaml'
  anchor.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="export-layout">
    <section class="panel validation-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">VALIDATION</p>
          <h2>{{ issues.length ? `${issues.length} 個問題` : '設定格式正確' }}</h2>
        </div>
        <el-tag :type="issues.length ? 'danger' : 'success'" size="large">
          {{ issues.length ? '不可匯出' : '可以匯出' }}
        </el-tag>
      </div>
      <el-alert
        v-if="!issues.length"
        title="瀏覽器 schema 驗證已通過。部署前仍需在目標主機執行 sudo backupctl validate。"
        type="success"
        :closable="false"
        show-icon
      />
      <el-table v-else :data="issues" size="small" max-height="360">
        <el-table-column prop="path" label="欄位" min-width="180" />
        <el-table-column prop="message" label="問題" min-width="280" />
      </el-table>
      <div class="validation-command">
        <code>sudo backupctl validate</code>
      </div>
      <p class="field-help">
        靜態編輯器無法檢查實際檔案權限、filesystem、Docker volume、Compose 插值或 executable。
      </p>
    </section>

    <section class="panel yaml-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">YAML</p>
          <h2>輸出預覽</h2>
        </div>
        <div class="button-row">
          <el-button @click="copyYaml">複製</el-button>
          <el-button type="primary" :disabled="issues.length > 0" @click="download">
            下載 {{ kind === 'global' ? 'config.yaml' : 'backup.yaml' }}
          </el-button>
        </div>
      </div>
      <el-input :model-value="yaml" type="textarea" :rows="24" readonly />
    </section>
  </div>
</template>
