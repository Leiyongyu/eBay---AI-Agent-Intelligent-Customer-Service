<script setup lang="ts">
// 对话里的发票：什么时候生成、发到哪个邮箱、结果如何；点发票号打开 PDF（已发出的留底只有管理员能看）
import { fmtTime } from '../format'
import type { InvoiceTimelineItem } from '../types'

defineProps<{ item: InvoiceTimelineItem }>()
</script>

<template>
  <div class="invoice-row">
    <div class="invoice-card" :class="item.status">
      <div class="head">
        <span class="icon">🧾</span>
        <b>{{ item.status === 'SENT' || item.status === 'SIMULATED' ? '已发送发票' : item.status === 'VOID' ? '发票已作废' : '已生成发票' }}</b>
        <span class="muted">{{ fmtTime(item.at) }}</span>
        <el-tag size="small" effect="plain">{{ item.kind_label }}</el-tag>
        <a v-if="item.pdf_url" class="mono number" :href="item.pdf_url" target="_blank" rel="noopener" title="打开发票 PDF">{{ item.number }}</a>
        <span v-else class="mono number" title="发票留底只有管理员能查看">{{ item.number }}</span>
      </div>
      <div class="small">订单 <span class="mono">{{ item.order_id }}</span> · 金额 {{ item.total.toFixed(2) }} {{ item.currency }}</div>
      <div class="small">
        <el-tag v-if="item.status === 'SENT'" size="small" type="success">已发到 {{ item.sent_to }}</el-tag>
        <el-tag v-else-if="item.status === 'SIMULATED'" size="small" type="info">模拟发送（邮件未开启，没有真正发出）</el-tag>
        <el-tag v-else-if="item.status === 'SEND_FAILED'" size="small" type="danger">发送失败：{{ item.send_error }}</el-tag>
        <el-tag v-else-if="item.status === 'ISSUED'" size="small" type="warning">还没发给买家</el-tag>
        <span v-if="item.buyer_email && item.sent_to && item.buyer_email !== item.sent_to" class="muted">
          （测试期改发到测试邮箱，买家邮箱 {{ item.buyer_email }}）
        </span>
      </div>
      <div class="small muted">
        {{ item.created_by }} 生成{{ item.sent_by ? ` · ${item.sent_by} 发送` : '' }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.invoice-row {
  display: flex;
  justify-content: center;
  margin: 10px 0 14px;
}
.invoice-card {
  max-width: 78%;
  border: 1px dashed var(--el-color-success-light-5, #b3e19d);
  border-radius: 8px;
  padding: 8px 12px;
  background: var(--panel-soft, #f7f9fb);
  font-size: var(--fs-sm, 13px);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.invoice-card.SEND_FAILED {
  border-color: var(--el-color-danger-light-5, #fab6b6);
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.number {
  font-weight: 600;
}
.small {
  font-size: var(--fs-xs, 12px);
}
</style>
