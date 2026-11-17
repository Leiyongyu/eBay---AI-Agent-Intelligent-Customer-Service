<script setup lang="ts">
// 售后管理 → 取消交易的“纠纷处理”页：买家的取消申请 同意（eBay 自动全额退款，只有管理员 / 售后）/ 拒绝（原因必填，只记在本系统；
// 已发货的把发货时间、运单号带给 eBay）。提交走 POST /internal/ebay-cases/{id}/cancel/{approve|reject}（cancel_actions.py）。
// eBay 的取消申请没有给买家发信的接口：要联系买家请打开会话，用 AI 起草后人工审核发送。
import { computed, reactive, ref } from 'vue'
import { copyText, fmtFull, money } from '../caseCommon'
import type { CaseRow, CaseTag } from '../caseCommon'
import { dotStyle } from '../tagColor'
import { useCaseDialog } from '../caseDialog'
import CaseSections from './CaseSections.vue'

const props = defineProps<{ rows: CaseRow[]; index: number; tags: CaseTag[] }>()
const emit = defineEmits<{ close: []; move: [index: number]; changed: [row: CaseRow]; open: [convId: number] }>()
const { row, full, loading, busy, blocked, addNote, submit, prev, next, setOnLoaded } = useCaseDialog(props, emit, 'cancel')

const tab = ref('approve')
const rej = reactive({ reason: '', shipped: null as string | null, tracking: '' })
setOnLoaded((f) => {
  Object.assign(rej, { reason: '', shipped: null, tracking: '' })
  tab.value = f.actions.can.approve ? 'approve' : 'reject'
})
const live = computed(() => full.value?.live)
const caseTags = computed(() => props.tags.filter((t) => (row.value?.tag_ids || []).includes(t.id)))
const convId = computed(() => full.value?.conversation_id ?? row.value?.conversation_id ?? null)
const REQ: Record<string, string> = { BUYER: '买家', SELLER: '卖家' }

function doApprove() {
  const r = row.value
  if (!r) return
  submit('approve', { confirm_order_id: r.order_id },
    `确认同意买家 ${r.buyer || ''} 取消订单 ${r.order_id}？订单会取消，eBay 自动全额退款给买家，不能撤回。`, true)
}
function doReject() {
  const r = row.value
  if (!r || !rej.reason.trim()) return
  submit('reject', { reason: rej.reason.trim(), shipped_at: rej.shipped || null, tracking_number: rej.tracking.trim() || null },
    `确认拒绝买家取消订单 ${r.order_id}？订单继续履约。拒绝原因只记在本系统（eBay 不收原因）。`, true)
}
</script>

