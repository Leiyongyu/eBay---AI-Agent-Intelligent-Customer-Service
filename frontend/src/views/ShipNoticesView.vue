<script setup lang="ts">
// 发货通知：每天汇总所有店铺当天（北京时间）在 eBay 标记已发货的订单，按站点语言写好订单号、承运商、跟踪号、承运商官网（买家自己输入单号查）。
// 客服审核后发送（可修改、跳过、勾选批量发送）；买家已有会话就发在会话里，没有就按订单新建。目前为模拟发送，不发到 eBay。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post } from '../api'
import { errorText, fmtTime } from '../format'
import type { Store } from '../types'

interface Pkg { carrier: string; tracking_no: string; url: string; ebay_carrier?: string }
interface Notice {
  id: number; store_id: string; store_name: string | null; order_id: string; buyer_key: string | null
  marketplace_id: string | null; ship_date: string; shipped_at: string | null; packages: Pkg[]; language: string
  text: string; text_zh: string | null; status: 'PENDING' | 'SENT' | 'SKIPPED'; skip_reason: string | null
  conversation_id: number | null; sent_by: string | null; sent_at: string | null
}
interface ListResp {
  date: string; counts: Record<string, number>; items: Notice[]; collecting: boolean
  last: { date: string; stores: number; orders: number; new: number; skipped: number; waiting?: number; errors: string[] } | null
  last_at: string | null; auto: boolean
}

const emit = defineEmits<{ open: [convId: number] }>()
const pad = (n: number) => String(n).padStart(2, '0')
const today = () => {
  const d = new Date(Date.now() + 8 * 3600 * 1000) // 北京时间
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
}
const day = ref(today())
const status = ref<'PENDING' | 'SENT' | 'SKIPPED' | ''>('PENDING')
const storeId = ref('')
const keyword = ref('')
const stores = ref<Store[]>([])
const data = ref<ListResp | null>(null)
const loading = ref(false)
const selected = ref<Notice[]>([])
const editing = ref<Notice | null>(null)
const editText = ref('')
const busy = ref(false)

