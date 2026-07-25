<script setup lang="ts">
import { computed } from 'vue'
import type { GlobalConfig } from '../../domain/types'
import StringListEditor from '../shared/StringListEditor.vue'

const model = defineModel<GlobalConfig>({ required: true })
model.value.rclone ??= {}
model.value.check ??= {}

const rclone = computed(() => model.value.rclone!)
const check = computed(() => model.value.check!)
</script>

<template>
  <div class="editor-stack">
    <el-alert
      title="全域設定控制備份儲存庫、工作目錄與受信任資料邊界。路徑仍需在主機上由 backupctl validate 檢查權限與 filesystem。"
      type="info"
      :closable="false"
      show-icon
    />

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">IDENTITY</p>
          <h2>主機與服務位置</h2>
        </div>
      </div>
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="Host ID">
            <el-input v-model="model.host_id" placeholder="server-a" />
          </el-form-item>
          <el-form-item label="服務根目錄">
            <el-input v-model="model.services_root" placeholder="/srv/stacks" />
          </el-form-item>
        </div>
        <el-form-item label="Trusted data roots">
          <StringListEditor
            v-model="model.trusted_data_roots"
            placeholder="/srv/data"
            :minimum="1"
          />
          <div class="field-help">
            各 root 必須為絕對路徑、不可重疊；path source 必須嚴格位於其中一個 root 之下。
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">STORAGE</p>
          <h2>Restic 與 rclone</h2>
        </div>
      </div>
      <el-form label-position="top">
        <el-form-item label="Repository">
          <el-input
            v-model="model.repository"
            placeholder="rclone:remote:Backups/restic/server-a"
          />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="Restic 密碼檔">
            <el-input v-model="model.password_file" />
          </el-form-item>
          <el-form-item label="rclone 設定檔">
            <el-input v-model="model.rclone_config" />
          </el-form-item>
          <el-form-item label="rclone 頻寬限制">
            <el-input
              v-model="rclone.bwlimit"
              placeholder="例如 10M；空白表示不限制"
            />
          </el-form-item>
          <el-form-item label="Repository check 抽樣">
            <el-input v-model="check.read_data_subset" placeholder="5%" />
          </el-form-item>
        </div>
      </el-form>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">RUNTIME</p>
          <h2>執行時目錄</h2>
        </div>
      </div>
      <el-form label-position="top">
        <div class="form-grid">
          <el-form-item label="Staging root">
            <el-input v-model="model.staging_root" />
          </el-form-item>
          <el-form-item label="Restore root">
            <el-input v-model="model.restore_root" />
          </el-form-item>
          <el-form-item label="State root">
            <el-input v-model="model.state_root" />
          </el-form-item>
          <el-form-item label="Cache root">
            <el-input v-model="model.cache_root" />
          </el-form-item>
          <el-form-item label="Lock file">
            <el-input v-model="model.lock_file" />
          </el-form-item>
          <el-form-item label="Volume helper image">
            <el-input v-model="model.volume_helper_image" />
          </el-form-item>
        </div>
      </el-form>
    </section>
  </div>
</template>
