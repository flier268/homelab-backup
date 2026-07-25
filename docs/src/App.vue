<script setup lang="ts">
import EditorView from './components/editor/EditorView.vue'
import GuideView from './components/guide/GuideView.vue'
import {
  useHashNavigation,
  type EditorSection,
} from './composables/useHashNavigation'

const { route, navigate } = useHashNavigation()

function openEditor(section: EditorSection): void {
  navigate('editor', section)
}

function updateEditorSection(section: EditorSection): void {
  if (route.value.view === 'editor') navigate('editor', section)
}
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand">
        <span class="brand-mark">HB</span>
        <div>
          <p>homelab-backup</p>
          <h1>備份設定工作台</h1>
        </div>
      </div>
      <nav class="app-view-nav" aria-label="主要功能">
        <button
          type="button"
          :class="{ active: route.view === 'editor' }"
          @click="navigate('editor', 'overview')"
        >
          設定編輯器
        </button>
        <button
          type="button"
          :class="{ active: route.view === 'guide' }"
          @click="navigate('guide', 'goal')"
        >
          完整指南
        </button>
      </nav>
    </header>

    <EditorView
      v-show="route.view === 'editor'"
      :section="route.section"
      @open-guide="navigate('guide', $event)"
      @section-change="updateEditorSection"
    />
    <GuideView
      v-show="route.view === 'guide'"
      :section="route.section"
      @open-editor="openEditor"
    />

    <footer>
      所有文件與設定工具皆包含在本機 HTML；不會上傳設定或檔案清單。
    </footer>
  </div>
</template>
