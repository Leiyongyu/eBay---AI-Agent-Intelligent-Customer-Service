<script setup lang="ts">
// 申请退款（提交退款申请单）：会话侧栏订单、退货 / 纠纷 / 取消页都用它，提交到 POST /internal/refund-requests。
// 按审核规则先审核或直接退款（直接退款只对管理员 / 售后生效）；提交前二次确认金额，确认的金额原样带给后端核对
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'
import { fmtMoney } from '../refundRequest'
import type { RefundPrefill, RefundRequestRow } from '../refundRequest'

const props = defineProps<{ modelValue: boolean; conversationId?: number | null; caseId?: number | null; orderId?: string | null;
  storeId?: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean]; submitted: [r: RefundRequestRow] }>()

const pf = ref<RefundPrefill | null>(null)
const loading = ref(false)
const saving = ref(false)
const err = ref('')
const f = reactive({ order: '', full: true, amount: 0, byLine: false, lines: {} as Record<string, number | null>, reason: '',
  comment: '', note: '' })

async function load(order?: string) {
  loading.value = true
  err.value = ''
  try {
    const q = new URLSearchParams()
    if (props.caseId) q.set('case_id', String(props.caseId))
    if (props.conversationId) q.set('conversation_id', String(props.conversationId))
    if (props.storeId) q.set('store_id', props.storeId)
    const oid = order || props.orderId
    if (oid) q.set('order_id', oid)
    const d = await get<RefundPrefill>(`/internal/refund-requests/prefill?${q}`)
    pf.value = d
    f.order = d.order_id || ''
    f.full = d.requested <= 0
    f.amount = d.remaining ?? 0
    f.byLine = false
    f.lines = Object.fromEntries(d.lines.map((l) => [l.line_item_id, null]))
    f.reason = d.reason
  } catch (e) {
    pf.value = null
    err.value = errorText(e)
  } finally {
    loading.value = false
  }
}
watch(() => props.modelValue, (v) => {
  if (v) {
    Object.assign(f, { comment: '', note: '' })
    load()
  }
})
function pickOrder(o: string) {
  load(o)
}

const cur = computed(() => pf.value?.order?.currency || pf.value?.lines[0]?.currency || '')
const lineSum = computed(() => Object.values(f.lines).reduce<number>((s, v) => s + (Number(v) || 0), 0))
const amount = computed(() => (f.full ? pf.value?.remaining ?? 0 : f.byLine ? Math.round(lineSum.value * 100) / 100 : Number(f.amount) || 0))
watch(() => f.full, (v) => {
  if (v) {
    f.byLine = false
    f.amount = pf.value?.remaining ?? 0
  }
})
const problem = computed(() => {
  const d = pf.value
  if (!d) return err.value || '加载中'
  if (d.blocked) return d.blocked
  if (d.execution === 'post_order_return') return '请在退货的纠纷处理页里申请退款'
  if (!f.order) return '请选择订单'
  if (d.open_request) return `这张订单已经有一张${d.open_request.status_label}的退款申请（#${d.open_request.id}），请先处理那一张`
  if (d.remaining != null && d.remaining <= 0) return '这张订单在本系统已经申请退完了'
  if (!(amount.value > 0)) return '退款金额要大于 0'
  if (d.remaining != null && amount.value > d.remaining + 0.005) return `最多还能退 ${fmtMoney(d.remaining, cur.value)}`
  if (!f.reason) return '请选择退款原因'
  return ''
})
const modeText = computed(() => {
  const d = pf.value
  if (!d) return ''
  if (d.mode === 'direct' && d.can_refund_directly) return '按审核规则：直接退款——提交后马上在 eBay 上退款'
  if (d.mode === 'direct') return '按审核规则可以直接退款，但退款只能由管理员 / 售后执行：提交后进入待审核'
  return '按审核规则：先审核——管理员 / 售后审核通过后才在 eBay 上退款'
})

