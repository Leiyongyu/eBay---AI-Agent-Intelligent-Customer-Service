<script setup lang="ts">
// 退货退款：全额 / 部分按 eBay 给的退款明细（每一项的估算和允许范围）和给卖家的下一步决定；
// 只允许“提出部分退款”时走 eBay 的部分退款提议（买家同意后才退）
import { computed, reactive, watch } from 'vue'
import { fmtFull, money } from '../caseCommon'
import type { Amt, OrderInfo } from '../caseDialog'

interface Line { fee_type: string; label: string; estimated: Amt; min: Amt; max: Amt }
const props = defineProps<{ live: any; order?: OrderInfo | null; blocked: string | null; receivedBlocked?: string | null
  busy: boolean; orderId?: string | null; sku?: string | null }>()
const emit = defineEmits<{ submit: [payload: Record<string, any>]; received: [comment: string] }>()

const opts = computed<string[]>(() => props.live?.options || [])
const canIssue = computed(() => opts.value.includes('SELLER_ISSUE_REFUND'))
const canOffer = computed(() => opts.value.includes('SELLER_OFFER_PARTIAL_REFUND'))
const lines = computed<Line[]>(() => props.live?.refund_lines || [])
const total = computed<Amt | null>(() => props.live?.estimated_refund || null)
const cur = computed(() => total.value?.currency || '')
const purchase = computed(() => lines.value.find((l) => l.fee_type === 'PURCHASE_PRICE'))
const others = computed(() => lines.value.filter((l) => l.fee_type !== 'PURCHASE_PRICE').reduce((s, l) => s + l.estimated.value, 0))
// 部分退款能填的范围：退款单开放时按商品价格这一项的 [min, max]；只能提议时 (0, 全额)
const range = computed(() => {
  if (canIssue.value && purchase.value && props.live?.partial_refund_allowed)
    return { lo: +(purchase.value.min.value + others.value).toFixed(2), hi: +((total.value?.value || 0) - 0.01).toFixed(2) }
  if (canOffer.value && total.value) return { lo: 0.01, hi: +(total.value.value - 0.01).toFixed(2) }
  return null
})
const fullOk = computed(() => canIssue.value && !!total.value)
const form = reactive({ mode: 'full', amount: 0 as number, comment: '', keep: true, receivedNote: '' })
watch(() => props.live, () => {
  form.mode = fullOk.value ? 'full' : range.value ? 'partial' : 'full'
  form.amount = form.mode === 'full' ? total.value?.value || 0 : range.value?.lo || 0
  form.comment = ''
}, { immediate: true })
const amount = computed(() => (form.mode === 'full' ? total.value?.value || 0 : form.amount))
const isOffer = computed(() => form.mode === 'partial' && !canIssue.value && canOffer.value)
const stop = computed(() => {
  if (props.blocked) return props.blocked
  if (props.order?.payment_status === 'FULLY_REFUNDED') return 'eBay 显示这笔订单已经全额退款'
  if (props.live?.refunded?.value > 0) return `eBay 显示这条退货已经退过款（${money(props.live.refunded.value, props.live.refunded.currency)}）`
  if (form.mode === 'full' && !fullOk.value) return 'eBay 现在不允许全额退款'
  if (form.mode === 'partial' && !range.value) return 'eBay 给的范围不能部分退款'
  if (form.mode === 'partial' && range.value && (form.amount < range.value.lo || form.amount > range.value.hi))
    return `部分退款金额要在 ${cur.value} ${range.value.lo.toFixed(2)}–${range.value.hi.toFixed(2)} 之间`
  return null
})
function submit() {
  emit('submit', { mode: form.mode, amount: +amount.value.toFixed(2), currency: cur.value || null, confirm: true,
                   comment: form.comment, keep_item: form.keep, _offer: isOffer.value })
}
</script>

