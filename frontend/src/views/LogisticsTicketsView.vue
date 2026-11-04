<script setup lang="ts">
// 谷仓查件单总表：本系统起草的草稿（客服在这里核对、修改后提交到谷仓）、谷仓有新回复待回买家、需人工、处理中、已处理。
// 每天自动同步（含在谷仓后台手动建的）；点“打开会话”到对应买家会话回复。客服、售后只看自己负责的店铺。
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { errorText, fmtTime } from '../format'
import TicketDraftDialog from '../components/TicketDraftDialog.vue'
import type { LogisticsTicketItem, LogisticsTicketRow, ReshipStock, Store } from '../types'

const emit = defineEmits<{ open: [convId: number] }>()

type ViewKey = 'draft' | 'replied' | 'reply' | 'human' | 'open' | 'done' | 'all'
const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'draft', label: '草稿待提交' },
  { key: 'replied', label: '谷仓已回复' },
  { key: 'reply', label: '有新回复待回买家' },
  { key: 'human', label: '需人工' },
  { key: 'open', label: '谷仓处理中' },
  { key: 'done', label: '已处理/已完结' },
  { key: 'all', label: '全部' },
]
const HINTS: Partial<Record<ViewKey, string>> = {
  draft: '系统按物流规则起草的查件单，只存在本系统、还没发到谷仓。点“修改并提交”核对查件类型和查件需求后提交到谷仓；建错的点废弃。提交前不算已联系承运商。（在谷仓后台手动保存的草稿也会同步到这里，但要在谷仓后台提交。）',
  replied: '谷仓回复过的查件单（含在谷仓后台手动建的）：回复和附件（POD 等）每小时同步一次，附件自动下载；不需要人工的由 AI 按买家语言写好消息、附上附件自动发给买家（目前为模拟发送），需人工的不发。“发给买家”一列是结果。',
  reply: '谷仓已回复，AI 起草了给买家的回复。打开会话，在右侧“谷仓查件单”里填入回复框，审核后发送，再点“已处理”。',
  human: '查件结果需要人工决定（妥投未收到、退回、丢失、可能补发等）。补发前先点“查补发库存”：原发货仓有货才能补发，没货只能协商退款。',
}

