<script setup lang="ts">
// 发票页里审核一条发票申请：核对邮箱、订单、抬头 → 生成 PDF（留底）→ 预览核对 → 确认后发邮件，并在买家会话里发消息提醒查收
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import { errorText, fmtTime } from '../format'
import type { ConversationInvoice, InvoiceInfo, InvoiceRequestRow } from '../types'

const props = defineProps<{ reqId: number | null }>()
const emit = defineEmits<{ close: []; changed: []; openConversation: [id: number] }>()

type Detail = ConversationInvoice & { request: InvoiceRequestRow; sender: string | null }
const info = ref<Detail | null>(null)
const busy = ref(false)
const form = ref({ email: '', order_id: '', buyer_company: '', buyer_vat: '', language: '', invoice_type: 'NORMAL' as 'NORMAL' | 'VAT' })
const isAdmin = computed(() => session.user?.role === 'ADMIN')
const LANG_LABELS: Record<string, string> = { de: '德语', en: '英语', fr: '法语', it: '意大利语', es: '西班牙语' }
const req = computed(() => info.value?.request || null)

async function load() {
  if (!props.reqId) return
  try {
    info.value = await get<Detail>(`/internal/invoice-requests/${props.reqId}`)
    const r = info.value.request
    form.value = {
      email: r.email || '',
      order_id: r.order_id || (info.value.orders.length === 1 ? info.value.orders[0].order_id : ''),
      buyer_company: r.buyer_company || '', buyer_vat: r.buyer_vat || '', language: '',
      invoice_type: r.invoice_type || (r.buyer_company || r.buyer_vat ? 'VAT' : 'NORMAL'),
    }
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
watch(() => props.reqId, () => { info.value = null; load() }, { immediate: true })

function body(extra: Record<string, unknown> = {}) {
  return { email: form.value.email, order_id: form.value.order_id, buyer_company: form.value.buyer_company,
           buyer_vat: form.value.buyer_vat, invoice_type: form.value.invoice_type, ...extra }
}

async function save(status?: 'OPEN' | 'CANCELLED') {
  busy.value = true
  try {
    await api('PUT', `/internal/invoice-requests/${props.reqId}`, body(status ? { status } : {}))
    ElMessage.success(status === 'CANCELLED' ? '已取消这条发票申请' : '已保存')
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function issue() {
  if (!form.value.order_id.trim()) return ElMessage.warning('请选择或填写订单号')
  busy.value = true
  try {
    await api('PUT', `/internal/invoice-requests/${props.reqId}`, body())
    const inv = await post<InvoiceInfo>(`/internal/invoice-requests/${props.reqId}/invoices`, {
      order_id: form.value.order_id.trim(), language: form.value.language || null, kind: 'NORMAL',
      buyer_company: form.value.buyer_company, buyer_vat: form.value.buyer_vat,
    })
    ElMessage.success(`已生成${inv.kind_label} ${inv.number}（已留底）`)
    await load()
    emit('changed')
    startReview(inv)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

const pdfUrl = (inv: InvoiceInfo) => `/internal/invoices/${inv.id}/pdf`
const canView = (inv: InvoiceInfo) => isAdmin.value || inv.status === 'ISSUED' || inv.status === 'SEND_FAILED'
const canSend = (inv: InvoiceInfo) =>
  inv.status === 'ISSUED' || inv.status === 'SEND_FAILED' || (inv.status === 'SIMULATED' && isAdmin.value)

// 审核发送
const review = ref<InvoiceInfo | null>(null)
const checked = ref(false)
const message = ref('')
const messageZh = ref('')
async function startReview(inv: InvoiceInfo) {
  review.value = inv
  checked.value = false
  try {
    const n = await get<{ text: string; text_zh: string }>(`/internal/invoices/${inv.id}/notice`)
    message.value = n.text
    messageZh.value = n.text_zh
  } catch {
    message.value = ''
  }
}

async function send() {
  const inv = review.value
  if (!inv) return
  busy.value = true
  try {
    const r = await post<{ invoice: InvoiceInfo; notice: { sent: boolean; error: string | null } }>(
      `/internal/invoice-requests/${props.reqId}/send`,
      { invoice_id: inv.id, email: form.value.email, confirmed: true, message: message.value })
    if (r.invoice.status === 'SEND_FAILED') ElMessage.error(r.invoice.send_error || '邮件发送失败')
    else {
      const how = r.invoice.status === 'SENT' ? '已发到买家邮箱' : '邮件发送未开启，已记为模拟发送'
      if (r.notice.sent) ElMessage.success(`发票 ${r.invoice.number} ${how}，并已给买家发消息提醒查收`)
      else ElMessage.warning(`发票 ${r.invoice.number} ${how}；提醒消息没发出：${r.notice.error}，请到工作台补发`)
    }
    review.value = null
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function voidInvoice(inv: InvoiceInfo) {
  try {
    const { value } = await ElMessageBox.prompt(`作废发票 ${inv.number}？号码和留底文件会保留。`, '作废发票', {
      inputPlaceholder: '作废原因，如：订单选错', inputValidator: (v) => !!v?.trim() || '请写原因' })
    await post(`/internal/invoices/${inv.id}/void`, { reason: value })
    await load()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'primary'> = {
  ISSUED: 'primary', SENT: 'success', SIMULATED: 'warning', SEND_FAILED: 'danger', VOID: 'info',
}
</script>

<template>
  <el-drawer :model-value="!!reqId" size="720px" :title="req ? `发票申请 #${req.id}` : '发票申请'" @close="emit('close')">
    <template v-if="info && req">
      <div class="head">
        <el-tag :type="req.status === 'OPEN' ? 'warning' : req.status === 'DONE' ? 'success' : 'info'">
          {{ req.status_label }}
        </el-tag>
        <span>买家 <b class="mono">{{ req.buyer_key }}</b> · {{ req.store_id }} · {{ req.marketplace_id }}</span>
        <el-tag v-if="req.test_conversation" size="small" type="info">测试会话</el-tag>
        <el-button link type="primary" size="small" @click="emit('openConversation', req.conversation_id)">打开会话</el-button>
      </div>
      <el-alert v-if="req.note" :title="req.note" type="warning" :closable="false" class="mb" />
      <el-alert v-if="!info.mail.enabled" type="warning" :closable="false" show-icon class="mb"
        title="邮件发送未开启：现在发送只记为模拟发送，买家收不到邮件" :description="info.mail.not_ready_reasons.join('；')" />
      <el-alert v-if="req.invoice_type === 'VAT'" type="error" :closable="false" show-icon class="mb"
        title="买家要增值税发票：系统不开增值税发票，请人工开具后发给买家（处理完点“不用开了”关掉这条申请）" />
      <div class="muted small mb">发票抬头（公司名、注册人、地址）自动取这笔订单所属店铺的 eBay 注册资料。</div>

      <el-form label-width="90px">
        <el-form-item label="发票种类">
          <span>普通发票</span>
          <div class="muted small">和 eBay 后台下载的订单单据一样：列明 eBay 代收的增值税（OSS 税号），不带卖方税号。系统只开普通发票。</div>
        </el-form-item>
        <el-form-item label="买家邮箱">
          <el-input v-model="form.email" placeholder="买家在消息里给的接收发票邮箱" />
        </el-form-item>
        <el-form-item label="订单">
          <el-select v-model="form.order_id" filterable allow-create default-first-option style="width: 100%"
            placeholder="选择买家的订单，或直接输入订单号">
            <el-option v-for="o in info.orders" :key="o.order_id" :value="o.order_id"
              :label="`${o.order_id} · ${(o.created_at || '').slice(0, 10)} · ${o.total ? o.total.value + ' ' + o.total.currency : ''} · ${o.title || ''}`" />
          </el-select>
          <div v-if="info.orders_error" class="muted small">查订单失败：{{ info.orders_error }}（可直接输入订单号）</div>
        </el-form-item>
        <el-form-item label="公司抬头"><el-input v-model="form.buyer_company" placeholder="要增值税发票时填（可空）" /></el-form-item>
        <el-form-item label="买家税号"><el-input v-model="form.buyer_vat" placeholder="可空" /></el-form-item>
        <el-form-item label="发票语言">
          <el-select v-model="form.language" placeholder="按收货国家自动选" clearable style="width: 200px">
            <el-option v-for="l in info.languages" :key="l" :value="l" :label="LANG_LABELS[l] || l" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="actions">
        <el-button v-if="req.status !== 'DONE' && req.status !== 'CANCELLED'" :disabled="busy" @click="save('CANCELLED')">不用开了</el-button>
        <el-button v-if="req.status === 'CANCELLED'" :disabled="busy" @click="save('OPEN')">重新打开</el-button>
        <el-button :disabled="busy" @click="save()">保存</el-button>
        <el-button type="primary" :loading="busy" @click="issue">生成普通发票</el-button>
      </div>

      <h4>发票（每份都已留底）</h4>
      <el-empty v-if="!info.invoices.length" :image-size="60" description="还没生成发票" />
      <el-table v-else :data="info.invoices" size="small">
        <el-table-column label="发票号" min-width="140">
          <template #default="{ row }"><span class="mono">{{ row.number }}</span><div class="muted small">{{ row.kind_label }}</div></template>
        </el-table-column>
        <el-table-column label="订单" prop="order_id" min-width="120" />
        <el-table-column label="金额" min-width="90"><template #default="{ row }">{{ row.total.toFixed(2) }} {{ row.currency }}</template></el-table-column>
        <el-table-column label="状态" min-width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_TYPE[row.status]">{{ row.status_label }}</el-tag>
            <div v-if="row.sent_at" class="muted small">{{ row.sent_by }} · {{ fmtTime(row.sent_at) }}</div>
            <div v-if="row.send_error" class="warn small">{{ row.send_error }}</div>
          </template>
        </el-table-column>
        <el-table-column min-width="170">
          <template #default="{ row }">
            <a v-if="canView(row)" :href="pdfUrl(row)" target="_blank" rel="noopener">预览</a>
            <span v-else class="muted small">留底仅管理员可看</span>
            <el-button v-if="canSend(row)" link type="primary" size="small" @click="startReview(row)">审核并发送</el-button>
            <el-button v-if="row.status !== 'VOID' && canView(row)" link type="danger" size="small" @click="voidInvoice(row)">作废</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-dialog :model-value="!!review" title="发送前审核" width="600px" append-to-body @close="review = null">
      <template v-if="review">
        <p>{{ review.kind_label }} <b class="mono">{{ review.number }}</b> · 订单 {{ review.order_id }} ·
          <b>{{ review.total.toFixed(2) }} {{ review.currency }}</b>
          <template v-if="review.vat_rate != null">（含增值税 {{ review.vat_rate }}%：{{ review.vat_amount?.toFixed(2) }}）</template>
          · <a :href="pdfUrl(review)" target="_blank" rel="noopener">打开 PDF 核对 ↗</a>
        </p>
        <el-alert v-for="w in review.warnings" :key="w" :title="w" type="warning" :closable="false" class="mb-s" />
        <el-form label-width="96px">
          <el-form-item label="发到邮箱"><el-input v-model="form.email" /></el-form-item>
          <el-form-item label="发件邮箱"><span>{{ info?.sender || '未配置（.env 的 INVOICE_SMTP_USER）' }}</span></el-form-item>
          <el-form-item label="提醒买家">
            <el-input v-model="message" type="textarea" :rows="6" />
            <div class="muted small pre">中文对照：{{ messageZh }}</div>
            <div class="muted small">发出邮件后，这条消息会发到买家的会话里（不写买家的邮箱）。</div>
          </el-form-item>
        </el-form>
        <el-checkbox v-model="checked">我已核对发票内容（抬头、地址、金额、税率）、买家邮箱和提醒消息</el-checkbox>
      </template>
      <template #footer>
        <el-button @click="review = null">取消</el-button>
        <el-button type="primary" :loading="busy" :disabled="!checked || !form.email || !message.trim()" @click="send">
          {{ info?.mail.enabled ? '确认：发邮件并提醒买家' : '确认（模拟发送）并提醒买家' }}
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.mb {
  margin-bottom: 10px;
}
.mb-s {
  margin-bottom: 6px;
}
.small {
  font-size: var(--fs-xs);
}
.pre {
  white-space: pre-wrap;
}
.warn {
  color: var(--warn-text);
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
h4 {
  margin: 16px 0 6px;
}
</style>
