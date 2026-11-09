<script setup lang="ts">
// 售后工单（飞书售后群）：待发送、已发待回复、群已回复待审核、已回复买家、已完结。点“打开会话”去会话里发群、审核回复。
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { errorText, fmtTime } from '../format'
import type { Store } from '../types'

interface Row {
  id: number; conversation_id: number; store_name: string | null; buyer_key: string | null; title: string
  category: string; status: string; status_label: string; problem_zh: string; replies: { sender: string; text: string }[]
  missing: string[]; sent_at: string | null; created_at: string | null; draft_reply: string | null
}
const emit = defineEmits<{ open: [convId: number] }>()
const VIEWS = [
  { key: 'DRAFT', label: '待发送' }, { key: 'SENT', label: '已发待回复' }, { key: 'REPLIED', label: '群已回复待审核' },
  { key: 'ANSWERED', label: '已回复买家' }, { key: 'CLOSED', label: '已完结' }, { key: '', label: '全部' },
]
const status = ref('REPLIED')
const storeId = ref('')
const keyword = ref('')
const stores = ref<Store[]>([])
const rows = ref<Row[]>([])
const counts = ref<Record<string, number>>({})
const ready = ref(true)
const lastPoll = ref<string | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    if (status.value) q.set('status', status.value)
    if (storeId.value) q.set('store_id', storeId.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    const r = await get<{ counts: Record<string, number>; items: Row[]; feishu_ready: boolean; last_poll_at: string | null }>(
      `/internal/aftersales-cases?${q}`)
    rows.value = r.items
    counts.value = r.counts
    ready.value = r.feishu_ready
    lastPoll.value = r.last_poll_at
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

async function pollNow() {
  try {
    await post('/internal/aftersales-cases/poll', {})
    ElMessage.info('正在读取群里的回复，几秒后刷新')
    setTimeout(load, 6000)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const total = () => Object.values(counts.value).reduce((a, b) => a + b, 0)
let t: number | undefined
watch([status, storeId], load)
watch(keyword, () => {
  clearTimeout(t)
  t = window.setTimeout(load, 300)
})
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  await load()
})
</script>

<template>
  <div class="cs-view panel">
    <div class="bar">
      <el-radio-group v-model="status" size="small">
        <el-radio-button v-for="v in VIEWS" :key="v.key" :value="v.key">
          {{ v.label }}<span class="cnt">{{ v.key ? counts[v.key] || 0 : total() }}</span>
        </el-radio-button>
      </el-radio-group>
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-input v-model="keyword" size="small" clearable placeholder="订单号 / 买家 / SKU" style="width: 200px" />
      <el-button size="small" :disabled="!ready" @click="pollNow">立即读取群回复</el-button>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <span v-if="lastPoll" class="muted">最近读取 {{ fmtTime(lastPoll) }}</span>
    </div>
    <el-alert type="info" :closable="false" show-icon
      :title="ready ? '质量问题、破损错发缺件、安装技术问题、买家发 VIN 核对适配，AI 会起草工单；在会话里核对后发到飞书售后群。群里回在工单话题里或 @ 机器人（带订单号）都算回复；按回复起草的买家回复一律人工审核后发送。'
                    : '飞书售后群还没配置好：.env 里填 FEISHU_CASE_ENABLED=true、FEISHU_CASE_CHAT_ID，并重启。'" />
    <el-table :data="rows" v-loading="loading" size="small" class="tbl" empty-text="没有工单">
      <el-table-column label="工单" min-width="280">
        <template #default="{ row }">
          <b>{{ row.title }}</b>
          <div class="muted">{{ row.store_name }} · {{ row.buyer_key }} · {{ row.category }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ row.status_label }}</el-tag>
          <div v-if="row.missing.length && row.status === 'DRAFT'" class="warn">缺：{{ row.missing.join('、') }}</div>
        </template>
      </el-table-column>
      <el-table-column label="问题 / 最新回复" min-width="320">
        <template #default="{ row }">
          <div class="clip">{{ row.problem_zh }}</div>
          <div v-if="row.replies.length" class="muted clip">
            {{ row.replies[row.replies.length - 1].sender }}：{{ row.replies[row.replies.length - 1].text }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="130">
        <template #default="{ row }">{{ fmtTime(row.sent_at || row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link size="small" type="primary" @click="emit('open', row.conversation_id)">打开会话</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.cs-view {
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
.cnt {
  margin-left: 4px;
  opacity: 0.75;
}
.tbl {
  flex: 1;
}
.clip {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.warn {
  color: #d97706;
  font-size: 12px;
}
</style>
