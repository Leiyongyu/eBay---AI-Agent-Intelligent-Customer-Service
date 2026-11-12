<script setup lang="ts">
// 售后管理：咨询（未收到货）、纠纷、退货、付款纠纷、取消交易、退款单（评价管理见 FeedbackView）。
// eBay 上的数据定时拉取、只读；本系统另外记处理状态、星标、标签、备注、操作员，处理动作请到 eBay 后台或买家会话
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText } from '../format'
import { copyText, downloadCsv, fmtFull, fmtRemain, money, useCases } from '../caseCommon'
import type { CaseOptions, CaseRow, CaseTag, ViewCounts } from '../caseCommon'
import CaseTagDialog from '../components/CaseTagDialog.vue'
import CaseTagFilter from '../components/CaseTagFilter.vue'
import { dotStyle, tagHex } from '../tagColor'
import FeedbackView from './FeedbackView.vue'

const props = defineProps<{ kind?: string }>()
const emit = defineEmits<{ open: [id: number] }>()
const kind = computed(() => props.kind || 'INQUIRY')
const isAdmin = computed(() => session.user?.role === 'ADMIN')

// 每个类型的叫法
const META: Record<string, { label: string; id: string; reason: string; amount: string; hold?: boolean }> = {
  INQUIRY: { label: '咨询', id: '咨询编号', reason: '咨询类型', amount: '索赔金额' },
  CASE: { label: '纠纷', id: '纠纷编号', reason: '纠纷类型', amount: '纠纷金额' },
  RETURN: { label: '退货', id: '退货编号', reason: '开启原因', amount: '退款金额', hold: true },
  PAYMENT_DISPUTE: { label: '付款纠纷', id: '纠纷编号', reason: '纠纷原因', amount: '纠纷金额' },
  CANCEL: { label: '取消交易', id: '取消编号', reason: '取消原因', amount: '退款金额' },
  REFUND: { label: '退款单', id: '交易编号', reason: '原因', amount: '退款金额' },
}
const meta = computed(() => META[kind.value] || META.INQUIRY)
const SEARCH_FIELDS = computed(() => [
  { value: 'external_id', label: meta.value.id }, { value: 'order_id', label: '订单编号' },
  { value: 'buyer', label: '客户ID' }, { value: 'item_id', label: 'Item ID' }, { value: 'title', label: '商品标题' },
])
const MATCH = [{ value: 'prefix', label: '开头' }, { value: 'contains', label: '包含' }, { value: 'exact', label: '精确' }]
const VIEWS = [
  { value: 'all', label: '全部' }, { value: 'pending', label: '待处理' },
  { value: 'processing', label: '处理中' }, { value: 'done', label: '已处理' },
]

// ---------------- 筛选 ----------------
const f = reactive({
  q: '', qField: 'external_id', match: 'prefix', storeIds: [] as string[], reason: '', status: '',
  tagIds: [] as string[], tagMode: 'any' as 'any' | 'all', operator: '', hold: '', opened: null as [string, string] | null,
  updated: null as [string, string] | null, starred: false,
})
const view = ref('pending')
const applied = ref({ ...f })
const rows = ref<CaseRow[]>([])
const counts = ref<ViewCounts | null>(null)
const opts = ref<CaseOptions | null>(null)
const loading = ref(false)
const sync = ref<{ last_at: string | null; running: boolean } | null>(null)

function query(): URLSearchParams {
  const a = applied.value
  const q = new URLSearchParams({ kind: kind.value, view: view.value })
  if (a.q.trim()) {
    q.set('q', a.q)
    q.set('q_field', a.qField)
    q.set('match', a.match)
  }
  if (a.storeIds.length) q.set('store_ids', a.storeIds.join(','))
  if (a.reason) q.set('reason', a.reason)
  if (a.status) q.set('status', a.status)
  if (a.tagIds.length) {
    q.set('tag_ids', a.tagIds.join(','))
    q.set('tag_mode', a.tagMode)
  }
  if (a.operator) q.set('operator', a.operator)
  if (a.hold) q.set('hold_reason', a.hold)
  if (a.starred) q.set('starred', 'true')
  if (a.opened) {
    q.set('opened_from', a.opened[0])
    q.set('opened_to', a.opened[1])
  }
  if (a.updated) {
    q.set('updated_from', a.updated[0])
    q.set('updated_to', a.updated[1])
  }
  return q
}

