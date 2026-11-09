<script setup lang="ts">
// 会话右侧“相关信息”的下半部分：发票、买家评价，最下面一个模块用“待处理 / 备注”两个按钮切换（默认待处理）。
// 待处理 = 售后状态、售后群工单、查件单（可直接审核提交）、发票申请（可直接审核发送）、内部参考；
// 备注 = 内部备注（同事可见）。数据由 CustomerSidebar 按所选订单加载后传进来
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, post, session } from '../api'
import { errorText } from '../format'
import type { ConvContext, CustomerOrder } from '../types'
import ConvTodoPanel from './ConvTodoPanel.vue'

const props = defineProps<{ convId: number; order: CustomerOrder | null; ex: any; ctx?: ConvContext | null }>()
const emit = defineEmits<{ reload: []; insert: [text: string]; convChanged: [] }>()

const bottomTab = ref<'todo' | 'notes'>('todo')
watch(() => props.convId, () => (bottomTab.value = 'todo'))

// ---- 备注 ----
const noteText = ref('')
const noteForBuyer = ref(false)
const saving = ref(false)
async function addNote() {
  if (!noteText.value.trim()) return
  saving.value = true
  try {
    await post(`/internal/conversations/${props.convId}/order-notes`,
               { text: noteText.value.trim(), order_id: noteForBuyer.value || !props.order ? null : props.order.order_id })
    noteText.value = ''
    emit('reload')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}
async function delNote(n: any) {
  try {
    await ElMessageBox.confirm('删除这条备注？', '删除备注', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api('DELETE', `/internal/order-notes/${n.id}`)
    emit('reload')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
const canDelete = (n: any) => session.user?.role === 'ADMIN' || n.created_by === session.user?.display_name

const pad = (n: number) => String(n).padStart(2, '0')
function fmt(v?: string | null) {
  if (!v) return '—'
  const d = new Date(v.includes('T') || v.endsWith('Z') ? v : v.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return v
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
const money = (a: number | null, c: string | null) => (a != null ? `${c || ''} ${a.toFixed(2)}` : '')
const RATING: Record<string, [string, string]> = { Positive: ['好评', 'ok'], Neutral: ['中评', 'warn'], Negative: ['差评', 'bad'] }
</script>

<template>
  <div class="extras">
    <!-- 发票 -->
    <template v-if="ex?.invoices?.length">
      <div class="title">发票</div>
      <table class="kv">
        <tbody>
          <tr v-for="i in ex.invoices" :key="i.id">
            <th>
              <a v-if="i.pdf_url" :href="i.pdf_url" target="_blank" rel="noopener" class="mono">{{ i.number }}</a>
              <span v-else class="mono">{{ i.number }}</span>
            </th>
            <td>{{ i.status_label }} · {{ money(i.total, i.currency) }}<div v-if="i.sent_at" class="muted">{{ fmt(i.sent_at) }} 发出</div></td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 评价 -->
    <template v-if="ex?.feedback?.length">
      <div class="title">买家评价</div>
      <table class="kv">
        <tbody>
          <tr v-for="f in ex.feedback" :key="f.id">
            <th><span class="st" :class="RATING[f.rating]?.[1]">{{ f.rating_label || RATING[f.rating]?.[0] || f.rating }}</span></th>
            <td>{{ f.comment || '—' }}<div class="muted">{{ fmt(f.opened_at) }}</div></td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 待处理 / 备注：两个按钮切换，默认待处理（用 v-show，切换时不重新加载） -->
    <section class="notes">
    <div class="mod-tabs">
      <button :class="{ on: bottomTab === 'todo' }" @click="bottomTab = 'todo'">待处理</button>
      <button :class="{ on: bottomTab === 'notes' }" @click="bottomTab = 'notes'">
        备注<span v-if="ex?.notes?.length" class="cnt">{{ ex.notes.length }}</span>
      </button>
      <span v-if="bottomTab === 'notes'" class="hint-inline">内部，同事可见，不发给买家</span>
    </div>
    <ConvTodoPanel v-show="bottomTab === 'todo'" :conv-id="convId" :ctx="ctx || null" bare
                   @insert="(t: string) => emit('insert', t)" @changed="emit('convChanged')" />
    <div v-show="bottomTab === 'notes'" class="notes-body">
    <div class="note-input">
      <el-input v-model="noteText" type="textarea" :rows="3" maxlength="2000" placeholder="请填写备注" />
      <el-button :loading="saving" :disabled="!noteText.trim()" @click="addNote">新增</el-button>
    </div>
    <el-checkbox v-if="order" v-model="noteForBuyer" size="small" class="for-buyer">记在买家上（不限这单）</el-checkbox>
    <div v-else class="for-buyer hint">没有订单，备注记在买家上</div>
    <div v-for="n in ex?.notes || []" :key="n.id" class="note">
      <div class="note-text">{{ n.text }}</div>
      <div class="note-meta">
        {{ fmt(n.created_at) }} by {{ n.created_by }}<span v-if="!n.order_id"> · 买家备注</span>
        <button v-if="canDelete(n)" class="link" @click="delNote(n)">删除</button>
      </div>
    </div>
    <div v-if="!ex?.notes?.length" class="hint empty-notes">{{ ex ? '还没有备注' : '加载中…' }}</div>
    </div>
    </section>
  </div>
</template>

<style scoped>
.extras {
  display: flex;
  flex-direction: column;
}
.title {
  margin: 18px 0 8px;
  font-size: 13px;
  color: var(--text-2);
}
table.kv {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  border: 1px solid var(--line);
}
.kv th,
.kv td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
  text-align: left;
  line-height: 1.55;
}
.kv th {
  width: 112px;
  font-weight: 400;
  color: var(--text-2);
  word-break: break-all;
}
.kv td {
  color: var(--text);
  word-break: break-word;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
.reply {
  margin-top: 4px;
  white-space: pre-wrap;
}
.st.ok {
  color: var(--ok-text);
}
.st.warn {
  color: var(--warn-text);
}
.st.bad {
  color: var(--danger);
}
.note-input {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.for-buyer {
  margin-top: 4px;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}
.note {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 3px;
  font-size: 13px;
}
.note-text {
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.note-meta {
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
}
.link {
  margin-left: 8px;
  border: 0;
  background: none;
  padding: 0;
  font-family: inherit;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}
.link:hover {
  color: var(--danger);
}
.notes {
  margin-top: 18px;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.mod-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
  background: var(--sunken);
  border-bottom: 1px solid var(--line);
}
.mod-tabs button {
  height: 36px;
  padding: 0 12px;
  border: 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
}
.mod-tabs button.on {
  color: var(--accent);
  font-weight: 600;
  border-bottom-color: var(--accent);
}
.cnt {
  margin-left: 4px;
  padding: 0 5px;
  border-radius: 8px;
  background: var(--line);
  font-size: 11px;
  font-weight: 400;
  color: var(--text-2);
}
.hint-inline {
  margin-left: 6px;
  font-weight: 400;
  font-size: 12px;
  color: var(--muted);
}
.notes-body {
  padding: 10px 12px 12px;
}
.empty-notes {
  margin-top: 8px;
}
</style>
