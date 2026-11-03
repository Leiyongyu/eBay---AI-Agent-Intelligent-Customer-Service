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
import type { Attachment, ChatMessage, ComplianceResult, ConversationDetail, JobTrace, ListingCard } from '../types'

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
const editText = ref('')
const editDraftId = ref<number | null>(null)
const openedAt = ref<number>(Date.now())
const sending = ref(false)
const generating = ref(false)
const blocked = ref<ComplianceResult | null>(null)
const editTranslation = ref('')
const translating = ref<Record<number, boolean>>({})
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
    editText.value = draft.text
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
      // 中文对照：现成的译文（当前文本的机翻，或未改动的 AI 草稿回译）一起带上；都没有时后端发出后再补
      text_zh: editTranslation.value || (!edited.value && activeDraft.value?.text_zh) || undefined,
    })
    ElMessage.success('已模拟发送（只记录在本系统，没有发到 eBay）')
    editText.value = ''
    editTranslation.value = ''
    emit('changed')
  } catch (e) {
    fail(e)
  } finally {
    sending.value = false
    await load()
    scrollToBottom()
  }
}

// 会话标记：给整段会话标当前状态（如 查适配中、不适配）并写备注
const msgTags = ref<{ id: number; name: string; color: string }[]>([])
const markOpen = ref(false)
const convMarkForm = ref({ tag_id: null as number | null, note: '' })
const convMark = computed(() => d.value?.conversation.mark || null)

async function openConvMark() {
  if (!msgTags.value.length) msgTags.value = await get('/internal/message-tags')
  convMarkForm.value = { tag_id: convMark.value?.tag_id ?? null, note: convMark.value?.note || '' }
  markOpen.value = true
}

