<script setup lang="ts">
// 售后管理 → 纠纷（eBay 介入的 case）的“纠纷处理”页。eBay 的纠纷接口只能查询和申诉：
// 退货升级来的纠纷，退款 / 发信 / 上传证据转到对应的退货（按那条退货现在 eBay 允许的）；未收到货纠纷只能申诉，其他到 eBay 后台。
import { computed, ref } from 'vue'
import { copyText, fmtFull, money } from '../caseCommon'
import type { CaseRow, CaseTag } from '../caseCommon'
import { dotStyle } from '../tagColor'
import { useCaseDialog } from '../caseDialog'
import CaseSections from './CaseSections.vue'
import CaseMessageTab from './CaseMessageTab.vue'
import CaseFileForm from './CaseFileForm.vue'
import ReturnRefundForm from './ReturnRefundForm.vue'
import EbayBlockedNote from './EbayBlockedNote.vue'

const props = defineProps<{ rows: CaseRow[]; index: number; tags: CaseTag[] }>()
const emit = defineEmits<{ close: []; move: [index: number]; changed: [row: CaseRow]; open: [convId: number] }>()
const { row, full, loading, busy, blocked, roleBlocked, note, addNote, submit, submitRefund, prev, next, setOnLoaded } = useCaseDialog(props, emit, 'case')

const tab = ref('message')
const appealText = ref('')
const msgTab = ref<InstanceType<typeof CaseMessageTab> | null>(null)
const fileForm = ref<InstanceType<typeof CaseFileForm> | null>(null)
setOnLoaded((f) => {
  appealText.value = ''
  tab.value = f.actions.can.appeal ? 'appeal' : 'message'
})
const live = computed(() => full.value?.live)
const ret = computed(() => full.value?.return_live)
const caseTags = computed(() => props.tags.filter((t) => (row.value?.tag_ids || []).includes(t.id)))
const line = computed(() => full.value?.order?.lines?.find((l) => l.item_id === row.value?.item_id) || full.value?.order?.lines?.[0])
const TYPE: Record<string, string> = { INR: '未收到货', ITEM_NOT_RECEIVED: '未收到货', RETURN: '退货', SNAD: '与描述不符' }
const typeText = computed(() => { const t = live.value?.case_type; return t ? TYPE[t] || t : '—' })
const NO_API = 'eBay 没有开放接口，请到 eBay 后台处理'

function doRefund(p: Record<string, any>) {
  const offer = p._offer
  delete p._offer
  const amt = money(p.amount, p.currency)
  submitRefund(p, live.value?.return_id || null, ret.value?.order_id || full.value?.order?.order_id || null, offer
    ? `提交退款申请：向买家 ${row.value?.buyer} 提出部分退款 ${amt}（买家同意后 eBay 才会退款）？按退款审核规则先审核或直接执行。`
    : `提交退款申请：给买家 ${row.value?.buyer} ${p.mode === 'full' ? '全额' : '部分'}退款 ${amt}？按退款审核规则先审核或直接执行；执行后退款会真实打到买家账户，不能撤回。`)
}
async function doMessage(p: { text: string; text_zh: string | null }) {
  if (await submit('message', p, `确认给买家 ${row.value?.buyer} 发信？会先进发件箱，延迟发出，之前可在会话里撤回。`)) msgTab.value?.clear()
}
async function doFile(p: Record<string, any>) {
  if (await submit('file', p, `确认把 ${p.file_name} 作为证据上传到 eBay？买家和 eBay 都能看到。`)) fileForm.value?.clear()
}
function doAppeal() {
  submit('appeal', { comment: appealText.value }, '确认对 eBay 的裁决提出申诉？', true)
}
</script>

