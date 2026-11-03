<script setup lang="ts">
// 会话里只显示发票申请的状态，处理统一在“发票”页（审核后发邮件并提醒买家）
import { ref, watch } from 'vue'
import { get } from '../api'
import type { ConversationInvoice } from '../types'

const props = defineProps<{ convId: number; refreshKey?: string }>()
const info = ref<ConversationInvoice | null>(null)

async function load() {
  try {
    info.value = await get<ConversationInvoice>(`/internal/conversations/${props.convId}/invoice`)
  } catch {
    info.value = null
  }
}
watch(() => [props.convId, props.refreshKey], load, { immediate: true })
</script>

<template>
  <div v-if="info?.request" class="inv-box" :class="info.request.status">
    <el-tag size="small" :type="info.request.status === 'OPEN' ? 'warning' : info.request.status === 'DONE' ? 'success' : 'info'" effect="dark">
      发票：{{ info.request.status_label }}
    </el-tag>
    <span v-if="info.request.note" class="muted">{{ info.request.note }}</span>
    <span class="spacer" />
    <a :href="`#/invoices?req=${info.request.id}`">去发票页处理 →</a>
  </div>
</template>

<style scoped>
.inv-box {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: #f3fbf5;
  font-size: 13px;
}
.inv-box.OPEN,
.inv-box.WAITING_EMAIL {
  background: #fff8ec;
}
.spacer {
  flex: 1;
}
</style>
