<script setup lang="ts">
// 会话里的谷仓查件单：每天同步（含客服在谷仓后台手动建的）；谷仓回复后 AI 起草了给买家的回复建议，
// 客服可一键填进回复框，处理完点“已处理”。谷仓回复是内部信息，不直接发给买家。
// 系统按物流规则起草的查件单只存在本系统，客服核对、可修改后点“修改并提交”发到谷仓；补发前可查原发货仓库存。
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { errorText, fmtTime } from '../format'
import TicketDraftDialog from './TicketDraftDialog.vue'
import type { LogisticsTicketItem, ReshipStock } from '../types'

const props = defineProps<{ convId: number; refreshKey?: string }>()
const emit = defineEmits<{ insert: [text: string]; changed: [] }>()

const items = ref<LogisticsTicketItem[]>([])
const open = ref(true)
const pending = computed(() => items.value.filter((t) => t.suggestion && !t.handled_at).length)

async function load() {
  try {
    items.value = await get<LogisticsTicketItem[]>(`/internal/conversations/${props.convId}/logistics-tickets`)
  } catch {
    items.value = []
  }
}

const stock = ref<Record<number, ReshipStock | string>>({})
const stockLoading = ref<number | null>(null)
const editing = ref<LogisticsTicketItem | null>(null)

async function loadStock(t: LogisticsTicketItem) {
  stockLoading.value = t.id
  try {
    stock.value[t.id] = await get<ReshipStock>(`/internal/logistics-tickets/${t.id}/stock`)
  } catch (e) {
    stock.value[t.id] = errorText(e)
  } finally {
    stockLoading.value = null
  }
}

async function handled(t: LogisticsTicketItem) {
  try {
    Object.assign(t, await post<LogisticsTicketItem>(`/internal/logistics-tickets/${t.id}/handled`, {}))
    ElMessage.success('已标记处理')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

watch(() => [props.convId, props.refreshKey], load, { immediate: true })
</script>

<template>
  <div v-if="items.length" class="lt-box">
    <div class="lt-head">
      <el-tag size="small" type="warning">谷仓查件单</el-tag>
      <span>{{ items.length }} 张</span>
      <span v-if="pending" class="warn">· {{ pending }} 张有新结果待回复买家</span>
      <span class="spacer" />
      <el-button link size="small" @click="open = !open">{{ open ? '收起' : '展开' }}</el-button>
    </div>
    <div v-if="open" class="lt-body">
      <div v-for="t in items" :key="t.id" class="lt-item" :class="{ done: t.handled_at }">
        <div class="row">
          <b>{{ t.type }}</b>
          <el-tag size="small" effect="plain">{{ t.status }}</el-tag>
          <span class="mono muted">{{ t.io_code }} · {{ t.tracking_no }}</span>
          <span class="muted">建于 {{ t.added_at }}<template v-if="t.replied_at"> · 回复于 {{ t.replied_at }}</template>（北京时间）</span>
          <el-tag v-if="t.needs_human" size="small" type="danger" effect="plain">需人工处理</el-tag>
          <el-tag v-if="t.handled_at" size="small" type="success" effect="plain">{{ t.handled_by }} 已处理</el-tag>
        </div>
        <div v-if="t.is_local_draft" class="reply warn">
          {{ t.created_by || '系统' }}起草了查件单（还没发到谷仓），提交后才算已联系承运商。
          <el-button size="small" type="warning" plain @click="editing = t">修改并提交</el-button>
        </div>
        <div v-else-if="t.is_draft" class="reply warn">这是在谷仓后台保存的草稿，请在谷仓后台提交。</div>
        <div v-if="t.io_desc" class="reply muted">查件需求：{{ t.io_desc }}</div>
        <div v-if="t.reply" class="reply"><span class="muted">谷仓回复（内部信息，不要原样发给买家）：</span>{{ t.reply }}</div>
        <div v-if="t.reject_reason" class="reply warn">驳回原因：{{ t.reject_reason }}</div>
        <div v-if="t.attachments.length" class="muted">
          附件：
          <template v-for="(a, i) in t.attachments" :key="i">
            <a v-if="a.file_id" :href="`/internal/aftersales-files/${a.file_id}/content`" target="_blank">{{ a.name }}</a>
            <span v-else>{{ a.name }}（未下载）</span>{{ i < t.attachments.length - 1 ? '、' : '' }}
          </template>
        </div>
        <div v-if="t.order_id" class="row">
          <el-button link size="small" :loading="stockLoading === t.id" @click="loadStock(t)">查补发库存</el-button>
          <template v-if="stock[t.id]">
            <span v-if="typeof stock[t.id] === 'string'" class="warn">{{ stock[t.id] }}</span>
            <span v-else :class="(stock[t.id] as ReshipStock).all_in_stock ? 'ok' : 'warn'">{{ (stock[t.id] as ReshipStock).summary }}</span>
          </template>
        </div>
        <div v-if="t.suggestion && !t.handled_at" class="sugg">
          <div class="muted">AI 起草的回复（{{ t.suggested_at ? fmtTime(t.suggested_at) : '' }}）：</div>
          <div class="sugg-text">{{ t.suggestion }}</div>
          <div v-if="t.suggestion_zh" class="muted">中文：{{ t.suggestion_zh }}</div>
          <div class="row actions">
            <el-button size="small" type="primary" plain @click="emit('insert', t.suggestion)">填入回复框</el-button>
            <el-button size="small" @click="handled(t)">已处理</el-button>
          </div>
        </div>
        <div v-else-if="!t.handled_at && t.reply" class="row actions">
          <el-button size="small" @click="handled(t)">已处理</el-button>
        </div>
      </div>
    </div>
    <TicketDraftDialog :ticket="editing" @close="editing = null" @changed="() => { load(); emit('changed') }" />
  </div>
</template>

<style scoped>
.lt-box {
  border-bottom: 1px solid var(--line);
  background: var(--warn-soft);
  padding: 6px 12px;
}
.lt-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
}
.spacer {
  flex: 1;
}
.warn {
  color: var(--warn-text);
}
.ok {
  color: var(--ok-text);
}
.lt-body {
  margin-top: 6px;
  max-height: 320px;
  overflow: auto;
}
.lt-item {
  border-left: 3px solid var(--warn);
  padding: 4px 8px;
  margin-bottom: 8px;
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.lt-item.done {
  border-left-color: #c9cfcd;
  opacity: 0.8;
}
.reply {
  white-space: pre-wrap;
}
.sugg {
  background: var(--panel);
  border: 1px dashed var(--warn);
  border-radius: 6px;
  padding: 6px 8px;
  margin-top: 4px;
}
.sugg-text {
  white-space: pre-wrap;
}
.actions {
  margin-top: 4px;
}
</style>
