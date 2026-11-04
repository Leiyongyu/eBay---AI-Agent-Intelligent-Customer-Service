<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import { REVIEW_STATES, STAGE_LABELS, STATE_LABELS, errorText, fmtWaiting, intentLabel, riskTag, stateTag } from '../format'
import type { ConversationItem, MessageTag, Store } from '../types'
import ConversationPanel from '../components/ConversationPanel.vue'
import SituationChip from '../components/SituationChip.vue'

// 会话卡片右上角的最后一条消息时间：今天只显示时分，今年显示月-日 时分，更早显示年-月-日
function fmtLast(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  if (d.toDateString() === now.toDateString()) return hm
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${hm}`
  if (d.getFullYear() === now.getFullYear()) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const ROLE_SHORT: Record<string, string> = { BUYER: '买家', SELLER: '客服', SYSTEM: '系统', UNKNOWN: '未知' }

const props = defineProps<{ focus: number | null }>()
const emit = defineEmits<{ select: [id: number | null] }>()

// 列表默认显示全部会话（按最后消息时间）；筛选：售后队列，或某个消息标记
const tags = ref<MessageTag[]>([])
async function loadTags() {
  tags.value = await get<MessageTag[]>('/internal/message-tags?include_inactive=true')
}

// 会话卡片标签分组用：系统自动打的（打标人以 “AI” 开头，如 AI（自动标记）、AI（物流规则）、AI（查件结果），以及买家上传页）算 AI 智能标签
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

// 买家消息与 eBay 系统消息（退货、退款、取消等通知）分开看
const kind = ref<'buyer' | 'system'>('buyer')
// 售后默认看“售后待处理”，其他角色默认看“待审核”
const filter = ref(session.user?.role === 'AFTERSALES' ? 'aftersales' : 'all') // all / invoice / aftersales / aftersales_done / tag:ID
// 买家消息顶部三个页签：客服平时只看“待审核”（需要人工审核的会自动进来）；AI 已自动发出的在“自动回复”里抽查；人工已回复、生成中的在“全部”
type View = 'pending' | 'auto' | 'all'
const view = ref<View>(session.user?.role === 'AFTERSALES' ? 'all' : 'pending')
const VIEWS: { value: View; label: string }[] = [
  { value: 'pending', label: '待审核' }, { value: 'auto', label: '自动回复' }, { value: 'all', label: '全部' },
]
const storeId = ref<string>('')
const stores = ref<Store[]>([])
const items = ref<ConversationItem[]>([])
const loading = ref(false)
const keyword = ref('') // 按买家名模糊搜索
const unreadOnly = ref(false)
// 下拉筛选每项的会话数（按当前店铺、搜索词、只看未读统计）
type FilterCounts = {
  all: number; invoice: number; aftersales: number; aftersales_done: number; tags: Record<string, number>
  views?: Record<View, number>
  stages?: Record<string, number>
}
const counts = ref<FilterCounts | null>(null)
function withCount(label: string, n: number | undefined) {
  return n === undefined ? label : `${label}（${n}）`
}
const filterTotal = computed(() => {
  const c = counts.value
  if (!c || kind.value !== 'buyer') return undefined
  const f = filter.value
  if (f.startsWith('stage:')) return c.stages?.[f.slice(6)] || 0
  return f.startsWith('tag:') ? c.tags[f.slice(4)] || 0 : (c as unknown as Record<string, number>)[f]
})

// AI 自动回复：店铺开关 + 全局总开关（.env AUTO_SEND_ENABLED，只读）
const autoSettings = ref<{ auto_send_enabled: boolean; intents: string[]; clarify_intents: string[] } | null>(null)
const currentStore = computed(() => stores.value.find((s) => s.id === storeId.value) || null)
const autoSwitching = ref(false)

async function toggleAutoReply(enabled: boolean) {
  const s = currentStore.value
  if (!s) return
  try {
    await ElMessageBox.confirm(
      enabled
        ? `开启后，「${s.name}」24 小时内的新买家来信会自动交给 AI；有依据（知识库、商品信息、订单物流查询结果）的一般问题自动发出（目前是模拟发送，不发到 eBay）；退款取消、适配结论、议价、缺信息或没把握的进待审核。`
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

const scopeStores = computed(() => (currentStore.value ? [currentStore.value] : stores.value))
const buyerUnread = computed(() => scopeStores.value.reduce((n, s) => n + (s.unread_conversations || 0), 0))
const systemUnread = computed(() => scopeStores.value.reduce((n, s) => n + (s.unread_system || 0), 0))
// 浏览器标签标题只算买家消息
const totalUnread = computed(() => stores.value.reduce((n, s) => n + (s.unread_conversations || 0), 0))
const storeUnread = (s: Store) => (kind.value === 'system' ? s.unread_system : s.unread_conversations) || 0
const BASE_TITLE = document.title

async function refresh() {
  loading.value = true
  loadTags() // 标记项可能刚在会话弹窗里新建或删除
  try {
    // 系统消息没有回复任务，不分待审核等分组
    const f = kind.value === 'system' ? 'all' : filter.value
    const q = new URLSearchParams({ group: f.startsWith('tag:') || f.startsWith('stage:') ? 'all' : f, kind: kind.value })
    if (f.startsWith('tag:')) q.set('tag_id', f.slice(4))
    if (f.startsWith('stage:')) q.set('stage', f.slice(6))
    if (kind.value === 'buyer') q.set('view', view.value)
    if (storeId.value) q.set('store_id', storeId.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    if (unreadOnly.value) q.set('unread_only', 'true')
    const cq = new URLSearchParams()
    for (const k of ['store_id', 'q', 'unread_only', 'view']) if (q.has(k)) cq.set(k, q.get(k)!)
    const [list, storeList, c] = await Promise.all([
      get<ConversationItem[]>(`/internal/conversations?${q}`),
      get<Store[]>('/internal/stores'),
      kind.value === 'buyer' ? get<FilterCounts>(`/internal/conversations/filter-counts?${cq}`).catch(() => null) : null,
    ])
    items.value = list
    stores.value = storeList
    counts.value = c
  } finally {
    loading.value = false
  }
}

watch([filter, storeId, unreadOnly, kind, view], refresh)

// 管理标记项（只有管理员）
const tagDialog = ref(false)
const newTag = ref({ name: '', color: 'primary' })
const TAG_COLORS = [
  { value: 'primary', label: '蓝' }, { value: 'success', label: '绿' }, { value: 'warning', label: '橙' },
  { value: 'danger', label: '红' }, { value: 'info', label: '灰' },
]
async function saveTag(tag: MessageTag | null, body: Record<string, unknown>) {
  try {
    if (tag) await api('PATCH', `/internal/message-tags/${tag.id}`, body)
    else {
      await post('/internal/message-tags', body)
      newTag.value = { name: '', color: 'primary' }
    }
    await loadTags()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
let searchTimer: number | undefined
watch(keyword, () => {
  clearTimeout(searchTimer)
  searchTimer = window.setTimeout(refresh, 300)
})
watch(totalUnread, (n) => {
  document.title = n ? `(${n}) ${BASE_TITLE}` : BASE_TITLE
})

// 打开会话即标为已读（团队共享，只改本系统，不写回 eBay）
async function setRead(id: number, read = true) {
  try {
    await post(`/internal/conversations/${id}/read`, { read })
  } finally {
    await refresh()
  }
}

function open(c: ConversationItem) {
  // 切换会话由下面的 watch 标已读；再次点击当前会话（打开期间来了新消息）在这里标
  if (c.id === props.focus && c.unread_count) setRead(c.id)
  emit('select', c.id)
}

// 测试期：删除会话后回到空白详情；“清空测试会话”删掉全部（或当前店铺的）测试会话（正式上线前移除）
function onDeleted() {
  emit('select', null)
  refresh()
}

async function purgeTest() {
  if (!session.reviewer) return ElMessage.warning('请先在顶栏填写审核员名称')
  const scope = currentStore.value ? `「${currentStore.value.name}」的` : '全部店铺的'
  try {
    await ElMessageBox.confirm(`删除${scope}所有测试会话（消息、图片、AI 草稿和回复任务）？从 eBay 拉取的真实会话不受影响，删除后不能恢复。`,
                               '清空测试会话', { type: 'warning', confirmButtonText: '全部删除' })
  } catch {
    return
  }
  try {
    const r = await post<{ conversations: number; skipped_busy: number }>('/internal/conversations/purge-test',
                                                                         { store_id: storeId.value || null })
    ElMessage.success(`已删除 ${r.conversations} 个测试会话${r.skipped_busy ? `；${r.skipped_busy} 个 AI 正在处理，已跳过` : ''}`)
    onDeleted()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function readAll() {
  const ids = items.value.filter((c) => c.unread_count).map((c) => c.id)
  if (!ids.length) return
  await post('/internal/conversations/read-batch', { conversation_ids: ids })
  await refresh()
}

watch(
  () => props.focus,
  (id) => {
    if (id) setRead(id)
  },
)

let timer: number | undefined
onMounted(async () => {
  // 从其他页面跳转过来的会话可能不在“待审核”里（还在生成），切到“全部”方便看到
  if (props.focus) {
    filter.value = 'all'
    view.value = 'all'
  }
  loadTags()
  get('/internal/auto-reply/settings').then((r) => (autoSettings.value = r))
  await refresh()
  if (props.focus) setRead(props.focus)
  timer = window.setInterval(refresh, 4000)
})
onUnmounted(() => {
  clearInterval(timer)
  clearTimeout(searchTimer)
  document.title = BASE_TITLE
})
</script>

<template>
  <div class="wb">
    <aside class="panel queue">
      <div class="kinds">
        <button :class="{ active: kind === 'buyer' }" @click="kind = 'buyer'">
          买家消息<span v-if="buyerUnread" class="badge">{{ buyerUnread }}</span>
        </button>
        <button :class="{ active: kind === 'system' }" @click="kind = 'system'">
          eBay 系统消息<span v-if="systemUnread" class="badge muted-badge">{{ systemUnread }}</span>
        </button>
      </div>
      <div v-if="kind === 'buyer'" class="views">
        <button v-for="v in VIEWS" :key="v.value" :class="{ active: view === v.value }" @click="view = v.value">
          {{ v.label }}<span v-if="counts?.views" class="badge" :class="{ 'muted-badge': v.value !== 'pending' }">{{ counts.views[v.value] }}</span>
        </button>
      </div>
      <div class="panel-head">
        <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 210px">
          <el-option
            v-for="s in stores"
            :key="s.id"
            :label="`${s.name} · ${s.default_marketplace_id.replace('EBAY_', '')}${storeUnread(s) ? `（${storeUnread(s)} 未读）` : ''}`"
            :value="s.id"
          />
        </el-select>
        <span class="muted" style="margin-left: auto">
          {{ filterTotal !== undefined && filterTotal > items.length ? `显示 ${items.length} / 共 ${filterTotal}` : items.length }} 个会话
        </span>
      </div>
      <div v-if="currentStore && session.user?.role === 'ADMIN'" class="auto-bar">
        <el-switch
          :model-value="currentStore.auto_send_enabled"
          :loading="autoSwitching"
          size="small"
          active-text="AI 自动回复"
          @update:model-value="(v: string | number | boolean) => toggleAutoReply(!!v)"
        />
        <span v-if="currentStore.auto_send_enabled && autoSettings && !autoSettings.auto_send_enabled" class="muted warn-text">
          总开关未开（.env AUTO_SEND_ENABLED），只自动生成草稿
        </span>
      </div>
      <div class="search">
        <el-input v-model="keyword" size="small" clearable
                  :placeholder="kind === 'system' ? '搜索通知标题（模糊匹配）' : '搜索买家名（模糊匹配）'" />
      </div>
      <div v-if="kind === 'buyer'" class="filter-bar">
        <el-select v-model="filter" size="small" style="flex: 1">
          <el-option :label="withCount(view === 'all' ? '全部会话' : `全部${VIEWS.find((v) => v.value === view)?.label}`, counts?.all)" value="all" />
          <el-option :label="withCount('待审核发票（买家已给邮箱）', counts?.invoice)" value="invoice" />
          <el-option-group label="售后">
            <el-option :label="withCount('待售后处理', counts?.aftersales)" value="aftersales" />
            <el-option :label="withCount('售后已完成', counts?.aftersales_done)" value="aftersales_done" />
          </el-option-group>
          <el-option-group label="会话阶段（AI 判断）">
            <el-option v-for="(label, k) in STAGE_LABELS" :key="k" :label="withCount(label, counts?.stages?.[k])" :value="`stage:${k}`" />
          </el-option-group>
          <el-option-group label="会话标记">
            <el-option v-for="t in tags" :key="t.id"
                       :label="withCount(t.is_active ? t.name : `${t.name}（已停用）`, counts ? counts.tags[String(t.id)] || 0 : undefined)"
                       :value="`tag:${t.id}`" />
          </el-option-group>
        </el-select>
        <el-button v-if="session.user?.role === 'ADMIN'" link size="small" @click="tagDialog = true">管理标记</el-button>
      </div>
      <el-dialog v-model="tagDialog" title="消息标记选项" width="460px">
        <div v-for="t in tags" :key="t.id" class="tag-row">
          <el-tag :type="t.color" size="small">{{ t.name }}</el-tag>
          <el-select :model-value="t.color" size="small" style="width: 70px" @update:model-value="(v: string) => saveTag(t, { color: v })">
            <el-option v-for="c in TAG_COLORS" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
          <el-switch :model-value="t.is_active" size="small" active-text="可选" @update:model-value="(v: string | number | boolean) => saveTag(t, { is_active: !!v })" />
        </div>
        <div class="tag-row">
          <el-input v-model="newTag.name" size="small" placeholder="新标记名称，如 等补发" style="flex: 1" />
          <el-select v-model="newTag.color" size="small" style="width: 70px">
            <el-option v-for="c in TAG_COLORS" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
          <el-button size="small" type="primary" :disabled="!newTag.name.trim()" @click="saveTag(null, newTag)">添加</el-button>
        </div>
        <p class="muted">这些是给会话打标记时的可选项。停用的标记不能再选，已经打上的照常显示。</p>
      </el-dialog>
      <div class="read-bar">
        <el-checkbox v-model="unreadOnly" size="small">只看未读</el-checkbox>
        <span>
          <el-button v-if="session.meta?.test_delete_enabled && kind === 'buyer'" link size="small" type="danger" @click="purgeTest">
            清空测试会话
          </el-button>
          <el-button link size="small" type="primary" :disabled="!items.some((c) => c.unread_count)" @click="readAll">
            全部标为已读
          </el-button>
        </span>
      </div>
      <div class="panel-body list" v-loading="loading && !items.length">
        <el-empty v-if="!items.length" description="暂无会话。可用“客户测试消息系统”发一条试试" :image-size="70" />
        <div
          v-for="c in items"
          :key="c.id"
          class="item"
          :class="{ selected: c.id === props.focus, unread: c.unread_count > 0 }"
          @click="open(c)"
        >
          <span v-if="c.last_message_at" class="when" :title="new Date(c.last_message_at).toLocaleString('zh-CN', { hour12: false })">
            {{ fmtLast(c.last_message_at) }}
          </span>
          <div class="row first-row">
            <b v-if="kind === 'system'" class="buyer sys-title">{{ c.conversation_title || 'eBay 通知' }}</b>
            <b v-else class="buyer">{{ c.buyer_key }}</b>
            <span v-if="c.unread_count" class="badge" :title="`${c.unread_count} 条未读`">{{ c.unread_count }}</span>
            <el-tag v-if="c.channel_source === 'ebay'" size="small" type="primary">eBay</el-tag>
            <span class="muted" :title="`店铺 ID：${c.store_id}`">{{ c.store_name || c.store_id }} · {{ c.marketplace_id }}</span>
            <el-tag v-if="c.job?.risk_level" :type="riskTag(c.job.risk_level)" size="small">{{ c.job.risk_level }}</el-tag>
          </div>
          <!-- 标签按三组排：① 回复状态（待审核 / 人工回复 / AI 自动回复）② 人工打的标记 ③ AI 智能标签（会话情况、自动标记、发票、AI 转的售后） -->
          <div v-if="kind === 'buyer'" class="tag-groups">
            <span v-if="c.job || c.handoff_state === 'HUMAN'" class="tag-group">
              <el-tag v-if="c.job" :type="c.auto_replied ? 'success' : stateTag(c.job.state)" size="small" effect="plain"
                      :class="{ 'review-hot': REVIEW_STATES.includes(c.job.state) }">{{ replyLabel(c) }}</el-tag>
              <el-tag v-if="c.handoff_state === 'HUMAN'" type="danger" size="small" effect="plain">已接管</el-tag>
            </span>
            <span v-if="manualMarks(c).length || (c.aftersales && !isAi(c.aftersales.marked_by))" class="tag-group">
              <el-tag v-for="m in manualMarks(c)" :key="m.tag_id" size="small" :type="m.color"
                      :title="`${m.marked_by} 标记${m.note ? `：${m.note}` : ''}`">{{ m.tag }}</el-tag>
              <el-tag v-if="c.aftersales && !isAi(c.aftersales.marked_by)" size="small" effect="plain" :type="aftersalesType(c)">
                {{ aftersalesLabel(c) }}
              </el-tag>
            </span>
            <span v-if="c.situation || aiMarks(c).length || c.invoice_status === 'OPEN' || (c.aftersales && isAi(c.aftersales.marked_by))"
                  class="tag-group" title="AI 智能标签">
              <SituationChip v-if="c.situation" :s="c.situation" />
              <el-tag v-for="m in aiMarks(c)" :key="m.tag_id" size="small" :type="m.color" effect="plain" class="ai-tag"
                      :title="`AI 按问题类型自动标记${m.note ? `：${m.note}` : ''}`">{{ m.tag }}</el-tag>
              <el-tag v-if="c.invoice_status === 'OPEN'" size="small" type="warning" effect="plain" class="ai-tag">待发发票</el-tag>
              <el-tag v-if="c.aftersales && isAi(c.aftersales.marked_by)" size="small" effect="plain" class="ai-tag"
                      :type="aftersalesType(c)" :title="`${c.aftersales.marked_by} 自动转售后`">{{ aftersalesLabel(c) }}</el-tag>
            </span>
          </div>
          <div v-if="c.conversation_title && kind === 'buyer'" class="title-line">
            <a v-if="c.item_url" :href="c.item_url" target="_blank" rel="noopener noreferrer" title="在 eBay 打开商品" @click.stop>↗</a>
            {{ c.conversation_title }}
          </div>
          <div class="preview">
            <span class="muted">{{ ROLE_SHORT[c.preview_role || ''] || '—' }}：</span>{{ c.preview }}
          </div>
          <div class="row muted">
            <span>#{{ c.id }}</span>
            <span v-if="c.intent">· {{ intentLabel(c.intent) }}</span>
            <span v-if="c.waiting_seconds != null" class="wait" :class="{ late: c.waiting_seconds > 900 }">
              已等待 {{ fmtWaiting(c.waiting_seconds) }}
            </span>
            <span v-if="c.claim" class="claim">{{ c.claim.claimed_by }} 认领</span>
            <el-button
              class="read-toggle"
              link
              size="small"
              :title="c.read_by ? `最后由 ${c.read_by} 标记` : ''"
              @click.stop="setRead(c.id, !!c.unread_count)"
            >
              {{ c.unread_count ? '标为已读' : '标为未读' }}
            </el-button>
          </div>
        </div>
      </div>
    </aside>
    <section class="detail">
      <ConversationPanel v-if="props.focus" :key="props.focus" :conv-id="props.focus" @changed="refresh" @deleted="onDeleted" />
      <div v-else class="panel placeholder">
        <el-empty description="从左侧选择一个会话" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.wb {
  display: grid;
  grid-template-columns: 348px 1fr;
  gap: 14px;
  height: 100%;
}
.queue {
  min-height: 0;
}
.filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 0;
}
.tag-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
/* 待审核 / 自动回复 / 全部：分段按钮，和上面的“买家消息 / 系统消息”区分开 */
.views {
  display: flex;
  gap: 2px;
  margin: 8px 10px 0;
  padding: 2px;
  border-radius: 8px;
  background: var(--panel-soft, #eef2f1);
}
.views button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--muted);
  padding: 5px 0;
  font-size: var(--fs-sm);
  font-family: inherit;
  cursor: pointer;
}
.views button:hover {
  color: var(--text);
}
.views button.active {
  background: var(--panel, #fff);
  color: var(--accent);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}
.groups {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
}
.groups button {
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--text-2);
  border-radius: 14px;
  padding: 3px 10px;
  font-size: var(--fs-xs);
  cursor: pointer;
}
.auto-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 0;
}
.warn-text {
  color: var(--warn-text);
  font-size: var(--fs-xs);
}
.kinds {
  display: flex;
  border-bottom: 1px solid var(--line);
}
.kinds button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 0 9px;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-size: var(--fs-sm);
  font-family: inherit;
  cursor: pointer;
  color: var(--muted);
}
.kinds button:hover {
  color: var(--text);
}
.kinds button.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}
.badge.muted-badge {
  background: #9aa6a4;
}
.sys-title {
  max-width: 220px;
}
.search {
  padding: 8px 10px 0;
}
.read-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 10px;
  border-bottom: 1px solid var(--line);
}
.buyer {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--danger);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.item.unread .preview {
  font-weight: 600;
  color: var(--text);
}
.read-toggle {
  margin-left: auto;
  visibility: hidden;
}
.item:hover .read-toggle {
  visibility: visible;
}
.claim + .read-toggle {
  margin-left: 6px;
}
.groups button.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.list {
  padding: 4px 0;
}
/* 会话条目：分隔线排成清单，选中时左侧一条深青竖线，不用整块描边 */
.item {
  position: relative;
  padding: 10px 12px 10px 14px;
  cursor: pointer;
  border-bottom: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
  transition: background-color 0.12s;
}
.item::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: transparent;
}
.when {
  position: absolute;
  top: 9px;
  right: 10px;
  font-size: var(--fs-xs);
  color: var(--muted);
  white-space: nowrap;
}
.item.unread .when {
  color: var(--danger-text);
  font-weight: 600;
}
.tag-groups {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 0;
  margin: 5px 0 2px;
}
.tag-group {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
/* 组与组之间一条竖线隔开 */
.tag-group + .tag-group {
  margin-left: 7px;
  padding-left: 7px;
  border-left: 1px solid var(--line);
}
.ai-tag {
  border-style: dashed;
}
.first-row {
  padding-right: 72px;
}
.item:hover {
  background: #f4f2ed;
}
.item.selected {
  background: var(--accent-soft);
}
.item.selected::before {
  background: var(--accent);
}
.item.unread:not(.selected)::before {
  background: var(--aqua);
}
.title-line {
  font-size: var(--fs-xs);
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preview {
  font-size: var(--fs-sm);
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wait.late {
  color: var(--danger-text);
  font-weight: 600;
}
.claim {
  margin-left: auto;
}
.detail {
  min-width: 0;
  min-height: 0;
}
.placeholder {
  height: 100%;
  justify-content: center;
}
@media (max-width: 1100px) {
  .wb {
    grid-template-columns: minmax(0, 1fr);
    height: auto;
  }
  .queue {
    max-height: 45vh;
  }
}
</style>
