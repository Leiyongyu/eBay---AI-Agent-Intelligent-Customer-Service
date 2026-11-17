<script setup lang="ts">
// 售后管理 → 咨询（未收到货）的“纠纷处理”页：基本信息、纠纷备注、订单信息、处理方案（上传运单号 / 升级为纠纷 /
// 申请退款 / 给买家发信，真实提交到 eBay，只有对接店铺可用）、处理历史。上一个 / 下一个按当前列表顺序。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'
import { copyText, fmtFull, money } from '../caseCommon'
import type { CaseRow, CaseTag } from '../caseCommon'
import { dotStyle } from '../tagColor'
import CaseMessageTab from './CaseMessageTab.vue'

const props = defineProps<{ rows: CaseRow[]; index: number; tags: CaseTag[] }>()
const emit = defineEmits<{ close: []; move: [index: number]; changed: [row: CaseRow]; open: [convId: number] }>()

interface Note { id: number; text: string; kind: string; created_by: string | null; created_at: string | null }
interface HistoryItem { actor: string | null; action: string | null; description: string | null; at: string | null }
interface Amt { value: number; currency: string | null }
interface Live {
  status: string | null; creation_reason: string | null; claim_amount: Amt | null
  expected_refund: Amt | null; refunded: Amt | null; final_refund: Amt | null
  opened_at: string | null; escalated_at: string | null; expires_at: string | null; closed_at: string | null
  tracking: { carrier: string | null; number: string | null; status: string | null; url: string | null } | null
  history: HistoryItem[]
}
interface OrderInfo {
  order_id?: string; buyer?: string | null; name?: string | null; address?: string | null; country?: string | null
  created_at?: string | null; paid_at?: string | null; status?: string | null; total?: string | null; currency?: string | null
  lines?: { sku: string | null; title: string | null; quantity: number | null; item_id: string | null; price: string | null; currency: string | null }[]
  packages?: { carrier: string | null; tracking_number: string | null; shipped_at: string | null }[]
  payment_status?: string | null; payment_status_label?: string | null
  refunds?: { amount: Amt | null; at: string | null; status: string | null; line: string | null }[]
  error?: string
}
interface Full {
  notes: Note[]; extras: Record<string, any>; order: OrderInfo; inquiry: Live | null; inquiry_error: string | null
  actions: { blocked: string | null; can: Record<string, boolean>; labels: Record<string, string> }
  escalate_reasons: { code: string; label: string }[]
}

const row = computed(() => props.rows[props.index])
const full = ref<Full | null>(null)
const loading = ref(false)
const tab = ref('shipment')
const noteText = ref('')
const busy = ref(false)
const form = reactive({ tracking_number: '', carrier: '', shipped_at: '' as string | null, ship_comment: '',
                        reason: '', esc_comment: '', refund_comment: '', message: '', message_zh: null as string | null })
const msgTab = ref<InstanceType<typeof CaseMessageTab> | null>(null)