async function submit() {
  if (problem.value || !pf.value) return
  const a = amount.value
  const text = `${f.full ? '全额' : '部分'}退款 ${fmtMoney(a, cur.value)}（订单 ${f.order}）`
  try {
    await ElMessageBox.confirm(
      `${text}\n原因：${pf.value.reasons.find((r) => r.code === f.reason)?.label || f.reason}\n${modeText.value}`,
      '确认退款金额', { type: 'warning', confirmButtonText: `确认 ${fmtMoney(a, cur.value)}` })
  } catch {
    return
  }
  saving.value = true
  try {
    const lines = !f.full && f.byLine
      ? Object.entries(f.lines).filter(([, v]) => Number(v) > 0).map(([id, v]) => ({ line_item_id: id, amount: Number(v) })) : null
    const r = await post<RefundRequestRow>('/internal/refund-requests', {
      order_id: f.order, store_id: pf.value.store_id, conversation_id: pf.value.conversation_id, case_id: props.caseId || null,
      source: pf.value.source, full: f.full, amount: a, currency: cur.value, reason: f.reason, lines,
      comment: f.comment.trim() || null, note: f.note.trim() || null, confirm_amount: a, confirm_currency: cur.value,
    })
    ElMessage.success(r.status === 'PENDING' ? `已提交退款申请 #${r.id}，等待审核` : `退款申请 #${r.id}：${r.status_label}`)
    if (r.status === 'ERROR' && r.error) ElMessage.error(r.error)
    emit('submitted', r)
    emit('update:modelValue', false)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" title="申请退款" width="min(600px, calc(100vw - 24px))" append-to-body
             @update:model-value="(v: boolean) => emit('update:modelValue', v)">
    <div v-loading="loading" class="rrd">
      <el-alert v-if="err" type="error" :closable="false" show-icon :title="err" />
      <template v-if="pf">
        <el-alert v-if="pf.blocked" type="warning" :closable="false" show-icon :title="pf.blocked" />
        <el-alert v-if="pf.open_case_notice" type="warning" :closable="false" show-icon :title="pf.open_case_notice" />
        <el-alert v-else-if="pf.execution === 'post_order_return'" type="info" :closable="false" show-icon
                  :title="`这条退货（${pf.return_id}）还开着：请在退货的纠纷处理页里申请退款（按退货的费用项退，eBay 才会关闭这条退货）`" />
        <div class="grid">
          <div class="k">店铺</div><div>{{ pf.store_name }}<span v-if="pf.buyer" class="muted"> · 买家 {{ pf.buyer }}</span></div>
          <div class="k">订单</div>
          <div>
            <el-select v-if="pf.orders.length > 1 || !pf.order_id" :model-value="f.order" placeholder="选择订单" style="width: 220px"
                       @update:model-value="pickOrder">
              <el-option v-for="o in pf.orders" :key="o" :label="o" :value="o" />
            </el-select>
            <b v-else>{{ f.order }}</b>
            <span v-if="pf.order" class="muted">　订单总额 {{ fmtMoney(pf.order.total, cur) }}
              <template v-if="pf.requested > 0"> · 本系统已申请 {{ fmtMoney(pf.requested, cur) }}</template></span>
          </div>
          <div class="k">来源</div>
          <div>{{ pf.source_label }}<span v-if="pf.case" class="muted"> · {{ pf.case.kind }} {{ pf.case.external_id }}</span></div>
          <template v-if="f.order && pf.order">
            <div class="k">退款类型</div>
            <div>
              <el-radio-group v-model="f.full">
                <el-radio :value="true" :disabled="pf.requested > 0">全额退款</el-radio>
                <el-radio :value="false">部分退款</el-radio>
              </el-radio-group>
              <div v-if="pf.requested > 0" class="muted small">这张订单之前申请过退款，只能部分退</div>
            </div>
            <div class="k">退款金额</div>
            <div>
              <template v-if="f.full"><b>{{ fmtMoney(amount, cur) }}</b></template>
              <template v-else>
                <el-checkbox v-if="pf.lines.length > 1" v-model="f.byLine">按商品退</el-checkbox>
                <div v-if="f.byLine" class="lines">
                  <div v-for="l in pf.lines" :key="l.line_item_id" class="line">
                    <span class="mono">{{ l.sku || l.line_item_id }}</span>
                    <span class="muted ttl">{{ l.title }}</span>
                    <span class="muted">{{ fmtMoney(l.amount, l.currency) }}</span>
                    <el-input-number v-model="f.lines[l.line_item_id]" :min="0" :max="l.amount ?? undefined" :precision="2" :step="1"
                                     size="small" controls-position="right" />
                  </div>
                  <div>合计 <b>{{ fmtMoney(amount, cur) }}</b></div>
                </div>
                <el-input-number v-else v-model="f.amount" :min="0.01" :max="pf.remaining ?? undefined" :precision="2" :step="1"
                                 controls-position="right" />
                <span class="muted">　{{ cur }}，最多 {{ fmtMoney(pf.remaining, cur) }}</span>
              </template>
            </div>
            <div class="k">退款原因</div>
            <div>
              <el-select v-model="f.reason" style="width: 260px">
                <el-option v-for="r in pf.reasons" :key="r.code" :label="r.label" :value="r.code" />
              </el-select>
              <div class="muted small">发给 eBay 的退款原因（买家退货、取消请选对应的，eBay 据此判断卖家责任）</div>
            </div>
            <div class="k">给买家的说明</div>
            <div>
              <el-input v-model="f.comment" type="textarea" :rows="2" maxlength="1000" show-word-limit
                        placeholder="可空。随退款发给 eBay，买家能看到；请用买家的语言" />
            </div>
            <div class="k">内部备注</div>
            <div><el-input v-model="f.note" type="textarea" :rows="2" maxlength="2000" placeholder="可空，只在本系统（审核人能看到）" /></div>
          </template>
        </div>
        <div v-if="f.order && pf.order" class="mode" :class="pf.mode">{{ modeText }}</div>
        <p class="muted small">退款成功后会在会话里记一条系统事件；给买家的通知不会自动发，请在会话里用 AI 起草后人工审核发送。</p>
      </template>
    </div>
    <template #footer>
      <span v-if="problem && pf" class="problem">{{ problem }}</span>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :disabled="!!problem" :loading="saving" @click="submit">
        提交{{ amount > 0 && !problem ? ` ${fmtMoney(amount, cur)}` : '' }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rrd {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 120px;
}
.grid {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 10px 12px;
  align-items: start;
  font-size: 13px;
}
.k {
  color: var(--muted);
  text-align: right;
  padding-top: 4px;
}
.lines {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}
.line {
  display: grid;
  grid-template-columns: 120px 1fr 90px 130px;
  gap: 6px;
  align-items: center;
}
.ttl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mono {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 12px;
}
.mode {
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 13px;
  background: var(--sunken);
}
.mode.direct {
  background: var(--warn-soft, #fff4e5);
}
.small {
  font-size: 12px;
}
.problem {
  margin-right: 10px;
  font-size: 12px;
  color: var(--warn-text, #b45309);
}
</style>
