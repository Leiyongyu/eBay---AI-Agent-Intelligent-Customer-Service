<script setup lang="ts">
// 售后管理 → 售后原因：订单维度的售后原因最终分类（AI 自动 + 人工，人工优先；都没标的也列出），
// 上半部统计（退款率按原因 / SKU / 店铺），下半部订单表（标出对话 ID，可打开会话核对、行内改人工分类）。
// 接口 /internal/aftersales-reasons（后端由 1 号负责）；客服只看自己负责的店铺，“重新计算”只给管理员
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, isAdmin, post } from '../api'
import { errorText, fmtTime } from '../format'
import { REASONS, REASON_LABEL, STATUS_LABEL, UC_KINDS, UC_LABEL, aiExplain, finalLabel } from '../aftersalesReason'
import AftersalesReasonEditor from '../components/AftersalesReasonEditor.vue'
import type { AftersalesReasonCode, AftersalesReasonList, AftersalesReasonRow, AftersalesReasonStats, RefundAmount, Store, UnclassifiedKind } from '../types'

const emit = defineEmits<{ open: [convId: number] }>()

const SITES = ['EBAY_US', 'EBAY_GB', 'EBAY_DE', 'EBAY_FR', 'EBAY_IT', 'EBAY_ES', 'EBAY_AU', 'EBAY_CA', 'EBAY_AT']
// eBay 售后单 + 本系统信号（PARCEL 异常包裹跟进 / TICKET 谷仓查件单 / FEISHU 飞书售后工单）
const KIND_LABEL: Record<string, string> = {
  RETURN: '退货', INQUIRY: '咨询', CASE: '纠纷', PAYMENT_DISPUTE: '付款纠纷', CANCEL: '取消交易', REFUND: '退款单',
  PARCEL: '异常包裹', TICKET: '查件单', FEISHU: '售后工单',
}
const KIND_TYPE: Record<string, 'danger' | 'warning' | 'info' | 'primary' | 'success'> = {
  RETURN: 'warning', INQUIRY: 'primary', CASE: 'danger', PAYMENT_DISPUTE: 'danger', CANCEL: 'info', REFUND: 'info',
  PARCEL: 'success', TICKET: 'success', FEISHU: 'success',
}

// ---- 筛选（统计和订单表共用）----
const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
const back = (days: number) => {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d
}
type Quick = '30' | '90' | '180' | 'custom'
const QUICKS: { key: Quick; label: string }[] = [{ key: '30', label: '近30天' }, { key: '90', label: '近90天' }, { key: '180', label: '近180天' }]
const quick = ref<Quick>('90')
const range = ref<[string, string]>([ymd(back(89)), ymd(new Date())])
function pick(q: Quick) {
  quick.value = q
  if (q !== 'custom') range.value = [ymd(back(Number(q) - 1)), ymd(new Date())]
}
function onRange(v: [string, string] | null) {
  if (!v) return pick('90')
  quick.value = 'custom'
  range.value = v
}
const stores = ref<Store[]>([])
const storeId = ref('')
const site = ref('')
function baseQuery() {
  const q = new URLSearchParams({ date_from: range.value[0], date_to: range.value[1] })
  if (storeId.value) q.set('store_id', storeId.value)
  if (site.value) q.set('marketplace_id', site.value)
  return q
}

