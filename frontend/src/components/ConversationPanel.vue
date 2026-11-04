<script setup lang="ts">
// 右侧会话面板：完整聊天记录 + 输入框。AI 草稿只在点击“AI 生成草稿”时生成并填进输入框，
// 生成过程（读了什么、查了什么、依据什么）显示在聊天末尾；“发送”是模拟发送，只记在本系统，不会发到 eBay。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, get, post, api, session } from '../api'
import { STATE_LABELS, errorText, fmtTime } from '../format'
import DraftTrace from './DraftTrace.vue'
import InvoicePanel from './InvoicePanel.vue'
import AftersalesFilesPanel from './AftersalesFilesPanel.vue'
import LogisticsTicketsPanel from './LogisticsTicketsPanel.vue'
import TicketTimelineCard from './TicketTimelineCard.vue'
import type { Attachment, ChatMessage, ComplianceResult, ConversationDetail, JobTrace, ListingCard, TicketTimelineItem } from '../types'

const props = defineProps<{ convId: number }>()
const emit = defineEmits<{ changed: []; deleted: [] }>()

const d = ref<ConversationDetail | null>(null)
const listing = ref<ListingCard | null>(null) // 商品卡片：打开会话后单独加载，不拖慢聊天记录

async function loadListing() {
  try {
    listing.value = await get<ListingCard>(`/internal/conversations/${props.convId}/listing`)
  } catch (e) {
    listing.value = { item_id: null, url: null, error: errorText(e) }
  }
}
const loadError = ref('')

// 查件记录按时间插进聊天：排在它之后的第一条消息前面，比所有消息都晚的放最后
const ticketPlacement = computed(() => {
  const msgs = d.value?.messages || []
  const before = new Map<number, TicketTimelineItem[]>()
  const after: TicketTimelineItem[] = []
  for (const it of d.value?.ticket_timeline || []) {
    const at = Date.parse(it.at)
    const m = msgs.find((x) => Date.parse(x.sent_at) > at)
    if (m) before.set(m.id, [...(before.get(m.id) || []), it])
    else after.push(it)
  }
  return { before, after }
})
const editText = ref('')
const editDraftId = ref<number | null>(null)
const openedAt = ref<number>(Date.now())
const sending = ref(false)
const generating = ref(false)
const blocked = ref<ComplianceResult | null>(null)
// 中文对照：AI 草稿的中文回译或“译成中文”的结果；审核员可以直接改中文，停笔约 1 秒后自动译成买家语言填回正式回复
const zhText = ref('')
const zhStale = ref(false) // 正式回复在中文之后又被直接改过：中文可能对不上
const zhSyncing = ref(false)
const zhLang = ref('')
let zhTimer: number | undefined
let zhSeq = 0
let applyingZh = false
const translating = ref<Record<number, boolean>>({})
// 右侧信息栏（刊登、售后、附件、查件单、发票）：开关记在本机
const sideOpen = ref((() => {
  try {
    return localStorage.getItem('cp-side') !== 'off'
  } catch {
    return true
  }
})())
function toggleSide() {
  sideOpen.value = !sideOpen.value
  try {
    localStorage.setItem('cp-side', sideOpen.value ? 'on' : 'off')
  } catch {
    /* 隐私模式等写不进去时只在本次生效 */
  }
}
const traceOpen = ref(false) // 生成依据默认收起，需要时点标题展开
const chatEl = ref<HTMLElement | null>(null)

const BUSY_STATES = ['RECEIVED', 'RUNNING']
const PENDING = ['REVIEW_REQUIRED', 'AUTO_ELIGIBLE', 'FAILED_HANDOFF', 'RETRYABLE_ERROR']

const job = computed(() => d.value?.job || null)
const aiBusy = computed(() => !!job.value && BUSY_STATES.includes(job.value.state))
const pending = computed(() => !!job.value && PENDING.includes(job.value.state))
const activeDraft = computed(() => d.value?.drafts.find((x) => x.status === 'ACTIVE' && x.author === 'agent') || null)
const claimedByOther = computed(() => !!d.value?.claim && d.value.claim.claimed_by !== session.reviewer)
const isEbay = computed(() => d.value?.conversation.channel_source === 'ebay')
const isSystemConv = computed(() => d.value?.conversation.conversation_type === 'FROM_EBAY')
const edited = computed(() => !!activeDraft.value && editText.value.trim() !== activeDraft.value.text.trim())
const simulatedCount = computed(() => d.value?.messages.filter((m) => m.simulated).length || 0)
// 店铺最后一次回复之后的买家消息：AI 生成草稿要回复的就是这些
const unanswered = computed(() => {
  const msgs = d.value?.messages || []
  const lastSeller = msgs.map((m) => m.role).lastIndexOf('SELLER')
  return msgs.slice(lastSeller + 1).filter((m) => m.role === 'BUYER')
})
const canGenerate = computed(
  () => !!d.value && !isSystemConv.value && !aiBusy.value && unanswered.value.length > 0 && !claimedByOther.value,
)
const generateHint = computed(() => {
  if (isSystemConv.value) return 'eBay 系统消息不需要回复'
  if (aiBusy.value) return 'AI 正在生成…'
  if (!unanswered.value.length) return '没有待回复的买家消息（最后一条是店铺回复）'
  if (claimedByOther.value) return '他人认领中'
  return `回复店铺最后一次回复之后的 ${unanswered.value.length} 条买家消息`
})
// 聊天末尾显示当前这次生成的过程：生成中，或已生成、尚未发出
const showTrace = computed(() => !!job.value && (aiBusy.value || (pending.value && (!!d.value?.steps.length || !!d.value?.agent_draft))))
const traceSourceSet = computed(() => new Set(showTrace.value ? d.value?.sources || [] : []))

const ROLE_LABELS: Record<string, string> = {
  BUYER: '买家',
  SELLER: '店铺',
  SYSTEM: 'eBay 系统消息',
  UNKNOWN: '发送方未知',
}