const total = computed(() => Object.values(data.value?.counts || {}).reduce((a, b) => a + b, 0))
const LANG: Record<string, string> = { de: '德语', en: '英语', fr: '法语', it: '意大利语', es: '西班牙语' }
const STATUS: Record<string, { label: string; type: 'warning' | 'success' | 'info' }> = {
  PENDING: { label: '待审核', type: 'warning' }, SENT: { label: '已发送', type: 'success' }, SKIPPED: { label: '不发', type: 'info' },
}

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ day: day.value })
    if (status.value) q.set('status', status.value)
    if (storeId.value) q.set('store_id', storeId.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    data.value = await get<ListResp>(`/internal/ship-notices?${q}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

async function collect() {
  try {
    const r = await post<{ started: boolean; message?: string }>('/internal/ship-notices/collect', { day: day.value })
    ElMessage.info(r.started ? '开始汇总所有店铺当天已发货的订单，一两分钟后会自动刷新' : r.message || '正在汇总')
    if (data.value) data.value.collecting = true
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function sendOne(n: Notice) {
  busy.value = true
  try {
    await post(`/internal/ship-notices/${n.id}/send`, {})
    ElMessage.success(`已发送（模拟）：${n.order_id}`)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function sendSelected() {
  const ids = selected.value.filter((n) => n.status === 'PENDING').map((n) => n.id)
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(`把选中的 ${ids.length} 条发货通知发给对应买家？（模拟发送，只记在本系统）`, '批量发送', { type: 'warning' })
  } catch {
    return
  }
  busy.value = true
  try {
    const r = await post<{ sent: number; failed: { order_id: string; error: string }[] }>('/internal/ship-notices/send-batch', { ids })
    if (r.failed.length) ElMessage.warning(`已发送 ${r.sent} 条；${r.failed.length} 条失败：${r.failed.map((f) => `${f.order_id} ${f.error}`).join('；')}`)
    else ElMessage.success(`已发送 ${r.sent} 条（模拟）`)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function sendAllPending() {
  const ids = (data.value?.items || []).filter((n) => n.status === 'PENDING').map((n) => n.id)
  if (!ids.length) return
  selected.value = (data.value?.items || []).filter((n) => n.status === 'PENDING')
  await sendSelected()
}

async function skip(n: Notice) {
  let reason = '审核员跳过'
  try {
    const r = await ElMessageBox.prompt('不发这条发货通知的原因（可不填）', '不发', { inputValue: reason })
    reason = (r as { value: string }).value || reason
  } catch {
    return
  }
  try {
    await post(`/internal/ship-notices/${n.id}/skip`, { reason })
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function openEdit(n: Notice) {
  editing.value = n
  editText.value = n.text
}
async function saveEdit(andSend: boolean) {
  const n = editing.value
  if (!n) return
  try {
    await api('PATCH', `/internal/ship-notices/${n.id}`, { text: editText.value })
    editing.value = null
    if (andSend) await sendOne(n)
    else await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

let timer: number | undefined
let searchTimer: number | undefined
watch([day, status, storeId], load)
watch(keyword, () => {
  clearTimeout(searchTimer)
  searchTimer = window.setTimeout(load, 300)
})
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  await load()
  timer = window.setInterval(() => data.value?.collecting && load(), 8000) // 汇总进行中时自动刷新
})
onUnmounted(() => {
  clearInterval(timer)
  clearTimeout(searchTimer)
})
</script>

<template>
  <div class="sn-view panel">
    <div class="bar">
      <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" size="small" :clearable="false" style="width: 140px" />
      <el-radio-group v-model="status" size="small">
        <el-radio-button value="PENDING">待审核<span class="cnt">{{ data?.counts.PENDING || 0 }}</span></el-radio-button>
        <el-radio-button value="SENT">已发送<span class="cnt">{{ data?.counts.SENT || 0 }}</span></el-radio-button>
        <el-radio-button value="SKIPPED">不发<span class="cnt">{{ data?.counts.SKIPPED || 0 }}</span></el-radio-button>
        <el-radio-button value="">全部<span class="cnt">{{ total }}</span></el-radio-button>
      </el-radio-group>
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-input v-model="keyword" size="small" clearable placeholder="订单号 / 买家" style="width: 180px" />
      <el-button size="small" :loading="data?.collecting" @click="collect">{{ data?.collecting ? '汇总中…' : '立即汇总' }}</el-button>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <span class="spacer" />
      <el-button size="small" :disabled="!data?.items.some((n) => n.status === 'PENDING')" :loading="busy" @click="sendAllPending">
        全部发送（{{ data?.items.filter((n) => n.status === 'PENDING').length || 0 }}）
      </el-button>
      <el-button size="small" type="primary" :disabled="!selected.some((n) => n.status === 'PENDING')" :loading="busy" @click="sendSelected">
        发送选中（{{ selected.filter((n) => n.status === 'PENDING').length }}）
      </el-button>
    </div>
    <el-alert type="info" :closable="false" show-icon class="hint"
      :title="`当天（北京时间）在 eBay 标记已发货的订单，${data?.auto ? '每小时自动汇总一次' : '自动汇总未开启（.env SHIP_NOTICE_ENABLED）'}，也可点“立即汇总”。按站点语言写好订单号、承运商、跟踪号和承运商官网（买家自己在官网输入单号查），不写送达日期；审核后发送（目前为模拟发送，只记在本系统会话里，买家收不到）。有取消申请或退款的订单自动标“不发”。`" />
    <div v-if="data?.last" class="muted last">
      最近汇总 {{ data.last_at ? fmtTime(data.last_at) : '' }}（{{ data.last.date }}）：{{ data.last.stores }} 个店铺，当天发货 {{ data.last.orders }} 单，新增 {{ data.last.new }} 条<template v-if="data.last.waiting">，{{ data.last.waiting }} 单仓库还没实际发出（下次再看）</template>
      <span v-if="data.last.errors.length" class="warn">；{{ data.last.errors.length }} 个店铺失败：{{ data.last.errors.join('；') }}</span>
    </div>
    <el-table :data="data?.items || []" v-loading="loading" size="small" row-key="id" class="tbl" empty-text="当天还没有已发货的订单"
              @selection-change="(rows: Notice[]) => (selected = rows)">
      <el-table-column type="selection" width="36" :selectable="(r: Notice) => r.status === 'PENDING'" />
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="detail">
            <div class="pre">{{ row.text }}</div>
            <div v-if="row.text_zh" class="muted pre">中文：{{ row.text_zh }}</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="店铺 / 买家" min-width="150">
        <template #default="{ row }">
          <div>{{ row.store_name || row.store_id }}</div>
          <div class="muted">{{ row.buyer_key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="订单号" min-width="140">
        <template #default="{ row }">
          <div class="mono">{{ row.order_id }}</div>
          <div class="muted">{{ row.marketplace_id }} · {{ LANG[row.language] || row.language }}</div>
        </template>
      </el-table-column>
      <el-table-column label="承运商 / 跟踪号 / 官网" min-width="280">
        <template #default="{ row }">
          <div v-for="p in row.packages" :key="p.tracking_no">
            <b>{{ p.carrier }}</b>
            <span v-if="p.ebay_carrier && p.ebay_carrier !== p.carrier" class="muted">（eBay：{{ p.ebay_carrier }}）</span>
            <span class="mono"> {{ p.tracking_no }}</span>
            <a :href="p.url" target="_blank" rel="noopener noreferrer"> 官网 ↗</a>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="发货时间" width="120">
        <template #default="{ row }">{{ row.shipped_at ? fmtTime(row.shipped_at) : '' }}</template>
      </el-table-column>
      <el-table-column label="状态" min-width="140">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="STATUS[row.status].type">{{ STATUS[row.status].label }}</el-tag>
          <div v-if="row.skip_reason" class="muted">{{ row.skip_reason }}</div>
          <div v-if="row.sent_by && row.status === 'SENT'" class="muted">{{ row.sent_by }} · {{ row.sent_at ? fmtTime(row.sent_at) : '' }}</div>
          <div v-if="row.status === 'PENDING'" class="muted">{{ row.conversation_id ? '发到已有会话' : '将按订单新建会话' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'PENDING'">
            <el-button link size="small" type="primary" :disabled="busy" @click="sendOne(row)">发送</el-button>
            <el-button link size="small" @click="openEdit(row)">修改</el-button>
            <el-button link size="small" @click="skip(row)">不发</el-button>
          </template>
          <el-button v-if="row.conversation_id" link size="small" @click="emit('open', row.conversation_id)">打开会话</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog :model-value="!!editing" title="修改发货通知" width="620px" @close="editing = null">
      <div v-if="editing" class="muted">订单 {{ editing.order_id }} · {{ editing.buyer_key }} · {{ LANG[editing.language] }}</div>
      <el-input v-model="editText" type="textarea" :rows="12" maxlength="3000" show-word-limit style="margin-top: 8px" />
      <template #footer>
        <el-button @click="editing = null">取消</el-button>
        <el-button :disabled="!editText.trim()" @click="saveEdit(false)">保存</el-button>
        <el-button type="primary" :disabled="!editText.trim()" @click="saveEdit(true)">保存并发送</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sn-view {
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
.spacer {
  flex: 1;
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
  font-size: 13px;
  line-height: 1.7;
}
.pre {
  white-space: pre-wrap;
}
.warn {
  color: #d97706;
}
.last {
  font-size: 12px;
}
</style>
