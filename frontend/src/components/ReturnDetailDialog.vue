<script setup lang="ts">
// 售后管理 → 退货的“纠纷处理”页：基本信息、纠纷备注、订单信息、处理方案（接受退货 / 申请退款 / 给买家发信 / 上传文件，
// 真实提交到 eBay，只有对接店铺可用；能做哪些按 eBay 给卖家的下一步）、处理历史。上一个 / 下一个按当前列表顺序。
import { computed, reactive, ref } from 'vue'
import { copyText, fmtFull, money } from '../caseCommon'
import type { CaseRow, CaseTag } from '../caseCommon'
import { dotStyle } from '../tagColor'
import { readFile, useCaseDialog } from '../caseDialog'
import type { Choice } from '../caseDialog'
import CaseSections from './CaseSections.vue'
import CaseMessageTab from './CaseMessageTab.vue'
import CaseFileForm from './CaseFileForm.vue'
import ReturnRefundForm from './ReturnRefundForm.vue'
import EbayBlockedNote from './EbayBlockedNote.vue'

const props = defineProps<{ rows: CaseRow[]; index: number; tags: CaseTag[] }>()
const emit = defineEmits<{ close: []; move: [index: number]; changed: [row: CaseRow]; open: [convId: number] }>()
const { row, full, loading, busy, blocked, roleBlocked, note, addNote, submit, submitRefund, prev, next, setOnLoaded } = useCaseDialog(props, emit, 'return')
const RETURN_ID = computed(() => row.value?.external_id || null)

const tab = ref('accept')
const msgTab = ref<InstanceType<typeof CaseMessageTab> | null>(null)
const fileForm = ref<InstanceType<typeof CaseFileForm> | null>(null)
const acc = reactive({ method: 'upload_label', rma: '', tracking_number: '', carrier: '', no_label_reason: '', carrier_enum: '',
                       comment: '', file: null as { data: string; name: string } | null })
const labelInput = ref<HTMLInputElement | null>(null)
setOnLoaded((f) => {
  Object.assign(acc, { method: 'upload_label', rma: '', tracking_number: '', carrier: '', no_label_reason: '', carrier_enum: '',
                       comment: '', file: null })
  tab.value = f.actions.can.accept ? 'accept' : f.actions.can.refund ? 'refund' : 'message'
})

const live = computed(() => full.value?.live)
const caseTags = computed(() => props.tags.filter((t) => (row.value?.tag_ids || []).includes(t.id)))
const line = computed(() => full.value?.order?.lines?.find((l) => l.item_id === row.value?.item_id) || full.value?.order?.lines?.[0])
const methods = computed<Choice[]>(() => full.value?.accept_methods || [])
const reasons = computed<Choice[]>(() => full.value?.no_label_reasons || [])
const RETURN_REASON: Record<string, string> = {
  NO_LONGER_NEED_ITEM: '不再需要', ORDERED_WRONG_ITEM: '买错了', ORDERED_DIFFERENT_ITEM: '买了别的', DOES_NOT_FIT: '不适配',
  DEFECTIVE_ITEM: '商品有问题', ARRIVED_DAMAGED: '到货损坏', MISSING_PARTS: '缺件', NOT_AS_DESCRIBED: '与描述不符',
  WRONG_ITEM: '发错货', FOUND_BETTER_PRICE: '找到更便宜的', CHANGED_MIND: '改变主意',
}
const reasonText = computed(() => {
  const r = live.value?.reason || row.value?.reason
  return r ? `${RETURN_REASON[r] || row.value?.reason_label || r}（${r}）` : '—'
})

