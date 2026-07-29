<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useEditorWorkspace } from '../../composables/useEditorWorkspace'
import ExportPanel from '../export/ExportPanel.vue'
import GlobalConfigEditor from '../global/GlobalConfigEditor.vue'
import ConsistencyActionsEditor from '../service/ConsistencyActionsEditor.vue'
import ServiceOverviewEditor from '../service/ServiceOverviewEditor.vue'
import SourcesEditor from '../service/SourcesEditor.vue'
import type { DocumentKind } from '../../domain/types'
import type { EditorSection } from '../../composables/useHashNavigation'

const props = defineProps<{
  section?: string
}>()

const emit = defineEmits<{
  openGuide: [section: string]
  sectionChange: [section: EditorSection]
}>()

const {
  document, importError, importWarning, issues, yaml, create, importYaml,
} = useEditorWorkspace()
const activeServiceTab = ref<Exclude<EditorSection, 'global'>>('overview')
const yamlInput = ref<HTMLInputElement>()

const guideSection = computed(() => {
  if (document.value.kind === 'global') return 'global-config'
  return {
    overview: 'manifest',
    sources: 'sources',
    actions: 'consistency',
  }[activeServiceTab.value]
})

watch(
  () => props.section,
  (section) => {
    if (section === 'global') {
      if (document.value.kind !== 'global') create('global')
      return
    }
    if (section === 'overview' || section === 'sources' || section === 'actions') {
      if (document.value.kind !== 'service') create('service')
      activeServiceTab.value = section
    }
  },
  { immediate: true },
)

watch(activeServiceTab, (section) => emit('sectionChange', section))

function newDocument(kind: DocumentKind): void {
  create(kind)
  activeServiceTab.value = 'overview'
  emit('sectionChange', kind === 'global' ? 'global' : 'overview')
}

async function importYamlFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    importYaml(await file.text())
    emit('sectionChange', document.value.kind === 'global' ? 'global' : 'overview')
    ElMessage.success(`已匯入 ${file.name}`)
  } catch {
    ElMessage.error('無法匯入設定檔')
  } finally {
    input.value = ''
  }
}
</script>

<template>
  <main class="editor-main">
    <div class="editor-toolbar">
      <div>
        <strong>{{ document.kind === 'global' ? '全域 config.yaml' : '服務 backup.yaml' }}</strong>
        <span>所有資料只在這個瀏覽器分頁處理</span>
      </div>
      <div class="header-actions">
        <input
          ref="yamlInput"
          class="visually-hidden"
          type="file"
          accept=".yaml,.yml,application/yaml,text/yaml,text/plain"
          @change="importYamlFile"
        />
        <el-button @click="yamlInput?.click()">匯入 YAML</el-button>
        <el-dropdown trigger="click" @command="newDocument">
          <el-button type="primary">建立新設定</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="service">服務 backup.yaml</el-dropdown-item>
              <el-dropdown-item command="global">全域 config.yaml</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <section class="hero">
      <div>
        <el-tag effect="dark" round>OFFLINE · VERSION 1</el-tag>
        <h2>把複雜的備份規則，整理成可驗證的設定。</h2>
        <p>
          從服務檔案清單分組、選擇一致性策略、安排生命週期命令，
          最後輸出可交給 backupctl 的 YAML。
        </p>
      </div>
      <div class="document-switch">
        <button
          type="button"
          :class="{ active: document.kind === 'service' }"
          @click="newDocument('service')"
        >
          <span>服務設定</span>
          <small>backup.yaml</small>
        </button>
        <button
          type="button"
          :class="{ active: document.kind === 'global' }"
          @click="newDocument('global')"
        >
          <span>全域設定</span>
          <small>config.yaml</small>
        </button>
      </div>
    </section>

    <div class="context-guide-bar">
      <span>需要欄位規則、風險與完整範例？</span>
      <el-button type="primary" text @click="emit('openGuide', guideSection)">
        查看本頁指南
      </el-button>
    </div>

    <el-alert
      v-if="importError"
      class="page-alert"
      :title="importError"
      type="error"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="importWarning"
      class="page-alert"
      :title="importWarning"
      type="warning"
      :closable="false"
      show-icon
    />

    <GlobalConfigEditor
      v-if="document.kind === 'global'"
      v-model="document.value"
    />

    <el-tabs
      v-else
      v-model="activeServiceTab"
      class="service-tabs"
      stretch
    >
      <el-tab-pane label="基本資料與排程" name="overview">
        <ServiceOverviewEditor v-model="document.value" />
      </el-tab-pane>
      <el-tab-pane label="備份來源與分組" name="sources">
        <SourcesEditor v-model="document.value" />
      </el-tab-pane>
      <el-tab-pane label="模式與執行指令" name="actions">
        <ConsistencyActionsEditor v-model="document.value" />
      </el-tab-pane>
    </el-tabs>

    <ExportPanel
      :kind="document.kind"
      :yaml="yaml"
      :issues="issues"
    />
  </main>
</template>
