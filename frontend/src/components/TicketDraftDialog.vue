<script setup lang="ts">
// 本系统的查件单草稿：改查件类型（按这单物流方式支持的）、查件需求，保存或提交到谷仓；建错的废弃。
// 提交才会在谷仓真正建单，之后每天同步谷仓状态和回复。
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post } from '../api'
import { errorText } from '../format'
import type { LogisticsTicketItem } from '../types'

const props = defineProps<{ ticket: LogisticsTicketItem | null }>()
const emit = defineEmits<{ close: []; changed: [t: LogisticsTicketItem] }>()

const types = ref<{ value: number; name: string }[]>([])
const typeId = ref<number | null>(null)
const desc = ref('')
const busy = ref(false)
const typeError = ref('')
// 提交时同时给买家发查件安抚消息（已向承运商发起查询，有结果马上通知）；点提交即审核通过
const notifyBuyer = ref(true)

watch(
  () => props.ticket,
  async (t) => {
    if (!t) return
    typeId.value = t.it_type_id
    desc.value = t.io_desc || ''
    types.value = []
    typeError.value = ''
    notifyBuyer.value = true
    try {
      types.value = await get<{ value: number; name: string }[]>(`/internal/logistics-tickets/${t.id}/types`)
    } catch (e) {
      typeError.value = errorText(e)
    }
  },
  { immediate: true },
)

const body = computed(() => ({
  it_type_id: typeId.value,
  type_name: types.value.find((x) => x.value === typeId.value)?.name ?? props.ticket?.type ?? null,
  io_desc: desc.value,
}))
const valid = computed(() => !!typeId.value && desc.value.trim().length >= 5 && desc.value.trim().length <= 300)

async function run(kind: 'save' | 'submit' | 'discard') {
  const t = props.ticket
  if (!t) return
  if (kind !== 'save') {
    try {
      await ElMessageBox.confirm(
        kind === 'submit'
          ? `在谷仓正式建一张“${body.value.type_name}”查件单（订单 ${t.order_id}，跟踪号 ${t.tracking_no}），交谷仓客服处理。确定提交？`
          : '废弃这张草稿？不会发到谷仓。',
        kind === 'submit' ? '提交到谷仓' : '废弃草稿',
        { type: 'warning', confirmButtonText: kind === 'submit' ? '提交' : '废弃' },
      )
    } catch {
      return
    }
  }
  busy.value = true
  try {
    const r =
      kind === 'save'
        ? await api<LogisticsTicketItem>('PATCH', `/internal/logistics-tickets/${t.id}`, body.value)
        : kind === 'submit'
          ? await post<LogisticsTicketItem & { notice?: string }>(`/internal/logistics-tickets/${t.id}/submit`, {
              ...body.value,
              notify_buyer: notifyBuyer.value,
            })
          : await post<LogisticsTicketItem>(`/internal/logistics-tickets/${t.id}/discard`, {})
    if (kind === 'submit') {
      const notice = (r as { notice?: string }).notice
      if (notice === 'SENT') ElMessage.success(`已提交，谷仓查件单号 ${r.io_code}；已给买家发查件安抚消息`)
      else if (notifyBuyer.value) ElMessage.warning(`已提交，谷仓查件单号 ${r.io_code}；没给买家发消息：${notice || '未知原因'}`)
      else ElMessage.success(`已提交，谷仓查件单号 ${r.io_code}（未通知买家）`)
    } else ElMessage.success(kind === 'save' ? '已保存' : '已废弃')
    emit('changed', r)
    if (kind !== 'save') emit('close')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog :model-value="!!ticket" title="查件单草稿" width="560px" @close="emit('close')">
    <template v-if="ticket">
      <el-descriptions :column="2" size="small" border>
        <el-descriptions-item label="订单号">{{ ticket.order_id }}</el-descriptions-item>
        <el-descriptions-item label="跟踪号">{{ ticket.tracking_no }}</el-descriptions-item>
        <el-descriptions-item label="物流方式">{{ ticket.sm_code || '—' }}</el-descriptions-item>
        <el-descriptions-item label="起草">{{ ticket.created_by || '—' }}</el-descriptions-item>
      </el-descriptions>
      <el-form label-width="80px" style="margin-top: 12px">
        <el-form-item label="查件类型">
          <el-select v-model="typeId" placeholder="选择查件类型" style="width: 100%">
            <el-option v-for="x in types" :key="x.value" :label="x.name" :value="x.value" />
            <el-option v-if="!types.length && ticket.it_type_id" :label="ticket.type || ''" :value="ticket.it_type_id" />
          </el-select>
          <div v-if="typeError" class="warn">查件类型读取失败：{{ typeError }}</div>
          <div v-else class="muted">只列出这单物流方式支持的类型</div>
        </el-form-item>
        <el-form-item label="查件需求">
          <el-input v-model="desc" type="textarea" :rows="5" maxlength="300" show-word-limit />
          <div class="muted">写给谷仓客服看的（中文），5–300 字</div>
        </el-form-item>
        <el-form-item label="通知买家">
          <el-checkbox v-model="notifyBuyer">提交后给买家发查件安抚消息</el-checkbox>
          <div class="muted">
            按买家语言发：“已把您的问题转给物流同事，已就包裹向承运商发起查询，一收到回复马上通知您。”点提交即视为审核通过；
            谷仓回复后，查件结果在“查件单”页审核后发给买家
          </div>
        </el-form-item>
      </el-form>
    </template>
    <template #footer>
      <el-button type="danger" plain :loading="busy" @click="run('discard')">废弃</el-button>
      <el-button :loading="busy" :disabled="!valid" @click="run('save')">保存修改</el-button>
      <el-button type="primary" :loading="busy" :disabled="!valid" @click="run('submit')">提交到谷仓</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.warn {
  color: var(--warn-text);
}
</style>
