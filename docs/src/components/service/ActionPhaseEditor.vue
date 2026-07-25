<script setup lang="ts">
import type { ActionDefinition, ActionPhase } from '../../domain/types'
import { useStableObjectKey } from '../../composables/useStableObjectKey'
import StringListEditor from '../shared/StringListEditor.vue'

const model = defineModel<ActionDefinition[]>({ required: true })
const actionKey = useStableObjectKey('action')

const props = defineProps<{
  phase: ActionPhase
  title: string
  description: string
}>()

function add(): void {
  const ordinal = model.value.length + 1
  model.value.push({
    name: `${props.phase.replace('_', '-')}-${ordinal}`,
    command: ['/usr/bin/true'],
    timeout: 30,
    required: true,
    run_as: 'root',
  })
}
</script>

<template>
  <div class="action-phase">
    <div class="section-heading section-heading--small">
      <div>
        <h3>{{ title }}</h3>
        <p>{{ description }}</p>
      </div>
      <el-button type="primary" plain @click="add">新增 Action</el-button>
    </div>
    <el-empty v-if="!model.length" description="此階段沒有 action" :image-size="56" />
    <div
      v-for="(action, index) in model"
      :key="actionKey(action)"
      class="action-card"
    >
      <div class="source-card__header">
        <strong>{{ title }} #{{ index + 1 }}</strong>
        <el-button type="danger" text @click="model.splice(index, 1)">刪除</el-button>
      </div>
      <div class="form-grid">
        <el-form-item label="唯一名稱">
          <el-input v-model="action.name" />
        </el-form-item>
        <el-form-item label="執行身分">
          <el-input v-model="action.run_as" placeholder='root 或 "UID:GID"' />
        </el-form-item>
        <el-form-item label="逾時秒數">
          <el-input-number
            v-model="action.timeout"
            :min="1"
            :precision="0"
            controls-position="right"
          />
        </el-form-item>
        <el-form-item label="失敗政策">
          <el-switch
            v-model="action.required"
            inline-prompt
            active-text="必要"
            inactive-text="警告"
          />
        </el-form-item>
      </div>
      <el-form-item label="Command argv">
        <StringListEditor
          v-model="action.command"
          placeholder="每一列是一個 argv 參數"
          :minimum="1"
        />
        <div class="field-help">
          不經 shell；executable 請使用絕對路徑。密碼與 token 不要放進 argv。
        </div>
      </el-form-item>
      <div class="command-preview">
        {{ action.command.map((part) => JSON.stringify(part)).join(' ') }}
      </div>
    </div>
  </div>
</template>
