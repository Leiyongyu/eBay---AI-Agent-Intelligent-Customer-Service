<script setup lang="ts">
// 异常包裹跟进：新建 / 修改一条记录。从会话打开时由父组件带上预填（店铺、买家、订单、SKU、跟踪号、查件单号、原因）。
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, get, post } from '../api'
import { errorText } from '../format'
import type { Store } from '../types'
import { ACTIONS, CARRIERS, CURRENCIES, REASONS, todayStr } from '../parcelFollowup'
import type { Followup, FollowupDraft } from '../parcelFollowup'

const props = defineProps<{ draft: FollowupDraft | null }>()
const emit = defineEmits<{ close: []; saved: [row: Followup] }>()

const form = ref<FollowupDraft>({})
const stores = ref<Store[]>([])
const busy = ref(false)
const isNew = computed(() => !props.draft?.id)

watch(
  () => props.draft,
  async (d) => {
    if (!d) return
    form.value = { status: 'OPEN', occurred_on: todayStr(), ...d }
    if (!stores.value.length) stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  },
  { immediate: true },
)

const FIELDS = ['occurred_on', 'store_id', 'conversation_id', 'buyer_key', 'order_id', 'sku', 'reason', 'reason_note',
  'carrier', 'tracking_no', 'action', 'result', 'reship_tracking_no', 'amount', 'currency', 'ticket_no',
  'next_follow_on', 'status', 'note'] as const

async function save() {
  const f = form.value
  if (!f.reason) {
    ElMessage.warning('请选择原因类型')
    return
  }
  const body: Record<string, unknown> = {}
  for (const k of FIELDS) body[k] = f[k] === '' ? null : (f[k] ?? null)
  busy.value = true
  try {
    const r = isNew.value
      ? await post<Followup>('/internal/parcel-followups', body)
      : await api<Followup>('PATCH', `/internal/parcel-followups/${props.draft!.id}`, body)
    ElMessage.success(isNew.value ? '已加入异常包裹跟进' : '已保存')
    emit('saved', r)
    emit('close')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog :model-value="!!draft" :title="isNew ? '加入异常包裹跟进' : '修改跟进记录'" width="680px" @close="emit('close')">
    <el-form label-width="92px" size="small" class="pf-form">
      <div class="row">
        <el-form-item label="日期">
          <el-date-picker v-model="form.occurred_on" type="date" value-format="YYYY-MM-DD" :clearable="false" style="width: 100%" />
        </el-form-item>
        <el-form-item label="店铺">
          <el-select v-model="form.store_id" filterable clearable placeholder="选择店铺" style="width: 100%">
            <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
      </div>
      <div class="row">
        <el-form-item label="买家 ID"><el-input v-model="form.buyer_key" maxlength="128" /></el-form-item>
        <el-form-item label="订单号"><el-input v-model="form.order_id" maxlength="64" placeholder="如 03-15358-18451" /></el-form-item>
      </div>
      <div class="row">
        <el-form-item label="SKU"><el-input v-model="form.sku" maxlength="200" placeholder="多个用 + 连" /></el-form-item>
        <el-form-item label="原因类型" required>
          <el-select v-model="form.reason" placeholder="选择原因" style="width: 100%">
            <el-option v-for="r in REASONS" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
      </div>
      <el-form-item label="原因说明">
        <el-input v-model="form.reason_note" maxlength="300" placeholder="如：4.30 断更；6.22 妥投未收到，买家开了 INR" />
      </el-form-item>
      <div class="row">
        <el-form-item label="物流商">
          <el-select v-model="form.carrier" filterable allow-create clearable placeholder="选择或输入" style="width: 100%">
            <el-option v-for="c in CARRIERS" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="跟踪号"><el-input v-model="form.tracking_no" maxlength="64" /></el-form-item>
      </div>
      <div class="row">
        <el-form-item label="处理方式">
          <el-select v-model="form.action" clearable placeholder="重发 / 退款 …" style="width: 100%">
            <el-option v-for="a in ACTIONS" :key="a" :label="a" :value="a" />
          </el-select>
        </el-form-item>
        <el-form-item label="重发跟踪号"><el-input v-model="form.reship_tracking_no" maxlength="64" /></el-form-item>
      </div>
      <el-form-item label="处理结果">
        <el-input v-model="form.result" maxlength="500" placeholder="如：已送达；已退款；未收到货纠纷；无库存不能重发" />
      </el-form-item>
      <div class="row">
        <el-form-item label="金额">
          <div class="money">
            <el-input-number v-model="form.amount" :min="0" :precision="2" :controls="false" placeholder="0.00" />
            <el-select v-model="form.currency" clearable placeholder="币种" style="width: 90px">
              <el-option v-for="c in CURRENCIES" :key="c" :label="c" :value="c" />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="查件单号"><el-input v-model="form.ticket_no" maxlength="200" placeholder="CJ…，多个用逗号" /></el-form-item>
      </div>
      <div class="row">
        <el-form-item label="下次跟进">
          <el-date-picker v-model="form.next_follow_on" type="date" value-format="YYYY-MM-DD" clearable style="width: 100%" />
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="form.status">
            <el-radio-button value="OPEN">跟进中</el-radio-button>
            <el-radio-button value="DONE">已完结</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </div>
      <el-form-item label="备注"><el-input v-model="form.note" type="textarea" :rows="3" maxlength="5000" /></el-form-item>
      <div v-if="form.conversation_id" class="muted">已关联会话 #{{ form.conversation_id }}，在列表里点“打开会话”可直接跳过去</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="busy" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pf-form .row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 12px;
}
.money {
  display: flex;
  gap: 6px;
  width: 100%;
}
.money :deep(.el-input-number) {
  flex: 1;
}
@media (max-width: 640px) {
  .pf-form .row {
    grid-template-columns: 1fr;
  }
}
</style>