async function pickLabel(e: Event) {
  const el = e.target as HTMLInputElement
  acc.file = await readFile(el.files?.[0])
  el.value = ''
}
const acceptStop = computed(() => {
  const b = blocked('accept')
  if (b) return b
  if (live.value?.rma_required && live.value?.options?.includes('SELLER_APPROVE_REQUEST') && !acc.rma.trim()) return '这个店铺开了 RMA，要填 RMA 单号'
  if (acc.method === 'upload_label' && (!acc.file || !acc.tracking_number.trim() || !acc.carrier.trim())) return '请选择面单文件并填写运单号、物流渠道'
  if (acc.method === 'no_label' && (!acc.no_label_reason || !acc.comment.trim())) return '请选择原因并填写给平台的留言'
  if (acc.method === 'ebay_label' && !acc.carrier_enum) return '请选择承运商'
  return null
})
function doAccept() {
  const m = methods.value.find((x) => x.code === acc.method)?.label || acc.method
  submit('accept', { method: acc.method, rma: acc.rma || null, tracking_number: acc.tracking_number, carrier: acc.carrier,
                     no_label_reason: acc.no_label_reason || null, carrier_enum: acc.carrier_enum || null, comment: acc.comment,
                     file_data: acc.file?.data || null, file_name: acc.file?.name || null },
         `确认在 eBay 上接受买家 ${row.value?.buyer} 的退货（处理方式：${m}）？提交后买家会收到通知，不能撤回。`, true)
}
function doRefund(p: Record<string, any>) {
  const offer = p._offer
  delete p._offer
  const amt = money(p.amount, p.currency)
  submitRefund(p, RETURN_ID.value, full.value?.order?.order_id || row.value?.order_id || null, offer
    ? `提交退款申请：向买家 ${row.value?.buyer} 提出部分退款 ${amt}（买家同意后 eBay 才会退款）？按退款审核规则先审核或直接执行。`
    : `提交退款申请：给买家 ${row.value?.buyer} ${p.mode === 'full' ? '全额' : '部分'}退款 ${amt}？按退款审核规则先审核或直接执行；执行后退款会真实打到买家账户，不能撤回。`)
}
function doReceived(comment: string) {
  submit('received', { comment }, '确认在 eBay 上标记“已收到退货”？', true)
}
async function doMessage(p: { text: string; text_zh: string | null }) {
  if (await submit('message', p, `确认给买家 ${row.value?.buyer} 发信？会先进发件箱，延迟发出，之前可在会话里撤回。`)) msgTab.value?.clear()
}
async function doFile(p: Record<string, any>) {
  if (await submit('file', p, `确认把 ${p.file_name} 上传到 eBay 这条退货？买家和 eBay 都能看到。`)) fileForm.value?.clear()
}
</script>