async function load() {
  try {
    d.value = await get<ConversationDetail>(`/internal/conversations/${props.convId}`)
    loadError.value = ''
    syncEditor()
  } catch (e) {
    loadError.value = errorText(e)
  }
}

// 新的 AI 草稿到达时填进输入框；其余时候不动审核员正在写的内容
function syncEditor() {
  const draft = activeDraft.value
  if (draft && draft.id !== editDraftId.value) {
    editDraftId.value = draft.id
    applyingZh = true
    editText.value = draft.text
    zhText.value = draft.text_zh || ''
    zhStale.value = false
    nextTick(() => (applyingZh = false))
    openedAt.value = Date.now()
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (chatEl.value) chatEl.value.scrollTop = chatEl.value.scrollHeight
  })
}

function fail(e: unknown) {
  if (e instanceof ApiError && e.detail && typeof e.detail === 'object' && 'hits' in (e.detail as object)) {
    blocked.value = e.detail as ComplianceResult
  }
  ElMessage.error(errorText(e))
}

function needReviewer() {
  if (session.reviewer) return false
  ElMessage.warning('请先在右上角填写审核员名称')
  return true
}

async function claim(action: 'claim' | 'renew' | 'release', silent = false) {
  if (!session.reviewer) {
    if (!silent) ElMessage.warning('请先在右上角填写审核员名称')
    return
  }
  try {
    await post(`/internal/conversations/${props.convId}/claim`, { action })
    if (!silent) ElMessage.success(action === 'release' ? '已释放' : '已认领')
  } catch (e) {
    if (!silent) ElMessage.error(errorText(e))
  }
  await load()
}

async function generateDraft() {
  if (needReviewer()) return
  if (editText.value.trim() && (edited.value || !activeDraft.value)) {
    try {
      await ElMessageBox.confirm('输入框里已有内容，AI 草稿生成后会替换它，继续？', '确认', { type: 'warning' })
    } catch {
      return
    }
  }
  generating.value = true
  try {
    await post(`/internal/conversations/${props.convId}/generate-draft`, {})
    emit('changed')
  } catch (e) {
    fail(e)
  } finally {
    generating.value = false
    await load()
    scrollToBottom()
  }
}

async function send() {
  if (needReviewer() || !d.value) return
  sending.value = true
  blocked.value = null
  try {
    await post(`/internal/conversations/${props.convId}/send`, {
      text: editText.value,
      handling_ms: Date.now() - openedAt.value,
      // 中文对照和正式回复对得上时一起带上；对不上（正式回复后来又改过）时后端发出后再补
      text_zh: (!zhStale.value && zhText.value.trim()) || undefined,
    })
    ElMessage.success('已模拟发送（只记录在本系统，没有发到 eBay）')
    editText.value = ''
    zhText.value = ''
    zhStale.value = false
    emit('changed')
  } catch (e) {
    fail(e)
  } finally {
    sending.value = false
    await load()
    scrollToBottom()
  }
}

// 会话标记：一个会话可以打多个标记（如 查适配中、等买家回复），每个带备注、可以单独去掉；
// 标记项可以在弹窗里直接新建、删除（所有账号都能）
const msgTags = ref<{ id: number; name: string; color: string; created_by?: string | null }[]>([])
const markOpen = ref(false)
const convMarkForm = ref({ tag_id: null as number | null, note: '' })
const convMarks = computed(() => d.value?.conversation.marks || (d.value?.conversation.mark ? [d.value.conversation.mark] : []))
const newTag = ref({ name: '', color: 'primary' })
const TAG_COLORS = [{ value: 'primary', label: '蓝' }, { value: 'success', label: '绿' }, { value: 'warning', label: '橙' },
                    { value: 'danger', label: '红' }, { value: 'info', label: '灰' }]

async function loadMsgTags() {
  msgTags.value = await get('/internal/message-tags')
}

async function openConvMark(tagId?: number) {
  await loadMsgTags()
  const m = convMarks.value.find((x) => x.tag_id === tagId)
  convMarkForm.value = { tag_id: tagId ?? null, note: m?.note || '' }
  newTag.value = { name: '', color: 'primary' }
  markOpen.value = true
}

function pickTag(id: number) {
  convMarkForm.value = { tag_id: id, note: convMarks.value.find((x) => x.tag_id === id)?.note || '' }
}

async function saveConvMark() {
  try {
    await api('PUT', `/internal/conversations/${props.convId}/mark`, convMarkForm.value)
    markOpen.value = false
    await load()
    emit('changed')
  } catch (e) {
    fail(e)
  }
}

async function removeMark(tagId: number) {
  try {
    await api('DELETE', `/internal/conversations/${props.convId}/mark?tag_id=${tagId}`)
    await load()
    emit('changed')
  } catch (e) {
    fail(e)
  }
}

async function createTag() {
  const name = newTag.value.name.trim()
  if (!name) return
  try {
    const t = await post<{ id: number }>('/internal/message-tags', { name, color: newTag.value.color })
    await loadMsgTags()
    pickTag(t.id)
    newTag.value = { name: '', color: newTag.value.color }
    emit('changed')
  } catch (e) {
    fail(e)
  }
}

async function deleteTag(t: { id: number; name: string }) {
  try {
    await ElMessageBox.confirm(`删除标记项“${t.name}”？所有会话上的这个标记都会一起去掉，不能恢复。`, '删除标记项',
                               { type: 'warning', confirmButtonText: '删除' })
    await api('DELETE', `/internal/message-tags/${t.id}`)
    if (convMarkForm.value.tag_id === t.id) convMarkForm.value = { tag_id: null, note: '' }
    await loadMsgTags()
    await load()
    emit('changed')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') fail(e)
  }
}

// 客服转售后 / 售后处理：客服判断回复不了的标记问题类型，售后接手并处理完成
const role = computed(() => session.user?.role)
const af = computed(() => d.value?.conversation.aftersales || null)
const canMark = computed(() => role.value !== 'AFTERSALES' && (!af.value || af.value.status === 'DONE'))
const canUnmark = computed(() => role.value !== 'AFTERSALES' && af.value?.status === 'PENDING')
const canStart = computed(() => role.value !== 'CS' && af.value?.status === 'PENDING')
const canFinish = computed(() => role.value !== 'CS' && (af.value?.status === 'PENDING' || af.value?.status === 'IN_PROGRESS'))
const markDialog = ref(false)
const markForm = ref({ type: '', note: '' })

