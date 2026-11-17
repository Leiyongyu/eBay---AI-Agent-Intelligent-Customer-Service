<script setup lang="ts">
// 纠纷处理弹窗共用的三块：纠纷备注（每次新增一条）、订单信息（eBay 实时）、处理历史（买家及平台 / 卖家两栏）
import { computed, ref } from 'vue'
import { copyText, fmtFull } from '../caseCommon'
import type { CaseRow } from '../caseCommon'
import { ACTOR } from '../caseDialog'
import type { FullBase, HistoryItem } from '../caseDialog'

const props = defineProps<{ part: 'notes' | 'order' | 'history'; row: CaseRow; full: FullBase | null
  history?: HistoryItem[]; addNote?: (t: string) => Promise<boolean> }>()
const emit = defineEmits<{ open: [convId: number] }>()

const noteText = ref('')
async function add() {
  if (props.addNote && (await props.addNote(noteText.value.trim()))) noteText.value = ''
}
const line = computed(() => props.full?.order?.lines?.find((l) => l.item_id === props.row.item_id) || props.full?.order?.lines?.[0])
const pkg = computed(() => props.full?.order?.packages?.[0])
const sellerSide = (h: HistoryItem) => (h.actor || '').toUpperCase() === 'SELLER'
const convId = computed(() => props.full?.conversation_id || props.row.conversation_id)
</script>

<template>
  <div v-if="part === 'notes'" class="note-box">
    <span class="lbl">纠纷备注：</span>
    <div class="note-main">
      <div class="note-input">
        <el-input v-model="noteText" type="textarea" :rows="3" maxlength="2000" placeholder="请填写纠纷备注" />
        <el-button :disabled="!noteText.trim()" @click="add">新增</el-button>
      </div>
      <div v-for="n in full?.notes || []" :key="n.id" class="note" :class="{ action: n.kind === 'ACTION' }">
        <div class="pre">{{ n.text }}</div>
        <div class="muted small">{{ fmtFull(n.created_at) }} by {{ n.created_by || '—' }}</div>
      </div>
    </div>
  </div>

  <template v-else-if="part === 'order'">
    <div class="sec">订单信息</div>
    <div class="buyer-bar">
      <span class="buyer">{{ row.buyer }}<template v-if="full?.order?.name">（{{ full.order.name }}）</template></span>
      <span>{{ full?.order?.country || '—' }}</span>
      <el-button v-if="convId" link type="warning" @click="emit('open', convId!)">联系TA</el-button>
      <span v-else class="muted small">没有会话</span>
    </div>
    <div v-if="full?.order?.error" class="warn-line">{{ full.order.error }}</div>
    <table class="grid">
      <thead><tr><th>产品信息</th><th>订单编号</th><th>运单号/物流公司</th><th>单价</th><th>订单金额</th><th>时间</th></tr></thead>
      <tbody>
        <tr>
          <td class="prod">
            <img v-if="full?.extras?.picture_url" :src="full.extras.picture_url" alt="" />
            <div>
              <div><b>{{ line?.sku || full?.extras?.sku || '—' }}</b> ×{{ line?.quantity || full?.extras?.quantity || 1 }}</div>
              <div class="title">{{ line?.title || full?.extras?.title || row.title || '' }}</div>
              <div class="muted">{{ row.item_id }}</div>
            </div>
          </td>
          <td>{{ full?.order?.order_id || full?.extras?.order_id || row.order_id || '—' }}
            <div :class="full?.order?.status === 'FULFILLED' ? 'green' : 'muted'">{{ full?.order?.status === 'FULFILLED' ? '已发货' : (full?.order?.status || '') }}</div></td>
          <td>{{ pkg?.tracking_number || '—' }}
            <button v-if="pkg?.tracking_number" class="copy" @click="copyText(pkg!.tracking_number!)">⧉</button>
            <div>{{ pkg?.carrier || '' }}</div></td>
          <td>{{ line?.price ? `${line.price} ${line.currency || ''}` : '—' }}</td>
          <td>{{ full?.order?.total ? `${full.order.total} ${full.order.currency || ''}` : '—' }}<div class="muted small">（eBay 订单总额）</div>
            <div v-if="full?.order?.payment_status" :class="full.order.payment_status === 'PAID' ? 'muted small' : 'red small'">{{ full.order.payment_status_label }}</div></td>
          <td class="small">
            <div>付款：{{ fmtFull(full?.order?.paid_at) }}</div>
            <div>开启：{{ fmtFull(row.opened_at) }}</div>
            <div>发货：{{ fmtFull(pkg?.shipped_at) }}</div>
          </td>
        </tr>
      </tbody>
    </table>
  </template>

  <template v-else>
    <div class="sec">处理历史</div>
    <div class="history">
      <div class="col-head">买家及平台（{{ row.buyer }}）</div>
      <div class="col-head">卖家（{{ row.store_name }}）</div>
      <template v-for="(h, i) in history || []" :key="i">
        <div :class="['h-cell', { empty: sellerSide(h) }]">
          <div v-if="!sellerSide(h)" class="bubble">
            <div class="b-head"><b>{{ h.description || h.action }}</b><span class="muted small">{{ fmtFull(h.at) }}</span></div>
            <div class="muted small">{{ ACTOR[(h.actor || '').toUpperCase()] || h.actor }} · {{ h.action }}</div>
          </div>
        </div>
        <div :class="['h-cell', { empty: !sellerSide(h) }]">
          <div v-if="sellerSide(h)" class="bubble seller">
            <div class="b-head"><b>{{ h.description || h.action }}</b><span class="muted small">{{ fmtFull(h.at) }}</span></div>
            <div class="muted small">{{ h.action }}</div>
          </div>
        </div>
      </template>
      <div v-if="!(history || []).length" class="muted empty-hist">{{ full?.live_error ? '没取到 eBay 的处理历史' : '还没有处理记录' }}</div>
    </div>
  </template>