<template>
  <el-dialog :model-value="true" width="1200px" top="3vh" append-to-body :close-on-click-modal="false" @close="emit('close')">
    <template #header>
      <div class="dlg-head">
        <b>纠纷处理 · 退货</b>
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
            <th>类型/状态</th>
            <td>退货 / <span :class="row.is_open ? 'red' : 'muted'">{{ row.status_label || row.status || '—' }}</span>
              <div v-if="live" class="muted small">{{ live.state }} / {{ live.status }}</div></td>
            <th>退货编号</th>
            <td>{{ row.external_id }} <button class="copy" @click="copyText(row.external_id)">⧉</button></td>
          </tr>
          <tr><th>Item ID</th><td>{{ row.item_id || '—' }}</td><th>开启原因</th><td>{{ reasonText }}</td></tr>
          <tr>
            <th>退款金额</th><td>{{ live?.estimated_refund ? money(live.estimated_refund.value, live.estimated_refund.currency) : money(row.amount, row.currency) }}
              <span class="muted small">（eBay 估算）</span>
              <div v-if="live?.refunded" class="red small">已退 {{ money(live.refunded.value, live.refunded.currency) }}</div></td>
            <th>开启 / 处理截止</th><td>{{ fmtFull(live?.opened_at || row.opened_at) }} / {{ fmtFull(live?.due_at || row.due_at) }}</td>
          </tr>
          <tr>
            <th>卖家账号</th><td>{{ row.store_name }}</td>
            <th>买家留言</th><td><span class="pre">{{ live?.comment || row.comment || '—' }}</span></td>
          </tr>
          <tr>
            <th>eBay 给卖家的下一步</th><td colspan="3">{{ (live?.option_labels || []).join('、') || '—（可能在等买家）' }}</td>
          </tr>
          <tr><th>客户姓名</th><td>{{ full?.order?.name || '—' }}</td><th>客户ID</th><td>{{ row.buyer || '—' }}</td></tr>
          <tr>
            <th>收件地址</th><td>{{ full?.order?.address || '—' }} {{ full?.order?.country || '' }}</td>
            <th>退货运单</th><td>{{ live?.tracking?.number || '—' }} {{ live?.tracking?.carrier || '' }}
              <span v-if="live?.tracking?.status" class="muted small">（{{ live.tracking.status }}）</span></td>
          </tr>
          <tr>
            <th>标签</th>
            <td colspan="3">
              <span v-for="t in caseTags" :key="t.id" class="tag"><i class="dot" :style="dotStyle(t.color)" />{{ t.name }}</span>
              <span v-if="!caseTags.length" class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="full?.live_error" class="warn-line">eBay 实时详情没取到：{{ full.live_error }}（能不能处理以 eBay 为准，下面的处理暂不能提交）</div>

      <CaseSections part="notes" :row="row" :full="full" :add-note="addNote" />
      <CaseSections part="order" :row="row" :full="full" @open="(id) => emit('open', id)" />

      <div class="sec">处理方案</div>
      <div v-if="full?.actions.blocked" class="warn-line">{{ full.actions.blocked }}</div>
      <el-tabs v-model="tab" type="card">
        <el-tab-pane label="接受退货" name="accept">
          <EbayBlockedNote :reason="note('accept')" :url="full?.ebay_url" :role="roleBlocked('accept')" />
          <el-form label-width="110px" class="plan-form">
            <el-form-item label="处理方式" required>
              <el-radio-group v-model="acc.method">
                <el-radio v-for="m in methods" :key="m.code" :value="m.code">{{ m.label }}</el-radio>
              </el-radio-group>
            </el-form-item>
            <template v-if="acc.method === 'upload_label'">
              <el-form-item label="退货面单" required>
                <input ref="labelInput" type="file" accept="image/*,.pdf" hidden @change="pickLabel" />
                <el-button @click="labelInput?.click()">选择文件</el-button>
                <span class="muted small">　{{ acc.file?.name || '图片或 PDF，最大 8 MB' }}</span>
              </el-form-item>
              <el-form-item label="运单号" required><el-input v-model="acc.tracking_number" maxlength="64" /></el-form-item>
              <el-form-item label="物流渠道" required><el-input v-model="acc.carrier" maxlength="64" placeholder="如 Royal Mail、DHL" /></el-form-item>
            </template>
            <el-form-item v-if="acc.method === 'no_label'" label="原因" required>
              <el-select v-model="acc.no_label_reason" placeholder="请选择" style="width: 320px">
                <el-option v-for="r in reasons" :key="r.code" :label="`${r.label}（${r.code}）`" :value="r.code" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="acc.method === 'ebay_label'" label="承运商" required>
              <el-select v-model="acc.carrier_enum" placeholder="eBay 运输标签的承运商" style="width: 260px" filterable>
                <el-option v-for="c in full?.label_carriers || []" :key="c" :label="c" :value="c" />
              </el-select>
              <span class="muted small">　eBay 生成标签，费用按 eBay 规则</span>
            </el-form-item>
            <el-form-item label="RMA 单号" :required="!!live?.rma_required">
              <el-input v-model="acc.rma" maxlength="64" style="width: 260px" placeholder="可不填" />
            </el-form-item>
            <el-form-item label="给平台的留言" :required="acc.method === 'no_label'">
              <el-input v-model="acc.comment" type="textarea" :rows="3" maxlength="1000" />
            </el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!acceptStop" :content="acceptStop || ''">
                <span><el-button type="success" :loading="busy" :disabled="!!acceptStop" @click="doAccept">提交</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="申请退款" name="refund">
          <EbayBlockedNote :reason="note('refund')" :url="full?.ebay_url" :role="roleBlocked('refund')" />
          <ReturnRefundForm :live="live" :order="full?.order" :blocked="full?.live_error ? 'eBay 退货详情没取到' : blocked('refund')"
                            :received-blocked="blocked('received')" :busy="busy" :order-id="full?.order?.order_id || row.order_id"
                            :sku="line?.sku" @submit="doRefund" @received="doReceived" />
        </el-tab-pane>
        <el-tab-pane label="给买家发信" name="message">
          <EbayBlockedNote :reason="note('message') || (full && !(full.conversation_id ?? row.conversation_id) ? '这张售后单对不上买家会话，不能走发件箱发信' : null)"
                           :url="full?.ebay_url" />
          <CaseMessageTab ref="msgTab" :conv-id="full?.conversation_id ?? row.conversation_id"
                          :blocked="blocked('message') || (!(full?.conversation_id ?? row.conversation_id) ? '这张售后单对不上买家会话，请到 eBay 后台留言' : null)"
                          :busy="busy" @submit="doMessage" />
        </el-tab-pane>
        <el-tab-pane label="上传文件" name="file">
          <EbayBlockedNote :reason="note('file')" :url="full?.ebay_url" :role="roleBlocked('file')" />
          <CaseFileForm ref="fileForm" :purposes="full?.file_purposes || []" :blocked="blocked('file')" :busy="busy" @submit="doFile" />
          <div v-if="live?.files?.length" class="files">
            <div class="muted small">eBay 上已有的文件：</div>
            <div v-for="f in live.files" :key="f.id" class="small">{{ f.name }} · {{ f.purpose }} · {{ f.by || '' }} · {{ fmtFull(f.at) }}</div>
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
.kv th { width: 120px; background: var(--el-fill-color-lighter); font-weight: normal; color: var(--el-text-color-regular); }
.red { color: var(--el-color-danger); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.pre { white-space: pre-wrap; }
.copy { border: none; background: none; cursor: pointer; color: var(--el-text-color-secondary); }
.tag { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0; }
.plan-form { max-width: 720px; padding-top: 6px; }
.files { margin: 6px 0 0 90px; }
</style>
