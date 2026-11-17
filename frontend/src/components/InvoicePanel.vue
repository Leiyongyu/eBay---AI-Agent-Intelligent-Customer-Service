<script setup lang="ts">
// 会话里的发票申请：显示状态，点“审核发票”直接在这里核对邮箱、订单、抬头 → 生成 PDF → 预览 → 发邮件
// （和发票页同一个审核面板），不用跳到发票页
import { ref, watch } from 'vue'
import { get } from '../api'
import type { ConversationInvoice } from '../types'
import InvoiceRequestDrawer from './InvoiceRequestDrawer.vue'

const props = defineProps<{ convId: number; refreshKey?: string }>()
const emit = defineEmits<{ changed: [] }>()
const info = ref<ConversationInvoice | null>(null)
const reviewing = ref<number | null>(null)

async function load() {
  try {
    info.value = await get<ConversationInvoice>(`/internal/conversations/${props.convId}/invoice`)
  } catch {
    info.value = null
  }
}
watch(() => [props.convId, props.refreshKey], load, { immediate: true })

function onChanged() {
  load()
  emit('changed') // 生成、发送后聊天记录里的发票卡片要刷新
}
</script>

<template>
  <div v-if="info?.request" class="inv-box" :class="info.request.status">
    <el-tag size="small" :type="info.request.status === 'OPEN' ? 'warning' : info.request.status === 'DONE' ? 'success' : 'info'">
      发票：{{ info.request.status_label }}
    </el-tag>
    <span v-if="info.request.note" class="muted">{{ info.request.note }}</span>
    <span class="spacer" />
    <el-button size="small" :type="info.request.status === 'DONE' ? 'default' : 'warning'" plain @click="reviewing = info.request.id">
      {{ info.request.status === 'DONE' ? '查看' : '审核发票' }}
    </el-button>
    <a class="page-link" :href="`#/invoices?req=${info.request.id}`" title="在发票页打开">发票页 ›</a>
    <InvoiceRequestDrawer :req-id="reviewing" @close="reviewing = null" @changed="onChanged" />
  </div>
</template>

<style scoped>
.inv-box {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #eff6ee;
  font-size: var(--fs-sm);
}
.inv-box.OPEN,
.inv-box.WAITING_EMAIL {
  background: var(--warn-soft);
}
.spacer {
  flex: 1;
}
.page-link {
  font-size: 12px;
  color: var(--muted);
}
</style>