const view = ref<ViewKey>('draft')
const storeId = ref('')
const keyword = ref('')
const stores = ref<Store[]>([])
const rows = ref<LogisticsTicketRow[]>([])
const counts = ref<Record<string, number>>({})
const loading = ref(false)
const stock = ref<Record<number, ReshipStock | string>>({})
const stockLoading = ref<number | null>(null)
const editing = ref<LogisticsTicketItem | null>(null)

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ view: view.value })
    if (storeId.value) q.set('store_id', storeId.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    const r = await get<{ counts: Record<string, number>; items: LogisticsTicketRow[] }>(`/internal/logistics-tickets?${q}`)
    rows.value = r.items
    counts.value = r.counts
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

async function loadStock(t: LogisticsTicketRow) {
  stockLoading.value = t.id
  try {
    stock.value[t.id] = await get<ReshipStock>(`/internal/logistics-tickets/${t.id}/stock`)
  } catch (e) {
    stock.value[t.id] = errorText(e)
  } finally {
    stockLoading.value = null
  }
}

async function handled(t: LogisticsTicketRow) {
  try {
    await post(`/internal/logistics-tickets/${t.id}/handled`, {})
    ElMessage.success('已标记处理')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function when(t: LogisticsTicketRow) {
  if (t.replied_at) return `回复 ${t.replied_at}`
  if (t.added_at) return `建于 ${t.added_at}`
  return t.created_at ? `保存于 ${fmtTime(t.created_at)}` : ''
}
const stockOf = (t: LogisticsTicketRow) => stock.value[t.id]
const hint = computed(() => HINTS[view.value])
const SEND_TAGS: Record<string, { label: string; type: 'success' | 'danger' | 'warning' | 'info' }> = {
  SENT: { label: '已发给买家', type: 'success' },
  NEEDS_HUMAN: { label: '需人工，未发送', type: 'danger' },
  FAILED: { label: '发送失败', type: 'danger' },
  NO_BUYER: { label: '找不到买家', type: 'warning' },
}
const isImage = (name: string | null) => /\.(jpe?g|png|gif|webp|bmp)$/i.test(name || '')

let timer: number | undefined
watch([view, storeId], load)
watch(keyword, () => {
  clearTimeout(timer)
  timer = window.setTimeout(load, 300)
})
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  await load()
})
</script>

<template>
  <div class="lt-view panel">
    <div class="bar">
      <el-radio-group v-model="view" size="small">
        <el-radio-button v-for="v in VIEWS" :key="v.key" :value="v.key">
          {{ v.label }}<span v-if="counts[v.key] !== undefined" class="cnt">{{ counts[v.key] }}</span>
        </el-radio-button>
      </el-radio-group>
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-input v-model="keyword" size="small" clearable placeholder="查件单号 / 订单号 / 跟踪号 / 买家" style="width: 240px" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>
    <el-alert v-if="hint" :title="hint" type="info" :closable="false" show-icon class="hint" />
    <el-table :data="rows" v-loading="loading" size="small" row-key="id" class="tbl" empty-text="没有查件单">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="detail">
            <div v-if="row.io_desc"><span class="muted">查件需求：</span>{{ row.io_desc }}</div>
            <div v-if="row.reply" class="pre"><span class="muted">谷仓回复（内部信息，不要原样发给买家）：</span>{{ row.reply }}</div>
            <div v-if="row.reject_reason" class="warn">驳回原因：{{ row.reject_reason }}</div>
            <div v-if="row.attachments.length" class="muted">
              附件：
              <template v-for="(a, i) in row.attachments" :key="i">
                <a v-if="a.file_id" :href="`/internal/aftersales-files/${a.file_id}/content`" target="_blank">{{ a.name }}</a>
                <span v-else>{{ a.name }}（未下载）</span>{{ i < row.attachments.length - 1 ? '、' : '' }}
              </template>
              <div class="thumbs">
                <a v-for="a in row.attachments.filter((x: { file_id: number | null; name: string }) => x.file_id && isImage(x.name))"
                   :key="a.file_id" :href="`/internal/aftersales-files/${a.file_id}/content`" target="_blank">
                  <img :src="`/internal/aftersales-files/${a.file_id}/content`" :alt="a.name" />
                </a>
              </div>
            </div>
            <div v-if="row.send_error" class="warn">没有自动发送：{{ row.send_error }}</div>
            <div v-if="row.suggestion" class="sugg">
              <div class="muted">AI 起草的回复：</div>
              <div class="pre">{{ row.suggestion }}</div>
              <div v-if="row.suggestion_zh" class="muted">中文：{{ row.suggestion_zh }}</div>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="查件单" min-width="150">
        <template #default="{ row }">
          <div class="mono">{{ row.io_code }}</div>
          <div class="muted">{{ when(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="类型 / 状态" min-width="150">
        <template #default="{ row }">
          <b>{{ row.type }}</b>
          <el-tag size="small" :type="row.is_draft ? 'warning' : 'info'" effect="plain" style="margin-left: 4px">{{ row.status }}</el-tag>
          <div>
            <el-tag v-if="row.needs_human && !row.handled_at" size="small" type="danger" effect="plain">需人工</el-tag>
            <el-tag v-if="row.handled_at" size="small" type="success" effect="plain">{{ row.handled_by }} 已处理</el-tag>
            <span v-if="row.submitted_by" class="muted">{{ row.submitted_by }} 提交</span>
            <span v-else-if="row.created_by" class="muted">{{ row.created_by }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="店铺 / 买家" min-width="150">
        <template #default="{ row }">
          <div>{{ row.store_name || '未匹配店铺' }}</div>
          <div class="muted">{{ row.buyer_key || '' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="订单 / 跟踪号" min-width="170">
        <template #default="{ row }">
          <div class="mono">{{ row.order_id }}</div>
          <div class="mono muted">{{ row.tracking_no }}</div>
          <el-tag v-if="row.open_same_order > 1" size="small" type="warning" effect="plain"
                  title="同一订单有多张没完结的查件单，可能重复建单">同订单 {{ row.open_same_order }} 张未完结</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="发给买家" min-width="130">
        <template #default="{ row }">
          <el-tag v-if="row.send_status && SEND_TAGS[row.send_status]" size="small" effect="plain"
                  :type="SEND_TAGS[row.send_status].type">{{ SEND_TAGS[row.send_status].label }}</el-tag>
          <span v-else-if="row.reply || row.replied_at" class="muted">待同步处理</span>
          <span v-else class="muted">—</span>
          <div v-if="row.sent_at" class="muted">{{ fmtTime(row.sent_at) }}</div>
          <div v-if="row.attachments.length" class="muted">附件 {{ row.attachments.filter((x: { file_id: number | null }) => x.file_id).length }}/{{ row.attachments.length }} 已下载</div>
        </template>
      </el-table-column>
      <el-table-column label="谷仓回复 / 查件需求" min-width="260">
        <template #default="{ row }">
          <div class="clip">{{ row.reply || row.io_desc || '—' }}</div>
          <div v-if="stockOf(row)" :class="typeof stockOf(row) === 'string' || !(stockOf(row) as ReshipStock).all_in_stock ? 'warn' : 'ok'">
            {{ typeof stockOf(row) === 'string' ? stockOf(row) : (stockOf(row) as ReshipStock).summary }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.is_local_draft" link size="small" type="warning" @click="editing = row">修改并提交</el-button>
          <el-button v-if="row.conversation_id" link size="small" type="primary" @click="emit('open', row.conversation_id)">打开会话</el-button>
          <el-button v-if="row.order_id" link size="small" :loading="stockLoading === row.id" @click="loadStock(row)">查补发库存</el-button>
          <el-button v-if="!row.handled_at && !row.is_draft" link size="small" @click="handled(row)">已处理</el-button>
        </template>
      </el-table-column>
    </el-table>
    <TicketDraftDialog :ticket="editing" @close="editing = null" @changed="load" />
  </div>
</template>

<style scoped>
.lt-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 10px 12px;
  gap: 8px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.thumbs {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
.thumbs img {
  max-height: 120px;
  max-width: 200px;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.cnt {
  margin-left: 4px;
  opacity: 0.75;
}
.tbl {
  flex: 1;
}
.detail {
  padding: 4px 48px;
  line-height: 1.7;
  font-size: var(--fs-sm);
}
.pre {
  white-space: pre-wrap;
}
.clip {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sugg {
  border: 1px dashed var(--warn);
  border-radius: 6px;
  padding: 4px 8px;
  margin-top: 4px;
}
.warn {
  color: var(--warn-text);
}
.ok {
  color: var(--ok-text);
}
</style>