async function load() {
  loading.value = true
  try {
    const sq = applied.value.storeIds.length === 1 ? `&store_id=${applied.value.storeIds[0]}` : ''
    const [list, c] = await Promise.all([
      get<CaseRow[]>(`/internal/ebay-cases?${query()}`),
      get<ViewCounts>(`/internal/ebay-cases/view-counts?kind=${kind.value}${sq}`),
    ])
    rows.value = list
    counts.value = c
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
async function loadMeta() {
  const [o, s] = await Promise.all([
    get<CaseOptions>(`/internal/ebay-cases/options?kind=${kind.value}`).catch(() => null),
    get<{ sync: { last_at: string | null; running: boolean } }>('/internal/ebay-cases/counts').catch(() => null),
  ])
  opts.value = o
  sync.value = s?.sync || null
}
const common = useCases(() => kind.value, load)
const { tags, extras, busy } = common

function search() {
  applied.value = { ...f, storeIds: [...f.storeIds], tagIds: [...f.tagIds] }
  page.value = 1
  load()
}
function reset() {
  Object.assign(f, { q: '', reason: '', status: '', tagIds: [], tagMode: 'any', operator: '', hold: '', opened: null, updated: null,
    starred: false, storeIds: [] })
  search()
}
/** 快捷标签栏：点一下只看这个标签，再点一下取消 */
function quickTag(id: string) {
  f.tagIds = applied.value.tagIds.length === 1 && applied.value.tagIds[0] === id ? [] : [id]
  search()
}
const pinnedTags = computed(() => tags.value.filter((t) => t.pinned))
async function createFromFilter(name: string, color: string, done: (t: CaseTag | null) => void) {
  done(await common.createTag(name, color))
}
function quickStar() {
  f.starred = !f.starred
  search()
}
watch(view, () => {
  page.value = 1
  selected.value = []
  load()
})
watch(kind, () => {
  reset()
  common.loadTags()
  loadMeta()
})

// ---------------- 分页、勾选 ----------------
const page = ref(1)
const pageSize = ref(20)
const pageRows = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(pageRows, (l) => common.want(l), { immediate: true })
const selected = ref<number[]>([])
const allChecked = computed(() => pageRows.value.length > 0 && pageRows.value.every((r) => selected.value.includes(r.id)))
function toggleAll(v: boolean) {
  const ids = pageRows.value.map((r) => r.id)
  selected.value = v ? [...new Set([...selected.value, ...ids])] : selected.value.filter((x) => !ids.includes(x))
}
function toggle(id: number, v: boolean) {
  selected.value = v ? [...selected.value, id] : selected.value.filter((x) => x !== id)
}

// ---------------- 批量操作 ----------------
const sel = () => selected.value
async function setState(state: string, label: string, hold?: string) {
  await common.batch(sel(), { action: 'state', state, hold_reason: hold }, label)
  selected.value = []
}
async function holdSelected() {
  if (!sel().length) return ElMessage.warning('请先勾选记录')
  try {
    const { value } = await ElMessageBox.prompt('暂不处理的原因（如 等买家寄回、等仓库确认）', '暂不处理', {
      inputPattern: /\S/, inputErrorMessage: '请写一下原因', inputValue: opts.value?.hold_reasons[0] || '',
    })
    await setState('HOLD', '已暂不处理', value.trim())
  } catch {
    /* 取消 */
  }
}
function onTag(cmd: string) {
  const [op, id] = cmd.split(':')
  common.batch(sel(), { action: op === 'add' ? 'tag_add' : 'tag_remove', tag_id: Number(id) }, op === 'add' ? '已加标签' : '已去标签')
}
function onBatch(cmd: string) {
  if (cmd === 'claim') common.batch(sel(), { action: 'claim' }, '已认领')
  else if (cmd === 'release') common.batch(sel(), { action: 'release' }, '已释放')
  else if (cmd === 'star') common.batch(sel(), { action: 'star' }, '已加星标')
  else if (cmd === 'unstar') common.batch(sel(), { action: 'unstar' }, '已取消星标')
  else if (cmd === 'pending') setState('PENDING', '已改回待处理')
  else if (cmd === 'processing') setState('PROCESSING', '已标为处理中')
}
async function removeSelected() {
  if (!sel().length) return ElMessage.warning('请先勾选记录')
  try {
    await ElMessageBox.confirm(`删除勾选的 ${sel().length} 条？只在本系统里隐藏，eBay 上不受影响，之后再拉取也不会恢复。`, '删除', { type: 'warning' })
  } catch {
    return
  }
  await common.batch(sel(), { action: 'hide' }, '已删除')
  selected.value = []
}
function exportCsv() {
  const list = selected.value.length ? rows.value.filter((r) => selected.value.includes(r.id)) : rows.value
  const head = [meta.value.id, '店铺', '客户ID', '订单编号', 'Item ID', '平台SKU', '库存SKU', '数量', '单价', meta.value.amount,
    meta.value.reason, 'eBay 状态', '处理状态', '暂不处理原因', '标签', '星标', '操作员', '创建时间', '截止时间', '备注']
  const STATE: Record<string, string> = { pending: '待处理', processing: '处理中', done: '已处理' }
  downloadCsv(`${meta.value.label}_${fmtFull(new Date().toISOString()).slice(0, 10)}.csv`, head, list.map((r) => {
    const x = extras[r.id]
    return [r.external_id, r.store_name, r.buyer, r.order_id, r.item_id, x?.sku, x?.stock_sku, x?.quantity,
      x?.price ? `${x.price.value} ${x.price.currency}` : '', money(r.amount, r.currency), r.reason_label || r.reason, r.status_label || r.status,
      STATE[r.view], r.hold_reason, r.tag_ids.map((t) => common.tagOf(t)?.name).filter(Boolean).join('、'),
      r.starred ? '是' : '', r.operator, fmtFull(r.opened_at), fmtFull(r.due_at), r.note]
  }))
}

const syncing = ref(false)
async function syncNow() {
  syncing.value = true
  try {
    const r = await post<{ started: boolean }>('/internal/ebay-cases/sync')
    ElMessage.success(r.started ? '已开始从 eBay 拉取（全部店铺约需几分钟），完成后自动刷新' : '正在拉取中，请稍候')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    syncing.value = false
  }
}

const tagDialog = ref(false)

// ---------------- 备注、详情 ----------------
const noteOpen = ref<Record<number, boolean>>({})
const noteEdit = ref<Record<number, string>>({})
const noteExpand = ref<Record<number, boolean>>({})
function editNote(r: CaseRow) {
  noteEdit.value[r.id] = r.note || ''
  noteOpen.value[r.id] = true
}
async function saveNote(r: CaseRow) {
  if (await common.saveNote(r, noteEdit.value[r.id] || '')) noteOpen.value[r.id] = false
}
const detail = ref<CaseRow | null>(null)
async function openDetail(r: CaseRow) {
  detail.value = r
  if (r.is_new) {
    post(`/internal/ebay-cases/${r.id}/seen`).then(() => (r.is_new = false)).catch(() => null)
  }
}
// eBay 状态里的“OPEN / WAITING…”等是 eBay 原样状态；“最新方案”取卖家需要做的动作或最近状态
const WHO: Record<string, string> = { SELLER: '卖家', BUYER: '买家', EBAY: 'eBay', SYSTEM: '系统' }
const detailZh = (r: CaseRow, key: string) => r.detail_labels?.find((x) => x.key === key)?.value_label ?? r.detail?.[key]
const latestAction = (r: CaseRow) => {
  const who = r.detail?.seller_action ? 'SELLER' : r.detail?.requestor || 'EBAY'
  const what = r.detail?.seller_action ? detailZh(r, 'seller_action') : r.detail?.close_reason ? detailZh(r, 'close_reason')
    : r.status_label || r.status
  return what ? { who: WHO[who] || who, what } : null
}
const isEscalated = (r: CaseRow) => /ESCALAT/i.test(`${r.status} ${JSON.stringify(r.detail || {})}`)
const remainClass = (r: CaseRow) => (r.overdue ? 'late' : r.due_soon ? 'soon' : '')

let timer: number | undefined
onMounted(async () => {
  common.loadTags()
  loadMeta()
  await load()
  timer = window.setInterval(() => {
    loadMeta()
    if (sync.value?.running) load()
  }, 20000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <FeedbackView v-if="kind === 'FEEDBACK'" @open="(id: number) => emit('open', id)" />
  <div v-else class="ec">
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
        <el-select v-model="f.storeIds" class="f-sel w-store" placeholder="eBay账号" multiple collapse-tags clearable filterable>
          <el-option v-for="s in opts?.stores || []" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select v-model="f.reason" class="f-sel" :placeholder="meta.reason" clearable filterable>
          <el-option v-for="r in opts?.reasons || []" :key="r" :label="opts?.reason_labels?.[r] || r" :value="r" />
        </el-select>
        <el-select v-model="f.status" class="f-sel" placeholder="状态" clearable filterable>
          <el-option v-for="s in opts?.statuses || []" :key="s" :label="opts?.status_labels?.[s] || s" :value="s" />
        </el-select>
        <CaseTagFilter v-model="f.tagIds" v-model:mode="f.tagMode" :tags="tags" :counts="counts?.tags" :is-admin="isAdmin"
                       @create="createFromFilter" @pin="(t, v) => common.updateTag(t, { pinned: v })" @manage="tagDialog = true" @apply="search" />
        <el-select v-if="meta.hold" v-model="f.hold" class="f-sel" placeholder="暂不处理原因" clearable filterable>
          <el-option v-for="h in opts?.hold_reasons || []" :key="h" :label="h" :value="h" />
        </el-select>
        <el-date-picker v-model="f.opened" class="f-date" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                        start-placeholder="创建时间" end-placeholder="结束日期" />
        <el-date-picker v-model="f.updated" class="f-date" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                        start-placeholder="最后处理" end-placeholder="结束日期" />
        <el-select v-model="f.operator" class="f-sel w-s" placeholder="操作员" clearable filterable>
          <el-option label="未认领" value="unassigned" />
          <el-option v-for="o in opts?.operators || []" :key="o" :label="o" :value="o" />
        </el-select>
        <el-tooltip content="只看星标" placement="top">
          <button class="star-btn" :class="{ on: f.starred }" @click="quickStar">★</button>
        </el-tooltip>
        <el-button class="btn-search" plain type="primary" @click="search">搜索</el-button>
        <el-button link @click="reset">重置</el-button>
      </div>
      <div class="tag-row">
        <span class="tag-title">标签:</span>
        <button class="chip" :class="{ on: applied.starred }" @click="quickStar">
          <span class="star">★</span>星标<span class="n">({{ counts?.starred ?? 0 }})</span>
        </button>
        <button v-for="t in pinnedTags" :key="t.id" class="chip" :class="{ on: applied.tagIds.includes(String(t.id)) }" @click="quickTag(String(t.id))">
          <i class="dot" :style="dotStyle(t.color)" />{{ t.name }}
          <span class="n">({{ counts?.tags[String(t.id)] || 0 }})</span>
        </button>
        <span v-if="!pinnedTags.length" class="muted">{{ tags.length ? '没有固定的标签，' : '快速访问标签，' }}</span>
        <el-button link type="primary" size="small" @click="tagDialog = true">{{ tags.length ? '标签管理' : '新建标签' }}</el-button>
      </div>
    </div>

    <!-- ======== 页签 ======== -->
    <div class="tabs">
      <button v-for="v in VIEWS" :key="v.value" :class="{ on: view === v.value }" @click="view = v.value">
        {{ v.label }}<span class="n">({{ counts?.views[v.value] ?? 0 }})</span>
      </button>
    </div>

    <!-- ======== 工具栏 ======== -->
    <div class="toolbar">
      <el-button v-if="meta.hold" :disabled="busy" @click="holdSelected">暂不处理</el-button>
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
      <el-button :disabled="busy" @click="setState('DONE', '已标为已处理')">标为已处理</el-button>
      <el-dropdown trigger="click" :disabled="busy" @command="onBatch">
        <el-button>批处理<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="processing">标为处理中</el-dropdown-item>
            <el-dropdown-item command="pending">改回待处理</el-dropdown-item>
            <el-dropdown-item command="claim" divided>认领给我</el-dropdown-item>
            <el-dropdown-item command="release">释放认领</el-dropdown-item>
            <el-dropdown-item command="star" divided>加星标</el-dropdown-item>
            <el-dropdown-item command="unstar">取消星标</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button @click="exportCsv">按模板导出</el-button>
      <el-button :disabled="busy" @click="removeSelected">删除</el-button>
      <span v-if="selected.length" class="picked">已选 {{ selected.length }} 条</span>
      <span class="spacer" />
      <span class="muted small">eBay 数据更新于 {{ fmtFull(sync?.last_at) }}{{ sync?.running ? '（拉取中…）' : '' }}</span>
      <el-button v-if="isAdmin" size="small" :loading="syncing || sync?.running" @click="syncNow">手动同步eBay</el-button>
      <span class="total">共{{ rows.length }}条记录</span>
    </div>

    <!-- ======== 列表（每条两行：编号行 + 明细行，下面是备注） ======== -->
    <div v-loading="loading && !rows.length" class="tbl-wrap">
      <table class="tbl">
        <colgroup>
          <col style="width: 36px" /><col style="width: 25%" /><col style="width: 11%" /><col style="width: 11%" />
          <col style="width: 9%" /><col style="width: 6%" /><col style="width: 7%" />
          <col v-if="meta.hold" style="width: 8%" /><col style="width: 12%" /><col style="width: 12%" />
          <col style="width: 6%" /><col style="width: 80px" />
        </colgroup>
        <thead>
          <tr>
            <th><el-checkbox :model-value="allChecked" @update:model-value="(v: any) => toggleAll(!!v)" /></th>
            <th>产品信息</th><th>类型/状态</th><th>客户ID/卖家账号</th><th>订单编号</th><th>单价</th><th>{{ meta.amount }}</th>
            <th v-if="meta.hold">开启/暂不处理原因</th><th>最新创建</th><th>最新方案</th><th>操作员</th><th>操作</th>
          </tr>
        </thead>
        <tbody v-for="r in pageRows" :key="r.id" class="rec" :class="{ checked: selected.includes(r.id) }">
          <tr class="head-row">
            <td><el-checkbox :model-value="selected.includes(r.id)" @update:model-value="(v: any) => toggle(r.id, !!v)" /></td>
            <td :colspan="meta.hold ? 11 : 10">
              <div class="head-flex">
              <span class="rec-id">{{ meta.id }}: <b class="dotted">{{ r.external_id }}</b></span>
              <button class="copy" title="复制编号" @click="copyText(r.external_id)">⧉</button>
              <button class="star" :class="{ on: r.starred }" :title="r.starred ? '取消星标' : '加星标'" @click="common.star(r)">{{ r.starred ? '★' : '☆' }}</button>
              <span v-if="r.is_new" class="flag new">新</span>
              <span v-if="isEscalated(r)" class="flag esc">已升级</span>
              <span v-if="r.view === 'done'" class="flag done">已处理</span>
              <span v-else-if="r.work_state === 'HOLD'" class="flag hold">暂不处理</span>
              <span v-else-if="r.view === 'processing'" class="flag proc">处理中</span>
              <span v-if="r.blacklisted" class="flag bl">黑名单</span>
              <span v-for="t in r.tag_ids" :key="t" class="flag" :style="{ background: tagHex(common.tagOf(t)?.color) || 'var(--muted)' }">{{ common.tagOf(t)?.name }}</span>
              <span class="remain" :class="remainClass(r)">
                <template v-if="r.remaining_seconds != null">剩余处理时间: {{ fmtRemain(r.remaining_seconds) }}</template>
              </span>
              </div>
            </td>
          </tr>
          <tr class="main-row">
            <td />
            <td>
              <div class="prod">
                <img v-if="extras[r.id]?.picture_url" :src="extras[r.id]!.picture_url!" alt="" referrerpolicy="no-referrer" loading="lazy" />
                <span v-else class="ph" aria-hidden="true" />
                <div class="prod-text">
                  <div><b>{{ extras[r.id]?.sku || '—' }}</b><span class="qty">× {{ extras[r.id]?.quantity ?? 1 }}</span>
                    <button v-if="extras[r.id]?.sku" class="copy" title="复制 SKU" @click="copyText(extras[r.id]!.sku)">⧉</button>
                  </div>
                  <div class="clamp" :title="extras[r.id]?.title || r.title || ''">{{ extras[r.id]?.title || r.title || '—' }}</div>
                  <div v-if="r.item_id" class="muted">{{ r.item_id }}<button class="copy" title="复制 Item ID" @click="copyText(r.item_id)">⧉</button></div>
                </div>
              </div>
            </td>
            <td>
              <div :title="r.reason || ''">{{ r.reason_label || r.reason || detailZh(r, 'type') || '—' }}</div>
              <div class="red" :title="r.status || ''">{{ r.status_label || r.status || '' }}</div>
            </td>
            <td>
              <div>{{ r.buyer || '—' }}
                <button v-if="r.conversation_id" class="contact" title="打开和这个买家的会话" @click="emit('open', r.conversation_id!)">联系TA</button>
              </div>
              <div class="muted">{{ r.store_name }}</div>
            </td>
            <td><span class="dotted">{{ extras[r.id]?.order_id || r.order_id || '—' }}</span></td>
            <td>{{ extras[r.id]?.price ? `${extras[r.id]!.price!.value} ${extras[r.id]!.price!.currency}` : '—' }}</td>
            <td>{{ money(r.amount, r.currency) }}</td>
            <td v-if="meta.hold">
              <div>{{ r.reason_label || r.reason || '—' }}</div>
              <div v-if="r.hold_reason" class="warn">暂不处理：{{ r.hold_reason }}</div>
            </td>
            <td class="times">
              <div>创建: {{ fmtFull(r.opened_at) }}</div>
              <div v-if="r.status_changed_at && r.status_changed_at !== r.opened_at">更新: {{ fmtFull(r.ebay_updated_at || r.status_changed_at) }}</div>
              <div v-if="r.due_at" class="muted">截止: {{ fmtFull(r.due_at) }}</div>
            </td>
            <td>
              <template v-if="latestAction(r)"><span class="muted">{{ latestAction(r)!.who }}:</span> {{ latestAction(r)!.what }}</template>
              <a v-if="r.detail?.action_url" class="ebay-link" :href="r.detail.action_url" target="_blank" rel="noopener noreferrer">去 eBay 处理 ↗</a>
            </td>
            <td>{{ r.operator || '' }}</td>
            <td><el-button size="small" @click="openDetail(r)">查看详情</el-button></td>
          </tr>
          <tr class="note-row">
            <td />
            <td :colspan="meta.hold ? 11 : 10">
              <template v-if="noteOpen[r.id]">
                <div class="note-edit">
                  <el-input v-model="noteEdit[r.id]" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" maxlength="2000"
                            placeholder="内部备注，同事都能看到，如：11.10到期 协商重发" />
                  <el-button size="small" type="primary" @click="saveNote(r)">保存</el-button>
                  <el-button size="small" @click="noteOpen[r.id] = false">取消</el-button>
                </div>
              </template>
              <template v-else>
                <span class="ok">✓</span><span class="muted">备注:</span>
                <span v-if="r.note" class="note" :class="{ clamp1: !noteExpand[r.id] }" :title="`${r.note_by || ''} ${fmtFull(r.note_at)}`">{{ r.note }}</span>
                <el-button v-if="r.note && r.note.length > 60" link type="primary" size="small" @click="noteExpand[r.id] = !noteExpand[r.id]">
                  {{ noteExpand[r.id] ? '收起' : '展开' }}
                </el-button>
                <el-button link type="primary" size="small" @click="editNote(r)">{{ r.note ? '编辑' : '添加备注' }}</el-button>
              </template>
            </td>
          </tr>
        </tbody>
        <tbody v-if="!loading && !rows.length">
          <tr><td :colspan="meta.hold ? 12 : 11" class="empty">没有记录</td></tr>
        </tbody>
      </table>
    </div>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="rows.length"
                     layout="total, sizes, prev, pager, next, jumper" background small />
    </div>

    <CaseTagDialog v-model="tagDialog" :tags="tags" :title="meta.label" :is-admin="isAdmin" @create="common.createTag"
                   @update="common.updateTag" @remove="common.deleteTag" />

    <!-- ======== 详情 ======== -->
    <el-drawer :model-value="!!detail" :title="detail ? `${meta.id} ${detail.external_id}` : ''" size="520px" append-to-body
               @close="detail = null">
      <template v-if="detail">
        <table class="kv">
          <tbody>
            <tr><th>店铺</th><td>{{ detail.store_name }}</td></tr>
            <tr><th>客户ID</th><td>{{ detail.buyer || '—' }}
              <el-button v-if="detail.conversation_id" link type="primary" size="small" @click="emit('open', detail.conversation_id!)">打开会话</el-button></td></tr>
            <tr><th>订单编号</th><td>{{ extras[detail.id]?.order_id || detail.order_id || '—' }}</td></tr>
            <tr><th>商品</th><td>{{ extras[detail.id]?.sku || '—' }} · {{ extras[detail.id]?.title || detail.title || '—' }}<div class="muted">{{ detail.item_id }}</div></td></tr>
            <tr><th>{{ meta.reason }}</th><td>{{ detail.reason_label || detail.reason || '—' }}<span v-if="detail.reason" class="muted raw"> {{ detail.reason }}</span></td></tr>
            <tr><th>eBay 状态</th><td class="red">{{ detail.status_label || detail.status || '—' }}<span v-if="detail.status" class="muted raw"> {{ detail.status }}</span></td></tr>
            <tr><th>{{ meta.amount }}</th><td>{{ money(detail.amount, detail.currency) }}</td></tr>
            <tr><th>创建</th><td>{{ fmtFull(detail.opened_at) }}</td></tr>
            <tr><th>处理截止</th><td :class="remainClass(detail)">{{ fmtFull(detail.due_at) }}
              <span v-if="detail.remaining_seconds != null">（{{ fmtRemain(detail.remaining_seconds) }}）</span></td></tr>
            <tr v-if="detail.comment"><th>买家留言</th><td class="pre">{{ detail.comment }}</td></tr>
            <tr><th>本系统状态</th><td>{{ { pending: '待处理', processing: '处理中', done: '已处理' }[detail.view] }}
              <span v-if="detail.hold_reason" class="warn">（暂不处理：{{ detail.hold_reason }}）</span>
              <span v-if="detail.worked_by" class="muted"> · {{ detail.worked_by }} {{ fmtFull(detail.worked_at) }}</span></td></tr>
            <tr><th>操作员</th><td>{{ detail.operator || '未认领' }}</td></tr>
            <tr><th>备注</th><td class="pre">{{ detail.note || '—' }}<div v-if="detail.note_by" class="muted">{{ detail.note_by }} · {{ fmtFull(detail.note_at) }}</div></td></tr>
            <tr v-for="x in detail.detail_labels || []" :key="x.key"><th class="muted">{{ x.key_label }}</th>
              <td class="muted"><a v-if="x.key === 'action_url'" :href="x.value" target="_blank" rel="noopener noreferrer">去 eBay 处理 ↗</a>
                <template v-else>{{ x.value_label }}<span v-if="x.value_label !== x.value" class="raw"> {{ x.value }}</span></template></td></tr>
          </tbody>
        </table>
        <div class="dr-actions">
          <el-button type="primary" @click="common.batch([detail.id], { action: 'state', state: 'DONE' }, '已标为已处理').then(() => (detail = null))">标为已处理</el-button>
          <el-button @click="common.batch([detail.id], { action: 'state', state: 'PROCESSING' }, '已标为处理中').then(() => (detail = null))">标为处理中</el-button>
          <a v-if="detail.detail?.action_url" :href="detail.detail.action_url" target="_blank" rel="noopener noreferrer">
            <el-button>去 eBay 处理 ↗</el-button>
          </a>
        </div>
        <p class="muted">处理状态、备注、标签只在本系统；同意退款、提供运单号等请到 eBay 后台操作。</p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.ec {
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
  width: 112px;
}
.s-input {
  width: 200px;
  border-left: 1px solid var(--line);
  border-right: 1px solid var(--line);
}
.s-match {
  width: 76px;
}
.f-sel {
  width: 118px;
}
.f-sel.w-store {
  width: 170px;
}
.f-sel.w-s {
  width: 100px;
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
  font-size: 12px;
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
.chip .star {
  color: #f5a623;
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
  gap: 4px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
}
.tabs button {
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
.tabs button .n {
  margin-left: 2px;
  font-size: 12px;
  color: var(--muted);
}
.tabs button.on,
.tabs button.on .n {
  color: var(--accent);
  border-bottom-color: var(--accent);
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
.spacer {
  flex: 1;
}
.small {
  font-size: 12px;
}
.total {
  font-size: 12px;
  color: var(--muted);
}
.tbl-wrap {
  flex: 1;
  padding: 0 16px;
  overflow-x: auto;
}
.tbl {
  width: 100%;
  min-width: 1180px;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 12px;
  color: var(--text);
}
.tbl th {
  height: 38px;
  padding: 0 8px;
  text-align: left;
  font-weight: 500;
  font-size: 13px;
  color: var(--text-2);
  background: var(--sunken);
}
.tbl td {
  padding: 8px;
  vertical-align: top;
  word-break: break-word;
}
.head-row td {
  background: var(--sunken);
  border-top: 1px solid var(--line);
  padding: 6px 8px;
  vertical-align: middle;
}
.rec.checked .head-row td {
  background: var(--accent-soft);
}
.rec-id {
  font-size: 13px;
}
.rec-id b {
  font-weight: 500;
}
.head-flex {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 0;
}
.remain {
  margin-left: auto;
  padding-right: 4px;
  white-space: nowrap;
  color: var(--text-2);
}
.remain.soon {
  color: var(--warn-text);
}
.remain.late {
  color: var(--danger);
  font-weight: 600;
}
.flag {
  display: inline-block;
  margin-left: 6px;
  padding: 0 5px;
  height: 18px;
  line-height: 18px;
  border-radius: 2px;
  font-size: 11px;
  color: #fff;
  background: #8a94a6;
}
.flag.new {
  background: var(--danger);
}
.flag.esc {
  background: none;
  color: var(--danger);
  padding: 0;
}
.flag.done {
  background: var(--ok);
}
.flag.proc {
  background: #3a7bfd;
}
.flag.hold {
  background: var(--warn);
}
.flag.bl {
  background: #1f2329;
}
.copy,
.star {
  border: 0;
  background: none;
  padding: 0 3px;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
}
.star {
  font-size: 15px;
}
.star.on {
  color: #f5a623;
}
.copy:hover,
.star:hover {
  color: var(--accent);
}
.main-row td {
  padding-top: 12px;
  padding-bottom: 10px;
}
.prod {
  display: flex;
  gap: 10px;
  min-width: 0;
}
.prod img,
.ph {
  width: 64px;
  height: 64px;
  flex-shrink: 0;
  object-fit: contain;
  border-radius: 3px;
  border: 1px solid var(--line-soft);
  background: var(--sunken);
}
.prod-text {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.qty {
  margin-left: 8px;
  color: var(--text-2);
}
.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.35;
}
.red {
  color: var(--danger);
}
.warn {
  color: var(--warn-text);
}
.contact {
  border: 0;
  background: none;
  padding: 0 0 0 6px;
  font-size: 11px;
  color: var(--accent);
  cursor: pointer;
}
.dotted {
  border-bottom: 1px dashed var(--muted);
}
.times {
  font-variant-numeric: tabular-nums;
}
.ebay-link {
  display: block;
  margin-top: 4px;
  color: var(--accent);
}
.note-row td {
  padding-top: 0;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.note-row td:nth-child(2) {
  border-top: 1px dashed var(--line-soft);
  padding-top: 8px;
}
.ok {
  color: var(--ok);
  margin-right: 4px;
}
.note {
  margin-left: 6px;
  white-space: pre-wrap;
}
.clamp1 {
  display: -webkit-inline-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
  max-width: 70%;
  vertical-align: bottom;
}
.note-edit {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  max-width: 760px;
}
.empty {
  text-align: center;
  padding: 40px 0;
  color: var(--muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px;
}
.kv {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.kv th,
.kv td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--line-soft);
  text-align: left;
  vertical-align: top;
}
.kv th {
  width: 110px;
  font-weight: 400;
  color: var(--text-2);
}
.pre {
  white-space: pre-wrap;
}
.dr-actions {
  display: flex;
  gap: 8px;
  margin: 16px 0 8px;
}
.raw {
  font-size: 11px;
  opacity: 0.6;
  margin-left: 4px;
}
</style>
