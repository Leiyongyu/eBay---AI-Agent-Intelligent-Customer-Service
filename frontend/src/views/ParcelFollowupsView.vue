<script setup lang="ts">
// 异常包裹跟进表（替代 Excel《异常包裹跟进表》）：断更、无轨迹、妥投未收到、退回、损坏、发错等包裹的处理记录。
// 每人只看自己记的，管理员看全部（可按负责人筛选）；关联了会话的点“打开会话”直接跳过去。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get } from '../api'
import { errorText, fmtTime } from '../format'
import { downloadCsv } from '../caseCommon'
import type { Store } from '../types'
import ParcelFollowupDialog from '../components/ParcelFollowupDialog.vue'
import { REASONS, REASON_TYPE, todayStr } from '../parcelFollowup'
import type { Followup, FollowupDraft } from '../parcelFollowup'

interface ListResp {
  items: Followup[]
  counts: { OPEN: number; DONE: number }
  due: number
  is_admin: boolean
  owners?: { id: number; name: string }[]
}

const emit = defineEmits<{ open: [convId: number] }>()
const status = ref<'OPEN' | 'DONE' | ''>('OPEN')
const reason = ref('')
const storeId = ref('')
const ownerId = ref<number | ''>('')
const origin = ref<'' | 'AUTO' | 'MANUAL'>('')
const keyword = ref('')
const onlyDue = ref(false)
const stores = ref<Store[]>([])
const data = ref<ListResp | null>(null)
const loading = ref(false)
const editing = ref<FollowupDraft | null>(null)

