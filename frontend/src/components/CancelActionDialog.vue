<script setup lang="ts">
// 取消交易（真实写到 eBay）：
//   mode=respond：买家的取消申请 → 同意（eBay 自动全额退款，只有管理员 / 售后）/ 拒绝（原因必填，只记在本系统；已发货的带发货日期、运单号）
//   mode=create ：卖家主动取消订单（从会话侧栏订单发起，只有管理员 / 售后；原因：缺货 / 买家要求 / 地址问题）
// 提交前二次确认订单号；成功后会话里记系统事件，给买家的通知请在会话里用 AI 起草后人工审核发送
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText } from '../format'

interface CancelCase { id: number; order_id: string | null; buyer: string | null; reason?: string | null; reason_label?: string | null;
  status_label?: string | null; is_open?: boolean; store_name?: string }

const props = defineProps<{ modelValue: boolean; mode: 'respond' | 'create'; case?: CancelCase | null; storeId?: string | null;
  orderId?: string | null; conversationId?: number | null }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean]; done: [r: Record<string, unknown>] }>()

const senior = computed(() => ['ADMIN', 'AFTERSALES'].includes(session.user?.role || ''))
const f = reactive({ action: 'approve' as 'approve' | 'reject', reason: '', shipped: '' as string | null, tracking: '',
  createReason: '', note: '' })
const saving = ref(false)
const elig = ref<{ blocked: string | null; can_create: boolean; reasons: { code: string; label: string }[];
  eligibility: { eligible: boolean | null; reasons: string[] } | null; eligibility_error?: string } | null>(null)
const loading = ref(false)

watch(() => props.modelValue, async (v) => {
  if (!v) return
  Object.assign(f, { action: senior.value ? 'approve' : 'reject', reason: '', shipped: null, tracking: '', createReason: '', note: '' })
  elig.value = null
  if (props.mode === 'create' && props.storeId && props.orderId) {
    loading.value = true
    try {
      elig.value = await get(`/internal/order-cancellations/eligibility?store_id=${encodeURIComponent(props.storeId)}&order_id=${encodeURIComponent(props.orderId)}`)
    } catch (e) {
      ElMessage.error(errorText(e))
    } finally {
      loading.value = false
    }
  }
})

const problem = computed(() => {
  if (props.mode === 'respond') {
    if (!props.case) return '没有取消申请'
    if (props.case.is_open === false) return '这条取消申请在 eBay 上已经处理过或关闭了'
    if (f.action === 'approve' && !senior.value) return '同意取消只有管理员和售后可以做'
    if (f.action === 'reject' && !f.reason.trim()) return '请填写拒绝原因'
    return ''
  }
  if (!props.orderId) return '没有订单'
  if (!elig.value) return loading.value ? '正在查 eBay 能不能取消' : ''
  if (elig.value.blocked) return elig.value.blocked
  if (!elig.value.can_create) return '主动取消订单只有管理员和售后可以做'
  if (elig.value.eligibility?.eligible === false) return `eBay 显示这张订单不能取消：${(elig.value.eligibility.reasons || []).join('、') || '原因未说明'}`
  if (!f.createReason) return '请选择取消原因'
  return ''
})

