<script setup lang="ts">
// 回复页右侧：相关信息（订单、包裹、商品、收件信息）/ 客户资料（累计订单与退款）/ 历史记录（同一买家的其他会话）
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiError, api, get } from '../api'
import { errorText } from '../format'
import type { ConvContext, ConversationItem, CustomerOrder, CustomerPanelData, ListingCard, MessageTag } from '../types'
import OrderExtras from './OrderExtras.vue'
import ParcelFollowupDialog from './ParcelFollowupDialog.vue'
import AftersalesReasonEditor from './AftersalesReasonEditor.vue'
import { REASON_LABEL, STATUS_LABEL, UC_LABEL, aiExplain, finalLabel } from '../aftersalesReason'
import type { AftersalesReasonCode, AftersalesReasonList, AftersalesReasonRow } from '../types'
import type { FollowupDraft } from '../parcelFollowup'
import { dotStyle, tagStyle } from '../tagColor'

const props = defineProps<{
  data: CustomerPanelData | null
  loading: boolean
  error: string
  conv: ConversationItem | null
  listing: ListingCard | null
  ctx?: ConvContext | null // 会话页传来的状态（售后、回复任务），给“当前对话待处理”刷新用
}>()
// changed：星标、标签变了（刷新列表）；insert：把建议回复填进回复框；convChanged：审核了查件单 / 发票，聊天记录要刷新
const emit = defineEmits<{ open: [id: number]; reload: [force?: boolean]; changed: []; insert: [text: string]; convChanged: [] }>()

const tab = ref<'order' | 'profile' | 'history'>('order')