<template>
  <el-form label-width="90px" class="plan-form">
    <el-form-item label="订单编号">{{ orderId || '—' }}<span v-if="sku" class="muted small">　SKU {{ sku }}</span></el-form-item>
    <el-form-item label="付款状态">
      <span :class="order?.payment_status && order.payment_status !== 'PAID' ? 'red' : ''">{{ order?.payment_status_label || '—' }}</span>
      <span class="muted small">（eBay 订单实时状态）</span>
    </el-form-item>
    <el-form-item v-if="order?.refunds?.length" label="已退款">
      <div><div v-for="(r, i) in order.refunds" :key="i">{{ money(r.amount?.value, r.amount?.currency) }} · {{ fmtFull(r.at) }} · {{ r.status || '' }}</div></div>
    </el-form-item>
    <el-form-item label="eBay 估算">
      <table v-if="lines.length" class="lines">
        <thead><tr><th>项目</th><th>估算</th><th>允许范围</th></tr></thead>
        <tbody>
          <tr v-for="l in lines" :key="l.fee_type"><td>{{ l.label }}</td><td>{{ money(l.estimated.value, l.estimated.currency) }}</td>
            <td class="muted">{{ l.min.value.toFixed(2) }}–{{ l.max.value.toFixed(2) }}</td></tr>
          <tr><td><b>合计</b></td><td><b>{{ money(total?.value, total?.currency) }}</b></td><td /></tr>
        </tbody>
      </table>
      <span v-else class="muted">eBay 没给退款明细{{ total ? `（估算 ${money(total.value, total.currency)}）` : '' }}</span>
    </el-form-item>
    <el-form-item label="退款类型" required>
      <el-radio-group v-model="form.mode" @change="form.amount = form.mode === 'full' ? total?.value || 0 : range?.lo || 0">
        <el-radio value="full" :disabled="!fullOk">全额退款</el-radio>
        <el-radio value="partial" :disabled="!range">部分退款</el-radio>
      </el-radio-group>
      <span class="muted small">（按 eBay 现在允许的：{{ (live?.option_labels || []).join('、') || '没有' }}）</span>
    </el-form-item>
    <el-form-item label="退款金额" required>
      <el-input-number v-if="form.mode === 'partial'" v-model="form.amount" :min="range?.lo || 0" :max="range?.hi || 0" :precision="2" :step="1" />
      <el-input v-else :model-value="(total?.value || 0).toFixed(2)" disabled style="width: 160px"><template #prefix>{{ cur }}</template></el-input>
      <span v-if="form.mode === 'partial' && range" class="muted small">　{{ cur }} {{ range.lo.toFixed(2) }}–{{ range.hi.toFixed(2) }}</span>
    </el-form-item>
    <el-form-item v-if="isOffer" label="买家留商品">
      <el-checkbox v-model="form.keep">买家不用退回商品</el-checkbox>
      <span class="muted small">　（这是给买家的部分退款提议，买家同意后才退）</span>
    </el-form-item>
    <el-form-item label="备注"><el-input v-model="form.comment" type="textarea" :rows="3" maxlength="1000" placeholder="给买家的退款说明（可不填）" /></el-form-item>
    <div v-if="stop && !blocked" class="warn-line">{{ stop }}</div>
    <div class="muted small tip">提交的是退款申请单：按“客服设置 → 退款审核”的规则先审核或直接执行；执行时会再按 eBay 现在允许的范围核对金额。</div>
    <el-form-item>
      <el-tooltip :disabled="!stop" :content="stop || ''">
        <span><el-button type="success" :loading="busy" :disabled="!!stop" @click="submit">提交退款申请</el-button></span>
      </el-tooltip>
      <template v-if="receivedBlocked !== undefined">
        <el-tooltip :disabled="!receivedBlocked" :content="receivedBlocked || ''">
          <span><el-button :loading="busy" :disabled="!!receivedBlocked" @click="emit('received', form.comment)">确认收到退货</el-button></span>
        </el-tooltip>
      </template>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.plan-form { max-width: 720px; padding-top: 6px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.red { color: var(--el-color-danger); }
.lines { border-collapse: collapse; min-width: 360px; }
.lines th, .lines td { border: 1px solid var(--el-border-color-lighter); padding: 4px 8px; text-align: left; }
.lines th { background: var(--el-fill-color-lighter); font-weight: normal; }
.tip { margin: 0 0 8px 90px; }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0 8px 90px; }
</style>
