<script setup lang="ts">
import { ref } from 'vue'
import { useStableObjectKey } from '../../composables/useStableObjectKey'
import type {
  PathSource,
  ServiceManifest,
  VolumeSource,
} from '../../domain/types'
import StringListEditor from '../shared/StringListEditor.vue'
import InventoryWorkspace from './InventoryWorkspace.vue'

const model = defineModel<ServiceManifest>({ required: true })
const inventoryOpen = ref(false)
const sourceKey = useStableObjectKey('source')

function sources() {
  model.value.sources ??= { paths: [], volumes: [] }
  return model.value.sources
}

function addPath(): void {
  const ordinal = sources().paths.length + 1
  sources().paths.push({ id: `path-${ordinal}`, path: '.', required: true })
}

function addVolume(): void {
  const ordinal = sources().volumes.length + 1
  sources().volumes.push({
    id: `volume-${ordinal}`,
    compose_volume: '',
    required: true,
  })
}

function setVolumeMode(source: VolumeSource, mode: 'compose' | 'name'): void {
  if (mode === 'compose') {
    delete source.name
    source.compose_volume = ''
  } else {
    delete source.compose_volume
    source.name = ''
  }
}

function setOptionalList(
  source: PathSource | VolumeSource,
  key: 'include' | 'exclude',
  value: string[],
): void {
  if (value.length) {
    if (key === 'include' && 'path' in source) source.include = value
    else source.exclude = value
    return
  }
  if (key === 'include' && 'path' in source) delete source.include
  else delete source.exclude
}

function applyInventory(pathSources: PathSource[]): void {
  sources().paths = pathSources
  inventoryOpen.value = false
}
</script>

<template>
  <div class="editor-stack">
    <el-alert
      title="backup.yaml 會自動保存到 snapshot 的 _meta/backup.yaml，不需要加入 path source。"
      type="success"
      :closable="false"
      show-icon
    />

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">PATH SOURCES</p>
          <h2>檔案與目錄</h2>
        </div>
        <div class="button-row">
          <el-button @click="inventoryOpen = true">從檔案清單分組</el-button>
          <el-button type="primary" @click="addPath">新增 Path</el-button>
        </div>
      </div>
      <el-empty
        v-if="!model.sources?.paths.length"
        description="尚未加入 path source"
      />
      <div
        v-for="(source, index) in model.sources?.paths"
        :key="sourceKey(source)"
        class="source-card"
      >
        <div class="source-card__header">
          <strong>Path {{ index + 1 }}</strong>
          <el-button type="danger" text @click="sources().paths.splice(index, 1)">
            刪除
          </el-button>
        </div>
        <div class="form-grid">
          <el-form-item label="Source ID">
            <el-input v-model="source.id" />
          </el-form-item>
          <el-form-item label="來源路徑">
            <el-input v-model="source.path" placeholder="./data" />
          </el-form-item>
        </div>
        <el-checkbox v-model="source.required">來源不存在時中止備份</el-checkbox>
        <div class="source-lists">
          <el-form-item label="Include 白名單">
            <StringListEditor
              :model-value="source.include ?? []"
              placeholder="world/**"
              @update:model-value="setOptionalList(source, 'include', $event)"
            />
          </el-form-item>
          <el-form-item label="Exclude（優先）">
            <StringListEditor
              :model-value="source.exclude ?? []"
              placeholder="cache/**"
              @update:model-value="setOptionalList(source, 'exclude', $event)"
            />
          </el-form-item>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">DOCKER VOLUMES</p>
          <h2>Named volumes</h2>
        </div>
        <el-button type="primary" @click="addVolume">新增 Volume</el-button>
      </div>
      <p class="section-copy">
        Compose volume 填頂層邏輯 key；外部 volume 才填 Docker 實際名稱。匿名 volume 無法安全宣告。
      </p>
      <el-empty
        v-if="!model.sources?.volumes.length"
        description="尚未加入 Docker volume"
      />
      <div
        v-for="(source, index) in model.sources?.volumes"
        :key="sourceKey(source)"
        class="source-card"
      >
        <div class="source-card__header">
          <strong>Volume {{ index + 1 }}</strong>
          <el-button type="danger" text @click="sources().volumes.splice(index, 1)">
            刪除
          </el-button>
        </div>
        <div class="form-grid">
          <el-form-item label="Source ID">
            <el-input v-model="source.id" />
          </el-form-item>
          <el-form-item label="Volume 類型">
            <el-radio-group
              :model-value="source.name !== undefined ? 'name' : 'compose'"
              @update:model-value="setVolumeMode(source, $event)"
            >
              <el-radio-button value="compose">Compose key</el-radio-button>
              <el-radio-button value="name">外部名稱</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item
            :label="source.name !== undefined ? 'Docker volume 名稱' : 'Compose volume key'"
          >
            <el-input
              v-if="source.name !== undefined"
              v-model="source.name"
              placeholder="company_shared_data"
            />
            <el-input
              v-else
              v-model="source.compose_volume"
              placeholder="db-data"
            />
          </el-form-item>
        </div>
        <el-checkbox v-model="source.required">Volume 不存在時中止備份</el-checkbox>
        <el-form-item label="Exclude">
          <StringListEditor
            :model-value="source.exclude ?? []"
            placeholder="cache/**"
            @update:model-value="setOptionalList(source, 'exclude', $event)"
          />
        </el-form-item>
      </div>
    </section>

    <el-drawer
      v-model="inventoryOpen"
      title="從檔案清單建立來源群組"
      size="min(1100px, 96vw)"
    >
      <InventoryWorkspace @apply="applyInventory" />
    </el-drawer>
  </div>
</template>
