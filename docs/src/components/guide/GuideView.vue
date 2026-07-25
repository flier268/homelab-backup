<script setup lang="ts">
import { nextTick, onMounted, watch } from 'vue'
import type { EditorSection } from '../../composables/useHashNavigation'
import GuideBackupSections from './GuideBackupSections.vue'
import GuideConfigSections from './GuideConfigSections.vue'
import GuideOperationsSections from './GuideOperationsSections.vue'
import GuideSetupSections from './GuideSetupSections.vue'

const props = defineProps<{
  section?: string
}>()

const emit = defineEmits<{
  openEditor: [section: EditorSection]
}>()

const chapters = [
  ['goal', '1. 目標與架構'],
  ['editor', '設定檔編輯器'],
  ['requirements', '2. 前置條件'],
  ['docker', '3. 安裝 Docker Compose'],
  ['directories', '4. 建立 /srv/stacks'],
  ['git', '5. 可選：用 Git 保存 stacks'],
  ['install', '6. 取得並安裝專案'],
  ['onedrive', '7. 設定 OneDrive'],
  ['global-config', '8. 全域設定'],
  ['config-backup', '9. 備份復原必要設定'],
  ['manifest', '10. backup.yaml'],
  ['consistency', '11. 一致性模式'],
  ['sources', '12. Bind 與 Volume'],
  ['cron', '13. Cron 排程'],
  ['retention', '14. 保留策略'],
  ['examples', '15. 服務範例'],
  ['validate', '16. 驗證與初始化'],
  ['first-backup', '17. 首次備份'],
  ['restore-test', '18. 還原與全選操作'],
  ['enable', '19. 啟用自動排程'],
  ['monitor', '20. 監控與維護'],
  ['secrets', '21. 災難復原必要材料'],
  ['disaster', '22. 主機全毀重建'],
] as const

const editorTargets: Record<string, EditorSection> = {
  editor: 'overview',
  'global-config': 'global',
  manifest: 'overview',
  consistency: 'actions',
  sources: 'sources',
  cron: 'overview',
  retention: 'overview',
  examples: 'overview',
}

async function scrollToSection(section: string): Promise<void> {
  await nextTick()
  const target = document.getElementById(section || 'goal')
  target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

watch(() => props.section, (section) => scrollToSection(section ?? 'goal'))
onMounted(() => scrollToSection(props.section ?? 'goal'))
</script>

<template>
  <main class="guide-shell">
    <aside class="guide-sidebar">
      <div class="guide-sidebar__heading">
        <p class="eyebrow">DOCUMENTATION</p>
        <h2>部署與維運指南</h2>
      </div>
      <nav aria-label="指南章節">
        <a
          v-for="[id, label] in chapters"
          :key="id"
          :href="`#/guide/${id}`"
          :class="{ active: section === id || (!section && id === 'goal') }"
        >
          {{ label }}
        </a>
      </nav>
    </aside>

    <div class="guide-main">
      <section class="guide-hero">
        <div>
          <el-tag effect="dark" round>GUIDE · OFFLINE</el-tag>
          <h1>從部署到災難復原，和設定編輯器放在同一個地方。</h1>
          <p>閱讀規則時直接打開對應表單；編輯設定時也能回到完整背景與風險說明。</p>
        </div>
        <el-button type="primary" size="large" @click="emit('openEditor', 'overview')">
          開始編輯 backup.yaml
        </el-button>
      </section>

      <div class="guide-context-action">
        <span>
          {{ editorTargets[section ?? ''] ? '這個章節有對應的視覺化設定頁。' : '需要建立設定檔？' }}
        </span>
        <el-button
          type="primary"
          text
          @click="emit('openEditor', editorTargets[section ?? ''] ?? 'overview')"
        >
          {{ editorTargets[section ?? ''] ? '前往對應編輯器' : '開啟設定編輯器' }}
        </el-button>
      </div>

      <article class="guide-content">
        <GuideSetupSections />
        <GuideConfigSections />
        <GuideBackupSections />
        <GuideOperationsSections />
      </article>
    </div>
  </main>
</template>
