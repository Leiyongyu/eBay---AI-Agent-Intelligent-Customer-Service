<script setup lang="ts">
// 退款单：eBay 财务接口的退款流水（近 90 天，定时拉取，只读）。退款单类型、纠纷编号、退款原因、当地币种金额、全额 / 部分
// 由后端按同一订单的退货 / 纠纷 / 取消和订单推出来（refunds.py）。本系统里可以打标签、写备注、标处理状态；
// 「查看退款单」弹窗看明细，「查看消息」定位到这个买家的会话
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText } from '../format'
import { copyText, downloadCsv, fmtFull, money, useCases } from '../caseCommon'
import type { CaseOptions, CaseRow, CaseTag, ViewCounts } from '../caseCommon'
import CaseTagDialog from '../components/CaseTagDialog.vue'
import CaseTagFilter from '../components/CaseTagFilter.vue'
import RefundRequestsPanel from '../components/RefundRequestsPanel.vue'
import { dotStyle } from '../tagColor'

const emit = defineEmits<{ open: [id: number] }>()
const KIND = 'REFUND'
const LIMIT = 1000
const isAdmin = computed(() => session.user?.role === 'ADMIN')

const SEARCH_FIELDS = [
  { value: 'order_id', label: '订单编号' }, { value: 'linked_id', label: '纠纷编号' }, { value: 'buyer', label: '客户ID' },
  { value: 'sku', label: 'SKU' }, { value: 'external_id', label: 'eBay 交易号' },
]
const MATCH = [{ value: 'prefix', label: '开头' }, { value: 'contains', label: '包含' }, { value: 'exact', label: '精确' }]
const VIEWS = [
  { value: 'all', label: '全部' }, { value: 'pending', label: '待处理' }, { value: 'processing', label: '处理中' }, { value: 'done', label: '已处理' },
]
const TYPES = [
  { value: '', label: '全部' }, { value: 'manual', label: '主动退款' }, { value: 'aftersales', label: '纠纷退货' }, { value: 'cancel', label: '取消交易' },
]
const TYPE_TAG: Record<string, 'primary' | 'warning' | 'info'> = { manual: 'info', aftersales: 'warning', cancel: 'primary' }

// ---------------- 筛选 ----------------
const f = reactive({
  q: '', qField: 'order_id', match: 'prefix', storeIds: [] as string[], reason: '', full: '', operator: '',
  tagIds: [] as string[], tagMode: 'any' as 'any' | 'all', time: null as [string, string] | null, starred: false,
})
const view = ref('all')
const rtype = ref('')
const applied = ref({ ...f })
const rows = ref<CaseRow[]>([])
const counts = ref<ViewCounts | null>(null)
const opts = ref<CaseOptions | null>(null)
const loading = ref(false)