</template>

<style scoped>
.sec { background: var(--el-fill-color-light); padding: 8px 12px; margin: 14px 0 10px; font-weight: 600; }
.red { color: var(--el-color-danger); }
.green { color: var(--el-color-success); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.pre { white-space: pre-wrap; }
.copy { border: none; background: none; cursor: pointer; color: var(--el-text-color-secondary); }
.warn-line { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 8px 0; }
.note-box { display: flex; gap: 8px; margin-top: 14px; }
.note-box .lbl { width: 70px; padding-top: 6px; }
.note-main { width: 640px; max-width: 100%; }
.note-input { display: flex; gap: 8px; align-items: flex-end; }
.note { border: 1px solid var(--el-border-color-lighter); padding: 6px 10px; margin-top: 8px; }
.note.action { background: var(--el-color-primary-light-9); }
.buyer-bar { display: flex; gap: 16px; align-items: center; border: 1px solid var(--el-border-color-lighter); padding: 6px 12px; width: fit-content; }
.buyer { color: var(--el-color-warning-dark-2); }
.grid { width: 100%; border-collapse: collapse; margin-top: 8px; }
.grid th { background: var(--el-fill-color-light); text-align: left; padding: 6px 8px; font-weight: normal; }
.grid td { padding: 8px; vertical-align: top; border-bottom: 1px solid var(--el-border-color-lighter); }
.prod { display: flex; gap: 8px; max-width: 420px; }
.prod img { width: 56px; height: 56px; object-fit: contain; }
.prod .title { font-size: 12px; max-width: 340px; }
.history { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; }
.col-head { background: var(--el-fill-color-light); padding: 8px 12px; }
.bubble { border: 1px solid var(--el-color-warning-light-5); background: var(--el-color-warning-light-9); border-radius: 6px; padding: 8px 12px; }
.bubble.seller { border-color: var(--el-color-success-light-5); background: var(--el-color-success-light-9); }
.b-head { display: flex; justify-content: space-between; gap: 12px; }
.empty-hist { grid-column: 1 / span 2; padding: 12px; }
</style>