async function load() {
  if (!row.value) return
  loading.value = true
  full.value = null
  try {
    full.value = await get<Full>(`/internal/ebay-cases/${row.value.id}/full`)
    const pkg = full.value.order?.packages?.[0]
    const t = full.value.inquiry?.tracking
    form.tracking_number = t?.number || pkg?.tracking_number || ''
    form.carrier = t?.carrier || pkg?.carrier || ''
    form.shipped_at = pkg?.shipped_at || null
    form.ship_comment = form.reason = form.esc_comment = form.refund_comment = form.message = ''
    if (row.value.is_new) post(`/internal/ebay-cases/${row.value.id}/seen`).then(() => (row.value.is_new = false)).catch(() => null)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
watch(() => props.index, load, { immediate: true })

const ACTOR: Record<string, string> = { BUYER: '买家', SELLER: '卖家', SYSTEM: '系统', CSR: 'eBay 客服', EBAY: 'eBay' }
const sellerSide = (h: HistoryItem) => (h.actor || '').toUpperCase() === 'SELLER'
const history = computed(() => full.value?.inquiry?.history || [])
const claim = computed(() => full.value?.inquiry?.claim_amount
  || (row.value?.amount != null ? { value: row.value.amount, currency: row.value.currency } : null))
const line = computed(() => full.value?.order?.lines?.find((l) => l.item_id === row.value?.item_id) || full.value?.order?.lines?.[0])
const pkg = computed(() => full.value?.order?.packages?.[0])
const caseTags = computed(() => props.tags.filter((t) => (row.value?.tag_ids || []).includes(t.id)))
const can = (a: string) => !!full.value?.actions.can[a]
const blocked = (a: string) => full.value?.actions.blocked
  || (!can(a) ? `${full.value?.actions.labels[a] || ''}只有管理员和售后可以做` : null)
const REASON_ZH: Record<string, string> = { ITEM_NOT_RECEIVED: '没收到货（ITEM_NOT_RECEIVED）' }
const reasonText = computed(() => {
  const r = full.value?.inquiry?.creation_reason
  return r ? REASON_ZH[r] || r : row.value?.reason_label || '—'
})
// 退款前按 eBay 实时数据拦：这条咨询已退过款 / 订单已全额退款 / 订单查不到（不确定退没退过）
const refundStop = computed(() => {
  const inq = full.value?.inquiry
  if (full.value && !inq) return 'eBay 咨询详情没取到，不能确认索赔金额'
  if (inq?.refunded && inq.refunded.value > 0) return `eBay 显示这条咨询已经退过款（${money(inq.refunded.value, inq.refunded.currency)}）`
  const o = full.value?.order
  if (o?.payment_status === 'FULLY_REFUNDED') return 'eBay 显示这笔订单已经全额退款'
  if (o?.error && row.value?.order_id) return `订单的退款情况没查到：${o.error}`
  return null
})

async function addNote() {
  const text = noteText.value.trim()
  if (!text || !row.value) return
  try {
    const n = await post<Note>(`/internal/ebay-cases/${row.value.id}/notes`, { text })
    full.value?.notes.unshift(n)
    row.value.note = n.text
    row.value.note_by = n.created_by
    row.value.note_at = n.created_at
    noteText.value = ''
    emit('changed', row.value)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function submit(action: string) {
  if (!row.value || busy.value) return
  const c = claim.value
  const payload: Record<string, any> = {
    shipment: { tracking_number: form.tracking_number, carrier: form.carrier, shipped_at: form.shipped_at || null, comment: form.ship_comment },
    escalate: { reason: form.reason, comment: form.esc_comment },
    refund: { amount: full.value?.inquiry?.claim_amount?.value, currency: full.value?.inquiry?.claim_amount?.currency, confirm: true, comment: form.refund_comment },
    message: { text: form.message, text_zh: form.message_zh },
  }[action]!
  const what = full.value?.actions.labels[action] || action
  const tip = action === 'refund'
    ? `确认给买家 ${row.value.buyer} 全额退款 ${money(c?.value, c?.currency)}？退款会真实打到买家账户，不能撤回。`
    : action === 'escalate' ? '确认把这条咨询升级为纠纷？升级后由 eBay 介入裁决。'
      : action === 'message' ? `确认给买家 ${row.value.buyer} 发信？会先进发件箱，延迟发出，之前可在会话里撤回。`
        : `确认在 eBay 上${what}？提交后会真实发给买家 / eBay。`
  try {
    await ElMessageBox.confirm(tip, what, { type: action === 'refund' ? 'warning' : 'info', confirmButtonText: '确认提交', cancelButtonText: '取消' })
  } catch {
    return
  }
  busy.value = true
  try {
    const r = await post<{ done: string; status: string | null; is_open: boolean }>(`/internal/ebay-cases/${row.value.id}/inquiry/${action}`, payload)
    ElMessage.success(action === 'message' ? '已放进发件箱，到时间自动发出，之前可在会话里撤回' : `已${what}：${r.done}`)
    if (action === 'message') msgTab.value?.clear()
    row.value.status = r.status
    row.value.is_open = r.is_open
    emit('changed', row.value)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

function sendMessage(p: { text: string; text_zh: string | null }) {
  form.message = p.text
  form.message_zh = p.text_zh
  submit('message')
}
const prev = () => props.index > 0 && emit('move', props.index - 1)
const next = () => props.index < props.rows.length - 1 && emit('move', props.index + 1)
</script>

<template>
  <el-dialog :model-value="true" width="1200px" top="3vh" class="inq-dialog" append-to-body :close-on-click-modal="false"
             @close="emit('close')">
    <template #header>
      <div class="dlg-head">
        <b>纠纷处理</b>
        <span class="muted">{{ index + 1 }} / {{ rows.length }}</span>
        <span class="grow" />
        <el-button size="small" :disabled="index <= 0" @click="prev">上一个</el-button>
        <el-button size="small" type="success" :disabled="index >= rows.length - 1" @click="next">下一个</el-button>
      </div>
    </template>
    <div v-if="row" v-loading="loading" class="inq">
      <!-- 基本信息 -->
      <div class="sec">基本信息</div>
      <table class="kv">
        <tbody>
          <tr>
            <th>类型/状态</th>
            <td>未收到货 / <span :class="row.is_open ? 'red' : 'muted'">{{ row.status_label || row.status || '—' }}</span></td>
            <th>纠纷编号</th>
            <td>{{ row.external_id }} <button class="copy" @click="copyText(row.external_id)">⧉</button></td>
          </tr>
          <tr>
            <th>Item ID</th><td>{{ row.item_id || '—' }}</td>
            <th>开启原因</th><td>{{ reasonText }}</td>
          </tr>
          <tr>
            <th>纠纷金额</th><td>{{ money(claim?.value, claim?.currency) }}</td>
            <th>开启 / 处理截止</th>
            <td>{{ fmtFull(full?.inquiry?.opened_at || row.opened_at) }} / {{ fmtFull(full?.inquiry?.expires_at || row.due_at) }}</td>
          </tr>
          <tr>
            <th>卖家账号</th><td>{{ row.store_name }}</td>
            <th>买家期待</th>
            <td>
              <div v-if="row.comment" class="pre">{{ row.comment }}</div>
              <div v-if="full?.inquiry?.expected_refund">期待退款 {{ money(full.inquiry.expected_refund.value, full.inquiry.expected_refund.currency) }}</div>
              <span v-if="!row.comment && !full?.inquiry?.expected_refund" class="muted">—（eBay 没有给出）</span>
            </td>
          </tr>
          <tr>
            <th>客户姓名</th><td>{{ full?.order?.name || '—' }}</td>
            <th>客户ID</th><td>{{ row.buyer || '—' }}</td>
          </tr>
          <tr>
            <th>收件地址</th><td>{{ full?.order?.address || '—' }} {{ full?.order?.country || '' }}</td>
            <th>标签</th>
            <td>
              <span v-for="t in caseTags" :key="t.id" class="tag"><i class="dot" :style="dotStyle(t.color)" />{{ t.name }}</span>
              <span v-if="!caseTags.length" class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="full?.inquiry_error" class="warn-line">eBay 实时详情没取到：{{ full.inquiry_error }}（下面用本地同步的数据）</div>

      <!-- 纠纷备注 -->
      <div class="note-box">
        <span class="lbl">纠纷备注：</span>
        <div class="note-main">
          <div class="note-input">
            <el-input v-model="noteText" type="textarea" :rows="3" maxlength="2000" placeholder="请填写纠纷备注" />
            <el-button :disabled="!noteText.trim()" @click="addNote">新增</el-button>
          </div>
          <div v-for="n in full?.notes || []" :key="n.id" class="note" :class="{ action: n.kind === 'ACTION' }">
            <div class="pre">{{ n.text }}</div>
            <div class="muted small">{{ fmtFull(n.created_at) }} by {{ n.created_by || '—' }}</div>
          </div>
        </div>
      </div>

      <!-- 订单信息 -->
      <div class="sec">订单信息</div>
      <div class="buyer-bar">
        <span class="buyer">{{ row.buyer }}<template v-if="full?.order?.name">（{{ full.order.name }}）</template></span>
        <span>{{ full?.order?.country || '—' }}</span>
        <el-button v-if="row.conversation_id" link type="warning" @click="emit('open', row.conversation_id!)">联系TA</el-button>
        <span v-else class="muted small">没有会话</span>
      </div>
      <div v-if="full?.order?.error" class="warn-line">{{ full.order.error }}</div>
      <table class="grid">
        <thead><tr><th>产品信息</th><th>订单编号</th><th>运单号/物流公司</th><th>单价</th><th>订单金额</th><th>时间</th></tr></thead>
        <tbody>
          <tr>
            <td class="prod">
              <img v-if="full?.extras?.picture_url" :src="full.extras.picture_url" alt="" />
              <div>
                <div><b>{{ line?.sku || full?.extras?.sku || '—' }}</b> ×{{ line?.quantity || full?.extras?.quantity || 1 }}</div>
                <div class="title">{{ line?.title || full?.extras?.title || row.title || '' }}</div>
                <div class="muted">{{ row.item_id }}</div>
              </div>
            </td>
            <td>{{ full?.order?.order_id || full?.extras?.order_id || row.order_id || '—' }}
              <div :class="full?.order?.status === 'FULFILLED' ? 'green' : 'muted'">{{ full?.order?.status === 'FULFILLED' ? '已发货' : (full?.order?.status || '') }}</div></td>
            <td>{{ pkg?.tracking_number || full?.inquiry?.tracking?.number || '—' }}
              <button v-if="pkg?.tracking_number" class="copy" @click="copyText(pkg!.tracking_number!)">⧉</button>
              <div>{{ pkg?.carrier || full?.inquiry?.tracking?.carrier || '' }}</div></td>
            <td>{{ line?.price ? `${line.price} ${line.currency || ''}` : '—' }}</td>
            <td>{{ full?.order?.total ? `${full.order.total} ${full.order.currency || ''}` : '—' }}<div class="muted small">（eBay 订单总额）</div>
              <div v-if="full?.order?.payment_status" :class="full.order.payment_status === 'PAID' ? 'muted small' : 'red small'">{{ full.order.payment_status_label }}</div></td>
            <td class="small">
              <div>付款：{{ fmtFull(full?.order?.paid_at) }}</div>
              <div>纠纷：{{ fmtFull(row.opened_at) }}</div>
              <div>发货：{{ fmtFull(pkg?.shipped_at) }}</div>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 处理方案 -->
      <div class="sec">处理方案</div>
      <div v-if="full?.actions.blocked" class="warn-line">{{ full.actions.blocked }}</div>
      <el-tabs v-model="tab" type="card" class="plan">
        <el-tab-pane label="上传运单号" name="shipment">
          <el-form label-width="90px" class="plan-form">
            <el-form-item label="运单号" required><el-input v-model="form.tracking_number" maxlength="64" /></el-form-item>
            <el-form-item label="物流渠道" required><el-input v-model="form.carrier" maxlength="64" placeholder="如 Royal Mail、DHL" /></el-form-item>
            <el-form-item label="发货时间">
              <el-date-picker v-model="form.shipped_at" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss[Z]" placeholder="不填用 eBay 上已有的" />
            </el-form-item>
            <el-form-item label="备注"><el-input v-model="form.ship_comment" type="textarea" :rows="4" maxlength="1000" /></el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!blocked('shipment')" :content="blocked('shipment') || ''">
                <span><el-button type="success" :loading="busy" :disabled="!!blocked('shipment')" @click="submit('shipment')">提交</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="升级为纠纷" name="escalate">
          <el-form label-width="90px" class="plan-form">
            <el-form-item label="升级原因" required>
              <el-select v-model="form.reason" placeholder="请选择" style="width: 300px">
                <el-option v-for="r in full?.escalate_reasons || []" :key="r.code" :label="r.label" :value="r.code" />
              </el-select>
            </el-form-item>
            <el-form-item label="留言" required><el-input v-model="form.esc_comment" type="textarea" :rows="4" maxlength="1000" placeholder="请输入给平台的留言" /></el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!blocked('escalate')" :content="blocked('escalate') || ''">
                <span><el-button type="success" :loading="busy" :disabled="!!blocked('escalate')" @click="submit('escalate')">提交</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="申请退款" name="refund">
          <el-form label-width="90px" class="plan-form">
            <el-form-item label="订单编号">{{ full?.order?.order_id || full?.extras?.order_id || row.order_id || '—' }}</el-form-item>
            <el-form-item label="SKU">{{ line?.sku || full?.extras?.sku || '—' }}</el-form-item>
            <el-form-item label="付款状态">
              <span :class="full?.order?.payment_status && full.order.payment_status !== 'PAID' ? 'red' : ''">{{ full?.order?.payment_status_label || '—' }}</span>
              <span class="muted small">（eBay 订单实时状态）</span>
            </el-form-item>
            <el-form-item v-if="full?.order?.refunds?.length" label="已退款">
              <div>
                <div v-for="(r, i) in full.order.refunds" :key="i">
                  {{ money(r.amount?.value, r.amount?.currency) }} · {{ fmtFull(r.at) }} · {{ r.status || '' }}<template v-if="r.line"> · {{ r.line }}</template>
                </div>
              </div>
            </el-form-item>
            <el-form-item label="退款类型">
              全额退款
              <span class="muted small">（eBay 的咨询接口只能全额退索赔金额；部分退款不在这里做）</span>
            </el-form-item>
            <el-form-item label="退款金额" required>
              <el-input :model-value="claim ? claim.value.toFixed(2) : ''" disabled style="width: 160px">
                <template #prefix>{{ claim?.currency || '' }}</template>
              </el-input>
              <span class="muted small">（eBay 上这条咨询的索赔金额）</span>
            </el-form-item>
            <el-form-item label="退款原因">{{ reasonText }}<span class="muted small">（咨询的开启原因）</span></el-form-item>
            <div v-if="refundStop" class="warn-line">{{ refundStop }}，不能在这里退款</div>
            <el-form-item label="备注"><el-input v-model="form.refund_comment" type="textarea" :rows="4" maxlength="1000" placeholder="给买家的退款说明（可不填）" /></el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!blocked('refund') && !refundStop" :content="blocked('refund') || refundStop || ''">
                <span><el-button type="success" :loading="busy" :disabled="!!blocked('refund') || !!refundStop || !full?.inquiry?.claim_amount" @click="submit('refund')">提交</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="给买家发信" name="message">
          <CaseMessageTab ref="msgTab" :conv-id="row.conversation_id"
                          :blocked="blocked('message') || (!row.conversation_id ? '这张售后单对不上买家会话，请到 eBay 后台留言' : null)"
                          :busy="busy" @submit="sendMessage" />
        </el-tab-pane>
      </el-tabs>

      <!-- 处理历史 -->
      <div class="sec">处理历史</div>
      <div class="history">
        <div class="col-head">买家及平台（{{ row.buyer }}）</div>
        <div class="col-head">卖家（{{ row.store_name }}）</div>
        <template v-for="(h, i) in history" :key="i">
          <div :class="['h-cell', { empty: sellerSide(h) }]">
            <div v-if="!sellerSide(h)" class="bubble">
              <div class="b-head"><b>{{ h.description || h.action }}</b><span class="muted small">{{ fmtFull(h.at) }}</span></div>
              <div class="muted small">{{ ACTOR[(h.actor || '').toUpperCase()] || h.actor }} · {{ h.action }}</div>
            </div>
          </div>
          <div :class="['h-cell', { empty: !sellerSide(h) }]">
            <div v-if="sellerSide(h)" class="bubble seller">
              <div class="b-head"><b>{{ h.description || h.action }}</b><span class="muted small">{{ fmtFull(h.at) }}</span></div>
              <div class="muted small">{{ h.action }}</div>
            </div>
          </div>
        </template>
        <div v-if="!history.length" class="muted empty-hist">{{ full?.inquiry_error ? '没取到 eBay 的处理历史' : '还没有处理记录' }}</div>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.dlg-head { display: flex; align-items: center; gap: 10px; }
.dlg-head b { font-size: 16px; }
.grow { flex: 1; }
.inq { font-size: 13px; }
.sec { background: var(--el-fill-color-light); padding: 8px 12px; margin: 14px 0 10px; font-weight: 600; }
.kv { width: 100%; border-collapse: collapse; }
.kv th, .kv td { border: 1px solid var(--el-border-color-lighter); padding: 6px 8px; text-align: left; vertical-align: top; }
.kv th { width: 110px; background: var(--el-fill-color-lighter); font-weight: normal; color: var(--el-text-color-regular); }
.red { color: var(--el-color-danger); }
.green { color: var(--el-color-success); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.pre { white-space: pre-wrap; }
.copy { border: none; background: none; cursor: pointer; color: var(--el-text-color-secondary); }
.tag { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0; }
.note-box { display: flex; gap: 8px; margin-top: 14px; }
.note-box .lbl { width: 70px; padding-top: 6px; }
.note-main { width: 640px; }
.note-input { display: flex; gap: 8px; align-items: flex-end; }
.note { border: 1px solid var(--el-border-color-lighter); padding: 6px 10px; margin-top: 8px; }
.note.action { background: var(--el-color-primary-light-9); }
.buyer-bar { display: flex; gap: 16px; align-items: center; border: 1px solid var(--el-border-color-lighter); padding: 6px 12px; width: fit-content; }
.buyer { color: var(--el-color-warning-dark-2); }
.grid { width: 100%; border-collapse: collapse; margin-top: 8px; }
.grid th { background: var(--el-fill-color-light); text-align: left; padding: 6px 8px; font-weight: normal; }
.grid td { padding: 8px; vertical-align: top; border-bottom: 1px solid var(--el-border-color-lighter); }
.prod { display: flex; gap: 8px; max-width: 420px; }
.prod img { width: 56px; height: 56px; object-fit: contain; }
.prod .title { font-size: 12px; max-width: 340px; }
.plan-form { max-width: 640px; padding-top: 6px; }
.msg-wrap { display: flex; gap: 20px; }
.msg-wrap .plan-form { max-width: none; }
.tpl { width: 260px; padding-top: 6px; }
.history { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; }
.col-head { background: var(--el-fill-color-light); padding: 8px 12px; }
.bubble { border: 1px solid var(--el-color-warning-light-5); background: var(--el-color-warning-light-9); border-radius: 6px; padding: 8px 12px; }
.bubble.seller { border-color: var(--el-color-success-light-5); background: var(--el-color-success-light-9); }
.b-head { display: flex; justify-content: space-between; gap: 12px; }
.empty-hist { grid-column: 1 / span 2; padding: 12px; }
</style>