function openMark() {
  const intent = activeDraft.value?.intent || ''
  markForm.value = { type: session.meta?.aftersales.intent_hint[intent] || '', note: '' }
  markDialog.value = true
}

async function aftersales(action: 'mark' | 'unmark' | 'start' | 'done', extra: Record<string, unknown> = {}) {
  try {
    await post(`/internal/conversations/${props.convId}/aftersales`, { action, ...extra })
    ElMessage.success({ mark: '已转售后', unmark: '已撤回', start: '已接手', done: '售后已完成' }[action])
    markDialog.value = false
    await load()
    emit('changed')
  } catch (e) {
    fail(e)
  }
}

async function finishAftersales() {
  try {
    const { value } = await ElMessageBox.prompt('处理结果（可选，如：已退款 20 欧元）', '售后处理完成',
                                                { confirmButtonText: '完成', inputPlaceholder: '处理结果' })
    aftersales('done', { note: value || '' })
  } catch {
    /* 取消 */
  }
}

// 测试期：删除整个测试会话（正式上线前移除）
const canDelete = computed(() => !!session.meta?.test_delete_enabled && !isEbay.value)
async function deleteConversation() {
  if (needReviewer()) return
  try {
    await ElMessageBox.confirm('删除这个测试会话的全部消息、图片、AI 草稿和回复任务？删除后不能恢复。', '删除会话（测试用）',
                               { type: 'warning', confirmButtonText: '删除' })
  } catch {
    return
  }
  try {
    await api('DELETE', `/internal/conversations/${props.convId}`)
    ElMessage.success('已删除')
    emit('deleted')
  } catch (e) {
    fail(e)
  }
}

async function clearSimulated() {
  try {
    await ElMessageBox.confirm(`删除本会话里 ${simulatedCount.value} 条模拟发送的消息？真实的 eBay 消息不受影响。`, '清除模拟消息', { type: 'warning' })
  } catch {
    return
  }
  try {
    const r = await api<{ deleted: number }>('DELETE', `/internal/conversations/${props.convId}/simulated-messages`)
    ElMessage.success(`已删除 ${r.deleted} 条模拟消息`)
    emit('changed')
  } catch (e) {
    fail(e)
  }
  load()
}

async function toggleHandoff() {
  if (!d.value) return
  try {
    await post(`/internal/conversations/${props.convId}/handoff`, {
      state: d.value.conversation.handoff_state === 'HUMAN' ? 'NONE' : 'HUMAN',
    })
  } catch (e) {
    fail(e)
  }
  load()
}

async function translateEdit() {
  try {
    const r = await post<{ translation: string }>('/internal/translate', { text: editText.value, target_language: '中文' })
    zhText.value = r.translation
    zhStale.value = false
  } catch (e) {
    fail(e)
  }
}

// 审核员改了中文：停笔 1 秒后译成买家语言，替换上方正式回复（只取最后一次的结果）
function onZhInput() {
  clearTimeout(zhTimer)
  if (!zhText.value.trim()) return
  zhTimer = window.setTimeout(async () => {
    const seq = ++zhSeq
    zhSyncing.value = true
    try {
      const r = await post<{ text: string; language: string; language_name: string }>(
        `/internal/conversations/${props.convId}/translate-from-zh`, { text_zh: zhText.value })
      if (seq !== zhSeq) return
      applyingZh = true
      editText.value = r.text
      zhLang.value = r.language_name
      zhStale.value = false
      nextTick(() => (applyingZh = false))
    } catch (e) {
      if (seq === zhSeq) fail(e)
    } finally {
      if (seq === zhSeq) zhSyncing.value = false
    }
  }, 1000)
}

async function translateMessage(id: number) {
  translating.value[id] = true
  try {
    await post(`/internal/messages/${id}/translate`)
    await load()
  } catch (e) {
    fail(e)
  } finally {
    translating.value[id] = false
  }
}

// eBay 原始 HTML 邮件：放进禁用脚本的沙箱 iframe 显示
const mailDialog = ref(false)
const mailHtml = ref('')
const mailSubject = ref('')
async function openMail(m: ChatMessage) {
  mailHtml.value = ''
  mailSubject.value = m.subject || ''
  mailDialog.value = true
  try {
    const r = await get<{ html: string; subject: string | null }>(`/internal/messages/${m.id}/html`)
    mailHtml.value = r.html
  } catch (e) {
    mailDialog.value = false
    fail(e)
  }
}

// 已发出消息的生成依据（抽屉）
const drawer = ref(false)
const drawerTrace = ref<JobTrace | null>(null)
const drawerMsg = ref<ChatMessage | null>(null)
async function openTrace(m: ChatMessage) {
  if (!m.trace) return
  drawerMsg.value = m
  drawerTrace.value = null
  drawer.value = true
  try {
    drawerTrace.value = await get<JobTrace>(`/internal/jobs/${m.trace.job_id}/trace?generation=${m.trace.generation}`)
  } catch (e) {
    drawer.value = false
    fail(e)
  }
}

const ATT_LABEL: Record<string, string> = {
  PENDING: '待下载',
  STORED: '已保存',
  SKIPPED: '未处理',
  FAILED: '失败',
  EXPIRED: '已过保留期',
}
const ATT_TAG: Record<string, 'success' | 'info' | 'warning' | 'danger'> = {
  PENDING: 'info',
  STORED: 'success',
  SKIPPED: 'info',
  FAILED: 'danger',
  EXPIRED: 'info',
}

function imageUrls(atts: Attachment[]): string[] {
  return atts.map((a) => a.file_url).filter((u): u is string => !!u)
}