function query(): URLSearchParams {
  const a = applied.value
  const q = new URLSearchParams({ kind: KIND, view: view.value, limit: String(LIMIT) })
  if (a.q.trim()) {
    q.set('q', a.q)
    q.set('q_field', a.qField)
    q.set('match', a.match)
  }
  if (a.storeIds.length) q.set('store_ids', a.storeIds.join(','))
  if (a.reason) q.set('refund_reason', a.reason)
  if (a.full) q.set('refund_full', a.full)
  if (rtype.value) q.set('refund_type', rtype.value)
  if (a.operator) q.set('operator', a.operator)
  if (a.tagIds.length) {
    q.set('tag_ids', a.tagIds.join(','))
    q.set('tag_mode', a.tagMode)
  }
  if (a.starred) q.set('starred', 'true')
  if (a.time) {
    q.set('opened_from', a.time[0])
    q.set('opened_to', a.time[1])
  }
  return q
}
async function load() {
  loading.value = true
  try {
    const sq = applied.value.storeIds.length === 1 ? `&store_id=${applied.value.storeIds[0]}` : ''
    const [list, c] = await Promise.all([
      get<CaseRow[]>(`/internal/ebay-cases?${query()}`),
      get<ViewCounts>(`/internal/ebay-cases/view-counts?kind=${KIND}${sq}`),
    ])
    rows.value = list
    counts.value = c
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
const common = useCases(() => KIND, load)
const { tags, extras, busy } = common

function search() {
  applied.value = { ...f, storeIds: [...f.storeIds], tagIds: [...f.tagIds] }
  page.value = 1
  load()
}
function reset() {
  Object.assign(f, { q: '', reason: '', full: '', operator: '', tagIds: [], tagMode: 'any', time: null, starred: false, storeIds: [] })
  rtype.value = ''
  search()
}
function quickTag(id: string) {
  f.tagIds = applied.value.tagIds.length === 1 && applied.value.tagIds[0] === id ? [] : [id]
  search()
}
const pinnedTags = computed(() => tags.value.filter((t) => t.pinned))
async function createFromFilter(name: string, color: string, done: (t: CaseTag | null) => void) {
  done(await common.createTag(name, color))
}
watch([view, rtype], () => {
  page.value = 1
  selected.value = []
  load()
})

const page = ref(1)
const pageSize = ref(20)
const pageRows = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(pageRows, (l) => common.want(l), { immediate: true })
const selected = ref<CaseRow[]>([])
const ids = () => selected.value.map((r) => r.id)

function onTag(cmd: string) {
  const [op, id] = cmd.split(':')
  common.batch(ids(), { action: op === 'add' ? 'tag_add' : 'tag_remove', tag_id: Number(id) }, op === 'add' ? '已加标签' : '已去标签')
}
function onState(cmd: string) {
  const label: Record<string, string> = { PROCESSING: '已标为处理中', DONE: '已标为已处理', PENDING: '已改回待处理' }
  common.batch(ids(), { action: 'state', state: cmd }, label[cmd])
}
async function removeSelected() {
  if (!selected.value.length) return ElMessage.warning('请先勾选记录')
  try {
    await ElMessageBox.confirm(`删除勾选的 ${selected.value.length} 条退款单？只在本系统里隐藏，eBay 上不受影响，之后再拉取也不会恢复。`, '删除',
      { type: 'warning' })
  } catch {
    return
  }
  await common.batch(ids(), { action: 'hide' }, '已删除')
  selected.value = []
}

// ---------------- 显示 ----------------
const r0 = (r: CaseRow) => r.refund || null
const mainLink = (r: CaseRow) => r.refund?.linked[0] || null
const skus = (r: CaseRow) => (r.refund?.skus.length ? r.refund.skus : [extras[r.id]?.sku].filter(Boolean)) as string[]
const localMoney = (r: CaseRow) => (r.refund?.local_amount != null
  ? `${r.refund.estimated ? '约 ' : ''}${money(r.refund.local_amount, r.refund.local_currency)}` : null)
const reasonText = (r: CaseRow) => r.refund?.reason_label || r.refund?.reason || r.refund?.request?.reason_label
  || (r.refund?.type === 'manual' ? r.refund?.memo : null) || '—'
const stateText = (r: CaseRow) => ({ done: '已处理', processing: '处理中', pending: '待处理' } as Record<string, string>)[r.view] || ''
const stateType = (r: CaseRow) => (r.view === 'done' ? 'success' : r.view === 'processing' ? 'primary' : 'info')

function exportCsv() {
  const list = selected.value.length ? selected.value : rows.value
  downloadCsv(`退款单_${fmtFull(new Date().toISOString()).slice(0, 10)}.csv`,
    ['订单编号', 'SKU', '纠纷编号', '关联类型', '退款单类型', '客户ID', '店铺', '退款原因', '退款金额（当地）', '币种', '退款类型',
      '结算金额', '结算币种', 'eBay 交易号', '退款时间', 'eBay 状态', '处理状态', '标签', '备注', '操作员'],
    list.map((r) => {
      const x = r0(r)
      const l = mainLink(r)
      return [r.order_id, skus(r).join(' + '), l?.external_id, l?.kind_label, x?.type_label, r.buyer, r.store_name,
        x?.reason_label || x?.reason, x?.local_amount, x?.local_currency, x?.full_label, r.amount, r.currency, r.external_id,
        fmtFull(r.opened_at), r.status_label || r.status, stateText(r),
        r.tag_ids.map((t) => common.tagOf(t)?.name).filter(Boolean).join('、'), r.note, r.operator]
    }))
}

// 备注：订单编号下面直接改
const editing = ref<number | null>(null)
const noteText = ref('')
function editNote(r: CaseRow) {
  editing.value = r.id
  noteText.value = r.note || ''
}
async function saveNote(r: CaseRow) {
  if (await common.saveNote(r, noteText.value)) editing.value = null
}

// 退款单弹窗
const detail = ref<CaseRow | null>(null)
function openDetail(r: CaseRow) {
  detail.value = r
  if (r.is_new) post(`/internal/ebay-cases/${r.id}/seen`).then(() => (r.is_new = false)).catch(() => null)
}
function openConv(r: CaseRow) {
  if (!r.conversation_id) return
  detail.value = null
  emit('open', r.conversation_id)
}
const tagDialog = ref(false)
// 退款流水（eBay 拉来的）/ 退款申请（本系统提交、审核后调 eBay 退款）
const panel = ref<'flow' | 'requests'>('flow')
// 链接 #/ebaycases?m=refund&panel=requests&rid=12：打开“退款申请”并定位到这一张（1 号退货页提交后给的链接）
const focusRid = ref<number | null>(null)
function readHash() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '')
  if (q.get('panel') === 'requests') panel.value = 'requests'
  focusRid.value = Number(q.get('rid')) || null
}
window.addEventListener('hashchange', readHash)
onUnmounted(() => window.removeEventListener('hashchange', readHash))
const pendingCount = ref<number | null>(null)
async function loadPending() {
  try {
    pendingCount.value = (await get<{ counts: Record<string, number> }>('/internal/refund-requests?status=PENDING&limit=1')).counts.PENDING ?? 0
  } catch {
    pendingCount.value = null
  }
}