<template>
  <el-dialog :model-value="true" width="1100px" top="3vh" append-to-body :close-on-click-modal="false" @close="emit('close')">
    <template #header>
      <div class="dlg-head">
        <b>纠纷处理 · 取消交易</b>
        <span class="muted">{{ index + 1 }} / {{ rows.length }}</span>
        <span class="grow" />
        <el-button size="small" :disabled="index <= 0" @click="prev">上一个</el-button>
        <el-button size="small" type="success" :disabled="index >= rows.length - 1" @click="next">下一个</el-button>
      </div>
    </template>
    <div v-if="row" v-loading="loading" class="inq">
      <div class="sec">基本信息</div>
      <table class="kv">
        <tbody>
          <tr>
            <th>状态</th>
            <td><span :class="row.is_open ? 'red' : 'muted'">{{ row.status_label || live?.status || row.status || '—' }}</span>
              <div v-if="live?.close_reason" class="muted small">关闭原因：{{ live.close_reason }}</div></td>
            <th>取消编号</th>
            <td>{{ row.external_id }} <button class="copy" @click="copyText(row.external_id)">⧉</button></td>
          </tr>
          <tr>
            <th>发起人 / 原因</th>
            <td>{{ REQ[live?.requestor || ''] || live?.requestor || '买家' }} · {{ row.reason_label || live?.reason || row.reason || '—' }}</td>
            <th>退款金额</th>
            <td>{{ live?.refund_amount ? money(live.refund_amount.value, live.refund_amount.currency) : money(row.amount, row.currency) }}
              <span v-if="live?.payment_status" class="muted small">（{{ live.payment_status }}）</span></td>
          </tr>
          <tr>
            <th>申请 / 截止</th><td>{{ fmtFull(live?.requested_at || row.opened_at) }} / {{ fmtFull(live?.due_at || row.due_at) }}</td>
            <th>卖家账号</th><td>{{ row.store_name }}</td>
          </tr>
          <tr><th>客户姓名</th><td>{{ full?.order?.name || '—' }}</td><th>客户ID</th><td>{{ row.buyer || '—' }}</td></tr>
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
      <div v-if="full?.live_error" class="warn-line">eBay 实时详情没取到：{{ full.live_error }}</div>

      <CaseSections part="notes" :row="row" :full="full" :add-note="addNote" />
      <CaseSections part="order" :row="row" :full="full" @open="(id) => emit('open', id)" />

      <div class="sec">处理方案</div>
      <div v-if="full?.actions.blocked" class="warn-line">{{ full.actions.blocked }}</div>
      <el-tabs v-model="tab" type="card">
        <el-tab-pane label="同意取消" name="approve">
          <div class="plan">
            <p>同意后订单取消，<b>eBay 自动全额退款给买家</b>，不能撤回。会话里会记一条系统事件，给买家的通知请在会话里用 AI 起草后人工审核发送。</p>
            <el-tooltip :disabled="!blocked('approve')" :content="blocked('approve') || ''">
              <span><el-button type="success" :loading="busy" :disabled="!!blocked('approve')" @click="doApprove">同意取消（订单 {{ row.order_id }}）</el-button></span>
            </el-tooltip>
          </div>
        </el-tab-pane>
        <el-tab-pane label="拒绝取消" name="reject">
          <el-form label-width="90px" class="plan-form">
            <el-form-item label="拒绝原因" required>
              <el-input v-model="rej.reason" type="textarea" :rows="2" maxlength="500" placeholder="必填，如 已经发货" />
              <div class="note-line">eBay 不接收拒绝原因，只记在本系统（处理备注、会话记录）</div>
            </el-form-item>
            <el-form-item label="已发货">
              <el-date-picker v-model="rej.shipped" type="datetime" value-format="YYYY-MM-DDTHH:mm:ss" placeholder="发货时间（可空）" />
              <el-input v-model="rej.tracking" placeholder="运单号（可空）" style="width: 220px; margin-left: 8px" />
              <div class="muted small">已经发货的建议填上，eBay 会显示给买家</div>
            </el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!blocked('reject')" :content="blocked('reject') || ''">
                <span><el-button type="warning" :loading="busy" :disabled="!!blocked('reject') || !rej.reason.trim()" @click="doReject">拒绝取消</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="联系买家" name="message">
          <div class="plan">
            <p class="muted">eBay 的取消申请没有给买家发信的接口。请打开买家会话，用 AI 起草后人工审核发送（处理结果已经记在会话里，AI 起草能看到）。</p>
            <el-button :disabled="!convId" @click="convId && emit('open', convId)">打开买家会话</el-button>
            <span v-if="!convId" class="muted small">这条取消申请对不上买家会话</span>
          </div>
        </el-tab-pane>
      </el-tabs>

      <CaseSections part="history" :row="row" :full="full" :history="live?.history || []" />
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
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.copy { border: none; background: none; cursor: pointer; color: var(--el-text-color-secondary); }
.tag { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0; }
.plan { padding: 6px 4px; display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
.plan-form { max-width: 680px; padding-top: 6px; }
.note-line { font-size: 12px; color: var(--el-color-warning-dark-2); }
</style>
