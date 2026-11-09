<script setup lang="ts">
// 回复页右侧“当前对话待处理”：售后状态、售后群工单、谷仓查件单（草稿可直接审核、修改、提交）、
// 发票申请（可直接审核、生成、发送）、内部参考。原来放在聊天记录上方，挪到这里让对话区干净一些。
import { computed } from 'vue'
import { fmtTime } from '../format'
import type { ConvContext } from '../types'
import AftersalesCasePanel from './AftersalesCasePanel.vue'
import InternalRefsCard from './InternalRefsCard.vue'
import InvoicePanel from './InvoicePanel.vue'
import LogisticsTicketsPanel from './LogisticsTicketsPanel.vue'

// bare：嵌在“待处理 / 备注”切换模块里，不要自己的边框和标题
const props = defineProps<{ convId: number; ctx: ConvContext | null; bare?: boolean }>()
const emit = defineEmits<{ insert: [text: string]; changed: [] }>()

const af = computed(() => props.ctx?.af || null)
const jobKey = computed(() => props.ctx?.jobKey || '')
const casesKey = computed(() => (props.ctx?.jobKey ? `${props.ctx.jobKey}-${props.ctx.messages}` : String(props.ctx?.messages ?? '')))
</script>

<template>
  <section class="todo" :class="{ bare }">
    <div v-if="!bare" class="mod-title">当前对话待处理</div>
    <!-- 下面各块没有内容时不显示；全都没有时 .todo-body 为空，显示“没有待处理” -->
    <div class="todo-body">
      <div v-if="af" class="af-box" :class="af.status">
        <div class="af-head">
          <el-tag size="small" :type="af.status === 'DONE' ? 'success' : af.status === 'IN_PROGRESS' ? 'primary' : 'warning'">
            {{ af.status_label }}
          </el-tag>
          <b>{{ af.type }}</b>
        </div>
        <div class="muted">客服 {{ af.marked_by }} 标记于 {{ af.marked_at ? fmtTime(af.marked_at) : '—' }}<template v-if="af.handled_by"> · 售后 {{ af.handled_by }}</template></div>
        <div v-if="af.note" class="af-note">{{ af.note }}</div>
      </div>
      <AftersalesCasePanel :conv-id="convId" :refresh-key="casesKey" @insert="(t: string) => emit('insert', t)" />
      <LogisticsTicketsPanel :conv-id="convId" :refresh-key="jobKey" @insert="(t: string) => emit('insert', t)"
                             @changed="emit('changed')" />
      <InvoicePanel :conv-id="convId" :refresh-key="jobKey" @changed="emit('changed')" />
      <InternalRefsCard :conv-id="convId" :refresh-key="`${jobKey}-${af?.type || ''}`" />
    </div>
  </section>
</template>

<style scoped>
.todo {
  margin-top: 12px;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.todo.bare {
  margin-top: 0;
  border: 0;
  border-radius: 0;
}
.mod-title {
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  background: var(--sunken);
  border-bottom: 1px solid var(--line);
}
.todo-body:empty::after {
  content: '当前对话没有待处理的事项';
  display: block;
  padding: 10px 12px;
  font-size: 12px;
  color: var(--muted);
}
/* 各块原来是聊天上方的横条，在这里改成上下排列、去掉多余的底边 */
.todo-body > :deep(*) {
  border-bottom: 1px solid var(--line-soft);
}
.todo-body > :deep(*:last-child) {
  border-bottom: 0;
}
.af-box {
  padding: 8px 12px;
  background: var(--warn-soft);
  font-size: 13px;
  line-height: 1.6;
}
.af-box.IN_PROGRESS {
  background: var(--accent-soft, #eef4ff);
}
.af-box.DONE {
  background: #eff6ee;
}
.af-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.af-note {
  white-space: pre-wrap;
  color: var(--text);
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
</style>