// ---- 异常包裹跟进：从这个会话新建（预填订单、SKU、跟踪号、查件单号），已有几条也显示出来 ----
const pfDraft = ref<FollowupDraft | null>(null)
const pfCount = ref(0)
async function loadPfCount() {
  const id = props.conv?.id
  if (!id) return
  const r = await get<{ items: unknown[] }>(`/internal/parcel-followups?status=&conversation_id=${id}`).catch(() => null)
  if (props.conv?.id === id) pfCount.value = r?.items.length || 0
}
watch(() => props.conv?.id, loadPfCount, { immediate: true })
async function openFollowup() {
  if (!props.conv) return
  try {
    const p = await get<FollowupDraft & { existing?: number }>(`/internal/parcel-followups/prefill?conversation_id=${props.conv.id}`)
    delete p.existing
    pfDraft.value = p
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---- 售后原因（订单维度的最终分类）：显示这单当前的分类（AI / 人工），可以改；会话对应多张订单时先选订单 ----
const arRows = ref<AftersalesReasonRow[]>([])
const arOpen = ref(false)
const arOrder = ref('')
const arSaving = ref(false)
async function loadReasons() {
  const id = props.conv?.id
  if (!id) return
  const r = await get<AftersalesReasonList>(`/internal/aftersales-reasons?conversation_id=${id}&page_size=50`).catch(() => null)
  if (props.conv?.id === id) arRows.value = r?.items || []
}
watch(() => props.conv?.id, loadReasons, { immediate: true })
// 这单（侧栏当前显示的订单）对应的分类；没有就取会话的第一张
const arCurrent = computed(() => arRows.value.find((r) => r.order_id === order.value?.order_id) || arRows.value[0] || null)
const arExtraOrders = ref<string[]>([]) // 后端 422 时给出的候选订单
const arOrders = computed(() => [...new Set([...arRows.value.map((r) => r.order_id), ...(props.data?.orders || []).map((o) => o.order_id),
  ...arExtraOrders.value])])
const arPicked = computed(() => arRows.value.find((r) => r.order_id === arOrder.value) || null)
function openReason() {
  arOrder.value = arCurrent.value?.order_id || order.value?.order_id || arOrders.value[0] || ''
  arOpen.value = true
}
async function saveReason(body: { reason: AftersalesReasonCode; note: string } | { text: string }) {
  if (!props.conv || !arOrder.value) return
  const text = 'text' in body ? body.text : `${REASON_LABEL[body.reason]}${body.note ? ` ${body.note}` : ''}`
  arSaving.value = true
  try {
    await api('PUT', `/internal/conversations/${props.conv.id}/aftersales-reason`, { order_id: arOrder.value, text })
    ElMessage.success('已保存售后原因')
    arOpen.value = false
    await loadReasons()
  } catch (e) {
    // 会话对应多张或对不上订单：后端给出候选订单，加进选项让客服选
    const orders = e instanceof ApiError && Array.isArray((e.detail as { orders?: unknown })?.orders)
      ? ((e.detail as { orders: string[] }).orders) : []
    if (orders.length) {
      arExtraOrders.value = orders
      if (!orders.includes(arOrder.value)) arOrder.value = orders[0]
    }
    ElMessage.error(errorText(e))
  } finally {
    arSaving.value = false
  }
}
async function clearReason() {
  if (!arPicked.value) return
  try {
    await api('DELETE', `/internal/aftersales-reasons/${arPicked.value.id}/manual`)
    await loadReasons()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---- 订单：默认显示与这条会话相关的那一单，可切换到买家在本店的其他订单 ----
const pickedId = ref<string | null>(null)
watch(() => props.data, () => (pickedId.value = null))
const order = computed<CustomerOrder | null>(() => {
  const d = props.data
  if (!d) return null
  return d.orders.find((o) => o.order_id === pickedId.value) || d.related_order
})
const otherOrders = computed(() => (props.data?.orders || []).filter((o) => o.order_id !== order.value?.order_id))

// ---- 所选订单的补充信息：售后详情、谷仓物流、查件单、发票、评价、商品图、备注、黑名单 ----
const ex = ref<any>(null)
const exLoading = ref(false)
const exError = ref('')
let exSeq = 0
// 补充信息对应的“会话 + 订单”：只有它变了才清空重查；同一单重查（如新增备注后）先保留旧内容，避免闪一下
const exKey = computed(() => (props.conv && props.data ? `${props.conv.id}|${order.value?.order_id || ''}` : ''))
let loadedKey = ''
// 点了“刷新”：下一次加载补充信息时带 refresh=true（订单、包裹、谷仓、商品图都重新调接口）
let forceExtras = false
function refreshAll() {
  forceExtras = true
  emit('reload', true)
}
async function loadExtras() {
  const o = order.value
  const my = ++exSeq
  if (exKey.value !== loadedKey) ex.value = null
  loadedKey = exKey.value
  exError.value = ''
  if (!props.conv || !props.data) return
  exLoading.value = true
  try {
    const q = new URLSearchParams({ logistics: 'false' })
    if (forceExtras) q.set('refresh', 'true')
    if (o) {
      q.set('order_id', o.order_id)
      if (o.legacy_order_id) q.set('legacy_order_id', o.legacy_order_id)
      const items = o.items.map((i) => i.item_id).filter(Boolean)
      if (items.length) q.set('item_ids', items.join(','))
    } else {
      // 没查到订单（多半是售前）：列出这个买家在本店的全部售后记录、备注
      if (props.conv.item_id) q.set('item_ids', props.conv.item_id)
      q.set('buyer_cases', 'true')
    }
    // 分两步：先取售后、备注、发票等（只查本地库，很快），备注马上能看到；
    // 物流（谷仓 + 17TRACK，新单号可能要等十几秒）和谷仓收件信息第二步再补上，不再拖住整块
    const convId = props.conv.id
    const r = await get<any>(`/internal/conversations/${convId}/order-extras?${q}`)
    if (my !== exSeq) return
    ex.value = { ...(ex.value || {}), ...r, logistics: ex.value?.logistics ?? null, consignee: ex.value?.consignee ?? null }
    if (o) {
      q.set('logistics', 'true')
      if (!o.ship_to.name || !o.ship_to.address || !o.ship_to.phone) q.set('need_consignee', 'true')
      const r2 = await get<any>(`/internal/conversations/${convId}/order-extras?${q}`)
      if (my === exSeq && ex.value) ex.value = { ...ex.value, logistics: r2.logistics, consignee: r2.consignee }
    }
    if (my === exSeq) forceExtras = false
  } catch (e) {
    if (my === exSeq) exError.value = errorText(e)
  } finally {
    if (my === exSeq) exLoading.value = false
  }
}
// 监听字符串而不是数组：列表每隔几秒刷新会换一个新的会话对象，数组每次都是新的，会被当成变化而反复重查（备注一闪一闪）
watch(exKey, loadExtras, { immediate: true })

// 收件信息：eBay 订单里有的用 eBay 的，没有的（较早的订单 eBay 不再返回姓名、电话）用谷仓出库单上的
const shipTo = computed(() => {
  const e = order.value?.ship_to || ({} as any)
  const g = ex.value?.consignee && !ex.value.consignee.error ? ex.value.consignee : {}
  const pick = (k: string) => (e[k] ? { v: e[k], gc: false } : g[k] ? { v: g[k], gc: true } : { v: null, gc: false })
  return { name: pick('name'), address: pick('address'), phone: pick('phone'), email: pick('email'),
           country: e.country || g.country || null }
})
const shipFromGc = computed(() => ['name', 'address', 'phone', 'email'].some((k) => (shipTo.value as any)[k].gc))
const allOrders = computed<any[]>(() => (props.data as any)?.all_stores?.orders || [])

// 售后详情：处理中的排前面
const cases = computed<any[]>(() => [...(ex.value?.cases || [])].sort((a, b) => Number(b.is_open) - Number(a.is_open)))
// 包裹：谷仓 + 17TRACK 的物流详情优先，还没查到时先用 eBay 订单里的运单号
const pkgs = computed<any[]>(() => {
  const l = ex.value?.logistics?.packages
  if (l?.length) return l
  if (order.value && order.value.order_id === props.data?.related_order?.order_id) return props.data?.packages || []
  return []
})
const showEvents = ref<Record<number, boolean>>({})
const picOf = (itemId: string | null) =>
  (itemId && ex.value?.pictures?.[itemId]) || (itemId && itemId === props.conv?.item_id ? props.listing?.picture_url : null) || null
// 物流阶段只显示短名（括号里的说明放到鼠标悬停）
const stageText = (p: any) =>
  String(p.stage_label || SHIP[order.value?.order_status || '']?.[0] || p.tracking_status || '已发货').split(/[（(]/)[0]
const STAGE_CLS = (p: any) => (p.needs_human ? 'bad' : p.stage === 'DELIVERED' ? 'ok' : p.stage ? 'warn' : 'ok')
const COUNTRY: Record<string, string> = {
  DE: '德国', AT: '奥地利', CH: '瑞士', FR: '法国', BE: '比利时', LU: '卢森堡', NL: '荷兰', IT: '意大利', ES: '西班牙',
  PT: '葡萄牙', GB: '英国', IE: '爱尔兰', US: '美国', CA: '加拿大', AU: '澳大利亚', PL: '波兰', CZ: '捷克', SK: '斯洛伐克',
  HU: '匈牙利', SI: '斯洛文尼亚', HR: '克罗地亚', RO: '罗马尼亚', BG: '保加利亚', GR: '希腊', SE: '瑞典', DK: '丹麦',
  FI: '芬兰', NO: '挪威', EE: '爱沙尼亚', LV: '拉脱维亚', LT: '立陶宛', CY: '塞浦路斯', MT: '马耳他',
}
const countryName = (c?: string | null) => (c ? COUNTRY[c.toUpperCase()] || c : '')

// ---- 顶部：星标、标签 ----
const starred = ref(false)
watch(() => props.conv?.id, () => (starred.value = !!props.conv?.starred), { immediate: true })
async function toggleStar() {
  if (!props.conv) return
  try {
    await api('PUT', `/internal/conversations/${props.conv.id}/star`, { starred: !starred.value })
    starred.value = !starred.value
    emit('changed')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
const tags = ref<MessageTag[]>([])
async function loadTags(open: boolean) {
  if (open && !tags.value.length) tags.value = await get<MessageTag[]>('/internal/message-tags').catch(() => [])
}
const hasMark = (id: number) => !!props.conv?.marks?.some((m) => m.tag_id === id)
async function toggleMark(id: number) {
  if (!props.conv) return
  try {
    if (hasMark(id)) await api('DELETE', `/internal/conversations/${props.conv.id}/mark?tag_id=${id}`)
    else await api('PUT', `/internal/conversations/${props.conv.id}/mark`, { tag_id: id })
    emit('changed')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const PAY: Record<string, [string, string]> = {
  PAID: ['已付款', 'ok'], PENDING: ['待付款', 'warn'], FAILED: ['付款失败', 'bad'],
  FULLY_REFUNDED: ['已全额退款', 'bad'], PARTIALLY_REFUNDED: ['部分退款', 'warn'],
}
const SHIP: Record<string, [string, string]> = {
  NOT_STARTED: ['未发货', 'bad'], IN_PROGRESS: ['部分发货', 'warn'], FULFILLED: ['已发货', 'ok'],
  SHIPPED: ['已发货', 'ok'], DELIVERED: ['已签收', 'ok'],
}
const money = (m?: { value: string; currency: string } | null) => (m ? `${m.currency} ${m.value}` : '—')
const pad = (n: number) => String(n).padStart(2, '0')
function fmt(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function daysAgo(iso?: string | null) {
  if (!iso) return ''
  const n = Math.floor((Date.now() - new Date(iso).getTime()) / 86400_000)
  return n <= 0 ? '今天' : `${n}天`
}
const amounts = (list: { currency: string; total: string }[]) =>
  list.length ? list.map((a) => `${a.currency} ${a.total}`).join(' · ') : '—'
const avgs = (list: { currency: string; avg: string }[]) =>
  list.length ? list.map((a) => `${a.currency} ${a.avg}`).join(' · ') : '—'

async function copy(text?: string | null) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

// ---- 历史记录：按需加载商品图 ----
const thumbs = ref<Record<number, string | null>>({})
watch(() => [tab.value, props.data?.history.length], () => {
  if (tab.value !== 'history') return
  for (const h of props.data?.history || []) {
    if (!h.item_id || h.id in thumbs.value) continue
    thumbs.value[h.id] = null
    get<ListingCard>(`/internal/conversations/${h.id}/listing`)
      .then((r) => (thumbs.value[h.id] = r.picture_url || null))
      .catch(() => null)
  }
})
const initial = (s: string) => (s || '?').slice(0, 1).toUpperCase()
</script>

<template>
  <aside class="cs">
    <div class="tabs">
      <button :class="{ on: tab === 'order' }" @click="tab = 'order'">相关信息</button>
      <button :class="{ on: tab === 'profile' }" @click="tab = 'profile'">客户资料</button>
      <button :class="{ on: tab === 'history' }" @click="tab = 'history'">历史记录 ({{ data?.history.length ?? 0 }})</button>
      <span class="tabs-spacer" />
      <button class="refresh" :disabled="loading || exLoading" title="订单、物流、商品信息默认用 1 小时内的缓存；点这里重新调接口查最新的"
              @click="refreshAll">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"
             :class="{ spin: loading || exLoading }">
          <path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        刷新
      </button>
    </div>

    <div class="body" v-loading="loading && !data">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
        <el-button link type="primary" @click="emit('reload')">重试</el-button>
      </el-alert>

      <ParcelFollowupDialog :draft="pfDraft" @close="pfDraft = null" @saved="loadPfCount" />

      <!-- ================= 相关信息 ================= -->
      <template v-if="tab === 'order' && data">
        <div class="owner-row">
          <span class="owner" :class="{ none: !conv?.claim }">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <circle cx="12" cy="12" r="11" opacity=".18" /><circle cx="12" cy="9.5" r="3.6" /><path d="M5.8 18.6c1.3-2.6 3.6-3.9 6.2-3.9s4.9 1.3 6.2 3.9" />
            </svg>
            {{ conv?.claim ? `${conv.claim.claimed_by} 处理中` : '未分配' }}
          </span>
          <span class="bar" />
          <button class="tool" :class="{ on: starred }" :title="starred ? '取消星标' : '加星标'" @click="toggleStar">
            <svg width="16" height="16" viewBox="0 0 24 24" :fill="starred ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
              <path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" stroke-linejoin="round" />
            </svg>
          </button>
          <el-dropdown trigger="click" :hide-on-click="false" @visible-change="loadTags">
            <button class="tool add-tag">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
                <circle cx="12" cy="12" r="9" /><path d="M12 8v8M8 12h8" />
              </svg>
              标签
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-for="t in tags" :key="t.id" @click="toggleMark(t.id)">
                  <span class="dot" :style="dotStyle(t.color)" />{{ t.name }}<span v-if="hasMark(t.id)" class="tick">✓</span>
                </el-dropdown-item>
                <el-dropdown-item v-if="!tags.length" disabled>还没有标签（在会话顶部「标记」里新建）</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button class="tool" :title="pfCount ? `这个会话已记了 ${pfCount} 条异常包裹跟进，再加一条` : '把这单加入异常包裹跟进表'"
                  @click="openFollowup">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
              <path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5z" stroke-linejoin="round" /><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9" />
            </svg>
            异常跟进<span v-if="pfCount" class="pf-cnt">{{ pfCount }}</span>
          </button>
          <button class="tool" :title="arCurrent ? `售后原因：${finalLabel(arCurrent)}（${STATUS_LABEL[arCurrent.status] || ''}），点击修改` : '给这单标售后原因'"
                  @click="openReason">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
              <path d="M4 5h16M4 12h10M4 19h7" stroke-linecap="round" /><circle cx="18" cy="17" r="3" />
            </svg>
            售后原因
            <span v-if="arCurrent?.final_reason" class="ar-tag" :class="arCurrent.status.toLowerCase()">{{ finalLabel(arCurrent) }}</span>
          </button>
          <span class="marks">
            <el-tag v-for="m in conv?.marks || []" :key="m.tag_id" size="small" :style="tagStyle(m.color, true)" closable
                    @close="toggleMark(m.tag_id)">{{ m.tag }}</el-tag>
          </span>
        </div>

        <el-dialog v-model="arOpen" title="售后原因" width="min(440px, calc(100vw - 24px))" append-to-body>
          <div v-if="arOrders.length > 1" class="ar-orders">
            <span class="muted">这个会话有 {{ arOrders.length }} 张订单，先选订单：</span>
            <el-radio-group v-model="arOrder" size="small">
              <el-radio-button v-for="o in arOrders" :key="o" :value="o">{{ o }}</el-radio-button>
            </el-radio-group>
          </div>
          <div v-else-if="arOrder" class="muted ar-orders">订单 {{ arOrder }}</div>
          <div v-else class="muted ar-orders">这个会话还没有对上订单，暂时不能标售后原因</div>
          <div v-if="arPicked" class="ar-now">
            <div>最终分类：<b>{{ finalLabel(arPicked) }}</b>
              <span class="muted">（{{ STATUS_LABEL[arPicked.status] || arPicked.status }}）</span></div>
            <div v-if="arPicked.ai_reason" class="muted">AI：{{ REASON_LABEL[arPicked.ai_reason] }}——{{
              [...aiExplain(arPicked).notes, ...aiExplain(arPicked).basis].join('；') || '没有依据说明' }}</div>
            <div v-else-if="arPicked.ai_note" class="muted">AI 没分类<template v-if="arPicked.unclassified_kind">（{{ UC_LABEL[arPicked.unclassified_kind] }}）</template>：{{ arPicked.ai_note }}</div>
            <div v-if="arPicked.manual_reason" class="muted">人工：{{ REASON_LABEL[arPicked.manual_reason] }} {{ arPicked.manual_note || '' }}
              · {{ arPicked.manual_by }}<el-button link size="small" type="danger" @click="clearReason">撤销人工</el-button></div>
          </div>
          <AftersalesReasonEditor v-if="arOrder" :key="arOrder" :reason="arPicked?.manual_reason" :note="arPicked?.manual_note"
                                  :saving="arSaving" @save="saveReason" @cancel="arOpen = false" />
        </el-dialog>

        <div v-if="ex?.blacklist" class="black">黑名单买家：{{ ex.blacklist.reason || '未填原因' }}（{{ ex.blacklist.created_by }} 加入）· AI 不起草回复</div>
        <div v-if="exError" class="err">补充信息加载失败：{{ exError }}
          <button class="icon-btn" @click="loadExtras">重试</button>
        </div>

        <!-- 售后详情：退货 / 未收到货咨询 / 纠纷 / 取消 / 付款纠纷 -->
        <template v-for="c in cases" :key="c.id">
          <div class="sec-title">{{ c.label }}详情</div>
          <table class="kv">
            <tbody>
              <tr>
                <th>平台,类型,状态</th>
                <td>eBay, {{ c.label }}, <span :class="c.is_open ? 'st-open' : 'muted'">{{ c.status_label || c.status || '—' }}</span></td>
              </tr>
              <tr>
                <th>{{ c.label }}编号</th>
                <td>
                  <a v-if="c.action_url" class="oid" :href="c.action_url" target="_blank" rel="noopener noreferrer" title="去 eBay 处理">{{ c.external_id }}</a>
                  <span v-else class="oid">{{ c.external_id }}</span>
                  <button class="icon-btn" title="复制编号" @click="copy(c.external_id)">⧉</button>
                </td>
              </tr>
              <tr>
                <th>开启原因</th>
                <td>{{ c.reason_label || c.reason || '—' }}<span v-if="c.reason && c.reason_label !== c.reason" class="raw">{{ c.reason }}</span></td>
              </tr>
              <tr v-if="c.amount != null"><th>金额</th><td>{{ c.currency }} {{ Number(c.amount).toFixed(2) }}</td></tr>
              <tr><th>开启时间</th><td>{{ fmt(c.opened_at) }}</td></tr>
              <tr v-if="c.is_open && (c.due_at || c.seller_action_label)">
                <th>卖家需要做</th>
                <td>
                  {{ c.seller_action_label || '回复处理' }}
                  <div v-if="c.due_at" :class="c.overdue ? 'st bad' : 'st warn'" class="due">
                    {{ c.overdue ? '已超时' : '截止' }} {{ fmt(c.due_at) }}
                  </div>
                </td>
              </tr>
              <tr><th>{{ c.kind === 'CANCEL' ? '取消说明' : '纠纷描述' }}</th><td class="note">{{ c.comment || '' }}</td></tr>
              <tr v-if="c.kind === 'RETURN'"><th>买家期待</th><td>{{ c.expect_label || '—' }}</td></tr>
            </tbody>
          </table>
        </template>

        <template v-if="order">
          <div class="order-head">
            订单:<b class="oid">{{ order.order_id }}</b>
            <button class="icon-btn" title="复制订单号" @click="copy(order.order_id)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                <rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" />
              </svg>
            </button>
            <a v-if="ex?.order_url" class="mini-btn" :href="ex.order_url" target="_blank" rel="noopener noreferrer"
               title="在 eBay 卖家后台打开这单（修改、取消、退款在那里操作）">eBay 订单详情</a>
            <span v-if="order.marketplace_id" class="chip">{{ order.marketplace_id.replace('EBAY_', '') }}</span>
            <span v-if="order.cancel_state && order.cancel_state !== 'NONE_REQUESTED'" class="chip bad">取消：{{ order.cancel_state }}</span>
          </div>
          <table class="kv">
            <tbody>
              <tr>
                <th>订单金额</th>
                <td>
                  {{ money(order.total) }}
                  <span v-if="order.payment_status" class="st" :class="PAY[order.payment_status]?.[1]">
                    {{ PAY[order.payment_status]?.[0] || order.payment_status }}
                  </span>
                  <span v-if="order.record_no" class="rec">record NO.: {{ order.record_no }}</span>
                  <div v-for="(r, i) in order.refunds" :key="i" class="muted">退款 {{ money(r.amount) }} · {{ fmt(r.date) }}</div>
                </td>
              </tr>

              <!-- 包裹（一个包裹时商品信息放在包裹里，和 ERP 一样） -->
              <tr v-for="(p, idx) in pkgs.length ? pkgs : [null]" :key="idx">
                <th>包裹{{ idx + 1 }}</th>
                <td class="pkg-cell">
                  <div class="pkg-info">
                    <template v-if="p">
                      <span class="st strong" :class="STAGE_CLS(p)" :title="p.stage_label || ''">{{ stageText(p) }}</span>
                      <span v-if="p.shipped_at || p.event_time" class="time">{{ fmt(p.shipped_at || p.event_time) }}</span>
                      <span v-if="p.days_without_update >= 1 && p.stage !== 'DELIVERED' && !p.delivered_at" class="st bad">
                        {{ Math.floor(p.days_without_update) }} 天没更新</span>
                      <div>{{ p.shipping_method || p.carrier || order.shipping_service || '承运商未知' }}<span v-if="p.warehouse_code"> [谷仓-{{ p.warehouse_code }}]</span></div>
                      <div v-if="p.tracking_number" class="trk">
                        <span class="mono">{{ p.tracking_number }}</span>
                        <button class="icon-btn" title="复制运单号" @click="copy(p.tracking_number)">⧉</button>
                        <a v-if="p.carrier_url" class="icon-btn" :href="p.carrier_url" target="_blank" rel="noopener noreferrer" title="去承运商官网查">
                          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                            <path d="M2 6h11v9H2zM13 9h4l3 3v3h-7z" /><circle cx="6" cy="17" r="1.8" /><circle cx="16.5" cy="17" r="1.8" />
                          </svg>
                        </a>
                      </div>
                      <div v-else class="muted">未分配运单号</div>
                      <div v-if="p.picked_up_at || p.delivered_at" class="muted">
                        <span v-if="p.picked_up_at">揽收 {{ fmt(p.picked_up_at) }}</span>
                        <span v-if="p.delivered_at"> · 妥投 {{ fmt(p.delivered_at) }}</span>
                      </div>
                      <div v-if="p.latest_event" class="muted">{{ fmt(p.event_time) }} {{ p.latest_event }}</div>
                      <div v-if="p.abnormal" class="st bad">异常：{{ p.abnormal }}</div>
                      <button v-if="p.events?.length" class="link" @click="showEvents[idx] = !showEvents[idx]">
                        {{ showEvents[idx] ? '收起轨迹' : `查看轨迹（${p.events.length}）` }}
                      </button>
                      <div v-if="showEvents[idx]" class="events">
                        <div v-for="(e, k) in p.events" :key="k"><span class="muted">{{ fmt(e.time) }}</span> {{ e.location }} {{ e.description }}</div>
                      </div>
                    </template>
                    <template v-else>
                      <span class="st strong" :class="SHIP[order.order_status || '']?.[1] || 'bad'">{{ SHIP[order.order_status || '']?.[0] || order.order_status || '未发货' }}</span>
                      <div v-if="order.shipping_service">{{ order.shipping_service }}</div>
                      <div class="muted">
                        {{ exLoading ? '正在查物流…' : ex?.logistics?.error ? `物流查询失败：${ex.logistics.error}` : (ex?.logistics?.note || data.packages_error || '未分配运单号') }}
                      </div>
                    </template>
                  </div>
                  <div v-if="pkgs.length <= 1" class="pkg-prod">
                    <div class="prod-title">产品信息</div>
                    <div v-for="(it, i) in order.items" :key="i" class="prod">
                      <img v-if="picOf(it.item_id)" :src="picOf(it.item_id)!" alt="" referrerpolicy="no-referrer" />
                      <span v-else class="ph" aria-hidden="true" />
                      <div class="prod-text">
                        <span class="dotted">{{ it.sku || '—' }}</span> * {{ it.quantity ?? 1 }}<template v-if="it.title">,
                          <span :title="it.title">{{ it.title }}</span></template>
                        <span v-if="it.item_id" class="dotted"> ({{ it.item_id }})</span>
                        <button v-if="it.sku" class="icon-btn" title="复制 SKU" @click="copy(it.sku)">⧉</button>
                      </div>
                    </div>
                  </div>
                </td>
              </tr>
              <tr v-if="pkgs.length > 1">
                <th>产品信息</th>
                <td>
                  <div v-for="(it, i) in order.items" :key="i" class="prod">
                    <img v-if="picOf(it.item_id)" :src="picOf(it.item_id)!" alt="" referrerpolicy="no-referrer" />
                    <span v-else class="ph" aria-hidden="true" />
                    <div class="prod-text">
                      <span class="dotted">{{ it.sku || '—' }}</span> * {{ it.quantity ?? 1 }}<template v-if="it.title">, {{ it.title }}</template>
                      <span v-if="it.item_id" class="dotted"> ({{ it.item_id }})</span>
                      <button v-if="it.sku" class="icon-btn" title="复制 SKU" @click="copy(it.sku)">⧉</button>
                    </div>
                  </div>
                </td>
              </tr>

              <tr>
                <th>付款时间</th>
                <td>{{ fmt(order.paid_at) }} <span v-if="order.paid_at" class="muted">(已付款{{ daysAgo(order.paid_at) }})</span></td>
              </tr>
              <tr>
                <th>客户ID, 姓名</th>
                <td>
                  {{ data.buyer_key }}<template v-if="shipTo.name.v">, {{ shipTo.name.v }}</template>
                  <button class="icon-btn" title="复制" @click="copy(shipTo.name.v || data.buyer_key)">⧉</button>
                  <el-button size="small" class="mini" @click="tab = 'history'">查看历史订单</el-button>
                </td>
              </tr>
              <tr v-if="order.buyer_note">
                <th>平台留言</th>
                <td class="note">{{ order.buyer_note }}</td>
              </tr>
              <tr>
                <th>收件地址</th>
                <td>
                  <template v-if="shipTo.address.v">
                    {{ shipTo.address.v }}<span class="muted">({{ countryName(shipTo.country) }})</span>
                    <button class="icon-btn" title="复制地址" @click="copy(`${shipTo.name.v || ''} ${shipTo.address.v} ${shipTo.country || ''}`.trim())">⧉</button>
                  </template>
                  <span v-else class="muted">{{ shipTo.country ? `收货国家：${countryName(shipTo.country)}` : '—' }}</span>
                  <div v-if="shipTo.phone.v" class="muted">电话：{{ shipTo.phone.v }}
                    <button class="icon-btn" title="复制电话" @click="copy(shipTo.phone.v)">⧉</button></div>
                  <div v-if="shipTo.email.v" class="muted">邮箱：{{ shipTo.email.v }}</div>
                  <div v-if="shipFromGc" class="gc-note">eBay 订单里没有的收件信息取自谷仓出库单 {{ ex?.consignee?.warehouse_order || '' }}</div>
                  <div v-else-if="ex?.consignee?.error" class="gc-note">谷仓收件信息：{{ ex.consignee.error }}</div>
                </td>
              </tr>
            </tbody>
          </table>
          <OrderExtras v-if="conv" :conv-id="conv.id" :order="order" :ex="ex" @reload="loadExtras"
                       :ctx="ctx || null" @insert="(t: string) => emit('insert', t)" @conv-changed="emit('convChanged')" />
          <div v-if="otherOrders.length" class="others">
            <div class="sub-title">该买家在本店的其他订单（{{ otherOrders.length }}）</div>
            <button v-for="o in otherOrders" :key="o.order_id" class="other" @click="pickedId = o.order_id">
              <span class="oid">{{ o.order_id }}</span>
              <span class="muted ellipsis">{{ o.items[0]?.sku }} · {{ money(o.total) }} · {{ fmt(o.created_at).slice(0, 10) }}</span>
            </button>
          </div>
          <el-button v-if="pickedId" link type="primary" size="small" @click="pickedId = null">← 回到相关订单</el-button>
        </template>

        <template v-else>
          <div class="presale">
            <b>售前咨询</b>：这个买家在本店最近 90 天没有订单{{ allOrders.length ? `，全部店铺的订单记录有 ${allOrders.length} 单` : '，全部店铺也没有订单记录' }}
          </div>
          <div class="sec-title">咨询的商品</div>
          <table class="kv">
            <tbody>
              <tr>
                <th>产品信息</th>
                <td>
                  <div class="prod">
                    <img v-if="listing?.picture_url" :src="listing.picture_url" alt="" referrerpolicy="no-referrer" />
                    <span v-else class="ph" aria-hidden="true" />
                    <div class="prod-text">
                      <a v-if="listing?.url" :href="listing.url" target="_blank" rel="noopener noreferrer">{{ listing?.title || conv?.item_id }}</a>
                      <span v-else>{{ listing?.title || conv?.conversation_title || '—' }}</span>
                    </div>
                  </div>
                </td>
              </tr>
              <tr v-if="conv?.item_id">
                <th>商品编号</th>
                <td><span class="dotted">{{ conv.item_id }}</span> <button class="icon-btn" title="复制商品号" @click="copy(conv.item_id)">⧉</button></td>
              </tr>
              <tr>
                <th>SKU</th>
                <td>
                  <template v-if="listing?.sku">{{ listing.sku }} <button class="icon-btn" title="复制 SKU" @click="copy(listing.sku)">⧉</button></template>
                  <span v-else class="muted">刊登没有填 SKU（自定义标签）</span>
                </td>
              </tr>
              <tr v-if="listing?.price"><th>售价</th><td>{{ listing.price.currency }} {{ listing.price.value }}</td></tr>
              <tr v-if="listing?.quantity_available != null"><th>可售数量</th><td>{{ listing.quantity_available }}</td></tr>
              <tr><th>站点, 店铺</th><td>{{ conv?.marketplace_id?.replace('EBAY_', '') || '—' }}, {{ conv?.store_name || conv?.store_id }}</td></tr>
              <tr>
                <th>客户ID</th>
                <td>
                  {{ data.buyer_key }} <button class="icon-btn" title="复制" @click="copy(data.buyer_key)">⧉</button>
                  <el-button size="small" class="mini" @click="tab = 'history'">查看历史记录</el-button>
                </td>
              </tr>
            </tbody>
          </table>
          <template v-if="allOrders.length">
            <div class="sec-title">这个买家在全部店铺的订单</div>
            <table class="kv">
              <tbody>
                <tr v-for="o in allOrders.slice(0, 10)" :key="o.store_id + o.order_id">
                  <th>{{ o.store_name }}</th>
                  <td>
                    <span class="oid">{{ o.order_id }}</span> <button class="icon-btn" title="复制订单号" @click="copy(o.order_id)">⧉</button>
                    <div class="muted">{{ fmt(o.created_at).slice(0, 10) }} · {{ o.currency }} {{ o.total }}<span v-if="o.refunded"> · 有退款</span><span v-if="o.cancelled"> · 已取消</span></div>
                    <div class="muted ellipsis" :title="o.item_title || ''">{{ o.skus || '' }} {{ o.item_title || '' }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
          <OrderExtras v-if="conv" :conv-id="conv.id" :order="null" :ex="ex" @reload="loadExtras"
                       :ctx="ctx || null" @insert="(t: string) => emit('insert', t)" @conv-changed="emit('convChanged')" />
        </template>
        <div v-for="e in data.errors" :key="e.store_id" class="err">{{ e.store_name }}：{{ e.error }}</div>
      </template>

      <!-- ================= 客户资料 ================= -->
      <template v-if="tab === 'profile' && data">
        <div class="who">
          <span class="avatar">{{ initial(data.buyer_key) }}</span>
          <b class="buyer">{{ data.buyer_key }}</b>
          <span v-if="data.profile.name" class="muted">{{ data.profile.name }}</span>
        </div>
        <div class="contact">
          <div><span class="lbl">联系邮箱:</span>{{ data.profile.email || '--' }}</div>
          <div><span class="lbl">联系电话:</span>{{ data.profile.phone || '--' }}</div>
          <div class="stores">
            <span class="lbl">联系过的店铺:</span>
            <span v-for="s in data.profile.stores" :key="s.store_id" class="chip">{{ s.store_name }} · {{ s.orders }}单</span>
          </div>
        </div>
        <div class="sub-title">累计订单</div>
        <table class="kv narrow">
          <tbody>
            <tr><th>总销量</th><td>{{ data.profile.total_quantity }}</td></tr>
            <tr><th>总订单数</th><td>{{ data.profile.order_count }}</td></tr>
            <tr><th>总订单金额</th><td>{{ amounts(data.profile.amounts) }}</td></tr>
            <tr><th>平均客单价</th><td>{{ avgs(data.profile.amounts) }}</td></tr>
            <tr><th>首次消费时间</th><td>{{ fmt(data.profile.first_order_at) }}</td></tr>
            <tr><th>最近消费时间</th><td>{{ fmt(data.profile.last_order_at) }}</td></tr>
          </tbody>
        </table>
        <div class="sub-title">累计退款</div>
        <table class="kv narrow">
          <tbody>
            <tr><th>总退款次数</th><td>{{ data.profile.refund_count }}</td></tr>
            <tr><th>总退款金额</th><td>{{ amounts(data.profile.refund_amounts) }}</td></tr>
          </tbody>
        </table>
        <p class="muted scope">统计范围：这个买家联系过的店铺、最近 90 天的订单。</p>
      </template>

      <!-- ================= 历史记录 ================= -->
      <template v-if="tab === 'history' && data">
        <div v-if="!data.history.length" class="empty"><div class="muted">这个买家没有在其他店铺或其他商品下发过消息</div></div>
        <div v-for="h in data.history" :key="h.id" class="hist">
          <div class="hist-head">
            <template v-if="h.order_id">
              订单: <b class="oid">{{ h.order_id }}</b>
              <button class="icon-btn" title="复制订单号" @click="copy(h.order_id)">⧉</button>
            </template>
            <span v-else class="muted">会话 #{{ h.id }}（未关联订单）</span>
            <span class="chip store">{{ h.store_name }}</span>
            <button class="go" @click="emit('open', h.id)">查看会话 ›</button>
          </div>
          <div class="hist-body">
            <img v-if="thumbs[h.id]" :src="thumbs[h.id]!" alt="" referrerpolicy="no-referrer" />
            <span v-else class="ph" aria-hidden="true" />
            <div class="hist-text">
              <a v-if="h.item_url" :href="h.item_url" target="_blank" rel="noopener noreferrer" class="ellipsis">{{ h.title || h.item_id }}</a>
              <div v-else class="ellipsis">{{ h.title || '（无商品）' }}</div>
              <div class="muted ellipsis">{{ h.preview }}</div>
              <div class="muted">{{ fmt(h.last_message_at) }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.pf-cnt {
  margin-left: 3px;
  padding: 0 5px;
  border-radius: 8px;
  font-size: 11px;
  background: var(--el-color-warning-light-8);
  color: var(--el-color-warning-dark-2);
}
.cs {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--panel);
  border-left: 1px solid var(--line);
}
.tabs {
  display: flex;
  gap: 4px;
  padding: 0 12px;
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.tabs button {
  height: 44px;
  padding: 0 16px;
  border: 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-family: inherit;
  font-size: 14px;
  color: var(--text);
  cursor: pointer;
}
.tabs button.on {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0 16px 20px;
}
.owner-row {
  display: flex;
  flex-wrap: wrap; /* 侧栏窄时按钮换行，不再一个字一行 */
  align-items: center;
  gap: 6px 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line-soft);
  font-size: 13px;
}
.owner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--accent);
  white-space: nowrap;
}
.owner.none {
  color: var(--muted);
}
.bar {
  width: 1px;
  height: 14px;
  background: var(--line);
}
.marks {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.order-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 14px 0 8px;
  font-size: 13px;
  color: var(--text);
}
.oid {
  color: var(--accent);
  font-weight: 500;
}
.icon-btn {
  border: 0;
  background: none;
  padding: 0 2px;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
}
.icon-btn:hover {
  color: var(--accent);
}
.chip {
  display: inline-block;
  padding: 0 6px;
  height: 20px;
  line-height: 18px;
  border: 1px solid var(--line);
  border-radius: 3px;
  font-size: 12px;
  color: var(--text-2);
  white-space: nowrap;
}
.chip.store {
  border-color: var(--accent-line);
  color: var(--accent);
}
.chip.bad {
  border-color: var(--el-color-danger-light-7);
  color: var(--danger-text);
}
table.kv {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  border: 1px solid var(--line);
}
.kv th,
.kv td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
  text-align: left;
  line-height: 1.55;
}
.kv th {
  width: 112px;
  font-weight: 400;
  color: var(--text-2);
  white-space: nowrap;
}
.kv.narrow th {
  width: 128px;
}
.kv td {
  color: var(--text);
  word-break: break-word;
}
.st {
  margin-left: 6px;
  font-size: 12px;
}
.st.strong {
  margin-left: 0;
}
.st.ok {
  color: var(--ok-text);
}
.st.warn {
  color: var(--warn-text);
}
.st.bad {
  color: var(--danger);
}
.rec {
  float: right;
  font-size: 12px;
  color: var(--text-2);
}
.pkg {
  margin-top: 4px;
}
.prod {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.prod + .prod {
  margin-top: 6px;
}
.prod img,
.ph {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: 3px;
  object-fit: contain;
  border: 1px solid var(--line-soft);
  background: var(--sunken);
}
.prod > div {
  min-width: 0;
}
.dotted {
  border-bottom: 1px dashed var(--muted);
}
.ellipsis {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mini {
  margin-left: 6px;
}
.note {
  white-space: pre-wrap;
}
.others {
  margin-top: 12px;
}
.sub-title {
  margin: 16px 0 8px;
  font-size: 13px;
  color: var(--text-2);
}
.other {
  width: 100%;
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 7px 10px;
  border: 1px solid var(--line-soft);
  border-radius: 3px;
  background: var(--panel);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}
.other + .other {
  margin-top: 4px;
}
.other:hover {
  border-color: var(--accent-line);
}
.empty {
  padding: 24px 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.empty-title {
  font-size: 14px;
  color: var(--text);
}
.err {
  margin-top: 10px;
  font-size: 12px;
  color: var(--danger-text);
}
.who {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 16px 0 10px;
}
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #8fd694;
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.buyer {
  font-weight: 400;
  color: var(--accent);
}
.contact {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  padding-left: 4px;
}
.lbl {
  display: inline-block;
  width: 84px;
  color: var(--text-2);
}
.stores {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.scope {
  margin-top: 10px;
}
.hist {
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
}
.hist-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.go {
  margin-left: auto;
  border: 0;
  background: none;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-2);
  cursor: pointer;
}
.go:hover {
  color: var(--accent);
}
.hist-body {
  display: flex;
  gap: 8px;
  margin-top: 6px;
  min-width: 0;
}
.hist-body img,
.hist-body .ph {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: 3px;
  object-fit: contain;
  border: 1px solid var(--line-soft);
  background: var(--sunken);
}
.hist-text {
  min-width: 0;
  font-size: 12px;
}
.hist-text a {
  color: var(--text);
  text-decoration: none;
}
.hist-text a:hover {
  color: var(--accent);
}
.tool {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
  border: 0;
  background: none;
  padding: 0 2px;
  font-family: inherit;
  font-size: 13px;
  color: var(--muted);
  cursor: pointer;
}
.tool:hover,
.tool.on {
  color: var(--warn-text);
}
.add-tag:hover {
  color: var(--accent);
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  background: #8a94a6;
}
.ar-tag {
  margin-left: 4px;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 11px;
  line-height: 16px;
  background: var(--sunken);
  color: var(--text-2);
}
.ar-tag.manual {
  background: var(--ok-text);
  color: #fff;
}
.ar-tag.ai {
  background: var(--accent-soft);
  color: var(--accent);
}
.ar-orders {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}
.ar-now {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 10px;
  font-size: 13px;
}
.tick {
  margin-left: 8px;
  color: var(--accent);
}
.black {
  margin-top: 10px;
  background: #a8071a;
  color: #fff;
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
}
.sec-title {
  margin: 16px 0 8px;
  font-size: 13px;
  color: var(--text-2);
}
.st-open {
  color: var(--danger-text);
}
.raw {
  margin-left: 6px;
  font-size: 11px;
  color: var(--muted);
}
.due {
  margin-left: 0;
}
.muted {
  color: var(--muted);
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
}
.mini-btn {
  margin-left: 6px;
  padding: 0 8px;
  height: 22px;
  line-height: 20px;
  border: 1px solid var(--line);
  border-radius: 3px;
  font-size: 12px;
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
}
.mini-btn:hover {
  border-color: var(--accent-line);
  color: var(--accent);
}
.pkg-cell {
  padding: 0 !important;
}
.pkg-info {
  padding: 10px 12px;
}
.time {
  margin-left: 6px;
}
.trk {
  display: flex;
  align-items: center;
  gap: 4px;
}
a.icon-btn {
  display: inline-flex;
}
.link {
  border: 0;
  background: none;
  padding: 0;
  font-family: inherit;
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
}
.events {
  margin-top: 4px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 220px;
  overflow: auto;
}
.pkg-prod {
  border-top: 1px solid var(--line);
  padding: 10px 12px;
}
.prod-title {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-2);
}
.prod-text {
  min-width: 0;
  word-break: break-word;
}
.presale {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 4px;
  background: var(--sunken);
  font-size: 13px;
  color: var(--text-2);
}
.presale b {
  color: var(--text);
  font-weight: 500;
}
.gc-note {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.tabs-spacer {
  flex: 1;
}
.tabs button.refresh {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
  font-size: 12px;
  color: var(--muted);
}
.tabs button.refresh:hover:not(:disabled) {
  color: var(--accent);
}
.tabs button.refresh:disabled {
  cursor: default;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