async function submit() {
  if (problem.value) return
  const oid = props.mode === 'respond' ? props.case!.order_id || '' : props.orderId || ''
  const what = props.mode === 'create' ? `主动取消订单 ${oid}（eBay 会自动退款给买家）`
    : f.action === 'approve' ? `同意买家取消订单 ${oid}（eBay 会自动全额退款给买家）` : `拒绝买家取消订单 ${oid}`
  try {
    await ElMessageBox.confirm(`${what}。\n这是真实操作，做了不能撤回。`, '确认', { type: 'warning', confirmButtonText: '确认' })
  } catch {
    return
  }
  saving.value = true
  try {
    let r: Record<string, unknown>
    if (props.mode === 'respond') {
      const body = f.action === 'approve' ? { confirm_order_id: oid }
        : { reason: f.reason.trim(), shipped_at: f.shipped || null, tracking_number: f.tracking.trim() || null }
      r = await post(`/internal/ebay-cases/${props.case!.id}/cancel/${f.action}`, body)
    } else {
      r = await post('/internal/order-cancellations', { store_id: props.storeId, order_id: oid, conversation_id: props.conversationId || null,
        reason: f.createReason, note: f.note.trim() || null, confirm_order_id: oid })
    }
    ElMessage.success(String(r.done || '已处理'))
    emit('done', r)
    emit('update:modelValue', false)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="mode === 'create' ? '取消订单' : '处理取消申请'" width="min(520px, calc(100vw - 24px))"
             append-to-body @update:model-value="(v: boolean) => emit('update:modelValue', v)">
    <div v-loading="loading" class="cad">
      <template v-if="mode === 'respond' && props.case">
        <div class="grid">
          <div class="k">订单</div><div class="mono">{{ props.case.order_id || '—' }}</div>
          <div class="k">买家</div><div>{{ props.case.buyer || '—' }}<span v-if="props.case.store_name" class="muted"> · {{ props.case.store_name }}</span></div>
          <div class="k">取消原因</div><div>{{ props.case.reason_label || props.case.reason || '—' }}</div>
          <div class="k">处理</div>
          <div>
            <el-radio-group v-model="f.action">
              <el-radio value="approve" :disabled="!senior">同意取消</el-radio>
              <el-radio value="reject">拒绝取消</el-radio>
            </el-radio-group>
            <div v-if="f.action === 'approve'" class="muted small">同意后订单取消，eBay 自动全额退款给买家</div>
          </div>
          <template v-if="f.action === 'reject'">
            <div class="k">拒绝原因</div>
            <div><el-input v-model="f.reason" type="textarea" :rows="2" maxlength="500" placeholder="必填，如 已经发货" />
              <div class="note-line">eBay 不接收拒绝原因，只记在本系统</div></div>
            <div class="k">已发货</div>
            <div class="inline">
              <el-date-picker v-model="f.shipped" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss" placeholder="发货时间（可空）" />
              <el-input v-model="f.tracking" placeholder="运单号（可空）" style="width: 200px" />
              <div class="muted small">已经发货的建议填上，eBay 会显示给买家</div>
            </div>
          </template>
        </div>
      </template>
      <template v-else-if="mode === 'create'">
        <el-alert v-if="elig?.eligibility_error" type="warning" :closable="false" show-icon :title="`查不到 eBay 能不能取消：${elig.eligibility_error}`" />
        <div class="grid">
          <div class="k">订单</div><div class="mono">{{ orderId }}</div>
          <div class="k">取消原因</div>
          <div>
            <el-select v-model="f.createReason" placeholder="选择原因" style="width: 220px">
              <el-option v-for="r in elig?.reasons || []" :key="r.code" :label="r.label" :value="r.code" />
            </el-select>
            <div class="muted small">eBay 只允许这 3 个卖家取消原因；取消后 eBay 自动退款给买家</div>
          </div>
          <div class="k">备注</div>
          <div><el-input v-model="f.note" type="textarea" :rows="2" maxlength="500" placeholder="可空，只在本系统" /></div>
        </div>
      </template>
      <p class="muted small">做完会在会话里记一条系统事件；给买家的通知不会自动发，请在会话里用 AI 起草后人工审核发送。</p>
    </div>
    <template #footer>
      <span v-if="problem" class="problem">{{ problem }}</span>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :disabled="!!problem" :loading="saving" @click="submit">
        {{ mode === 'create' ? '取消订单' : f.action === 'approve' ? '同意取消' : '拒绝取消' }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.cad {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.grid {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 10px 12px;
  align-items: start;
  font-size: 13px;
}
.k {
  color: var(--muted);
  text-align: right;
  padding-top: 4px;
}
.inline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.mono {
  font-family: var(--mono, ui-monospace, monospace);
}
.small {
  font-size: 12px;
}
.note-line {
  font-size: 12px;
  color: var(--warn-text, #b45309);
}
.problem {
  margin-right: 10px;
  font-size: 12px;
  color: var(--warn-text, #b45309);
}
</style>