const today = todayStr()
const rows = computed(() => (data.value?.items || []).filter((r) => !onlyDue.value || (r.next_follow_on && r.next_follow_on <= today)))
const total = computed(() => (data.value?.counts.OPEN || 0) + (data.value?.counts.DONE || 0))

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ status: status.value })
    if (reason.value) q.set('reason', reason.value)
    if (storeId.value) q.set('store_id', storeId.value)
    if (ownerId.value) q.set('owner_id', String(ownerId.value))
    if (origin.value) q.set('origin', origin.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    data.value = await get<ListResp>(`/internal/parcel-followups?${q}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

async function setStatus(r: Followup, s: 'OPEN' | 'DONE') {
  try {
    await api('PATCH', `/internal/parcel-followups/${r.id}`, { status: s })
    ElMessage.success(s === 'DONE' ? '已完结' : '已重新打开')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function remove(r: Followup) {
  try {
    await ElMessageBox.confirm(`删除这条跟进记录（${r.buyer_key || ''} ${r.order_id || ''}）？删除后不能恢复。`, '删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api('DELETE', `/internal/parcel-followups/${r.id}`)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const money = (r: Followup) => (r.amount != null ? `${r.currency || ''} ${r.amount.toFixed(2)}`.trim() : '')

function exportCsv() {
  const head = ['日期', '账号', '买家ID', '订单号', 'SKU', '原因', '原因说明', '物流商', '跟踪号', '处理方式', '处理结果', '重发跟踪号',
    '金额', '查件单号', '下一步', '下次跟进', '状态', '来源', ...(data.value?.is_admin ? ['负责人'] : []), '备注']
  downloadCsv(`异常包裹跟进_${today}.csv`, head, rows.value.map((r) => [
    r.occurred_on, r.store_name || r.store_id, r.buyer_key, r.order_id, r.sku, r.reason, r.reason_note, r.carrier, r.tracking_no,
    r.action, r.result, r.reship_tracking_no, money(r), r.ticket_no, r.next_step, r.next_follow_on,
    r.status === 'DONE' ? '已完结' : '跟进中', r.origin === 'AUTO' ? '自动' : '人工',
    ...(data.value?.is_admin ? [r.owner_name] : []), r.note,
  ]))
}

let searchTimer: number | undefined
watch([status, reason, storeId, ownerId, origin], load)
watch(keyword, () => {
  clearTimeout(searchTimer)
  searchTimer = window.setTimeout(load, 300)
})
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  await load()
})
onUnmounted(() => clearTimeout(searchTimer))
</script>

<template>
  <div class="pf-view panel">
    <div class="bar">
      <el-radio-group v-model="status" size="small">
        <el-radio-button value="OPEN">跟进中<span class="cnt">{{ data?.counts.OPEN || 0 }}</span></el-radio-button>
        <el-radio-button value="DONE">已完结<span class="cnt">{{ data?.counts.DONE || 0 }}</span></el-radio-button>
        <el-radio-button value="">全部<span class="cnt">{{ total }}</span></el-radio-button>
      </el-radio-group>
      <el-checkbox v-model="onlyDue" size="small">今天该跟进<span v-if="data?.due" class="due-cnt">{{ data.due }}</span></el-checkbox>
      <el-select v-model="reason" placeholder="全部原因" clearable size="small" style="width: 130px">
        <el-option v-for="r in REASONS" :key="r" :label="r" :value="r" />
      </el-select>
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 180px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-select v-model="origin" placeholder="自动 + 人工" clearable size="small" style="width: 110px">
        <el-option label="系统自动建" value="AUTO" />
        <el-option label="人工建" value="MANUAL" />
      </el-select>
      <el-select v-if="data?.is_admin" v-model="ownerId" placeholder="全部负责人" clearable size="small" style="width: 130px">
        <el-option v-for="o in data?.owners || []" :key="o.id" :label="o.name" :value="o.id" />
      </el-select>
      <el-input v-model="keyword" size="small" clearable placeholder="买家 / 订单号 / 跟踪号 / SKU / 查件单号" style="width: 260px" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <span class="spacer" />
      <el-button size="small" :disabled="!rows.length" @click="exportCsv">导出</el-button>
      <el-button size="small" type="primary" @click="editing = { status: 'OPEN', occurred_on: today }">新建记录</el-button>
    </div>
    <div class="muted hint">
      {{ data?.is_admin ? '管理员：看全部同事的记录' : '只显示你负责的记录' }}。断更、无轨迹、妥投未收到、退回 / 损坏、提交查件单、买家开 INR 或损坏 / 发错退货时系统会自动登记（归正在处理这个会话的人，没人处理归店铺的客服），
      并按进度写好“下一步”；已退款、纠纷关闭的标“可以完结”，由你确认。也可以在会话右侧点“异常跟进”手动加。
    </div>
    <el-table :data="rows" v-loading="loading" size="small" row-key="id" class="tbl"
              :empty-text="status === 'OPEN' ? '没有跟进中的异常包裹' : '没有记录'">
      <el-table-column label="日期" width="96">
        <template #default="{ row }">
          <div>{{ row.occurred_on }}</div>
          <el-tag v-if="row.origin === 'AUTO'" size="small" type="info" effect="plain" class="auto">自动</el-tag>
          <div v-if="data?.is_admin" class="muted">{{ row.owner_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="账号 / 买家" min-width="150">
        <template #default="{ row }">
          <div>{{ row.store_name || row.store_id || '—' }}</div>
          <div class="muted">{{ row.buyer_key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="订单号 / SKU" min-width="150">
        <template #default="{ row }">
          <div class="mono">{{ row.order_id || '—' }}</div>
          <div class="muted mono">{{ row.sku }}</div>
        </template>
      </el-table-column>
      <el-table-column label="原因" min-width="150">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="REASON_TYPE[row.reason] || 'info'">{{ row.reason }}</el-tag>
          <div v-if="row.reason_note" class="muted">{{ row.reason_note }}</div>
        </template>
      </el-table-column>
      <el-table-column label="物流商 / 跟踪号" min-width="190">
        <template #default="{ row }">
          <div>{{ row.carrier }}</div>
          <div class="mono">{{ row.tracking_no }}</div>
        </template>
      </el-table-column>
      <el-table-column label="处理 / 结果" min-width="200">
        <template #default="{ row }">
          <div><b v-if="row.action">{{ row.action }}</b><span v-if="money(row)" class="muted"> · {{ money(row) }}</span></div>
          <div v-if="row.result">{{ row.result }}</div>
          <div v-if="row.reship_tracking_no" class="muted">重发：<span class="mono">{{ row.reship_tracking_no }}</span></div>
        </template>
      </el-table-column>
      <el-table-column label="查件单号" min-width="130">
        <template #default="{ row }"><span class="mono">{{ row.ticket_no }}</span></template>
      </el-table-column>
      <el-table-column label="下一步" min-width="210">
        <template #default="{ row }">
          <div v-if="row.status === 'OPEN' && row.next_step" :class="{ closeable: row.can_close }">{{ row.next_step }}</div>
          <div v-else-if="row.status === 'OPEN'" class="muted">—</div>
        </template>
      </el-table-column>
      <el-table-column label="跟进" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'DONE' ? 'success' : 'warning'" effect="plain">
            {{ row.status === 'DONE' ? '已完结' : '跟进中' }}
          </el-tag>
          <div v-if="row.status === 'OPEN' && row.next_follow_on" :class="row.next_follow_on <= today ? 'bad' : 'muted'">
            {{ row.next_follow_on <= today ? '该跟进了' : '下次' }} {{ row.next_follow_on }}
          </div>
          <div v-if="row.updated_at" class="muted" :title="`${row.updated_by || ''} 修改`">{{ fmtTime(row.updated_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.conversation_id" link size="small" type="primary" @click="emit('open', row.conversation_id)">打开会话</el-button>
          <el-button link size="small" @click="editing = { ...row }">修改</el-button>
          <el-button v-if="row.status === 'OPEN'" :link="!row.can_close" :plain="row.can_close" size="small" type="success"
                     @click="setStatus(row, 'DONE')">完结</el-button>
          <el-button v-else link size="small" @click="setStatus(row, 'OPEN')">重开</el-button>
          <el-button link size="small" type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <ParcelFollowupDialog :draft="editing" @close="editing = null" @saved="load" />
  </div>
</template>

<style scoped>
.pf-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 10px 12px;
  gap: 8px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.spacer {
  flex: 1;
}
.cnt {
  margin-left: 4px;
  opacity: 0.75;
}
.due-cnt {
  margin-left: 4px;
  color: var(--el-color-danger);
  font-weight: 600;
}
.hint {
  font-size: 12px;
}
.tbl {
  flex: 1;
}
.bad {
  color: var(--el-color-danger);
}
.closeable {
  color: var(--el-color-success);
  font-weight: 600;
}
.auto {
  margin-top: 2px;
}
</style>