// ---- 统计 ----
const stats = ref<AftersalesReasonStats | null>(null)
const statsLoading = ref(false)
const statsError = ref('')
async function loadStats() {
  statsLoading.value = true
  statsError.value = ''
  try {
    stats.value = await get<AftersalesReasonStats>(`/internal/aftersales-reasons/stats?${baseQuery()}`)
  } catch (e) {
    stats.value = null
    statsError.value = errorText(e)
  } finally {
    statsLoading.value = false
  }
}
const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(v === 0 || v === 1 ? 0 : 1)}%`)
const money = (list: RefundAmount[] | null | undefined) =>
  (list || []).filter((m) => m.amount).map((m) => `${m.amount.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${m.currency}`).join(' · ') || '—'
/** 未分类再分：缺退货数据、直接退款、其他；后端没给就不显示 */
const ucKinds = computed(() => {
  const u = stats.value?.unclassified
  return u ? UC_KINDS.flatMap((k) => (u[k.key] ? [{ key: k.key, ...u[k.key]!, label: u[k.key]!.label || k.label }] : [])) : []
})
const reasonMax = computed(() => Math.max(1, ...(stats.value?.by_reason || []).map((r) => r.refunded)))
const skuSort = ref<'rate' | 'count'>('rate')
const skuRows = computed(() => [...(stats.value?.by_sku || [])].sort((a, b) =>
  skuSort.value === 'rate'
    ? Number(a.small_sample) - Number(b.small_sample) || (b.refund_rate ?? -1) - (a.refund_rate ?? -1) || b.refunded - a.refunded
    : b.refunded - a.refunded || (b.refund_rate ?? -1) - (a.refund_rate ?? -1)))
const skuList = (s: string[] | string | null) => (Array.isArray(s) ? s : (s || '').split('+').map((x) => x.trim()).filter(Boolean))

// ---- 订单表 ----
type Tab = 'all' | 'UNCLASSIFIED' | 'AI' | 'MANUAL'
const TABS: { key: Tab; label: string }[] = [
  { key: 'all', label: '全部' }, { key: 'UNCLASSIFIED', label: '未分类' }, { key: 'AI', label: 'AI 分类' }, { key: 'MANUAL', label: '人工标记' },
]
const tab = ref<Tab>('all')
const f = reactive({ reason: '' as AftersalesReasonCode | '', refunded: '' as '' | 'true' | 'false', q: '' })
const page = ref(1)
const pageSize = ref(50)
const list = ref<AftersalesReasonList | null>(null)
const loading = ref(false)
const listError = ref('')
function listQuery() {
  const q = baseQuery()
  if (tab.value !== 'all') q.set('status', tab.value)
  if (tab.value === 'UNCLASSIFIED' && ucKind.value) q.set('unclassified_kind', ucKind.value)
  if (f.reason) q.set('reason', f.reason)
  if (f.refunded) q.set('refunded', f.refunded)
  if (f.q.trim()) q.set('q', f.q.trim())
  q.set('page', String(page.value))
  q.set('page_size', String(pageSize.value))
  return q
}
async function loadList(silent = false) {
  if (!silent) loading.value = true
  listError.value = ''
  try {
    const r = await get<AftersalesReasonList>(`/internal/aftersales-reasons?${listQuery()}`)
    // 标完最后一页的最后一行：这一页空了，退回上一页
    if (silent && !r.items.length && page.value > 1) {
      page.value -= 1 // watch(page) 会再拉一次
      return
    }
    list.value = r
  } catch (e) {
    if (!silent) list.value = null
    listError.value = errorText(e)
  } finally {
    loading.value = false
  }
}
const total = computed(() => list.value?.total ?? list.value?.counts?.[tab.value] ?? list.value?.items.length ?? 0)
const tableEl = ref<HTMLElement | null>(null)
/** 未分类页签里的二级筛选（后端 unclassified_kind） */
const ucKind = ref<UnclassifiedKind | ''>('')
function showUnclassified(kind: UnclassifiedKind | '' = '') {
  tab.value = 'UNCLASSIFIED'
  ucKind.value = kind
  tableEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function exportCsv() {
  const q = listQuery()
  q.delete('page')
  q.delete('page_size')
  const a = document.createElement('a')
  a.href = `/internal/aftersales-reasons/export?${q}`
  a.click()
}

// 行内编辑人工分类
const editing = ref<number | null>(null)
const saving = ref(false)
/** 改完的一行还符合当前页签和原因筛选吗（比如在“未分类”里标好后就不再属于未分类） */
const stillMatches = (r: AftersalesReasonRow) =>
  (tab.value === 'all' || r.status === tab.value) && (!f.reason || r.final_reason === f.reason)
  && !(tab.value === 'UNCLASSIFIED' && ucKind.value && r.unclassified_kind !== ucKind.value)
function replaceRow(r: AftersalesReasonRow) {
  if (!list.value) return
  const i = list.value.items.findIndex((x) => x.id === r.id)
  if (i < 0) return
  if (stillMatches(r)) list.value.items.splice(i, 1, r)
  else list.value.items.splice(i, 1) // 马上从列表移走
}
/** 改完后：本地先换掉/移走这一行，再静默刷新列表（页签数字、补齐这一页）和统计 */
function afterEdit(r: AftersalesReasonRow) {
  replaceRow(r)
  loadList(true)
  loadStats()
}
async function saveManual(row: AftersalesReasonRow, body: object) {
  saving.value = true
  try {
    afterEdit(await api<AftersalesReasonRow>('PUT', `/internal/aftersales-reasons/${row.id}`, body))
    editing.value = null
    ElMessage.success('已保存人工分类')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}
async function clearManual(row: AftersalesReasonRow) {
  try {
    await ElMessageBox.confirm(`撤销订单 ${row.order_id} 的人工分类？撤销后按 AI 分类（没有就是未分类）。`, '撤销人工分类',
      { type: 'warning', confirmButtonText: '撤销' })
  } catch {
    return
  }
  try {
    afterEdit(await api<AftersalesReasonRow>('DELETE', `/internal/aftersales-reasons/${row.id}/manual`))
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
const rebuilding = ref(false)
async function rebuild() {
  rebuilding.value = true
  try {
    const r = await post<Record<string, unknown>>('/internal/aftersales-reasons/rebuild')
    ElMessage.success(`已重新计算${typeof r.updated === 'number' ? `（更新 ${r.updated} 单）` : ''}`)
    await Promise.all([loadStats(), loadList()])
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    rebuilding.value = false
  }
}

const dateOf = (s: string | null | undefined) => (s ? s.slice(0, 10) : '')
const convIds = (r: AftersalesReasonRow) => [...new Set([...(r.conversation_ids || []), ...(r.conversation_id ? [r.conversation_id] : [])])]
const statusType = (s: string) => (s === 'MANUAL' ? 'success' : s === 'AI' ? 'primary' : 'info')

watch([range, storeId, site], () => {
  page.value = 1
  loadStats()
  loadList()
})
watch(tab, (t) => {
  if (t !== 'UNCLASSIFIED') ucKind.value = ''
})
watch([tab, ucKind, () => f.reason, () => f.refunded], () => {
  page.value = 1
  loadList()
})
watch([page, pageSize], () => loadList())
let qTimer: number | undefined
watch(() => f.q, () => {
  clearTimeout(qTimer)
  qTimer = window.setTimeout(() => {
    page.value = 1
    loadList()
  }, 350)
})
onMounted(() => {
  get<Store[]>('/internal/stores').then((s) => (stores.value = s)).catch(() => null)
  loadStats()
  loadList()
})
</script>

<template>
  <div class="ar">
    <!-- 筛选 -->
    <div class="bar panel">
      <div class="quick">
        <button v-for="q in QUICKS" :key="q.key" :class="{ on: quick === q.key }" @click="pick(q.key)">{{ q.label }}</button>
      </div>
      <el-date-picker :model-value="range" type="daterange" value-format="YYYY-MM-DD" range-separator="至" start-placeholder="下单开始"
                      end-placeholder="下单结束" :clearable="false" class="range" @update:model-value="onRange" />
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable class="sel">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-select v-model="site" placeholder="全部站点" clearable class="sel-s">
        <el-option v-for="m in SITES" :key="m" :label="m.replace('EBAY_', '')" :value="m" />
      </el-select>
      <span class="spacer" />
      <el-button v-if="isAdmin()" :loading="rebuilding" @click="rebuild">重新计算</el-button>
    </div>

    <!-- ======== 统计 ======== -->
    <section v-loading="statsLoading && !stats" class="panel sec">
      <div class="panel-head">统计<span class="muted">按订单下单时间 · {{ range[0] }} 至 {{ range[1] }}</span></div>
      <div class="cur-note">退款金额为 eBay 结算币种（美元），订单金额为当地币种；部分退款没有金额，合计偏低。</div>
      <el-alert v-if="statsError" type="error" :closable="false" show-icon :title="`统计加载失败：${statsError}`" class="alert" />
      <template v-if="stats">
        <div class="kpis">
          <div class="kpi"><div class="k-label">订单</div><div class="k-value">{{ stats.overall.orders.toLocaleString('zh-CN') }}</div>
            <div class="k-sub">其中有售后（退货、咨询、纠纷等）{{ stats.overall.aftersales_orders.toLocaleString('zh-CN') }} 单</div></div>
          <div class="kpi"><div class="k-label">退款订单</div><div class="k-value">{{ stats.overall.refunded.toLocaleString('zh-CN') }}</div>
            <div class="k-sub">退款金额 {{ money(stats.overall.refund_amount) }}</div></div>
          <div class="kpi"><div class="k-label">退款率</div><div class="k-value">{{ pct(stats.overall.refund_rate) }}</div>
            <div class="k-sub">退款订单 ÷ 这段时间的订单数</div></div>
          <div class="kpi kpi-uc" :class="{ warn: stats.unclassified.orders }">
            <button class="kpi-main" title="查看全部未分类订单" @click="showUnclassified()">
              <div class="k-label">未分类</div><div class="k-value">{{ stats.unclassified.orders.toLocaleString('zh-CN') }}</div>
            </button>
            <div v-if="ucKinds.length" class="k-kinds">
              <button v-for="k in ucKinds" :key="k.key" class="k-kind" :title="`只看“${k.label}”的未分类订单`" @click="showUnclassified(k.key)">
                <span>{{ k.label }}</span><b>{{ k.orders.toLocaleString('zh-CN') }}</b><em>退款 {{ k.refunded }}</em>
              </button>
            </div>
            <div class="k-sub">AI 和人工都没标的售后订单（其中退款 {{ stats.unclassified.refunded }}），点击查看。
              <template v-if="!ucKinds.length">多数是只有退款记录、早于售后同步范围、缺少退货原因的订单，</template>可以人工补标</div>
          </div>
        </div>

        <div class="grid2">
          <div>
            <div class="sub-head">按售后原因<span class="muted">退款订单数、占全部退款的比例</span></div>
            <div class="dist">
              <div v-for="r in stats.by_reason" :key="r.reason || 'none'" class="drow" :class="{ none: !r.reason }">
                <span class="dl">{{ r.label }}</span>
                <span class="track"><i :style="{ width: `${(r.refunded / reasonMax) * 100}%` }" /></span>
                <span class="dn">{{ r.refunded }}<em>{{ pct(r.share) }}</em></span>
                <span class="dm muted">售后 {{ r.aftersales_orders }} 单 · 退款率 {{ pct(r.refund_rate) }} · {{ money(r.refund_amount) }}</span>
              </div>
              <div v-if="!stats.by_reason.length" class="muted empty">这段时间没有售后订单</div>
            </div>
          </div>
          <div>
            <div class="sub-head">按店铺</div>
            <el-table :data="stats.by_store" size="small" border max-height="320" empty-text="暂无" class="mini">
              <el-table-column prop="store_name" label="店铺" min-width="130" />
              <el-table-column prop="orders" label="订单" width="70" align="right" />
              <el-table-column prop="refunded" label="退款" width="70" align="right" />
              <el-table-column label="退款率" width="80" align="right"><template #default="{ row }">{{ pct(row.refund_rate) }}</template></el-table-column>
              <el-table-column label="退款金额" min-width="110" align="right"><template #default="{ row }">{{ money(row.refund_amount) }}</template></el-table-column>
            </el-table>
          </div>
        </div>

        <div class="sub-head">按 SKU
          <span class="seg">
            <button :class="{ on: skuSort === 'rate' }" @click="skuSort = 'rate'">按退款率</button>
            <button :class="{ on: skuSort === 'count' }" @click="skuSort = 'count'">按退款数</button>
          </span>
          <span class="muted">订单少于 {{ stats.min_orders ?? 5 }} 单的灰显，仅供参考<template v-if="stats.sku_count && stats.sku_count > stats.by_sku.length">
            · 共 {{ stats.sku_count }} 个 SKU，显示前 {{ stats.by_sku.length }} 个</template></span>
        </div>
        <el-table :data="skuRows" size="small" border max-height="360" empty-text="暂无"
                  :row-class-name="({ row }: { row: { small_sample: boolean } }) => (row.small_sample ? 'small-row' : '')" class="mini">
          <el-table-column prop="sku" label="SKU" min-width="150" />
          <el-table-column prop="orders" label="订单" width="70" align="right" />
          <el-table-column prop="refunded" label="退款" width="70" align="right" />
          <el-table-column label="退款率" width="90" align="right">
            <template #default="{ row }">{{ pct(row.refund_rate) }}<span v-if="row.small_sample" class="muted">（样本少）</span></template>
          </el-table-column>
          <el-table-column label="主要原因" min-width="110"><template #default="{ row }">{{ row.main_reason_label || '—' }}</template></el-table-column>
        </el-table>
      </template>
    </section>

    <!-- ======== 订单表 ======== -->
    <section ref="tableEl" class="panel sec">
      <div class="panel-head">订单
        <span class="tabs">
          <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="tab = t.key">
            {{ t.label }}<span v-if="list?.counts?.[t.key] != null" class="n">{{ list.counts[t.key] }}</span>
          </button>
        </span>
      </div>
      <div class="toolbar">
        <span v-if="tab === 'UNCLASSIFIED' && ucKinds.length" class="quick uc-quick">
          <button :class="{ on: !ucKind }" @click="ucKind = ''">全部未分类</button>
          <button v-for="k in UC_KINDS" :key="k.key" :class="{ on: ucKind === k.key }" @click="ucKind = k.key">
            {{ k.label }}<span v-if="stats?.unclassified[k.key]" class="n">{{ stats.unclassified[k.key]!.orders }}</span>
          </button>
        </span>
        <el-input v-model="f.q" clearable placeholder="订单号 / SKU / 买家 / 对话 ID" class="search" />
        <el-select v-model="f.reason" clearable placeholder="最终分类" class="sel-s">
          <el-option v-for="r in REASONS" :key="r.code" :label="r.label" :value="r.code" />
        </el-select>
        <el-select v-model="f.refunded" clearable placeholder="是否退款" class="sel-s">
          <el-option label="有退款" value="true" /><el-option label="没退款" value="false" />
        </el-select>
        <span class="spacer" />
        <el-button :disabled="!list?.items.length" @click="exportCsv">导出</el-button>
      </div>
      <el-alert v-if="listError" type="error" :closable="false" show-icon :title="`订单加载失败：${listError}`" class="alert" />
      <el-table v-loading="loading" :data="list?.items || []" border size="small" row-key="id" empty-text="没有符合条件的订单" class="tbl">
        <el-table-column label="店铺 / 订单" min-width="170" fixed>
          <template #default="{ row }">
            <div class="strong">{{ row.order_id }}</div>
            <div class="muted">{{ row.store_name }}<template v-if="row.marketplace_id"> · {{ row.marketplace_id.replace('EBAY_', '') }}</template></div>
            <div v-for="id in convIds(row)" :key="id">
              <el-button link size="small" type="primary" :title="`打开会话 ${id}`" @click="emit('open', id)">打开会话 {{ id }}</el-button>
            </div>
            <div v-for="b in row.baseline_conversations || []" :key="b.id" class="base-conv"
                 :title="'上线前补拉的历史会话，不能在工作台打开；可用这个 eBay 会话号到 eBay 后台核对'
                   + (b.first_message_at ? `\n${dateOf(b.first_message_at)} 至 ${dateOf(b.last_message_at)}` : '')">
              历史会话 <span class="mono">{{ b.ebay_conversation_id || `#${b.id}` }}</span>
            </div>
            <span v-if="!convIds(row).length && !row.baseline_conversations?.length" class="muted">没有关联会话</span>
          </template>
        </el-table-column>
        <el-table-column label="最终分类" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.final_reason" size="small" :type="statusType(row.status)">{{ finalLabel(row) }}</el-tag>
            <el-tag v-else size="small" type="info" effect="plain">未分类</el-tag>
            <div v-if="!row.final_reason && row.unclassified_kind" class="uc-kind">{{ UC_LABEL[row.unclassified_kind] || row.unclassified_kind }}</div>
            <div v-else class="muted">{{ row.status_label || STATUS_LABEL[row.status] || row.status }}</div>
          </template>
        </el-table-column>
        <el-table-column label="SKU" min-width="140">
          <template #default="{ row }"><div v-for="s in skuList(row.skus)" :key="s" class="mono">{{ s }}</div>
            <span v-if="!skuList(row.skus).length" class="muted">—</span></template>
        </el-table-column>
        <el-table-column prop="buyer_key" label="买家" min-width="120" />
        <el-table-column label="下单时间" width="140"><template #default="{ row }">{{ fmtTime(row.order_created_at) }}</template></el-table-column>
        <el-table-column label="退款" width="110" align="right">
          <template #default="{ row }">
            <template v-if="row.refunded">{{ row.refund_amount ?? '' }} {{ row.refund_currency || '' }}</template>
            <span v-else class="muted">没退款</span>
          </template>
        </el-table-column>
        <el-table-column label="售后事件" min-width="180">
          <template #default="{ row }">
            <div v-for="(e, i) in row.events" :key="i" class="ev">
              <el-tag size="small" :type="KIND_TYPE[e.kind] || 'info'" effect="plain">{{ e.kind_label || KIND_LABEL[e.kind] || e.kind }}</el-tag>
              <span v-if="e.reason_label || e.reason" class="muted">{{ e.reason_label || e.reason }}</span>
              <span v-if="e.at" class="muted">{{ e.at.slice(0, 10) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AI 分类" min-width="200">
          <template #default="{ row }">
            <span v-if="row.ai_reason" class="strong">{{ REASON_LABEL[row.ai_reason] || row.ai_reason }}</span>
            <span v-else-if="!row.ai_note" class="muted">—</span>
            <div v-for="(p, i) in aiExplain(row).notes" :key="`n${i}`" class="note"
                 :class="row.ai_reason ? (p.startsWith('冲突') || p.startsWith('以飞书') ? 'why' : 'muted') : 'why'">{{ p }}</div>
            <div v-for="b in aiExplain(row).basis" :key="b" class="note muted">依据：{{ b }}</div>
          </template>
        </el-table-column>
        <el-table-column label="人工分类" min-width="230">
          <template #default="{ row }">
            <AftersalesReasonEditor v-if="editing === row.id" :reason="row.manual_reason" :note="row.manual_note" :saving="saving" compact
                                    @save="(b) => saveManual(row, b)" @cancel="editing = null" />
            <template v-else>
              <template v-if="row.manual_reason">
                <span class="strong">{{ REASON_LABEL[row.manual_reason] || row.manual_reason }}</span>
                <span v-if="row.manual_note" class="note"> {{ row.manual_note }}</span>
                <div class="muted">{{ row.manual_by }} · {{ fmtTime(row.manual_at) }}</div>
              </template>
              <span v-else class="muted">—</span>
              <div class="acts">
                <el-button link size="small" type="primary" @click="editing = row.id">{{ row.manual_reason ? '修改' : '人工分类' }}</el-button>
                <el-button v-if="row.manual_reason" link size="small" @click="clearManual(row)">撤销</el-button>
              </div>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100, 200]" :total="total"
                       layout="total, sizes, prev, pager, next" background small />
      </div>
    </section>
  </div>
</template>

<style scoped>
.ar {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 100%;
  min-width: 0;
}
.bar {
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
}
.quick,
.seg,
.tabs {
  display: inline-flex;
  flex-wrap: wrap;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.quick button,
.seg button,
.tabs button {
  height: 28px;
  padding: 0 12px;
  border: 0;
  background: var(--panel);
  font-family: inherit;
  font-size: 13px;
  font-weight: 400;
  color: var(--text-2);
  cursor: pointer;
}
.quick button + button,
.seg button + button,
.tabs button + button {
  border-left: 1px solid var(--line);
}
.quick button.on,
.seg button.on,
.tabs button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.tabs {
  margin-left: 8px;
}
.n {
  margin-left: 4px;
  font-size: 12px;
  opacity: 0.8;
}
.range {
  width: 260px;
  max-width: 100%;
}
.sel {
  width: 180px;
  max-width: 100%;
}
.sel-s {
  width: 120px;
}
.search {
  width: 260px;
  max-width: 100%;
}
.spacer {
  flex: 1;
}
.sec {
  min-width: 0;
  padding-bottom: 12px;
}
.alert {
  margin: 8px 14px 0;
  width: auto;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px;
  padding: 12px 14px 4px;
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  background: var(--sunken);
  min-width: 0;
  text-align: left;
  font: inherit;
}
.kpi-btn {
  cursor: pointer;
}
.kpi-btn:hover {
  border-color: var(--accent);
}
.kpi-btn.warn .k-value,
.kpi-uc.warn .k-value {
  color: var(--warn-text);
}
.kpi-main,
.k-kind {
  border: 0;
  padding: 0;
  background: none;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.kpi-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi-main:hover .k-value,
.k-kind:hover span {
  color: var(--accent);
  text-decoration: underline;
}
.k-label {
  font-size: 13px;
  color: var(--text-2);
}
.k-value {
  font-size: 24px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.k-sub {
  font-size: 12px;
  color: var(--muted);
}
.k-kinds {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 2px 0 4px;
  font-size: 12px;
}
.k-kind {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.k-kind b {
  color: var(--text);
}
.k-kind em {
  font-style: normal;
  color: var(--muted);
}
.uc-kind {
  font-size: 12px;
  color: var(--warn-text);
}
.base-conv {
  font-size: 12px;
  color: var(--text-2);
  cursor: help;
}
.uc-quick {
  flex: none;
}
.cur-note {
  margin: -2px 0 10px;
  font-size: 12px;
  color: var(--muted);
}
.grid2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 0 18px;
  min-width: 0;
}
.sub-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 14px 14px 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.sub-head .muted {
  font-weight: 400;
  font-size: 12px;
}
.dist {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 4px 14px 6px;
}
.drow {
  display: grid;
  grid-template-columns: minmax(0, 7em) minmax(40px, 1fr) auto;
  grid-template-areas: "l t n" ". m m";
  align-items: center;
  column-gap: 10px;
  font-size: 13px;
}
.drow.none .dl {
  color: var(--muted);
}
.dl {
  grid-area: l;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-2);
}
.track {
  grid-area: t;
  height: 10px;
  border-radius: 5px;
  background: var(--line-soft);
  overflow: hidden;
}
.track i {
  display: block;
  height: 100%;
  min-width: 2px;
  border-radius: 5px;
  background: var(--accent);
}
.dn {
  grid-area: n;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.dn em {
  font-style: normal;
  margin-left: 6px;
  font-size: 12px;
  color: var(--muted);
}
.dm {
  grid-area: m;
  font-size: 12px;
}
.mini,
.tbl,
.toolbar,
.pager {
  margin: 0 14px;
  width: auto;
  max-width: calc(100% - 28px);
}
.mini :deep(.small-row td) {
  color: var(--muted);
  background: var(--sunken) !important;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 10px 0;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 10px;
}
.strong {
  font-weight: 600;
  color: var(--text);
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
}
.ev {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin: 2px 0;
}
.note {
  font-size: 12px;
}
.note.why {
  color: var(--warn-text);
}
.has-tip {
  border-bottom: 1px dashed var(--muted);
  cursor: help;
}
.acts {
  margin-top: 2px;
}
.empty {
  font-size: 12px;
}
@media (max-width: 640px) {
  .bar {
    padding: 10px;
  }
  .kpis,
  .grid2 {
    grid-template-columns: minmax(0, 1fr);
  }
  .tabs {
    margin: 6px 0 0;
  }
}
</style>