async function saveConvMark(remove = false) {
  try {
    if (remove) await api('DELETE', `/internal/conversations/${props.convId}/mark`)
    else await api('PUT', `/internal/conversations/${props.convId}/mark`, convMarkForm.value)
    markOpen.value = false
    await load()
    emit('changed')
  } catch (e) {
    fail(e)
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
    editTranslation.value = r.translation
  } catch (e) {
    fail(e)
  }
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

// 文本改了，旧的合规命中和译文就对不上了
watch(editText, () => {
  blocked.value = null
  editTranslation.value = ''
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
onUnmounted(() => clearTimeout(timer))
</script>

<template>
  <div v-if="loadError" class="panel"><el-alert :title="loadError" type="error" show-icon /></div>
  <div v-else-if="d" class="panel cp">
    <div class="panel-head">
      会话 #{{ d.conversation.id }}
      <el-tag size="small" effect="plain">{{ d.conversation.store_id }}</el-tag>
      <el-tag size="small" effect="plain" :type="d.conversation.marketplace_id === 'UNKNOWN' ? 'danger' : 'info'">
        {{ d.conversation.marketplace_id }}
      </el-tag>
      <el-tag v-if="isEbay" size="small" type="primary" effect="dark">eBay 真实会话</el-tag>
      <el-tag v-if="d.conversation.handoff_state === 'HUMAN'" size="small" type="danger">已人工接管</el-tag>
      <el-tag v-if="job" size="small" effect="plain">{{ STATE_LABELS[job.state] || job.state }}</el-tag>
      <span class="spacer" />
      <span v-if="d.claim" :class="claimedByOther ? 'warn' : 'muted'">🔒 {{ d.claim.claimed_by }} 认领中</span>
      <el-button v-if="d.claim && !claimedByOther" link size="small" @click="claim('release')">释放</el-button>
      <el-button link size="small" type="primary" @click="openConvMark">{{ convMark ? '改标记' : '标记' }}</el-button>
      <el-button v-if="canMark" link size="small" type="warning" @click="openMark">转售后</el-button>
      <el-button v-if="canUnmark" link size="small" @click="aftersales('unmark')">撤回转售后</el-button>
      <el-button v-if="canStart" link size="small" type="primary" @click="aftersales('start')">接手处理</el-button>
      <el-button v-if="canFinish" link size="small" type="success" @click="finishAftersales">处理完成</el-button>
      <el-button v-if="canDelete" link size="small" type="danger" @click="deleteConversation">删除会话（测试）</el-button>
      <el-button v-if="simulatedCount" link size="small" type="warning" @click="clearSimulated">清除模拟消息（{{ simulatedCount }}）</el-button>
      <el-button link size="small" :disabled="claimedByOther" @click="toggleHandoff">
        {{ d.conversation.handoff_state === 'HUMAN' ? '解除接管' : '人工接管' }}
      </el-button>
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
      <el-tag size="small" :type="af.status === 'DONE' ? 'success' : af.status === 'IN_PROGRESS' ? 'primary' : 'warning'" effect="dark">
        {{ af.status_label }}
      </el-tag>
      <b>{{ af.type }}</b>
      <span class="muted">客服 {{ af.marked_by }} 标记于 {{ af.marked_at ? fmtTime(af.marked_at) : '—' }}</span>
      <span v-if="af.handled_by" class="muted">· 售后 {{ af.handled_by }}</span>
      <div v-if="af.note" class="af-note">{{ af.note }}</div>
    </div>
    <div v-if="convMark" class="conv-mark">
      <el-tag size="small" :type="convMark.color" effect="dark">{{ convMark.tag }}</el-tag>
      <span v-if="convMark.note" class="mark-note">{{ convMark.note }}</span>
      <span class="muted">{{ convMark.marked_by }} · {{ convMark.marked_at ? fmtTime(convMark.marked_at) : '' }}</span>
      <el-button link size="small" type="primary" @click="openConvMark">修改</el-button>
    </div>
    <AftersalesFilesPanel :conv-id="convId" :marketplace="d.conversation.marketplace_id"
      :refresh-key="job ? `${job.id}-${job.state}-${d.messages.length}` : String(d.messages.length)"
      @insert="(t: string) => (editText = t)" />
    <LogisticsTicketsPanel :conv-id="convId" :refresh-key="job ? `${job.id}-${job.state}` : ''"
      @insert="(t: string) => (editText = t)" />
    <InvoicePanel :conv-id="convId" :refresh-key="job ? `${job.id}-${job.state}` : ''" />
    <el-dialog v-model="markOpen" title="标记这个会话" width="420px">
      <el-form label-width="60px">
        <el-form-item label="标记">
          <el-radio-group v-model="convMarkForm.tag_id">
            <el-radio v-for="t in msgTags" :key="t.id" :value="t.id">{{ t.name }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="convMarkForm.note" type="textarea" :rows="2" placeholder="如：已让买家提供 VIN，等回复" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button v-if="convMark" type="danger" plain @click="saveConvMark(true)">去掉标记</el-button>
        <el-button @click="markOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!convMarkForm.tag_id" @click="saveConvMark()">保存</el-button>
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

    <!-- 聊天记录 -->
    <div ref="chatEl" class="chat">
      <div v-for="m in d.messages" :key="m.id" class="msg-row" :class="m.role.toLowerCase()">
        <div class="bubble" :class="{ task: traceSourceSet.has(m.ebay_message_id), sim: m.simulated }">
          <div class="msg-head">
            <span>{{ ROLE_LABELS[m.role] || m.role }}</span>
            <span class="muted">{{ fmtTime(m.sent_at) }}</span>
            <el-tag v-if="m.trace?.auto" size="small" type="success" effect="dark">AI 自动回复</el-tag>
            <el-tag v-if="m.simulated" size="small" type="warning" effect="plain">模拟发送 · 未发到 eBay</el-tag>
            <el-tag v-if="traceSourceSet.has(m.ebay_message_id)" size="small" type="warning" effect="plain">本次回复对象</el-tag>
            <el-button v-if="m.trace?.from_ai" link size="small" type="primary" @click="openTrace(m)">查看依据</el-button>
            <el-button v-if="m.has_html" link size="small" type="primary" @click="openMail(m)">查看原邮件</el-button>
            <el-button v-if="!m.zh_translation" link size="small" :loading="translating[m.id]" @click="translateMessage(m.id)">译</el-button>
          </div>

          <div v-if="m.subject" class="muted">主题：{{ m.subject }}</div>
          <div class="msg-body">{{ m.body }}</div>
          <div v-if="m.zh_translation" class="trans">
            <span class="muted">{{ m.direction === 'OUT' ? '中文对照（买家收到的是上方原文）：' : '机翻，仅供理解：' }}</span>{{ m.zh_translation }}
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
          <div v-else-if="m.media.length" class="muted">附件：{{ m.media.map((x) => x.mediaName || x.mediaType).join('、') }}</div>
        </div>
      </div>

      <div v-for="e in d.events" :key="e.id" class="event muted">
        {{ fmtTime(e.occurred_at) }} · {{ e.event_type }} · {{ e.source }} <span v-if="e.case_id">· {{ e.case_id }}</span>
      </div>

      <!-- 本次 AI 草稿的生成过程 -->
      <div v-if="showTrace" class="msg-row seller">
        <div class="bubble ai">
          <div class="msg-head ai-head" @click="traceOpen = !traceOpen">
            <span>{{ aiBusy ? 'AI 正在生成草稿…' : 'AI 草稿的生成依据' }}</span>
            <span class="muted">第 {{ job?.run_generation }} 次生成</span>
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
      <div v-if="activeDraft?.text_zh && !edited && editText" class="trans"><span class="muted">AI 草稿中文回译：</span>{{ activeDraft.text_zh }}</div>
      <div v-if="editTranslation" class="trans"><span class="muted">当前文本中文译文（机翻）：</span>{{ editTranslation }}</div>
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
            <span class="muted">AI 原稿（发送前被修改过）：</span>{{ drawerTrace.agent_draft.text }}
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
  background: #f4f8ff;
  font-size: 13px;
}
.mark-note {
  color: #333;
}
.af-box {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: #fff7e6;
  font-size: 13px;
}
.af-box.IN_PROGRESS {
  background: #ecf5ff;
}
.af-box.DONE {
  background: #f0f9eb;
}
.af-note {
  width: 100%;
  white-space: pre-wrap;
  color: #333;
}
.listing-card {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 6px 12px;
  border-bottom: 1px solid var(--line);
  background: #fafbfc;
}
.listing-img {
  width: 48px;
  height: 48px;
  object-fit: contain;
  border-radius: 4px;
  background: #fff;
  border: 1px solid var(--line);
  flex: none;
}
.listing-info {
  min-width: 0;
}
.listing-title {
  font-size: 13px;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.listing-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
}
.conv-title {
  font-weight: 600;
  font-size: 13px;
}
.chat {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px;
  background: #fafbfc;
}
.msg-row {
  display: flex;
  margin-bottom: 10px;
}
.msg-row.seller {
  justify-content: flex-end;
}
.msg-row.system,
.msg-row.unknown {
  justify-content: center;
}
.bubble {
  max-width: 78%;
  border-radius: 10px;
  padding: 8px 12px;
  border: 1px solid transparent;
  background: var(--buyer);
}
.seller .bubble {
  background: var(--seller);
}
.system .bubble,
.unknown .bubble {
  background: var(--system);
  max-width: 90%;
}
.bubble.task {
  border-color: #e6a23c;
}
.bubble.sim {
  border-style: dashed;
  border-color: #e6a23c;
}
.bubble.ai {
  background: #fff;
  border: 1px dashed #a0cfff;
  width: 78%;
}
.msg-head {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
}
.ai-head {
  cursor: pointer;
  font-size: 13px;
}
.msg-body {
  white-space: pre-wrap;
  font-size: 14px;
  line-height: 1.5;
  word-break: break-word;
}
.trans {
  margin-top: 6px;
  font-size: 13px;
  color: #475467;
  background: #f8fafc;
  border-left: 3px solid #c0c4cc;
  padding: 4px 8px;
  white-space: pre-wrap;
}
.event {
  text-align: center;
  margin: 6px 0;
}
.composer {
  border-top: 1px solid var(--line);
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
  font-size: 13px;
}
.hl {
  white-space: pre-wrap;
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: 6px;
  padding: 6px 8px;
  margin-top: 6px;
}
.warn {
  color: #d97706;
  font-size: 12px;
}
.mail-note {
  margin-bottom: 8px;
}
.mail-frame {
  width: 100%;
  height: 70vh;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
.drawer-msg {
  background: var(--seller);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 14px;
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
  font-size: 12px;
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
