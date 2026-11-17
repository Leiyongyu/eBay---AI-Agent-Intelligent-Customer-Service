<script setup lang="ts">
// 退款单 → 退款申请：待审核 / 退款中 / 已退款 / 审核不通过 / 处理异常。审核通过 = 在 eBay 上退款（只有管理员和售后），
// 通过前再确认一次金额；处理异常（超时、eBay 报错）要到 eBay 后台核对后标“已退款”或“没退，退回待审核”
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText } from '../format'
import { fmtFull } from '../caseCommon'
import { STATUS_TYPE, fmtMoney } from '../refundRequest'
import type { RefundRequestRow } from '../refundRequest'

const props = defineProps<{ stores?: { id: string; name: string }[]; focusId?: number | null }>()
const emit = defineEmits<{ open: [conversationId: number]; changed: [] }>()

const TABS = [
  { value: '', label: '全部' }, { value: 'PENDING', label: '待审核' }, { value: 'REFUNDING', label: '退款中' },
  { value: 'REFUNDED', label: '已退款' }, { value: 'REJECTED', label: '审核不通过' }, { value: 'ERROR', label: '处理异常' },
]
const SOURCES = [{ value: 'MANUAL', label: '主动退款' }, { value: 'AFTERSALES', label: '纠纷退货' }, { value: 'CANCEL', label: '取消交易' }]
const canReview = computed(() => ['ADMIN', 'AFTERSALES'].includes(session.user?.role || ''))

const tab = ref('PENDING')
const f = reactive({ q: '', storeId: '', source: '' })
const rows = ref<RefundRequestRow[]>([])
const counts = ref<Record<string, number>>({})
const loading = ref(false)
const busy = ref<number | null>(null)

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    if (tab.value) q.set('status', tab.value)
    if (f.q.trim()) q.set('q', f.q.trim())
    if (f.storeId) q.set('store_id', f.storeId)
    if (f.source) q.set('source', f.source)
    const d = await get<{ items: RefundRequestRow[]; counts: Record<string, number> }>(`/internal/refund-requests?${q}`)
    rows.value = d.items
    counts.value = d.counts
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
watch(tab, load)
const count = (k: string) => (k ? counts.value[k] : counts.value.all) ?? 0

async function run(r: RefundRequestRow, fn: () => Promise<RefundRequestRow>, ok: (x: RefundRequestRow) => string) {
  busy.value = r.id
  try {
    const x = await fn()
    if (x.status === 'ERROR' && x.error) ElMessage.error(`退款申请 #${x.id}：${x.error}`)
    else ElMessage.success(ok(x))
    emit('changed')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
    await load()
  } finally {
    busy.value = null
  }
}

async function approve(r: RefundRequestRow) {
  try {
    await ElMessageBox.confirm(
      `在 eBay 上给买家 ${r.buyer || ''} ${r.full_label} ${fmtMoney(r.amount, r.currency)}（订单 ${r.order_id}）。\n` +
      `原因：${r.reason_label}${r.comment ? `\n给买家的说明：${r.comment}` : ''}\n退款后不能撤回。`,
      '审核通过并退款', { type: 'warning', confirmButtonText: `确认退款 ${fmtMoney(r.amount, r.currency)}` })
  } catch {
    return
  }
  // 确认的金额原样带回，后端和申请单核对
  run(r, () => post<RefundRequestRow>(`/internal/refund-requests/${r.id}/approve`,
    { confirm_amount: r.amount, confirm_currency: r.currency }), (x) => `#${x.id} ${x.status_label}${x.ebay_refund_id ? `，eBay 退款 ID ${x.ebay_refund_id}` : ''}`)
}
async function reject(r: RefundRequestRow) {
  let note = ''
  try {
    note = (await ElMessageBox.prompt('不通过的原因（提交人能看到）', `退款申请 #${r.id} 审核不通过`,
      { inputPattern: /\S/, inputErrorMessage: '请填写原因', inputType: 'textarea' })).value
  } catch {
    return
  }
  run(r, () => post<RefundRequestRow>(`/internal/refund-requests/${r.id}/reject`, { note }), (x) => `#${x.id} 已标为审核不通过`)
}
function refresh(r: RefundRequestRow) {
  run(r, () => post<RefundRequestRow>(`/internal/refund-requests/${r.id}/refresh`), (x) => `#${x.id}：${x.status_label}`)
}

