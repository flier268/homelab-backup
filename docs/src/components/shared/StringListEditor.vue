<script setup lang="ts">
const model = defineModel<string[]>({ required: true })

withDefaults(defineProps<{
  placeholder?: string
  minimum?: number
}>(), {
  placeholder: '',
  minimum: 0,
})

function add(): void {
  model.value = [...model.value, '']
}

function remove(index: number, minimum: number): void {
  if (model.value.length <= minimum) return
  model.value = model.value.filter((_item, itemIndex) => itemIndex !== index)
}

function update(index: number, value: string): void {
  model.value = model.value.map((item, itemIndex) => (
    itemIndex === index ? value : item
  ))
}
</script>

<template>
  <div class="string-list">
    <div v-for="(_item, index) in model" :key="index" class="inline-row">
      <el-input
        :model-value="model[index]"
        :placeholder="placeholder"
        @update:model-value="update(index, $event)"
      />
      <el-button
        aria-label="移除此項"
        :disabled="model.length <= minimum"
        @click="remove(index, minimum)"
      >
        移除
      </el-button>
    </div>
    <el-button type="primary" plain @click="add">新增一項</el-button>
  </div>
</template>
