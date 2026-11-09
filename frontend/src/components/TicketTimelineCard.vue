<script setup lang="ts">
// 对话里的查件记录：什么时候向谷仓发起了查件、什么时候收到反馈（以及是否已自动回复买家），按时间插在聊天记录中间。
// 本系统起草、还没提交的查件单可以直接在这里审核：改查件类型和需求、保存、提交到谷仓或废弃（不用去查件单页）
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { errorText, fmtTime } from '../format'
import type { LogisticsTicketItem, TicketTimelineItem } from '../types'
import TicketDraftDialog from './TicketDraftDialog.vue'

const props = defineProps<{ item: TicketTimelineItem; convId: number }>()
const emit = defineEmits<{ changed: [] }>()
const editing = ref<LogisticsTicketItem | null>(null)
const opening = ref(false)

async function review() {
  opening.value = true
  try {
    const list = await get<LogisticsTicketItem[]>(`/internal/conversations/${props.convId}/logistics-tickets`)
    const t = list.find((x) => x.id === props.item.ticket_id)
    if (!t) ElMessage.warning('没找到这张查件单，可能已被废弃')
    else if (!t.is_local_draft) ElMessage.info('这张查件单已经提交过了')
    else editing.value = t
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    opening.value = false
  }
}
</script>

<template>
  <div class="ticket-row">
    <div class="ticket-card" :class="item.kind">
      <div class="head">
        <span class="icon">{{ item.kind === 'opened' ? '🔎' : '📮' }}</span>
        <b>{{ item.kind === 'opened' ? (item.draft ? '已起草查件单（待提交）' : '已发起查件') : '收到查件反馈' }}</b>
        <span class="muted">{{ fmtTime(item.at) }}</span>
        <el-tag size="small" effect="plain">{{ item.type || '查件' }}</el-tag>
        <span class="muted mono small">{{ item.io_code }}</span>
      </div>
      <template v-if="item.kind === 'opened'">
        <div v-if="item.tracking_no" class="small muted">跟踪号 <span class="mono">{{ item.tracking_no }}</span></div>
        <div v-if="item.desc" class="small">{{ item.desc }}</div>
        <div class="small muted">{{ item.by }}{{ item.status ? ` · ${item.status}` : '' }}</div>
        <div v-if="item.draft" class="actions">
          <el-button size="small" type="warning" :loading="opening" @click="review">审核 / 修改并提交</el-button>
          <span class="small muted">提交后才会在谷仓建单、联系承运商</span>
        </div>
      </template>
      <template v-else>
        <div class="reply">{{ item.reply || '（服务商只回了附件）' }}</div>
        <div v-if="item.attachments?.length" class="small">
          附件：
          <template v-for="(a, i) in item.attachments" :key="i">
            <a v-if="a.file_id" :href="`/internal/aftersales-files/${a.file_id}/content`" target="_blank" rel="noopener">{{ a.name }}</a>
            <span v-else>{{ a.name }}</span>
            <span v-if="i < item.attachments.length - 1">、</span>
          </template>
        </div>
        <div class="small">
          <el-tag v-if="item.send_status === 'SENT'" size="small" type="success">已自动回复买家 {{ item.sent_at ? fmtTime(item.sent_at) : '' }}</el-tag>
          <el-tag v-else-if="item.send_status" size="small" type="warning">{{ item.send_error || '没有自动回复' }}</el-tag>
          <el-tag v-if="item.needs_human" size="small" type="danger" effect="plain">需售后跟进</el-tag>
        </div>
      </template>
    </div>
    <TicketDraftDialog :ticket="editing" @close="editing = null" @changed="emit('changed')" />
  </div>
</template>

<style scoped>
.ticket-row {
  display: flex;
  justify-content: center;
  margin: 10px 0 14px;
}
.ticket-card {
  max-width: 78%;
  border: 1px dashed var(--line);
  border-radius: 8px;
  padding: 8px 12px;
  background: var(--panel-soft, #f7f9fb);
  font-size: var(--fs-sm, 13px);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ticket-card.replied {
  border-color: var(--el-color-warning-light-5, #f3d19e);
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.reply {
  white-space: pre-wrap;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.small {
  font-size: var(--fs-xs, 12px);
}
</style>