// 处理异常：核对结果
const resolving = ref<RefundRequestRow | null>(null)
const rs = reactive({ outcome: 'refunded' as 'refunded' | 'not_refunded', refundId: '', note: '' })
function openResolve(r: RefundRequestRow) {
  Object.assign(rs, { outcome: 'refunded', refundId: '', note: '' })
  resolving.value = r
}
function doResolve() {
  const r = resolving.value
  if (!r) return
  resolving.value = null
  run(r, () => post<RefundRequestRow>(`/internal/refund-requests/${r.id}/resolve`,
    { outcome: rs.outcome, refund_id: rs.refundId.trim() || null, note: rs.note.trim() || null }),
  (x) => `#${x.id}：${x.status_label}`)
}

// 从链接定位到某一张：切到它所在的状态页签、高亮并滚到这一行
watch(() => props.focusId, async (id) => {
  if (!id) return
  try {
    const r = await get<RefundRequestRow>(`/internal/refund-requests/${id}`)
    if (tab.value !== r.status) tab.value = r.status
    else await load()
    await nextTick()
    document.querySelector('.rrp .row-focus')?.scrollIntoView({ block: 'center' })
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}, { immediate: true })
const rowClass = ({ row }: { row: RefundRequestRow }) =>
  [row.status === 'ERROR' ? 'row-error' : '', row.id === props.focusId ? 'row-focus' : ''].join(' ')

onMounted(() => { if (!props.focusId) load() })
defineExpose({ load })
</script>

<template>
  <div class="rrp">
    <div class="bar">
      <div class="seg">
        <button v-for="t in TABS" :key="t.value" :class="{ on: tab === t.value, warn: t.value === 'ERROR' && count('ERROR') }" @click="tab = t.value">
          {{ t.label }}<span class="n">({{ count(t.value) }})</span>
        </button>
      </div>
      <el-input v-model="f.q" clearable placeholder="订单号 / 买家 / 纠纷编号 / 提交人" style="width: 220px" @keyup.enter="load" @clear="load" />
      <el-select v-model="f.storeId" clearable filterable placeholder="eBay账号" style="width: 150px" @change="load">
        <el-option v-for="s in props.stores || []" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-select v-model="f.source" clearable placeholder="来源" style="width: 110px" @change="load">
        <el-option v-for="s in SOURCES" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-button plain type="primary" @click="load">搜索</el-button>
      <span class="spacer" />
      <span v-if="!canReview" class="muted small">审核只有管理员和售后可以做</span>
    </div>
    <el-table v-loading="loading" :data="rows" row-key="id" size="small" empty-text="没有退款申请" class="tbl"
              :row-class-name="rowClass">
      <el-table-column label="申请单" width="140">
        <template #default="{ row }">
          <div><b>#{{ row.id }}</b></div>
          <div class="muted">{{ row.submitted_by }}</div>
          <div class="muted small">{{ fmtFull(row.submitted_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="订单 / 客户" min-width="170">
        <template #default="{ row }">
          <div class="mono">{{ row.order_id }}</div>
          <div class="muted">{{ row.buyer || '—' }} · {{ row.store_name }}</div>
          <el-button v-if="row.conversation_id" link size="small" type="primary" @click="emit('open', row.conversation_id)">查看消息</el-button>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="120">
        <template #default="{ row }">
          <div>{{ row.source_label }}</div>
          <div v-if="row.case_external_id" class="muted small">{{ row.case_kind }} {{ row.case_external_id }}</div>
          <div class="muted small" :title="row.execution_label">{{ row.execution === 'post_order_return' ? `通过退货 ${row.return_id} 退` : '按订单退' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="退款金额" width="140">
        <template #default="{ row }">
          <div class="amt">{{ fmtMoney(row.amount, row.currency) }}</div>
          <div class="muted">{{ row.full_label }}<template v-if="row.order_total != null"> · 订单 {{ fmtMoney(row.order_total, row.order_currency) }}</template></div>
          <div v-for="l in row.lines" :key="l.line_item_id" class="muted small">{{ l.sku || l.line_item_id }}：{{ fmtMoney(l.amount, row.currency) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="退款原因 / 说明" min-width="170">
        <template #default="{ row }">
          <div>{{ row.reason_label }}</div>
          <div v-if="row.comment" class="muted small clamp" :title="row.comment">给买家：{{ row.comment }}</div>
          <div v-if="row.note" class="muted small clamp" :title="row.note">备注：{{ row.note }}</div>
        </template>
      </el-table-column>
      <el-table-column label="审核" width="150">
        <template #default="{ row }">
          <div class="muted small">{{ row.review_mode === 'direct' ? '规则：直接退款' : '规则：先审核' }}</div>
          <template v-if="row.reviewed_by">
            <div>{{ row.reviewed_by }}</div>
            <div class="muted small">{{ fmtFull(row.reviewed_at) }}</div>
          </template>
          <div v-if="row.review_note" class="small clamp" :title="row.review_note">{{ row.review_note }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="170">
        <template #default="{ row }">
          <el-tag size="small" :type="STATUS_TYPE[row.status]" effect="plain">{{ row.status_label }}</el-tag>
          <div v-if="row.ebay_refund_id" class="muted small">eBay 退款 ID {{ row.ebay_refund_id }}</div>
          <div v-if="row.error" class="err small">{{ row.error }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <div class="ops">
            <template v-if="row.status === 'PENDING' && canReview">
              <el-button size="small" type="primary" :loading="busy === row.id" @click="approve(row)">审核通过并退款</el-button>
              <el-button size="small" :disabled="busy === row.id" @click="reject(row)">不通过</el-button>
            </template>
            <el-button v-if="row.status === 'ERROR' && canReview" size="small" type="warning" @click="openResolve(row)">核对结果</el-button>
            <el-button v-if="row.status === 'REFUNDING'" size="small" :loading="busy === row.id" @click="refresh(row)">查 eBay 状态</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :model-value="!!resolving" title="核对处理结果" width="min(460px, calc(100vw - 24px))" append-to-body
               @update:model-value="resolving = null">
      <template v-if="resolving">
        <p class="muted">请到 eBay 卖家后台看订单 {{ resolving.order_id }} 有没有退款 {{ fmtMoney(resolving.amount, resolving.currency) }}。</p>
        <el-radio-group v-model="rs.outcome" class="col">
          <el-radio value="refunded">eBay 上已经退了</el-radio>
          <el-radio value="not_refunded">eBay 上没有退，退回待审核</el-radio>
        </el-radio-group>
        <el-input v-if="rs.outcome === 'refunded'" v-model="rs.refundId" placeholder="eBay 退款 ID（可空）" class="mt" />
        <el-input v-model="rs.note" type="textarea" :rows="2" placeholder="核对说明（可空）" class="mt" />
      </template>
      <template #footer>
        <el-button @click="resolving = null">取消</el-button>
        <el-button type="primary" @click="doResolve">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rrp {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.spacer {
  flex: 1;
}
.seg {
  display: inline-flex;
  flex-wrap: wrap;
  border: 1px solid var(--line);
  border-radius: 3px;
  overflow: hidden;
}
.seg button {
  height: 30px;
  padding: 0 12px;
  border: 0;
  background: var(--panel);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
}
.seg button + button {
  border-left: 1px solid var(--line);
}
.seg button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.seg button.warn .n {
  color: var(--danger-text, #e5484d);
  font-weight: 600;
}
.n {
  margin-left: 2px;
  font-size: 12px;
  color: var(--muted);
}
.tbl :deep(td.el-table__cell) {
  vertical-align: top;
}
/* 处理异常（超时、eBay 报错、超过 3 天对不上退款流水）：整行高亮，要人到 eBay 后台核对 */
.tbl :deep(.row-focus td.el-table__cell) {
  box-shadow: inset 0 2px 0 var(--accent), inset 0 -2px 0 var(--accent);
}
.tbl :deep(.row-error td.el-table__cell) {
  background: var(--danger-soft, #fdecec) !important;
}
.mono {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 12px;
}
.amt {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.err {
  color: var(--danger-text, #e5484d);
  white-space: pre-wrap;
}
.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ops {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}
.ops .el-button + .el-button {
  margin-left: 0;
}
.col {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.mt {
  margin-top: 8px;
}
</style>