onMounted(async () => {
  common.loadTags()
  readHash()
  opts.value = await get<CaseOptions>(`/internal/ebay-cases/options?kind=${KIND}`).catch(() => null)
  loadPending()
  await load()
})
</script>

<template>
  <div class="rf">
    <div class="panels">
      <button :class="{ on: panel === 'flow' }" @click="panel = 'flow'">退款流水<span class="muted">（eBay）</span></button>
      <button :class="{ on: panel === 'requests' }" @click="panel = 'requests'">退款申请
        <span v-if="pendingCount" class="badge" title="待审核">{{ pendingCount }}</span></button>
      <span class="muted small">退款申请：在本系统提交、审核通过后由系统在 eBay 上退款</span>
    </div>
    <RefundRequestsPanel v-if="panel === 'requests'" :stores="opts?.stores || []" :focus-id="focusRid" @open="(id: number) => emit('open', id)"
                         @changed="loadPending" />
    <template v-if="panel === 'flow'">
    <!-- ======== 筛选 ======== -->
    <div class="filters">
      <div class="f-row">
        <div class="search">
          <el-select v-model="f.qField" class="s-field">
            <el-option v-for="o in SEARCH_FIELDS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-input v-model="f.q" class="s-input" clearable placeholder="多个请用回车或逗号分隔" @keyup.enter="search" />
          <el-select v-model="f.match" class="s-match">
            <el-option v-for="o in MATCH" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </div>
        <el-select v-model="f.reason" class="f-sel" placeholder="退款原因" clearable filterable>
          <el-option v-for="r in opts?.reasons || []" :key="r" :label="opts?.reason_labels?.[r] || r" :value="r" />
        </el-select>
        <el-select v-model="f.storeIds" class="f-sel w-store" placeholder="eBay账号" multiple collapse-tags clearable filterable>
          <el-option v-for="s in opts?.stores || []" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-date-picker v-model="f.time" class="f-date" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                        start-placeholder="退款时间" end-placeholder="结束日期" />
        <el-select v-model="f.full" class="f-sel w-s" placeholder="退款类型" clearable>
          <el-option label="全额退款" value="full" /><el-option label="部分退款" value="partial" />
        </el-select>
        <el-select v-model="f.operator" class="f-sel w-s" placeholder="操作员" clearable filterable>
          <el-option label="未认领" value="unassigned" />
          <el-option v-for="o in opts?.operators || []" :key="o" :label="o" :value="o" />
        </el-select>
        <CaseTagFilter v-model="f.tagIds" v-model:mode="f.tagMode" :tags="tags" :counts="counts?.tags" :is-admin="isAdmin"
                       @create="createFromFilter" @pin="(t, v) => common.updateTag(t, { pinned: v })" @manage="tagDialog = true" @apply="search" />
        <el-tooltip content="只看星标" placement="top">
          <button class="star-btn" :class="{ on: f.starred }" @click="f.starred = !f.starred; search()">★</button>
        </el-tooltip>
        <el-button plain type="primary" @click="search">搜索</el-button>
        <el-button link @click="reset">重置</el-button>
      </div>
      <div class="tag-row">
        <span class="tag-title">标签:</span>
        <button v-for="t in pinnedTags" :key="t.id" class="chip" :class="{ on: applied.tagIds.includes(String(t.id)) }" @click="quickTag(String(t.id))">
          <i class="dot" :style="dotStyle(t.color)" />{{ t.name }}<span class="n">({{ counts?.tags[String(t.id)] || 0 }})</span>
        </button>
        <span v-if="!pinnedTags.length" class="muted">{{ tags.length ? '没有固定的标签，' : '快速访问标签，' }}</span>
        <el-button link type="primary" size="small" @click="tagDialog = true">{{ tags.length ? '标签管理' : '新建标签' }}</el-button>
      </div>
    </div>

    <!-- ======== 处理状态页签 + 退款单类型 ======== -->
    <div class="tabs">
      <button v-for="v in VIEWS" :key="v.value" :class="{ on: view === v.value }" @click="view = v.value">
        {{ v.label }}<span class="n">({{ counts?.views[v.value] ?? 0 }})</span>
      </button>
    </div>
    <div class="types">
      <button v-for="t in TYPES" :key="t.value" :class="{ on: rtype === t.value }" @click="rtype = t.value">
        {{ t.label }}<span class="n">({{ t.value ? counts?.refund_types?.[t.value] ?? 0 : counts?.views.all ?? 0 }})</span>
      </button>
      <span class="hint muted">按同一订单有没有退货 / 纠纷 / 取消来分；类型数量不受上面页签影响</span>
    </div>

    <!-- ======== 工具栏 ======== -->
    <div class="toolbar">
      <el-button @click="exportCsv">导出</el-button>
      <el-dropdown trigger="click" :disabled="busy" @command="onTag">
        <el-button>标签<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="t in tags" :key="`a${t.id}`" :command="`add:${t.id}`">
              <i class="dot" :style="dotStyle(t.color)" />加上「{{ t.name }}」
            </el-dropdown-item>
            <el-dropdown-item v-for="(t, i) in tags" :key="`r${t.id}`" :command="`remove:${t.id}`" :divided="i === 0">去掉「{{ t.name }}」</el-dropdown-item>
            <el-dropdown-item :divided="!!tags.length" @click="tagDialog = true">新建 / 管理标签…</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-dropdown trigger="click" :disabled="busy" @command="onState">
        <el-button>处理状态<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="PROCESSING">标为处理中</el-dropdown-item>
            <el-dropdown-item command="DONE">标为已处理</el-dropdown-item>
            <el-dropdown-item command="PENDING">改回待处理</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button :disabled="busy" @click="removeSelected">删除</el-button>
      <span v-if="selected.length" class="picked">已选 {{ selected.length }} 条</span>
      <span class="spacer" />
      <span class="total">共{{ rows.length }}条记录<template v-if="rows.length >= LIMIT">（只显示前 {{ LIMIT }} 条，请用筛选缩小范围）</template></span>
    </div>

    <!-- ======== 列表 ======== -->
    <div class="table-wrap">
      <el-table v-loading="loading && !rows.length" :data="pageRows" row-key="id" empty-text="没有退款单"
                @selection-change="(v: CaseRow[]) => (selected = v)">
        <el-table-column type="selection" width="40" />
        <el-table-column label="订单编号" width="150">
          <template #default="{ row }">
            <div><span class="dotted">{{ row.order_id || '—' }}</span><button v-if="row.order_id" class="copy" title="复制" @click="copyText(row.order_id)">⧉</button>
              <span v-if="row.is_new" class="new">新</span></div>
            <template v-if="editing === row.id">
              <el-input v-model="noteText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" maxlength="2000" class="note-in" />
              <div class="note-btns">
                <el-button size="small" type="primary" @click="saveNote(row)">保存</el-button>
                <el-button size="small" @click="editing = null">取消</el-button>
              </div>
            </template>
            <div v-else class="note-line">
              <span class="note-tag" :class="{ has: row.note }" :title="row.note ? `${row.note_by || ''} ${fmtFull(row.note_at)}` : ''"
                    @click="editNote(row)">备注:</span>
              <span v-if="row.note" class="note">{{ row.note }}</span>
              <el-button v-else link type="primary" size="small" @click="editNote(row)">添加</el-button>
            </div>
            <div v-if="row.tag_ids.length" class="flags">
              <span v-for="t in row.tag_ids" :key="t" class="flag"><i class="dot" :style="dotStyle(common.tagOf(t)?.color)" />{{ common.tagOf(t)?.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="SKU" width="120">
          <template #default="{ row }">
            <div v-for="s in skus(row)" :key="s" class="mono">{{ s }}</div>
            <span v-if="!skus(row).length" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="纠纷编号" width="115">
          <template #default="{ row }">
            <template v-if="mainLink(row)">
              <a v-if="mainLink(row)!.action_url" class="dotted lnk" :href="mainLink(row)!.action_url!" target="_blank" rel="noopener"
                 :title="`在 eBay 打开这个${mainLink(row)!.kind_label}`">{{ mainLink(row)!.external_id }}</a>
              <span v-else class="dotted">{{ mainLink(row)!.external_id }}</span>
              <div class="muted">{{ mainLink(row)!.kind_label }}<template v-if="row.refund!.linked.length > 1"> 等 {{ row.refund!.linked.length }} 条</template></div>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="退款单类型" width="92">
          <template #default="{ row }">
            <el-tag v-if="row.refund" size="small" effect="plain" :type="TYPE_TAG[row.refund.type]">{{ row.refund.type_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="客户ID / 卖家账号" min-width="120">
          <template #default="{ row }">
            <div>{{ row.buyer || '—' }}<el-tag v-if="row.blacklisted" size="small" type="danger" effect="plain" class="bl">黑名单</el-tag></div>
            <div class="muted">{{ row.store_name }}</div>
          </template>
        </el-table-column>
        <el-table-column label="退款原因" min-width="100">
          <template #default="{ row }">
            <span :title="row.refund?.reason || ''">{{ reasonText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="退款金额 / 类型" width="132">
          <template #default="{ row }">
            <div v-if="localMoney(row)" class="amt">{{ localMoney(row) }}</div>
            <div v-else class="amt" title="退给买家的金额暂时没有，这是 eBay 扣回费用后的结算净额">{{ money(row.amount, row.currency) }}<span class="est">结算净额</span></div>
            <div class="muted" :title="row.refund?.estimated ? '旧数据只有 eBay 结算净额，按比例估算；之后拉到的退款单会显示准确金额' : ''">
              {{ row.refund?.full_label || '全额 / 部分未知' }}<span v-if="row.refund?.estimated && row.refund.full_label" class="est">估算</span></div>
            <div v-if="localMoney(row) && row.currency !== row.refund?.local_currency" class="muted small" title="eBay 结算币种">
              结算 {{ money(row.amount, row.currency) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="退款时间 / 状态" width="140">
          <template #default="{ row }">
            <div>{{ fmtFull(row.opened_at) }}</div>
            <div class="muted" :title="`eBay 交易号 ${row.external_id}`">{{ row.status_label || row.status || '—' }}</div>
            <el-tag size="small" :type="stateType(row)" effect="plain">{{ stateText(row) }}</el-tag>
            <span v-if="row.operator || row.worked_by" class="muted small"> {{ row.operator || row.worked_by }}</span>
            <div v-if="row.refund?.request" class="muted small" :title="`本系统退款申请 #${row.refund.request.id}`">
              提交 {{ row.refund.request.submitted_by }}<template v-if="row.refund.request.reviewed_by"> · 审核 {{ row.refund.request.reviewed_by }}</template></div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="104" fixed="right">
          <template #default="{ row }">
            <div class="ops">
              <el-button v-if="row.conversation_id" size="small" @click="openConv(row)">查看消息</el-button>
              <el-button size="small" @click="openDetail(row)">查看退款单</el-button>
            </div>
            <button class="star" :class="{ on: row.starred }" @click="common.star(row)">{{ row.starred ? '★ 已星标' : '☆ 星标' }}</button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="rows.length"
                     layout="total, sizes, prev, pager, next, jumper" background small />
    </div>
    <p class="muted foot">退款单是 eBay 财务接口的退款流水（近 90 天）。退款单类型、纠纷编号、退款原因、全额 / 部分按同一订单的退货、纠纷、取消和订单金额推算；
      处理状态、标签、备注只在本系统，同事共用。</p>
    </template>

    <!-- ======== 退款单弹窗 ======== -->
    <el-dialog :model-value="!!detail" title="退款单" width="min(560px, calc(100vw - 24px))" append-to-body @update:model-value="detail = null">
      <div v-if="detail" class="dl">
        <div class="k">订单编号</div><div class="v">{{ detail.order_id || '—' }}<button v-if="detail.order_id" class="copy" @click="copyText(detail.order_id)">⧉</button></div>
        <div class="k">SKU</div><div class="v mono">{{ skus(detail).join(' + ') || '—' }}</div>
        <div class="k">退款单类型</div><div class="v">{{ detail.refund?.type_label || '—' }}</div>
        <div class="k">退款类型</div>
        <div class="v">{{ detail.refund?.full_label || '无法判断（缺当地币种金额或订单总额）' }}
          <span v-if="detail.refund?.estimated" class="muted">（估算：这条是旧数据，只有扣回费用后的结算净额，按比例推算）</span></div>
        <div class="k">退款金额</div>
        <div class="v">
          <b v-if="localMoney(detail)">{{ localMoney(detail) }}</b>
          <span v-else class="muted">退给买家的金额暂无（之后拉到的退款单会有）</span>
          <span class="muted">（eBay 结算净额 {{ money(detail.amount, detail.currency) }}）</span>
          <div v-if="detail.refund?.order_total != null" class="muted">订单总额 {{ money(detail.refund.order_total, detail.refund.order_currency) }}
            <template v-if="detail.refund.refunds_in_order > 1"> · 这个订单共有 {{ detail.refund.refunds_in_order }} 笔退款</template></div>
          <div class="muted small">eBay 结算金额已扣回费用，所以比退给买家的金额少</div>
        </div>
        <div class="k">eBay 交易号</div><div class="v mono">{{ detail.external_id }}<button class="copy" @click="copyText(detail.external_id)">⧉</button></div>
        <div class="k">退款原因</div><div class="v">{{ reasonText(detail) }}<span v-if="detail.refund?.reason" class="muted"> （{{ detail.refund.reason }}）</span></div>
        <div class="k">关联售后</div>
        <div class="v">
          <div v-for="l in detail.refund?.linked || []" :key="l.id" class="link-row">
            <span class="muted">{{ l.kind_label }}</span>
            <a v-if="l.action_url" :href="l.action_url" target="_blank" rel="noopener">{{ l.external_id }}</a><span v-else>{{ l.external_id }}</span>
            <span v-if="l.reason_label || l.reason">· {{ l.reason_label || l.reason }}</span>
            <span v-if="l.status_label || l.status" class="muted">· {{ l.status_label || l.status }}</span>
            <span v-if="l.amount != null" class="muted" title="eBay 上这条记录的金额（预估退款额，不一定是实际退的）">· {{ money(l.amount, l.currency) }}</span>
          </div>
          <span v-if="!detail.refund?.linked.length" class="muted">没有（卖家直接退款）</span>
        </div>
        <div class="k">退款时间</div><div class="v">{{ fmtFull(detail.opened_at) }} · {{ detail.status_label || detail.status }}</div>
        <template v-if="detail.refund?.request">
          <div class="k">本系统申请</div>
          <div class="v">#{{ detail.refund.request.id }} · 提交 {{ detail.refund.request.submitted_by }} {{ fmtFull(detail.refund.request.submitted_at) }}
            <template v-if="detail.refund.request.reviewed_by"> · 审核 {{ detail.refund.request.reviewed_by }}</template>
            <div v-if="detail.refund.request.comment" class="muted">给买家的说明：{{ detail.refund.request.comment }}</div></div>
        </template>
        <div class="k">客户 / 店铺</div><div class="v">{{ detail.buyer || '—' }} · {{ detail.store_name }}</div>
        <div v-if="detail.refund?.memo" class="k">eBay 说明</div><div v-if="detail.refund?.memo" class="v">{{ detail.refund.memo }}</div>
        <div class="k">备注</div>
        <div class="v"><el-input :model-value="detail.note || ''" type="textarea" :rows="3" readonly placeholder="没有备注（在列表的订单编号下面添加）" /></div>
      </div>
      <template #footer>
        <el-button v-if="detail?.conversation_id" type="primary" @click="openConv(detail!)">查看消息</el-button>
        <el-button @click="detail = null">取消</el-button>
      </template>
    </el-dialog>

    <CaseTagDialog v-model="tagDialog" :tags="tags" title="退款单" :is-admin="isAdmin" @create="common.createTag"
                   @update="common.updateTag" @remove="common.deleteTag" />
  </div>
</template>

<style scoped>
.panels {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 10px 16px 0;
  border-bottom: 1px solid var(--line);
}
.panels button {
  height: 34px;
  padding: 0 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
}
.panels button.on {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.panels .small {
  margin-left: 8px;
  font-size: 12px;
  font-weight: normal;
}
.badge {
  display: inline-block;
  min-width: 16px;
  margin-left: 4px;
  padding: 0 5px;
  border-radius: 8px;
  background: var(--danger-text, #e5484d);
  color: #fff;
  font-size: 11px;
  line-height: 16px;
}
.rf {
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  min-height: 100%;
}
.filters {
  padding: 12px 16px 6px;
}
.f-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.search {
  display: flex;
  align-items: center;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}
.search :deep(.el-select__wrapper),
.search :deep(.el-input__wrapper) {
  box-shadow: none !important;
  background: transparent;
}
.s-field {
  width: 110px;
}
.s-input {
  width: 220px;
  border-left: 1px solid var(--line);
}
.s-match {
  width: 76px;
}
.f-sel {
  width: 130px;
}
.f-sel.w-s {
  width: 110px;
}
.f-sel.w-store {
  width: 170px;
}
.f-date {
  width: 230px !important;
}
.star-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--panel);
  color: var(--muted);
  cursor: pointer;
}
.star-btn.on {
  color: #f5a623;
  border-color: #f5a623;
}
.tag-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-top: 10px;
}
.tag-title {
  color: var(--muted);
  font-size: 13px;
}
.chip {
  height: 26px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--panel);
  font-family: inherit;
  font-size: 12px;
  color: var(--text-2);
  cursor: pointer;
}
.chip.on {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.chip .n {
  color: var(--muted);
}
.dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 4px;
}
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
}
.tabs > button {
  height: 40px;
  padding: 0 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-family: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.tabs > button .n,
.types button .n {
  margin-left: 2px;
  font-size: 12px;
  color: var(--muted);
}
.tabs > button.on,
.tabs > button.on .n {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.types {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 12px 16px 0;
}
.types button {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--line);
  background: var(--panel);
  font-family: inherit;
  font-size: 12px;
  color: var(--text);
  cursor: pointer;
}
.types button:first-child {
  border-radius: 3px 0 0 3px;
}
.types button + button {
  margin-left: -11px;
}
.types button:nth-child(4) {
  border-radius: 0 3px 3px 0;
}
.types button.on,
.types button.on .n {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-soft);
  position: relative;
}
.hint {
  font-size: 12px;
}
.est {
  margin-left: 4px;
  padding: 0 3px;
  border: 1px solid var(--line);
  border-radius: 2px;
  font-size: 11px;
}
.spacer {
  flex: 1;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
}
.toolbar .el-button + .el-button,
.toolbar .el-dropdown + .el-button {
  margin-left: 0;
}
.arr {
  margin-left: 6px;
  font-size: 10px;
  color: var(--muted);
}
.picked {
  font-size: 12px;
  color: var(--accent);
}
.total {
  font-size: 12px;
  color: var(--muted);
}
.table-wrap {
  padding: 0 16px;
}
.table-wrap :deep(td.el-table__cell) {
  vertical-align: top;
}
.copy {
  border: 0;
  background: none;
  padding: 0 0 0 4px;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
}
.dotted {
  border-bottom: 1px dashed var(--muted);
}
.lnk {
  color: var(--accent);
  text-decoration: none;
}
.new {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 2px;
  background: var(--danger-text, #e5484d);
  color: #fff;
  font-size: 11px;
}
.mono {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 12px;
}
.small {
  font-size: 11px;
}
.amt {
  font-weight: 600;
}
.bl {
  margin-left: 4px;
}
.note-line {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-top: 6px;
  font-size: 12px;
}
.note-tag {
  flex: none;
  padding: 0 4px;
  border-radius: 2px;
  background: var(--sunken);
  color: var(--muted);
  cursor: pointer;
}
.note-tag.has {
  background: #f9e3a8;
  color: #6b4b00;
}
.note {
  white-space: pre-wrap;
  word-break: break-word;
}
.note-in {
  margin-top: 6px;
}
.note-btns {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
.flags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.flag {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  color: var(--text-2);
}
.ops {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}
.ops .el-button + .el-button {
  margin-left: 0;
}
.star {
  border: 0;
  background: none;
  padding: 0;
  margin-top: 6px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}
.star.on {
  color: #f5a623;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px 4px;
}
.foot {
  padding: 0 16px 12px;
  font-size: 12px;
}
.dl {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 10px 14px;
  font-size: 13px;
}
.dl .k {
  color: var(--muted);
  text-align: right;
}
.dl .v {
  min-width: 0;
  word-break: break-word;
}
.link-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.link-row a {
  color: var(--accent);
}
</style>