async function reviewObservation(a: Attachment, status: 'CONFIRMED' | 'CORRECTED') {
  if (!a.observation) return
  let note = ''
  if (status === 'CORRECTED') {
    try {
      const r = await ElMessageBox.prompt('写明图片里正确的内容（会用于评估和改进识图）', '纠正识图结果', {
        inputType: 'textarea',
        inputValidator: (v: string) => !!v?.trim() || '请填写正确的内容',
      })
      note = r.value
    } catch {
      return
    }
  }
  try {
    await post(`/internal/image-observations/${a.observation.id}/review`, { review_status: status, note })
    await load()
  } catch (e) {
    fail(e)
  }
}

function segments(text: string, c: ComplianceResult | null | undefined) {
  if (!c?.hits.length) return [{ text, cls: '' }]
  const out: { text: string; cls: string; title?: string }[] = []
  let pos = 0
  for (const h of [...c.hits].sort((a, b) => a.start - b.start)) {
    if (h.start < pos) continue
    out.push({ text: text.slice(pos, h.start), cls: '' })
    out.push({ text: text.slice(h.start, h.end), cls: h.severity === 'BLOCK' ? 'block' : 'review', title: `${h.rule} ${h.note}` })
    pos = h.end
  }
  out.push({ text: text.slice(pos), cls: '' })
  return out
}

let timer: number | undefined
function schedule() {
  clearTimeout(timer)
  timer = window.setTimeout(async () => {
    await load()
    schedule()
  }, aiBusy.value ? 1500 : 5000)
}

// 文本改了，旧的合规命中就对不上了；直接改正式回复（不是从中文译过来的）时，中文对照标为可能对不上
watch(editText, (v) => {
  blocked.value = null
  if (!applyingZh) {
    zhSeq++ // 正在译的中文结果作废，不覆盖审核员刚改的正式回复
    zhSyncing.value = false
    if (!v.trim()) zhText.value = ''
    else if (zhText.value) zhStale.value = true
  }
})
watch(() => job.value?.state, (now, before) => {
  if (before && now !== before) {
    emit('changed')
    scrollToBottom()
  }
})
watch(() => d.value?.messages.length, (now, before) => {
  if (now !== before) scrollToBottom()
})
watch(() => d.value?.steps.length, () => {
  if (aiBusy.value) scrollToBottom()
})

onMounted(async () => {
  await load()
  if (d.value?.conversation.item_id) loadListing()
  if (pending.value && !d.value?.claim) await claim('claim', true)
  scrollToBottom()
  schedule()
})
onUnmounted(() => {
  clearTimeout(timer)
  clearTimeout(zhTimer)
})
</script>

