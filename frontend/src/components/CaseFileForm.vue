<script setup lang="ts">
// 上传文件 / 证据到 eBay 售后单（图片或 PDF，最大 8 MB）
import { ref } from 'vue'
import { readFile } from '../caseDialog'
import type { Choice } from '../caseDialog'

const props = defineProps<{ purposes: Choice[]; blocked: string | null; busy: boolean }>()
const emit = defineEmits<{ submit: [payload: { purpose: string; file_data: string; file_name: string }] }>()
const purpose = ref(props.purposes[0]?.code || 'ITEM_RELATED')
const file = ref<{ data: string; name: string } | null>(null)
const input = ref<HTMLInputElement | null>(null)

async function pick(e: Event) {
  const el = e.target as HTMLInputElement
  file.value = await readFile(el.files?.[0])
  el.value = ''
}
function submit() {
  if (file.value) emit('submit', { purpose: purpose.value, file_data: file.value.data, file_name: file.value.name })
}
defineExpose({ clear: () => (file.value = null) })
</script>

<template>
  <el-form label-width="90px" class="plan-form">
    <el-form-item label="文件用途" required>
      <el-select v-model="purpose" style="width: 260px">
        <el-option v-for="p in purposes" :key="p.code" :label="p.label" :value="p.code" />
      </el-select>
    </el-form-item>
    <el-form-item label="文件" required>
      <input ref="input" type="file" accept="image/*,.pdf" hidden @change="pick" />
      <el-button @click="input?.click()">选择文件</el-button>
      <span class="name">{{ file?.name || '图片或 PDF，最大 8 MB' }}</span>
    </el-form-item>
    <el-form-item>
      <el-tooltip :disabled="!blocked" :content="blocked || ''">
        <span><el-button type="success" :loading="busy" :disabled="!!blocked || !file" @click="submit">提交</el-button></span>
      </el-tooltip>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.plan-form { max-width: 640px; padding-top: 6px; }
.name { margin-left: 10px; color: var(--el-text-color-secondary); }
</style>