<template>
  <el-dialog :model-value="true" width="1200px" top="3vh" append-to-body :close-on-click-modal="false" @close="emit('close')">
    <template #header>
      <div class="dlg-head">
        <b>纠纷处理 · 纠纷</b>
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
            <td>{{ typeText }} / <span :class="row.is_open ? 'red' : 'muted'">{{ row.status_label || live?.status || row.status || '—' }}</span></td>
            <th>纠纷编号</th>
            <td>{{ row.external_id }} <button class="copy" @click="copyText(row.external_id)">⧉</button>
              <div v-if="live?.return_id" class="muted small">关联退货 {{ live.return_id }}</div></td>
          </tr>
          <tr>
            <th>纠纷金额</th><td>{{ live?.claim_amount ? money(live.claim_amount.value, live.claim_amount.currency) : money(row.amount, row.currency) }}
              <div v-if="live?.refunded" class="red small">已退 {{ money(live.refunded.value, live.refunded.currency) }}</div></td>
            <th>开启 / 截止</th><td>{{ fmtFull(live?.opened_at || row.opened_at) }} / {{ fmtFull(live?.expires_at || row.due_at) }}</td>
          </tr>
          <tr>
            <th>纠纷结果</th>
            <td><b v-if="live?.outcome_label" :class="live.outcome === 'SELLER' ? 'green' : 'red'">{{ live.outcome_label }}</b>
              <span v-else class="muted">{{ row.is_open ? '还没有结果' : '—' }}</span>
              <div v-if="live?.seller_closure_reason || live?.buyer_closure_reason" class="muted small">
                {{ live.seller_closure_reason || live.buyer_closure_reason }}</div></td>
            <th>申诉</th>
            <td>{{ live?.appeal?.eligible ? '可以申诉' : '不能申诉' }}<span v-if="live?.appeal?.status" class="muted small">（{{ live.appeal.status }} {{ fmtFull(live.appeal.at) }}）</span></td>
          </tr>
          <tr>
            <th>卖家账号</th><td>{{ row.store_name }}</td>
            <th>买家期待</th>
            <td>{{ live?.expected || '—' }}<span v-if="live?.expected_refund">，期待退款 {{ money(live.expected_refund.value, live.expected_refund.currency) }}</span></td>
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
      <EbayBlockedNote v-else-if="live && !live.return_id" :url="full?.ebay_url"
                       :reason="`未收到货纠纷：eBay 只开放了查询和申诉接口，上传运单号、提供 POD、退款、留言——${NO_API}`" />
      <el-tabs v-model="tab" type="card">
        <el-tab-pane label="给买家发信" name="message">
          <EbayBlockedNote :reason="note('message') || (full && !(full.conversation_id ?? row.conversation_id) ? '这张售后单对不上买家会话，不能走发件箱发信' : null)"
                           :url="full?.ebay_url" :role="roleBlocked('message')" />
          <CaseMessageTab ref="msgTab" :conv-id="full?.conversation_id ?? row.conversation_id"
                          :blocked="blocked('message') || (!(full?.conversation_id ?? row.conversation_id) ? '这张售后单对不上买家会话，请到 eBay 后台留言' : null)"
                          :busy="busy" @submit="doMessage" />
        </el-tab-pane>
        <el-tab-pane label="申请退款" name="refund">
          <EbayBlockedNote :reason="note('refund') || (full && !ret ? (full.return_error || `没有对应退货的退款信息，${NO_API}`) : null)"
                           :url="full?.ebay_url" :role="roleBlocked('refund')" />
          <ReturnRefundForm v-if="ret" :live="ret" :order="full?.order" :blocked="blocked('refund')" :busy="busy"
                            :order-id="full?.order?.order_id || row.order_id" :sku="line?.sku" @submit="doRefund" />
        </el-tab-pane>
        <el-tab-pane label="上传运单号 / POD" name="pod">
          <EbayBlockedNote :url="full?.ebay_url"
                           :reason="`上传运单号 / 妥投证明（POD）：${NO_API}${live?.return_id ? '。退货升级来的纠纷也可以在“上传证据”里把面单、签收照片传到对应退货' : ''}`" />
          <div v-if="live?.tracking" class="small">eBay 记录的运单：{{ live.tracking.number }} {{ live.tracking.carrier }}（{{ live.tracking.status }}）</div>
        </el-tab-pane>
        <el-tab-pane label="上传证据" name="file">
          <EbayBlockedNote :reason="note('file')" :url="full?.ebay_url" :role="roleBlocked('file')" />
          <CaseFileForm ref="fileForm" :purposes="full?.file_purposes || []" :blocked="blocked('file')" :busy="busy" @submit="doFile" />
        </el-tab-pane>
        <el-tab-pane label="申诉裁决" name="appeal">
          <EbayBlockedNote :reason="note('appeal')" :url="full?.ebay_url" :role="roleBlocked('appeal')" />
          <el-form label-width="90px" class="plan-form">
            <el-form-item label="申诉理由" required>
              <el-input v-model="appealText" type="textarea" :rows="4" maxlength="1000" placeholder="写给 eBay 的申诉理由（英文）" />
            </el-form-item>
            <el-form-item>
              <el-tooltip :disabled="!blocked('appeal')" :content="blocked('appeal') || ''">
                <span><el-button type="success" :loading="busy" :disabled="!!blocked('appeal') || !appealText.trim()" @click="doAppeal">提交</el-button></span>
              </el-tooltip>
            </el-form-item>
          </el-form>
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
.green { color: var(--el-color-success); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.copy { border: none; background: none; cursor: pointer; color: var(--el-text-color-secondary); }
.tag { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0; }
.plan-form { max-width: 640px; padding-top: 6px; }
</style>
