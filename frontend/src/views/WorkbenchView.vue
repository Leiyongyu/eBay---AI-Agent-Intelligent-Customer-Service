<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { TAG_PALETTE, dotStyle, fromPicker, pickerValue, tagStyle } from '../tagColor'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import { REVIEW_STATES, STAGE_LABELS, STATE_LABELS, errorText, fmtWaiting, intentLabel, riskTag, stateTag } from '../format'
import type { ConversationItem, ListingCard, MessageTag, Store } from '../types'
import type { BoxKey } from '../nav'
import { allLeaves } from '../nav'
import ReplyView from './ReplyView.vue'
import SituationChip from '../components/SituationChip.vue'

const props = defineProps<{ box: BoxKey; focus: number | null }>()
const emit = defineEmits<{ select: [id: number | null]; counts: [] }>()

const isAdmin = computed(() => session.user?.role === 'ADMIN')
const isSystem = computed(() => props.box.startsWith('system'))
const boxLabel = computed(() => {
  const label = allLeaves().find((l) => l.box === props.box)?.label || '买家消息'
  return props.box === 'system_unread' || props.box === 'system_read' ? `平台消息 · ${label}` : label
})

// ---------------- 时间显示 ----------------
const pad = (n: number) => String(n).padStart(2, '0')
function fmtFull(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function fmtRel(iso: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}天前`
  return fmtFull(iso).slice(0, 10)
}

const ROLE_SHORT: Record<string, string> = { BUYER: '客户', SELLER: '客服', SYSTEM: '系统', UNKNOWN: '未知' }

// ---------------- 标签分组（人工 / AI） ----------------
const isAi = (who?: string | null) => !!who && (who.startsWith('AI') || who === '上传页')
const allMarks = (c: ConversationItem) => c.marks || (c.mark ? [c.mark] : [])
const manualMarks = (c: ConversationItem) => allMarks(c).filter((m) => !isAi(m.marked_by))
const aiMarks = (c: ConversationItem) => allMarks(c).filter((m) => isAi(m.marked_by))
function replyLabel(c: ConversationItem) {
  if (!c.job) return ''
  if (c.job.state === 'SENT') return c.auto_replied ? 'AI 自动回复' : '人工回复'
  return STATE_LABELS[c.job.state] || c.job.state
}
const aftersalesType = (c: ConversationItem) =>
  c.aftersales?.status === 'DONE' ? 'success' : c.aftersales?.status === 'IN_PROGRESS' ? 'primary' : 'warning'
const aftersalesLabel = (c: ConversationItem) =>
  `${c.aftersales?.status === 'DONE' ? '售后已完成' : c.aftersales?.status === 'IN_PROGRESS' ? '售后处理中' : '待售后'} · ${c.aftersales?.type || ''}`

// ---------------- 筛选条件 ----------------
const tags = ref<MessageTag[]>([])
async function loadTags() {
  tags.value = await get<MessageTag[]>('/internal/message-tags?include_inactive=true')
}
const stores = ref<Store[]>([])
const items = ref<ConversationItem[]>([])
const loading = ref(false)

const f = reactive({
  keyword: '', // 客户ID：多个用回车或逗号分隔
  storeId: '',
  site: '',
  tagId: '' as string,
  stage: '',
  aftersales: '', // aftersales 待售后 / aftersales_done 售后已完成 / invoice 待发发票
  reply: '', // 回复状态：review 待审核 / auto AI 自动回复 / manual 人工回复
  risk: '',
  days: 0, // 消息时间：0 全部 / 1 今天 / 7 / 30
  unreadOnly: false,
})
const applied = ref('') // 点“搜索”后生效的客户 ID
const keywords = computed(() => applied.value.split(/[\n,，\s]+/).map((s) => s.trim().toLowerCase()).filter(Boolean))

const SITES = computed(() => {
  const s = new Set<string>()
  stores.value.forEach((x) => s.add(x.default_marketplace_id))
  items.value.forEach((x) => x.marketplace_id && x.marketplace_id !== 'UNKNOWN' && s.add(x.marketplace_id))
  return [...s].sort()
})
const siteLabel = (m: string) => m.replace('EBAY_', '')

type FilterCounts = {
  all: number; invoice: number; aftersales: number; aftersales_done: number; tags: Record<string, number>
  views?: Record<string, number>; stages?: Record<string, number>
}
const counts = ref<FilterCounts | null>(null)

// 左侧消息箱 → 接口参数
function boxQuery(): URLSearchParams {
  const q = new URLSearchParams()
  const b = props.box
  q.set('kind', b.startsWith('system') ? 'system' : 'buyer')
  // 平台消息：未处理 = 还有未读，已处理 = 都读过了
  if (b === 'system_unread') q.set('unread_only', 'true')
  if (b === 'system_read') q.set('read_only', 'true')
  if (!b.startsWith('system')) q.set('view', b === 'pending' ? 'pending' : b === 'auto' ? 'auto' : 'all')
  q.set('group', ['resolved', 'handoff', 'failed', 'unassigned', 'mine'].includes(b) ? b : 'all')
  return q
}

async function refresh() {
  loading.value = true
  loadTags()
  try {
    const q = boxQuery()
    if (f.tagId) q.set('tag_id', f.tagId)
    if (f.stage) q.set('stage', f.stage)
    if (f.storeId) q.set('store_id', f.storeId)
    if (keywords.value.length === 1) q.set('q', keywords.value[0]) // 多个客户 ID 在本地过滤
    if (f.unreadOnly) q.set('unread_only', 'true')
    const cq = new URLSearchParams()
    for (const k of ['store_id', 'q', 'unread_only', 'view']) if (q.has(k)) cq.set(k, q.get(k)!)
    const [list, storeList, c] = await Promise.all([
      get<ConversationItem[]>(`/internal/conversations?${q}`),
      get<Store[]>('/internal/stores'),
      isSystem.value ? null : get<FilterCounts>(`/internal/conversations/filter-counts?${cq}`).catch(() => null),
    ])
    items.value = list
    stores.value = storeList
    counts.value = c
  } finally {
    loading.value = false
  }
}

// 本地再过滤一遍：多个客户 ID、站点、售后情况、回复状态、风险、消息时间
const rows = computed(() => {
  const since = f.days ? Date.now() - f.days * 86400_000 : 0
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return items.value.filter((c) => {
    if (keywords.value.length > 1 && !keywords.value.some((k) => c.buyer_key.toLowerCase().includes(k))) return false
    if (f.site && c.marketplace_id !== f.site) return false
    if (f.aftersales === 'aftersales' && !['PENDING', 'IN_PROGRESS'].includes(c.aftersales?.status || '')) return false
    if (f.aftersales === 'aftersales_done' && c.aftersales?.status !== 'DONE') return false
    if (f.aftersales === 'invoice' && c.invoice_status !== 'OPEN') return false
    if (f.reply === 'review' && !(c.job && REVIEW_STATES.includes(c.job.state))) return false
    if (f.reply === 'auto' && !c.auto_replied) return false
    if (f.reply === 'manual' && !(c.job?.state === 'SENT' && !c.auto_replied)) return false
    if (f.risk && c.job?.risk_level !== f.risk) return false
    if (f.days && c.last_message_at) {
      const t = new Date(c.last_message_at).getTime()
      if (f.days === 1 ? t < today.getTime() : t < since) return false
    }
    return true
  })
})

// ---------------- 分页 ----------------
const page = ref(1)
const pageSize = ref(20)
const pageRows = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(() => rows.value.length, (n) => {
  const max = Math.max(1, Math.ceil(n / pageSize.value))
  if (page.value > max) page.value = max
})

// ---------------- 商品图片 / SKU（当前页按需加载，缓存在本页） ----------------
const listings = reactive<Record<number, ListingCard | null>>({})
let fetching = 0
const queue: number[] = []
function pumpListings() {
  while (fetching < 4 && queue.length) {
    const id = queue.shift()!
    fetching++
    get<ListingCard>(`/internal/conversations/${id}/listing`)
      .then((r) => (listings[id] = r))
      .catch(() => (listings[id] = null))
      .finally(() => {
        fetching--
        pumpListings()
      })
  }
}
watch(pageRows, (list) => {
  for (const c of list) {
    if (!c.item_id || c.id in listings || queue.includes(c.id)) continue
    listings[c.id] = null // 占位，避免重复请求
    queue.push(c.id)
  }
  pumpListings()
}, { immediate: true })

// ---------------- 刷新时机 ----------------
watch(() => [f.storeId, f.tagId, f.stage, f.unreadOnly, props.box], () => {
  page.value = 1
  refresh()
})
watch(() => [f.site, f.aftersales, f.reply, f.risk, f.days], () => (page.value = 1))
function search() {
  applied.value = f.keyword
  page.value = 1
  refresh()
}
function reset() {
  Object.assign(f, { keyword: '', storeId: '', site: '', tagId: '', stage: '', aftersales: '', reply: '', risk: '', days: 0, unreadOnly: false })
  applied.value = ''
  refresh()
}
function toggleTag(id: number) {
  f.tagId = f.tagId === String(id) ? '' : String(id)
}

// ---------------- AI 自动回复开关（选中店铺时，管理员） ----------------
const autoSettings = ref<{ auto_send_enabled: boolean } | null>(null)
const currentStore = computed(() => stores.value.find((s) => s.id === f.storeId) || null)
const autoSwitching = ref(false)
async function toggleAutoReply(enabled: boolean) {
  const s = currentStore.value
  if (!s) return
  try {
    await ElMessageBox.confirm(
      enabled
        ? `开启后，「${s.name}」24 小时内的新买家来信会自动交给 AI；有依据（知识库、商品信息、订单物流查询结果）的一般问题自动回复（对接了 eBay 的店铺会真实发给买家，发出前有几分钟可撤回）；退款取消、适配结论、议价、缺信息或没把握的进待审核。`
        : `关闭后，「${s.name}」的新来信不再自动交给 AI，也不会自动发送。`,
      enabled ? '开启 AI 自动回复' : '关闭 AI 自动回复',
      { type: enabled ? 'warning' : 'info' },
    )
  } catch {
    return
  }
  autoSwitching.value = true
  try {
    await post(`/internal/stores/${s.id}/auto-reply`, { enabled })
    await refresh()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    autoSwitching.value = false
  }
}

// ---------------- 标记项管理（管理员） ----------------
const tagDialog = ref(false)
const newTag = ref({ name: '', color: '#3a7bfd' })
async function saveTag(tag: MessageTag | null, body: Record<string, unknown>) {
  try {
    if (tag) await api('PATCH', `/internal/message-tags/${tag.id}`, body)
    else {
      await post('/internal/message-tags', body)
      newTag.value = { name: '', color: newTag.value.color }
    }
    await loadTags()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function deleteTag(tag: MessageTag) {
  const used = (counts.value as any)?.tags?.[String(tag.id)]
  try {
    await ElMessageBox.confirm(
      `删除标记「${tag.name}」？${used ? `当前有 ${used} 个会话打着它，` : ''}所有会话上的这个标记会一起去掉，不能恢复。` +
        '只是暂时不用的话，可以关掉“可选”。',
      '删除标记', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
  } catch {
    return
  }
  try {
    const r = await api<{ removed_marks: number }>('DELETE', `/internal/message-tags/${tag.id}`)
    ElMessage.success(r.removed_marks ? `已删除，${r.removed_marks} 个会话上的这个标记已去掉` : '已删除')
    if (f.tagId === String(tag.id)) f.tagId = ''
    await loadTags()
    refresh()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---------------- 已读 / 打开 ----------------
async function setRead(id: number, read = true) {
  try {
    await post(`/internal/conversations/${id}/read`, { read })
  } finally {
    await refresh()
    emit('counts')
  }
}
function open(c: ConversationItem) {
  if (c.id === props.focus && c.unread_count) setRead(c.id)
  emit('select', c.id)
}
watch(() => props.focus, (id) => {
  if (id) setRead(id)
})

// ---------------- 批量操作 ----------------
const selected = ref<ConversationItem[]>([])
const busy = ref(false)
async function batch(label: string, fn: (c: ConversationItem) => Promise<unknown>) {
  if (!selected.value.length) return ElMessage.warning('请先勾选会话')
  busy.value = true
  let ok = 0
  const errs: string[] = []
  for (const c of selected.value) {
    try {
      await fn(c)
      ok++
    } catch (e) {
      errs.push(`${c.buyer_key}：${errorText(e)}`)
    }
  }
  busy.value = false
  if (errs.length) ElMessage.warning(`${label}：成功 ${ok} 个，失败 ${errs.length} 个（${errs[0]}）`)
  else ElMessage.success(`${label}：${ok} 个会话`)
  await refresh()
  emit('counts')
}
const activeTags = computed(() => tags.value.filter((t) => t.is_active))
function onMark(cmd: string) {
  const t = tags.value.find((x) => String(x.id) === cmd)
  if (t) batch(`打上「${t.name}」`, (c) => api('PUT', `/internal/conversations/${c.id}/mark`, { tag_id: t.id }))
}
function onUnmark(cmd: string) {
  const t = tags.value.find((x) => String(x.id) === cmd)
  if (t) batch(`去掉「${t.name}」`, (c) => api('DELETE', `/internal/conversations/${c.id}/mark?tag_id=${t.id}`))
}
async function onBatch(cmd: string) {
  if (cmd === 'read') {
    if (!selected.value.length) return ElMessage.warning('请先勾选会话')
    await post('/internal/conversations/read-batch', { conversation_ids: selected.value.map((c) => c.id) })
    ElMessage.success('已标为已读')
    await refresh()
    emit('counts')
  } else if (cmd === 'unread') batch('标为未读', (c) => post(`/internal/conversations/${c.id}/read`, { read: false }))
  else if (cmd === 'readAll') {
    const ids = items.value.filter((c) => c.unread_count).map((c) => c.id)
    if (!ids.length) return ElMessage.info('当前列表没有未读会话')
    await post('/internal/conversations/read-batch', { conversation_ids: ids })
    await refresh()
    emit('counts')
  } else if (cmd === 'release') batch('释放认领', (c) => post(`/internal/conversations/${c.id}/claim`, { action: 'release' }))
}
const claimMine = () => batch('认领给我', (c) => post(`/internal/conversations/${c.id}/claim`, { action: 'claim' }))
async function claimOne(c: ConversationItem) {
  try {
    await post(`/internal/conversations/${c.id}/claim`, { action: 'claim' })
    await refresh()
    emit('counts')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const syncing = ref(false)
async function syncNow() {
  syncing.value = true
  try {
    await post('/internal/ebay/sync')
    ElMessage.success('已从 eBay 拉取最新消息')
    await refresh()
    emit('counts')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    syncing.value = false
  }
}

function exportCsv() {
  const list = selected.value.length ? selected.value : rows.value
  const head = ['会话ID', '客户ID', '店铺', '站点', '刊登号', 'SKU', '最新消息', '发送方', '回复状态', '风险', '标记', '负责人', '最新消息时间']
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""').replace(/\r?\n/g, ' ')}"`
  const lines = list.map((c) => [
    c.id, c.buyer_key, c.store_name || c.store_id, c.marketplace_id, c.item_id, listings[c.id]?.sku, c.preview,
    ROLE_SHORT[c.preview_role || ''] || '', replyLabel(c), c.job?.risk_level, allMarks(c).map((m) => m.tag).join('、'),
    c.claim?.claimed_by, fmtFull(c.last_message_at),
  ].map(esc).join(','))
  const blob = new Blob(['﻿' + [head.map(esc).join(','), ...lines].join('\r\n')], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${boxLabel.value}_${fmtFull(new Date().toISOString()).slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}

// ---------------- 浏览器标题（买家未读） ----------------
const totalUnread = computed(() => stores.value.reduce((n, s) => n + (s.unread_conversations || 0), 0))
const BASE_TITLE = document.title
watch(totalUnread, (n) => (document.title = n ? `(${n}) ${BASE_TITLE}` : BASE_TITLE))

// 列表页生效的筛选项个数（回复页左上角显示）
const filterCount = computed(() =>
  [applied.value.trim(), f.storeId, f.site, f.tagId, f.stage, f.aftersales, f.reply, f.risk, f.days, f.unreadOnly].filter(Boolean).length)
const rowClass = ({ row }: { row: ConversationItem }) =>
  [row.id === props.focus ? 'is-focus' : '', row.unread_count ? 'is-unread' : ''].join(' ')

let timer: number | undefined
onMounted(async () => {
  loadTags()
  get('/internal/auto-reply/settings').then((r) => (autoSettings.value = r)).catch(() => null)
  await refresh()
  if (props.focus) setRead(props.focus)
  timer = window.setInterval(refresh, 6000)
})
onUnmounted(() => {
  clearInterval(timer)
  document.title = BASE_TITLE
})
</script>

<template>
  <div class="erp">
    <!-- ======== 筛选条件 ======== -->
    <div class="filters">
      <div class="f-row">
        <el-input v-model="f.keyword" class="f-kw" clearable :placeholder="isSystem ? '通知标题' : '多个请用回车或逗号分隔'"
                  @keyup.enter="search" @clear="search">
          <template #prepend>{{ isSystem ? '标题' : '客户ID' }}</template>
        </el-input>
        <el-select v-model="f.storeId" class="f-sel w-store" placeholder="eBay账号" clearable filterable>
          <el-option v-for="s in stores" :key="s.id" :value="s.id"
                     :label="`${s.name}${(isSystem ? s.unread_system : s.unread_conversations) ? `（${isSystem ? s.unread_system : s.unread_conversations}）` : ''}`" />
        </el-select>
        <el-select v-model="f.site" class="f-sel" placeholder="站点" clearable>
          <el-option v-for="m in SITES" :key="m" :label="siteLabel(m)" :value="m" />
        </el-select>
        <template v-if="!isSystem">
          <el-select v-model="f.tagId" class="f-sel" placeholder="标签" clearable filterable>
            <el-option v-for="t in tags" :key="t.id" :value="String(t.id)" :label="t.is_active ? t.name : `${t.name}（已停用）`" />
          </el-select>
          <el-select v-model="f.stage" class="f-sel" placeholder="会话阶段" clearable>
            <el-option v-for="(label, k) in STAGE_LABELS" :key="k" :value="k"
                       :label="counts?.stages ? `${label}（${counts.stages[k] || 0}）` : label" />
          </el-select>
          <el-select v-model="f.reply" class="f-sel" placeholder="回复状态" clearable>
            <el-option label="待审核" value="review" />
            <el-option label="AI 自动回复" value="auto" />
            <el-option label="人工回复" value="manual" />
          </el-select>
          <el-select v-model="f.aftersales" class="f-sel" placeholder="售后情况" clearable>
            <el-option :label="`待售后处理${counts ? `（${counts.aftersales}）` : ''}`" value="aftersales" />
            <el-option :label="`售后已完成${counts ? `（${counts.aftersales_done}）` : ''}`" value="aftersales_done" />
            <el-option :label="`待发发票${counts ? `（${counts.invoice}）` : ''}`" value="invoice" />
          </el-select>
          <el-select v-model="f.risk" class="f-sel w-s" placeholder="风险" clearable>
            <el-option v-for="r in ['L0', 'L1', 'L2', 'L3']" :key="r" :label="r" :value="r" />
          </el-select>
        </template>
        <el-checkbox v-model="f.unreadOnly" class="f-check">只看未读</el-checkbox>
        <el-button class="btn-search" plain type="primary" @click="search">搜索</el-button>
        <el-button link @click="reset">重置</el-button>
      </div>
      <!-- 标签快捷筛选 -->
      <div v-if="!isSystem" class="tag-row">
        <span class="tag-title">标签:</span>
        <div class="chips">
          <button v-for="t in tags.filter((x) => x.is_active || counts?.tags[String(x.id)])" :key="t.id" class="chip"
                  :class="{ on: f.tagId === String(t.id) }" @click="toggleTag(t.id)">
            <i class="dot" :style="dotStyle(t.color)" />{{ t.name }}
            <span class="n">({{ counts?.tags[String(t.id)] || 0 }})</span>
          </button>
          <el-button v-if="isAdmin" link type="primary" size="small" @click="tagDialog = true">配置</el-button>
        </div>
      </div>
    </div>

    <!-- ======== 工具栏 ======== -->
    <div class="toolbar">
      <template v-if="!isSystem">
        <el-dropdown trigger="click" :disabled="busy" @command="onMark">
          <el-button>标记<span class="arr">▾</span></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="t in activeTags" :key="t.id" :command="String(t.id)">
                <i class="dot" :style="dotStyle(t.color)" />{{ t.name }}
              </el-dropdown-item>
              <el-dropdown-item v-if="!activeTags.length" disabled>还没有标记项</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-dropdown trigger="click" :disabled="busy" @command="onUnmark">
          <el-button>去标记<span class="arr">▾</span></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="t in tags" :key="t.id" :command="String(t.id)">{{ t.name }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
      <el-dropdown trigger="click" :disabled="busy" @command="onBatch">
        <el-button>批处理<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="read">勾选的标为已读</el-dropdown-item>
            <el-dropdown-item command="unread">勾选的标为未读</el-dropdown-item>
            <el-dropdown-item command="readAll" divided>当前列表全部标为已读</el-dropdown-item>
            <el-dropdown-item v-if="!isSystem" command="release" divided>释放勾选的认领</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button v-if="isAdmin" :loading="syncing" @click="syncNow">手动同步</el-button>
      <el-button @click="exportCsv">导出</el-button>
      <el-button v-if="!isSystem" :disabled="busy" @click="claimMine">分配负责人</el-button>
      <span v-if="selected.length" class="picked">已选 {{ selected.length }} 项</span>

      <div class="tb-right">
        <template v-if="currentStore && isAdmin && !isSystem">
          <el-switch :model-value="currentStore.auto_send_enabled" :loading="autoSwitching" active-text="AI 自动回复"
                     @update:model-value="(v: string | number | boolean) => toggleAutoReply(!!v)" />
          <el-tooltip v-if="currentStore.auto_send_enabled && autoSettings && !autoSettings.auto_send_enabled"
                      content="总开关未开（.env AUTO_SEND_ENABLED），只自动生成草稿">
            <span class="warn-text">总开关未开</span>
          </el-tooltip>
        </template>
        <el-select v-model="f.days" class="f-days">
          <template #prefix>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
              <rect x="4" y="5" width="16" height="15" rx="2" /><path d="M4 10h16M9 3v4M15 3v4" />
            </svg>
          </template>
          <el-option label="全部时间" :value="0" />
          <el-option label="今天" :value="1" />
          <el-option label="近7天" :value="7" />
          <el-option label="近30天" :value="30" />
        </el-select>
        <span class="total">共{{ rows.length }}条记录</span>
      </div>
    </div>

    <!-- ======== 列表 ======== -->
    <div class="table-wrap">
      <el-table v-loading="loading && !items.length" :data="pageRows" height="100%" row-key="id" :row-class-name="rowClass"
                :empty-text="isSystem ? '暂无平台消息' : '暂无会话'" @row-click="open"
                @selection-change="(v: ConversationItem[]) => (selected = v)">
        <el-table-column type="selection" width="44" />
        <el-table-column label="图片" width="86">
          <template #default="{ row }">
            <div class="thumb">
              <img v-if="listings[row.id]?.picture_url" :src="listings[row.id]!.picture_url!" alt="" loading="lazy" />
              <svg v-else viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <rect x="3.5" y="5" width="17" height="14" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m4 17 5-4.5 4 3.5 3-2.5 4 3.5" />
              </svg>
              <span v-if="row.unread_count" class="unread" :title="`${row.unread_count} 条未读`">{{ row.unread_count }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="isSystem ? '通知' : '客户ID'" width="160">
          <template #default="{ row }">
            <div class="buyer" :title="row.buyer_key">{{ isSystem ? row.conversation_title || 'eBay 通知' : row.buyer_key }}</div>
            <div class="sub">#{{ row.id }}<span v-if="row.channel_source === 'ebay'" class="src">eBay</span></div>
          </template>
        </el-table-column>
        <el-table-column label="最新消息" min-width="380">
          <template #default="{ row }">
            <div class="msg" :class="{ bold: row.unread_count }">
              <span class="who" :class="row.preview_role === 'BUYER' ? 'buyer-role' : 'seller-role'">
                {{ ROLE_SHORT[row.preview_role || ''] || '—' }}:
              </span>{{ row.preview || '（无内容）' }}
            </div>
            <div class="sub">
              {{ fmtRel(row.last_message_at) }}
              <span v-if="row.intent"> · {{ intentLabel(row.intent) }}</span>
              <span v-if="row.waiting_seconds != null" class="wait" :class="{ late: row.waiting_seconds > 900 }">
                · 已等待 {{ fmtWaiting(row.waiting_seconds) }}
              </span>
            </div>
            <div v-if="!isSystem" class="tags">
              <el-tag v-if="row.job" size="small" :type="row.auto_replied ? 'success' : stateTag(row.job.state)"
                      :effect="REVIEW_STATES.includes(row.job.state) ? 'dark' : 'light'"
                      :class="{ 'review-hot': REVIEW_STATES.includes(row.job.state) }">{{ replyLabel(row) }}</el-tag>
              <el-tag v-if="row.job?.risk_level" size="small" :type="riskTag(row.job.risk_level)" effect="plain">{{ row.job.risk_level }}</el-tag>
              <el-tag v-if="row.handoff_state === 'HUMAN'" type="danger" size="small" effect="plain">已接管</el-tag>
              <el-tag v-if="row.blacklist" type="danger" size="small" effect="dark" :title="row.blacklist.reason || ''">黑名单</el-tag>
              <el-tag v-if="row.outbox_pending" type="warning" size="small">待发送 {{ row.outbox_pending }}</el-tag>
              <el-tag v-if="row.outbox_problem" type="danger" size="small">发送失败</el-tag>
              <el-tag v-for="m in manualMarks(row)" :key="m.tag_id" size="small" :style="tagStyle(m.color, true)"
                      :title="`${m.marked_by} 标记${m.note ? `：${m.note}` : ''}`">{{ m.tag }}</el-tag>
              <SituationChip v-if="row.situation" :s="row.situation" />
              <el-tag v-for="m in aiMarks(row)" :key="m.tag_id" size="small" :style="tagStyle(m.color)" class="ai-tag"
                      :title="`AI 自动标记${m.note ? `：${m.note}` : ''}`">{{ m.tag }}</el-tag>
              <el-tag v-if="row.invoice_status === 'OPEN'" size="small" type="warning" class="ai-tag">要发票</el-tag>
              <el-tag v-if="row.aftersales" size="small" effect="plain" :type="aftersalesType(row)"
                      :class="{ 'ai-tag': isAi(row.aftersales.marked_by) }">{{ aftersalesLabel(row) }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="平台/账号" width="180">
          <template #default="{ row }">
            <div class="store">
              <span class="ebay-mini" aria-hidden="true"><i /><i /><i /><i /></span>
              <span :title="`店铺 ID：${row.store_id}`">{{ row.store_name || row.store_id }}</span>
            </div>
            <div class="sub">站点：{{ siteLabel(row.marketplace_id) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="SKU/信息" width="230">
          <template #default="{ row }">
            <template v-if="row.item_id">
              <div class="kv"><span>平台:</span><b class="dotted">{{ listings[row.id]?.sku || '—' }}</b></div>
              <div class="kv">
                <span>Listing:</span>
                <a v-if="row.item_url" class="dotted" :href="row.item_url" target="_blank" rel="noopener noreferrer" @click.stop>{{ row.item_id }}</a>
                <b v-else class="dotted">{{ row.item_id }}</b>
              </div>
              <div v-if="row.conversation_title" class="kv title" :title="row.conversation_title">{{ row.conversation_title }}</div>
            </template>
            <span v-else class="sub">—</span>
          </template>
        </el-table-column>
        <el-table-column width="96" align="center">
          <template #header>
            <el-tooltip content="正在处理这个会话的客服（认领 10 分钟内有效）" placement="top">
              <span class="th-help">负责人 <i>?</i></span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span v-if="row.claim" class="owner" :title="`到期：${fmtFull(row.claim.expires_at)}`">{{ row.claim.claimed_by }}</span>
            <el-tooltip v-else-if="!isSystem" content="认领给我" placement="top">
              <button class="assign" @click.stop="claimOne(row)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
                  <circle cx="10" cy="8" r="3.5" /><path d="M3.5 20c0-3.6 2.9-6 6.5-6 1.3 0 2.5.3 3.5.9M18 14v6M15 17h6" />
                </svg>
              </button>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="消息时间" width="200">
          <template #default="{ row }">
            <div class="kv time"><span>最新:</span>{{ fmtFull(row.last_message_at) }}</div>
            <div v-if="row.job?.created_at" class="kv time"><span>任务:</span>{{ fmtFull(row.job.created_at) }}</div>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="rows.length"
                     layout="total, sizes, prev, pager, next, jumper" background small />
    </div>

    <!-- ======== 回复页面（盖在列表上，关掉回到列表） ======== -->
    <Teleport to="body">
      <div v-if="props.focus" class="reply-overlay" @click.self="emit('select', null)">
        <ReplyView :items="rows" :conv-id="props.focus" :box-label="boxLabel" :box-count="rows.length" :filter-count="filterCount"
                   @select="(id: number) => emit('select', id)" @close="emit('select', null)" @changed="refresh" />
      </div>
    </Teleport>

    <el-dialog v-model="tagDialog" title="消息标记选项" width="460px">
      <!-- 标记项列表：最多显示 10 行，多了在框里上下滑动 -->
      <div class="tag-list">
      <div v-for="t in tags" :key="t.id" class="tag-edit">
        <el-color-picker :model-value="pickerValue(t.color)" :predefine="TAG_PALETTE" size="small" color-format="hex" clearable
                         @change="(v: string | null) => saveTag(t, { color: fromPicker(v) })" />
        <el-tag :style="tagStyle(t.color, true)" size="small">{{ t.name }}</el-tag>
        <el-switch :model-value="t.is_active" size="small" active-text="可选" @update:model-value="(v: string | number | boolean) => saveTag(t, { is_active: !!v })" />
        <el-button class="tag-del" link type="danger" size="small" @click="deleteTag(t)">删除</el-button>
      </div>
      </div>
      <div class="tag-edit tag-new">
        <el-input v-model="newTag.name" size="small" placeholder="新标记名称，如 等补发" style="flex: 1" />
        <el-color-picker :model-value="pickerValue(newTag.color)" :predefine="TAG_PALETTE" size="small" color-format="hex" clearable @change="(v: string | null) => (newTag.color = fromPicker(v))" />
        <el-button size="small" type="primary" :disabled="!newTag.name.trim()" @click="saveTag(null, newTag)">添加</el-button>
      </div>
      <p class="muted">这些是给会话打标记时的可选项。停用的标记不能再选，已经打上的照常显示；删除会把所有会话上的这个标记一起去掉，AI 也不会再自动建回来。</p>
    </el-dialog>
  </div>
</template>

<style scoped>
.erp {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--panel);
}

/* ---- 筛选 ---- */
.filters {
  padding: 12px 16px 4px;
}
.f-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.f-kw {
  width: 268px;
}
.f-kw :deep(.el-input-group__prepend) {
  padding: 0 10px;
  background: var(--sunken);
  color: var(--text-2);
}
.f-sel {
  width: 108px;
}
.f-sel.w-store {
  width: 160px;
}
.f-sel.w-s {
  width: 84px;
}
.f-check {
  margin: 0 2px;
}
.btn-search {
  min-width: 64px;
}
.tag-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
}
.tag-title {
  flex-shrink: 0;
  line-height: 26px;
  font-size: 13px;
  color: var(--muted);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
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
  white-space: nowrap;
}
.chip:hover {
  border-color: var(--accent-line);
  color: var(--text);
}
.chip.on {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.chip .n {
  color: var(--muted);
}
.chip.on .n {
  color: var(--accent);
}
.dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 6px;
}
.chip .dot {
  margin-right: 0;
}

/* ---- 工具栏 ---- */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--line-soft);
}
.toolbar .el-dropdown + .el-dropdown,
.toolbar .el-dropdown + .el-button,
.toolbar .el-button + .el-button {
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
.tb-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 14px;
}
.f-days {
  width: 130px;
}
.total {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.warn-text {
  color: var(--warn-text);
  font-size: 12px;
}

/* ---- 表格 ---- */
.table-wrap {
  flex: 1;
  min-height: 0;
  padding: 0 16px;
}
.table-wrap :deep(.el-table__row) {
  cursor: pointer;
}
.table-wrap :deep(.el-table__row.is-focus > td) {
  background: var(--accent-soft) !important;
}
.table-wrap :deep(.el-table .cell) {
  line-height: 1.55;
}
.table-wrap :deep(.el-table td.el-table__cell) {
  vertical-align: top;
  padding: 10px 0;
}
.thumb {
  position: relative;
  width: 58px;
  height: 58px;
  border-radius: 4px;
  border: 1px solid var(--line-soft);
  background: var(--sunken);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #c3c8d0;
}
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 4px;
  background: #fff;
}
.unread {
  position: absolute;
  top: -6px;
  right: -7px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--danger);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
  font-weight: 700;
  box-shadow: 0 0 0 2px #fff;
}
.buyer {
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub {
  font-size: 12px;
  color: var(--muted);
}
.src {
  margin-left: 6px;
  padding: 0 4px;
  border-radius: 2px;
  font-size: 10px;
  color: #0064d2;
  background: #e8f1fc;
}
.msg {
  font-size: 13px;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.msg.bold {
  font-weight: 600;
}
.who {
  margin-right: 4px;
  color: var(--muted);
  font-weight: 400;
}
.who.seller-role {
  color: var(--accent);
}
.wait.late {
  color: var(--danger-text);
  font-weight: 600;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 5px;
}
.ai-tag {
  border-style: dashed;
}
.store {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  color: var(--text);
}
.ebay-mini {
  flex-shrink: 0;
  display: inline-grid;
  grid-template-columns: 5px 5px;
  gap: 1.5px;
}
.ebay-mini i {
  width: 5px;
  height: 5px;
  border-radius: 1px;
}
.ebay-mini i:nth-child(1) { background: #e53238; }
.ebay-mini i:nth-child(2) { background: #0064d2; }
.ebay-mini i:nth-child(3) { background: #f5af02; }
.ebay-mini i:nth-child(4) { background: #86b817; }
.kv {
  font-size: 12px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kv > span {
  color: var(--muted);
  margin-right: 4px;
}
.kv b {
  font-weight: 400;
}
.kv.title {
  color: var(--muted);
}
.kv.time {
  font-variant-numeric: tabular-nums;
}
.dotted {
  border-bottom: 1px dashed var(--line);
  color: var(--text);
  text-decoration: none;
}
a.dotted:hover {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.th-help i {
  display: inline-block;
  width: 13px;
  height: 13px;
  line-height: 11px;
  border: 1px solid var(--muted);
  border-radius: 50%;
  font-size: 9px;
  font-style: normal;
  text-align: center;
  color: var(--muted);
}
.owner {
  font-size: 12px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--accent);
}
.assign {
  border: 0;
  background: none;
  padding: 2px;
  color: #b4bac4;
  cursor: pointer;
}
.assign:hover {
  color: var(--accent);
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 10px 16px;
  border-top: 1px solid var(--line-soft);
}
.tag-list {
  max-height: 360px; /* 10 行 × 36px */
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  margin-bottom: 12px;
}
.tag-edit {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tag-list .tag-edit {
  height: 36px;
  padding: 0 10px;
  border-bottom: 1px solid var(--line-soft);
}
.tag-list .tag-edit:last-child {
  border-bottom: 0;
}
.tag-new {
  margin-bottom: 8px;
}
.tag-del {
  margin-left: auto;
}
@media (max-width: 900px) {
  .f-kw {
    width: 100%;
  }
  .tb-right {
    margin-left: 0;
  }
}
</style>

<style>
/* 回复页面：铺满窗口，四周留一点边，背后压暗 */
.reply-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  padding: 10px 16px;
  background: rgba(31, 35, 41, 0.45);
  display: flex;
}
.reply-overlay > .rv {
  flex: 1;
  min-width: 0;
  border-radius: 4px;
  overflow: hidden;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
}
@media (max-width: 760px) {
  .reply-overlay {
    padding: 0;
  }
}
</style>