<template>
  <div v-if="loadError" class="panel"><el-alert :title="loadError" type="error" show-icon /></div>
  <div v-else-if="d" class="panel cp">
    <div class="panel-head">
      <span class="store-name" :title="`店铺 ID：${d.conversation.store_id}`">{{ d.conversation.store_name || d.conversation.store_id }}</span>
      <el-tag size="small" effect="plain" :type="d.conversation.marketplace_id === 'UNKNOWN' ? 'danger' : 'info'">
        {{ d.conversation.marketplace_id }}
      </el-tag>
      <el-tag v-if="isEbay" size="small" type="primary">eBay 真实会话</el-tag>
      <span class="conv-no" title="会话编号">#{{ d.conversation.id }}</span>
      <el-tag v-if="d.conversation.handoff_state === 'HUMAN'" size="small" type="danger">已人工接管</el-tag>
      <el-tag v-if="job" size="small" effect="plain">{{ STATE_LABELS[job.state] || job.state }}</el-tag>
      <span class="spacer" />
      <span v-if="d.claim" :class="claimedByOther ? 'warn' : 'muted'">{{ d.claim.claimed_by }} 认领中</span>
      <el-button v-if="d.claim && !claimedByOther" link size="small" @click="claim('release')">释放</el-button>
      <el-button link size="small" type="primary" @click="openConvMark()">{{ convMarks.length ? '加标记' : '标记' }}</el-button>
      <el-button v-if="canMark" link size="small" type="warning" @click="openMark">转售后</el-button>
      <el-button v-if="canUnmark" link size="small" @click="aftersales('unmark')">撤回转售后</el-button>
      <el-button v-if="canStart" link size="small" type="primary" @click="aftersales('start')">接手处理</el-button>
      <el-button v-if="canFinish" link size="small" type="success" @click="finishAftersales">处理完成</el-button>
      <el-button v-if="canDelete" link size="small" type="danger" @click="deleteConversation">删除会话（测试）</el-button>
      <el-button v-if="simulatedCount" link size="small" type="warning" @click="clearSimulated">清除模拟消息（{{ simulatedCount }}）</el-button>
      <el-button link size="small" @click="toggleSide">{{ sideOpen ? '收起信息栏' : '展开信息栏' }}</el-button>
      <el-button link size="small" :disabled="claimedByOther" @click="toggleHandoff">
        {{ d.conversation.handoff_state === 'HUMAN' ? '解除接管' : '人工接管' }}
      </el-button>
    </div>
    <div class="cp-body">
      <div class="cp-main">
        <!-- 聊天记录 -->
        <div ref="chatEl" class="chat">
          <template v-for="m in d.messages" :key="m.id">
          <TicketTimelineCard v-for="it in ticketPlacement.before.get(m.id) || []" :key="it.key" :item="it" />
          <div class="msg-row" :class="m.role.toLowerCase()">
            <div class="bubble" :class="{ task: traceSourceSet.has(m.ebay_message_id), sim: m.simulated }">
              <div class="msg-head">
                <span class="who">{{ ROLE_LABELS[m.role] || m.role }}</span>
                <span class="time">{{ fmtTime(m.sent_at) }}</span>
                <el-tag v-if="m.trace?.auto" size="small" type="success">AI 自动回复</el-tag>
                <el-tag v-if="m.simulated" size="small" type="warning" effect="plain">模拟发送 · 未发到 eBay</el-tag>
                <el-tag v-if="traceSourceSet.has(m.ebay_message_id)" size="small" type="warning" effect="plain">本次回复对象</el-tag>
                <span class="spacer" />
                <el-button v-if="m.trace?.from_ai" link size="small" type="primary" @click="openTrace(m)">查看依据</el-button>
                <el-button v-if="m.has_html" link size="small" type="primary" @click="openMail(m)">查看原邮件</el-button>
                <span v-if="!m.zh_translation && m.translating" class="muted">翻译中…</span>
                <el-button v-else-if="!m.zh_translation" link size="small" :loading="translating[m.id]" @click="translateMessage(m.id)">译</el-button>
              </div>

              <div v-if="m.subject" class="subject">主题：{{ m.subject }}</div>
              <div class="msg-body">{{ m.body }}</div>
              <div v-if="m.zh_translation" class="trans">
                <div class="trans-label">{{ m.direction === 'OUT' ? '中文对照 · 买家收到的是上方原文' : '中文译文 · 自动翻译，仅供理解' }}</div>
                <div class="trans-body">{{ m.zh_translation }}</div>
              </div>
              <div v-if="m.attachments.length" class="atts">
                <div v-for="a in m.attachments" :key="a.id" class="att">
                  <el-image v-if="a.file_url" :src="a.file_url" fit="cover" class="thumb" :preview-src-list="imageUrls(m.attachments)"
                    :initial-index="imageUrls(m.attachments).indexOf(a.file_url)" preview-teleported hide-on-click-modal />
                  <div v-else class="thumb placeholder">{{ a.media_type }}</div>
                  <div class="att-info">
                    <div>
                      <el-tag size="small" :type="ATT_TAG[a.status]" effect="plain">{{ ATT_LABEL[a.status] || a.status }}</el-tag>
                      <span class="muted"> {{ a.media_name || `附件 ${a.seq}` }}</span>
                      <span v-if="a.width" class="muted"> · {{ a.width }}×{{ a.height }}</span>
                    </div>
                    <div v-if="a.status_reason" class="muted">{{ a.status_reason }}</div>
                    <template v-if="a.observation">
                      <div v-if="a.observation.status === 'OK' && a.observation.observation" class="obs">
                        <div class="muted">识图结果（模型观察，未经人工确认{{ a.observation.cached ? '；复用同一图片的结果' : '' }}）</div>
                        <div>
                          {{ a.observation.observation.image_kind }} ·
                          <span :class="{ warn: a.observation.observation.quality !== '清晰' }">{{ a.observation.observation.quality }}</span>
                          <span v-if="a.observation.observation.damage_observed" class="danger"> · 外观异常：{{ a.observation.observation.damage_description }}</span>
                        </div>
                        <div>{{ a.observation.observation.description }}</div>
                        <div v-if="a.observation.observation.error_codes.length">故障码：<span class="mono">{{ a.observation.observation.error_codes.join('、') }}</span></div>
                        <div v-if="a.observation.observation.part_numbers.length">零件号：<span class="mono">{{ a.observation.observation.part_numbers.join('、') }}</span></div>
                        <div v-if="a.observation.observation.vin_masked">VIN：<span class="mono">{{ a.observation.observation.vin_masked }}</span></div>
                        <div v-if="a.observation.observation.visible_text.length" class="muted">可见文字：{{ a.observation.observation.visible_text.join(' / ') }}</div>
                        <div v-if="a.observation.observation.uncertain.length" class="muted">不确定：{{ a.observation.observation.uncertain.join('；') }}</div>
                        <div v-if="a.observation.observation.contains_personal_info" class="muted">图中含个人信息（未转录）</div>
                        <div class="obs-review">
                          <el-tag v-if="a.observation.review_status !== 'UNREVIEWED'" size="small"
                            :type="a.observation.review_status === 'CONFIRMED' ? 'success' : 'warning'">
                            {{ a.observation.review_status === 'CONFIRMED' ? '已确认' : '已纠正' }} · {{ a.observation.reviewed_by }}
                          </el-tag>
                          <span v-if="a.observation.review_note">纠正：{{ a.observation.review_note }}</span>
                          <el-button link size="small" :disabled="!session.reviewer" @click="reviewObservation(a, 'CONFIRMED')">确认无误</el-button>
                          <el-button link size="small" :disabled="!session.reviewer" @click="reviewObservation(a, 'CORRECTED')">纠正</el-button>
                        </div>
                      </div>
                      <div v-else class="muted">识图失败：{{ a.observation.error }}</div>
                    </template>
                  </div>
                </div>
              </div>
              <div v-else-if="m.media.some((x) => x.fileId)" class="out-media">
                <template v-for="x in m.media" :key="x.fileId || x.mediaName">
                  <a v-if="x.fileId && x.mediaType === 'IMAGE'" :href="`/internal/aftersales-files/${x.fileId}/content`" target="_blank">
                    <img :src="`/internal/aftersales-files/${x.fileId}/content`" :alt="x.mediaName" />
                  </a>
                  <a v-else-if="x.fileId" :href="`/internal/aftersales-files/${x.fileId}/content`" target="_blank">{{ x.mediaName }}</a>
                </template>
              </div>
              <div v-else-if="m.media.length" class="muted">附件：{{ m.media.map((x) => x.mediaName || x.mediaType).join('、') }}</div>
            </div>
          </div>
          </template>
          <TicketTimelineCard v-for="it in ticketPlacement.after" :key="it.key" :item="it" />

          <div v-for="e in d.events" :key="e.id" class="event">
            <span>{{ fmtTime(e.occurred_at) }} · {{ e.event_type }} · {{ e.source }}<template v-if="e.case_id"> · {{ e.case_id }}</template></span>
          </div>

          <!-- 本次 AI 草稿的生成过程 -->
          <div v-if="showTrace" class="msg-row seller">
            <div class="bubble ai">
              <div class="msg-head ai-head" @click="traceOpen = !traceOpen">
                <span class="who">{{ aiBusy ? 'AI 正在生成草稿…' : 'AI 草稿的生成依据' }}</span>
                <span class="time">第 {{ job?.run_generation }} 次生成</span>
                <span class="spacer" />
                <el-button link size="small">{{ traceOpen ? '收起' : '展开' }}</el-button>
              </div>
              <div v-if="job?.state === 'RETRYABLE_ERROR'" class="warn">生成失败，等待自动重试（也可以点“重新生成”）：{{ job?.last_error }}</div>
              <DraftTrace v-if="traceOpen" :trace="d" :messages="d.messages" :live="aiBusy" :reasons="job?.decision_reasons || []" />
            </div>
          </div>
        </div>

        <!-- 输入框 -->
        <div class="composer">
          <div v-if="blocked" class="blocked">
            合规命中：<el-tag size="small" :type="blocked.result === 'BLOCK' ? 'danger' : 'warning'">{{ blocked.result }}</el-tag>
            <div class="hl">
              <template v-for="(s, i) in segments(editText, blocked)" :key="i">
                <mark v-if="s.cls" :class="s.cls" :title="s.title">{{ s.text }}</mark><span v-else>{{ s.text }}</span>
              </template>
            </div>
          </div>
          <el-input v-model="editText" type="textarea" :autosize="{ minRows: 3, maxRows: 10 }" :disabled="claimedByOther || isSystemConv"
            :placeholder="isSystemConv ? 'eBay 系统消息不需要回复' : '输入回复（用买家的语言）；点“AI 生成草稿”会把 AI 写的回复填到这里，可以再修改'" />
          <div v-if="!isSystemConv" class="zh-box">
            <div class="zh-head">
              <span class="muted">中文对照：可直接改中文，停笔约 1 秒后上方正式回复会按{{ zhLang || '买家语言' }}自动更新</span>
              <span v-if="zhSyncing" class="muted">· 正在更新正式回复…</span>
              <span v-else-if="zhStale" class="warn">· 正式回复改过了，中文可能对不上，点“译成中文”刷新</span>
            </div>
            <el-input v-model="zhText" type="textarea" :autosize="{ minRows: 2, maxRows: 8 }" :disabled="claimedByOther"
              placeholder="也可以先在这里用中文写回复，会自动译成买家语言填到上面" @input="onZhInput" />
          </div>
          <div class="composer-bar">
            <el-tooltip :content="generateHint" placement="top">
              <span>
                <el-button :loading="generating || aiBusy" :disabled="!canGenerate" @click="generateDraft">
                  {{ aiBusy ? 'AI 生成中…' : activeDraft ? '重新生成 AI 草稿' : 'AI 生成草稿' }}
                </el-button>
              </span>
            </el-tooltip>
            <el-button :disabled="!editText.trim()" @click="translateEdit">译成中文</el-button>
            <span v-if="edited" class="warn">已修改 AI 草稿</span>
            <span class="spacer" />
            <span class="muted">模拟发送：只记录在本系统，不会发到 eBay</span>
            <el-button type="primary" :loading="sending" :disabled="!editText.trim() || claimedByOther || isSystemConv || aiBusy" @click="send">
              发送
            </el-button>
          </div>
        </div>
      </div>
      <aside v-show="sideOpen" class="cp-side">
        <div v-if="d.conversation.item_id && listing" class="listing-card">
          <img v-if="listing.picture_url" :src="listing.picture_url" alt="" class="listing-img" referrerpolicy="no-referrer" />
          <div class="listing-info">
            <a v-if="listing.url" :href="listing.url" target="_blank" rel="noopener noreferrer" class="listing-title">
              {{ listing.title || d.conversation.conversation_title || listing.item_id }}
            </a>
            <div class="muted listing-facts">
              <span v-if="listing.sku">SKU <span class="mono">{{ listing.sku }}</span></span>
              <span v-if="listing.price">{{ listing.price.value }} {{ listing.price.currency }}</span>
              <span v-if="listing.quantity_available != null">可售 {{ listing.quantity_available }}</span>
              <span v-if="listing.condition">{{ listing.condition }}</span>
              <span v-if="listing.listing_status">{{ listing.listing_status }}</span>
              <span v-if="listing.marketplace_id">{{ listing.marketplace_id }}</span>
              <span v-if="listing.variations && listing.variations > 1">{{ listing.variations }} 个变体</span>
              <span v-if="listing.compatibility_total">兼容车型 {{ listing.compatibility_total }} 条</span>
            </div>
            <div v-if="listing.error" class="warn">{{ listing.error }}</div>
          </div>
        </div>
        <div class="conv-meta">
          <span v-if="d.conversation.conversation_title" class="conv-title">{{ d.conversation.conversation_title }}</span>
          <span class="muted">
            买家 <span class="mono">{{ d.conversation.buyer_key }}</span> · 刊登
            <a v-if="d.conversation.item_url" class="mono" :href="d.conversation.item_url" target="_blank" rel="noopener noreferrer">
              {{ d.conversation.item_id }} ↗
            </a>
            <span v-else class="mono">{{ d.conversation.item_id || '无' }}</span>
          </span>
        </div>
        <div v-if="af" class="af-box" :class="af.status">
          <el-tag size="small" :type="af.status === 'DONE' ? 'success' : af.status === 'IN_PROGRESS' ? 'primary' : 'warning'">
            {{ af.status_label }}
          </el-tag>
          <b>{{ af.type }}</b>
          <span class="muted">客服 {{ af.marked_by }} 标记于 {{ af.marked_at ? fmtTime(af.marked_at) : '—' }}</span>
          <span v-if="af.handled_by" class="muted">· 售后 {{ af.handled_by }}</span>
          <div v-if="af.note" class="af-note">{{ af.note }}</div>
        </div>
        <div v-if="convMarks.length" class="conv-mark">
          <span v-for="m in convMarks" :key="m.tag_id" class="mark-item"
                :title="`${m.marked_by} · ${m.marked_at ? fmtTime(m.marked_at) : ''}（点标记改备注）`">
            <el-tag size="small" :type="m.color" closable class="mark-tag" @click="openConvMark(m.tag_id)" @close="removeMark(m.tag_id)">
              {{ m.tag }}
            </el-tag>
            <span v-if="m.note" class="mark-note">{{ m.note }}</span>
          </span>
          <el-button link size="small" type="primary" @click="openConvMark()">+ 加标记</el-button>
        </div>
        <AftersalesFilesPanel :conv-id="convId" :marketplace="d.conversation.marketplace_id"
          :refresh-key="job ? `${job.id}-${job.state}-${d.messages.length}` : String(d.messages.length)"
          @insert="(t: string) => (editText = t)" />
        <LogisticsTicketsPanel :conv-id="convId" :refresh-key="job ? `${job.id}-${job.state}` : ''"
          @insert="(t: string) => (editText = t)" />
        <InvoicePanel :conv-id="convId" :refresh-key="job ? `${job.id}-${job.state}` : ''" />
      </aside>
    </div>
    <el-dialog v-model="markOpen" title="给这个会话加标记" width="500px">
      <div class="muted small">选一个标记（可以给同一个会话打多个）；没有合适的就在下面新建。</div>
      <div class="tag-options">
        <div v-for="t in msgTags" :key="t.id" class="tag-opt" :class="{ on: convMarkForm.tag_id === t.id }" @click="pickTag(t.id)">
          <el-tag size="small" :type="t.color as any" :effect="convMarkForm.tag_id === t.id ? 'dark' : 'light'">{{ t.name }}</el-tag>
          <span v-if="convMarks.some((m) => m.tag_id === t.id)" class="muted small">已打</span>
          <el-button link size="small" type="danger" class="del" title="删除这个标记项（所有会话上的都会去掉）" @click.stop="deleteTag(t)">删除</el-button>
        </div>
        <div v-if="!msgTags.length" class="muted small">还没有标记项，先新建一个</div>
      </div>
      <div class="new-tag">
        <el-input v-model="newTag.name" size="small" maxlength="32" placeholder="新建标记，如 等配件到货" @keyup.enter="createTag" />
        <el-select v-model="newTag.color" size="small" style="width: 80px">
          <el-option v-for="c in TAG_COLORS" :key="c.value" :value="c.value" :label="c.label" />
        </el-select>
        <el-button size="small" :disabled="!newTag.name.trim()" @click="createTag">新建</el-button>
      </div>
      <el-form label-width="50px" style="margin-top: 12px">
        <el-form-item label="备注">
          <el-input v-model="convMarkForm.note" type="textarea" :rows="2" placeholder="如：已让买家提供 VIN，等回复" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button v-if="convMarkForm.tag_id && convMarks.some((m) => m.tag_id === convMarkForm.tag_id)" type="danger" plain
                   @click="removeMark(convMarkForm.tag_id!); markOpen = false">去掉这个标记</el-button>
        <el-button @click="markOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!convMarkForm.tag_id" @click="saveConvMark()">
          {{ convMarkForm.tag_id && convMarks.some((m) => m.tag_id === convMarkForm.tag_id) ? '保存备注' : '加上这个标记' }}
        </el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="markDialog" title="转售后" width="420px">
      <el-form label-width="70px">
        <el-form-item label="问题类型">
          <el-select v-model="markForm.type" placeholder="选择问题类型" style="width: 100%">
            <el-option v-for="t in session.meta?.aftersales.types || []" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="markForm.note" type="textarea" :rows="3" placeholder="买家诉求、已核实的情况，方便售后直接处理" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="markDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!markForm.type" @click="aftersales('mark', markForm)">转给售后</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="mailDialog" :title="mailSubject || 'eBay 原邮件'" width="760px" append-to-body top="5vh">
      <div class="muted mail-note">eBay 发来的原始邮件排版（已禁用其中的脚本；邮件里的按钮和链接请到 eBay 后台操作）；聊天记录里显示的是转换后的纯文本。</div>
      <iframe v-if="mailHtml" class="mail-frame" sandbox="" referrerpolicy="no-referrer" :srcdoc="mailHtml" title="eBay 原邮件" />
      <div v-else v-loading="true" style="height: 200px" />
    </el-dialog>

    <el-drawer v-model="drawer" title="这条回复的生成依据" size="560px" append-to-body>
      <template v-if="drawerTrace && drawerMsg">
        <div class="drawer-msg">
          <div class="muted">{{ fmtTime(drawerMsg.sent_at) }} 发出（{{ drawerMsg.simulated ? '模拟发送' : '已发送' }}）· 任务 #{{ drawerTrace.job.id }} 第 {{ drawerTrace.generation }} 次生成</div>
          <div class="msg-body">{{ drawerMsg.body }}</div>
          <div v-if="drawerTrace.agent_draft && drawerTrace.agent_draft.text.trim() !== drawerMsg.body.trim()" class="trans">
            <div class="trans-label">AI 原稿 · 发送前被修改过</div>
            <div class="trans-body">{{ drawerTrace.agent_draft.text }}</div>
          </div>
        </div>
        <DraftTrace :trace="drawerTrace" :messages="d.messages" :reasons="drawerTrace.agent_draft?.review_reasons || []" />
      </template>
      <div v-else v-loading="true" style="height: 200px" />
    </el-drawer>
  </div>
  <div v-else class="panel" v-loading="true" style="height: 200px" />
</template>

<style scoped>
.cp {
  height: 100%;
}
.cp-body {
  flex: 1;
  min-height: 0;
  display: flex;
}
.cp-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.cp-side {
  width: 360px;
  flex-shrink: 0;
  overflow: auto;
  border-left: 1px solid var(--line);
  background: var(--panel);
}
@media (max-width: 1400px) {
  .cp-side {
    width: 300px;
  }
}
.cp-side .listing-card {
  align-items: flex-start;
}
.spacer {
  flex: 1;
}
.conv-meta {
  padding: 6px 14px;
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
}
.conv-mark {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--accent-soft);
  font-size: var(--fs-sm);
}
.mark-note {
  color: var(--text-2);
}
.mark-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mark-tag {
  cursor: pointer;
}
.tag-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 10px 0;
  max-height: 220px;
  overflow: auto;
}
.tag-opt {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
}
.tag-opt.on {
  border-color: var(--el-color-primary);
}
.tag-opt .del {
  margin-left: 2px;
}
.new-tag {
  display: flex;
  gap: 6px;
}
.store-name {
  font-size: var(--fs-md, 15px);
  font-weight: 700;
}
.conv-no {
  font-size: var(--fs-xs, 12px);
  font-weight: 400;
  color: var(--text-3, #8a8f98);
}
.small {
  font-size: var(--fs-xs, 12px);
}
.af-box {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--warn-soft);
  font-size: var(--fs-sm);
}
.af-box.IN_PROGRESS {
  background: var(--accent-soft);
}
.af-box.DONE {
  background: #eff6ee;
}
.af-note {
  width: 100%;
  white-space: pre-wrap;
  color: var(--text-2);
}
.listing-card {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--sunken);
}
.listing-img {
  width: 48px;
  height: 48px;
  object-fit: contain;
  border-radius: 4px;
  background: #ffffff;
  border: 1px solid var(--line);
  flex: none;
}
.listing-info {
  min-width: 0;
}
.listing-title {
  font-size: var(--fs-sm);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.listing-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: var(--fs-xs);
}
.conv-title {
  font-weight: 600;
  font-size: var(--fs-sm);
}
/* ---- 聊天记录 ---- */
.chat {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 18px 22px 8px;
  background: var(--sunken);
}
.msg-row {
  display: flex;
  margin-bottom: 14px;
}
.msg-row.seller {
  justify-content: flex-end;
}
.msg-row.system,
.msg-row.unknown {
  justify-content: center;
}
/* 买家：白底细边，贴左；店铺：浅青底，贴右。靠近发言方的角收小，读起来像对话而不是卡片 */
.bubble {
  max-width: min(78%, 760px);
  border-radius: 10px 10px 10px 3px;
  padding: 9px 14px 11px;
  border: 1px solid var(--line);
  background: #ffffff;
  color: var(--text);
}
.seller .bubble {
  background: var(--seller);
  border-color: #d3e3e0;
  border-radius: 10px 10px 3px 10px;
}
.system .bubble,
.unknown .bubble {
  background: var(--system);
  border-color: #ece2cc;
  border-radius: 8px;
  max-width: min(90%, 820px);
}
/* 本次回复对象：左侧一条暖黄标线，不改整个边框 */
.bubble.task {
  border-color: var(--el-color-warning-light-5);
  box-shadow: inset 3px 0 0 var(--warn);
}
.bubble.sim {
  border-style: dashed;
  border-color: var(--el-color-warning-light-3);
}
/* 待审核的回复草稿：米白底 + 浅青虚线框，和已发送的消息明显区分 */
.bubble.ai {
  background: var(--panel);
  border: 1px dashed #8fc4c6;
  border-radius: 10px;
  width: min(78%, 760px);
}
.msg-head {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  min-height: 22px;
  margin-bottom: 4px;
}
.msg-head .who {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-2);
}
.seller .msg-head .who {
  color: var(--accent);
}
.msg-head .time {
  font-size: var(--fs-xs);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.msg-head .el-button {
  font-size: var(--fs-xs);
}
.msg-head .el-button + .el-button {
  margin-left: 4px;
}
.ai-head {
  cursor: pointer;
}
.subject {
  font-size: var(--fs-sm);
  color: var(--text-2);
  margin-bottom: 4px;
}
.msg-body {
  white-space: pre-wrap;
  font-size: var(--fs-msg);
  line-height: 1.7;
  word-break: break-word;
  overflow-wrap: anywhere;
  max-width: 72ch;
}
/* 中文翻译：放在原文下方，用一条虚线隔开，字号小一级 */
.trans {
  margin-top: 8px;
  padding-top: 7px;
  border-top: 1px dashed rgba(45, 58, 58, 0.16);
}
.trans-label {
  font-size: var(--fs-xs);
  color: var(--muted);
  margin-bottom: 2px;
}
.trans-body {
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
.event {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0 14px;
  font-size: var(--fs-xs);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.event::before,
.event::after {
  content: '';
  flex: 1;
  border-top: 1px solid var(--line);
}
.zh-box {
  margin-top: 6px;
}
.out-media {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.out-media img {
  max-height: 140px;
  max-width: 220px;
  border-radius: 4px;
}
.zh-head {
  font-size: var(--fs-xs);
  margin-bottom: 4px;
}
.composer {
  border-top: 1px solid var(--line);
  background: var(--panel);
  padding: 10px 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.composer-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.composer-bar .el-button + .el-button {
  margin-left: 0;
}
.blocked {
  font-size: var(--fs-sm);
}
.hl {
  white-space: pre-wrap;
  font-size: var(--fs-sm);
  border: 1px dashed var(--line);
  border-radius: 6px;
  padding: 6px 8px;
  margin-top: 6px;
}
.warn {
  color: var(--warn-text);
  font-size: var(--fs-xs);
}
.mail-note {
  margin-bottom: 8px;
}
.mail-frame {
  width: 100%;
  height: 70vh;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #ffffff;
}
.drawer-msg {
  background: var(--seller);
  border: 1px solid #d3e3e0;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
}
.atts {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.att {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: var(--fs-xs);
  align-items: flex-start;
}
.thumb {
  width: 96px;
  height: 96px;
  flex: none;
  border-radius: 4px;
  border: 1px solid var(--el-border-color-lighter);
  cursor: zoom-in;
}
.thumb.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: default;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
}
.att-info {
  flex: 1 1 200px;
  min-width: 0;
  line-height: 1.6;
}
.obs {
  margin-top: 2px;
  padding: 4px 6px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
}
.obs .warn {
  color: var(--el-color-warning);
}
.obs .danger {
  color: var(--el-color-danger);
}
.obs-review {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
/* 工作台在 1100px 以下改为上下排列、高度自适应：给面板固定视口高度，聊天区内部滚动、输入框始终可见 */
@media (max-width: 1100px) {
  .cp {
    height: 85vh;
  }
}
@media (max-width: 800px) {
  .bubble,
  .bubble.ai {
    max-width: 92%;
    width: auto;
  }
}
</style>
