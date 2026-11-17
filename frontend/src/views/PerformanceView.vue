<script setup lang="ts">
// 绩效统计：按客服看回复量、响应时长、24 小时内回复率、AI 草稿采纳情况和其他处理动作
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { errorText } from '../format'
import type { Store } from '../types'

interface Row {
  actor: string
  is_ai: boolean
  submitted: number
  sent: number
  recalled: number
  failed: number
  pending: number
  conversations: number
  avg_response_seconds: number | null
  median_response_seconds: number | null
  late: number
  on_time_rate: number | null
  adopted: number
  edited: number
  own: number
  adoption_rate: number | null
  marks: number
  aftersales: number
  handoffs: number
  tickets: number
  invoices: number
}
interface Summary {
  start: string
  end: string
  totals: {
    buyer_messages: number
    replies: number
    manual: number
    auto: number
    auto_share: number | null
    avg_response_seconds: number | null
    on_time_rate: number | null
    adoption_rate: number | null
    staff: number
  }
  team: Row
  people: Row[]
  ai: Row
  days: { date: string; manual: number; auto: number }[]
}

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
const today = () => new Date()
function back(days: number) {
  const d = today()
  d.setDate(d.getDate() - days)
  return d
}

type Quick = 'today' | 'yesterday' | '7' | '30' | 'month' | 'custom'
const quick = ref<Quick>('7')
const range = ref<[string, string]>([ymd(back(6)), ymd(today())])
const QUICKS: { key: Quick; label: string }[] = [
  { key: 'today', label: '今天' }, { key: 'yesterday', label: '昨天' }, { key: '7', label: '近7天' },
  { key: '30', label: '近30天' }, { key: 'month', label: '本月' },
]
function pick(q: Quick) {
  quick.value = q
  const t = today()
  if (q === 'today') range.value = [ymd(t), ymd(t)]
  else if (q === 'yesterday') range.value = [ymd(back(1)), ymd(back(1))]
  else if (q === '7') range.value = [ymd(back(6)), ymd(t)]
  else if (q === '30') range.value = [ymd(back(29)), ymd(t)]
  else if (q === 'month') range.value = [ymd(new Date(t.getFullYear(), t.getMonth(), 1)), ymd(t)]
}
function onRange(v: [string, string] | null) {
  if (!v) return pick('7')
  quick.value = 'custom'
  range.value = v
}

const stores = ref<Store[]>([])
const storeId = ref('')
const data = ref<Summary | null>(null)
const loading = ref(false)
async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ start: range.value[0], end: range.value[1] })
    if (storeId.value) q.set('store_id', storeId.value)
    data.value = await get<Summary>(`/api/performance/summary?${q}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
watch([range, storeId], load)
onMounted(async () => {
  get<Store[]>('/internal/stores').then((s) => (stores.value = s)).catch(() => null)
  await load()
})

// ---- 显示格式 ----
function dur(s: number | null | undefined) {
  if (s == null) return '—'
  if (s < 60) return `${s}秒`
  if (s < 3600) return `${Math.round(s / 60)}分钟`
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h >= 48 ? `${(s / 86400).toFixed(1)}天` : `${h}小时${m ? `${m}分` : ''}`
}
const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(v === 1 || v === 0 ? 0 : 1)}%`)

// ---- 客服表：可排序，最后是 AI 自动回复和合计 ----
const rows = computed(() => (data.value ? [...data.value.people] : []))
const tableRows = computed(() => {
  const d = data.value
  if (!d) return []
  const out: Row[] = [...rows.value]
  if (d.ai.submitted) out.push(d.ai)
  return out
})
function summaryRow() {
  const t = data.value?.team
  if (!t) return []
  return ['合计（人工）', t.conversations, t.submitted, t.sent, t.recalled, t.failed, dur(t.avg_response_seconds),
    dur(t.median_response_seconds), t.late, pct(t.on_time_rate), t.adopted, t.edited, t.own, pct(t.adoption_rate),
    t.marks, t.aftersales, t.handoffs, t.tickets, t.invoices]
}
const rateClass = (v: number | null, good = 0.95, ok = 0.8) => (v == null ? '' : v >= good ? 'good' : v >= ok ? 'ok' : 'bad')

// ---- 每日趋势（人工 / AI 堆叠柱） ----
const maxDay = computed(() => Math.max(1, ...(data.value?.days || []).map((d) => d.manual + d.auto)))
const dayLabel = (s: string) => s.slice(5).replace('-', '/')
const showEvery = computed(() => Math.max(1, Math.ceil((data.value?.days.length || 1) / 15)))

function exportCsv() {
  const d = data.value
  if (!d) return
  const head = ['客服', '处理会话', '回复数', '已发出', '撤回', '发送失败', '平均响应', '中位响应', '超时(>24h)', '24h内回复率',
    'AI直接采纳', '修改后发送', '人工撰写', 'AI采纳率', '打标记', '转售后', '人工接管', '提交查件', '发发票']
  const line = (r: Row) => [r.actor, r.conversations, r.submitted, r.sent, r.recalled, r.failed, dur(r.avg_response_seconds),
    dur(r.median_response_seconds), r.late, pct(r.on_time_rate), r.adopted, r.edited, r.own, pct(r.adoption_rate),
    r.marks, r.aftersales, r.handoffs, r.tickets, r.invoices]
  const all = [head, ...tableRows.value.map(line), line({ ...d.team, actor: '合计（人工）' })]
  const csv = all.map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\r\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }))
  a.download = `绩效统计_${d.start}_${d.end}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="pf">
    <!-- 筛选 -->
    <div class="bar panel">
      <div class="quick">
        <button v-for="q in QUICKS" :key="q.key" :class="{ on: quick === q.key }" @click="pick(q.key)">{{ q.label }}</button>
      </div>
      <el-date-picker :model-value="range" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                      start-placeholder="开始日期" end-placeholder="结束日期" :clearable="false" style="width: 260px"
                      @update:model-value="onRange" />
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable style="width: 190px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <span class="spacer" />
      <el-button :loading="loading" @click="load">刷新</el-button>
      <el-button :disabled="!data" @click="exportCsv">导出</el-button>
    </div>

    <div v-loading="loading && !data" class="body">
      <template v-if="data">
        <!-- 关键指标 -->
        <div class="kpis">
          <div class="kpi panel">
            <div class="k-label">买家来信</div>
            <div class="k-value">{{ data.totals.buyer_messages }}</div>
            <div class="k-sub">条消息</div>
          </div>
          <div class="kpi panel">
            <div class="k-label">回复总数</div>
            <div class="k-value">{{ data.totals.replies }}</div>
            <div class="k-sub">人工 {{ data.totals.manual }} · AI 自动 {{ data.totals.auto }}</div>
          </div>
          <div class="kpi panel">
            <div class="k-label">AI 自动回复占比</div>
            <div class="k-value">{{ pct(data.totals.auto_share) }}</div>
            <div class="k-sub">AI 直接发出的回复 / 全部回复</div>
          </div>
          <div class="kpi panel">
            <div class="k-label">人工平均响应</div>
            <div class="k-value">{{ dur(data.totals.avg_response_seconds) }}</div>
            <div class="k-sub">买家来信 → 客服提交回复</div>
          </div>
          <div class="kpi panel">
            <div class="k-label">24 小时内回复率</div>
            <div class="k-value" :class="rateClass(data.totals.on_time_rate)">{{ pct(data.totals.on_time_rate) }}</div>
            <div class="k-sub">eBay 要求 24 小时内回复</div>
          </div>
          <div class="kpi panel">
            <div class="k-label">AI 草稿直接采纳率</div>
            <div class="k-value">{{ pct(data.totals.adoption_rate) }}</div>
            <div class="k-sub">一字不改直接发出的比例</div>
          </div>
        </div>

        <!-- 每日趋势 -->
        <div class="panel chart-card">
          <div class="panel-head">
            每日回复量
            <span class="legend"><i class="sw manual" />人工回复</span>
            <span class="legend"><i class="sw auto" />AI 自动回复</span>
          </div>
          <div class="chart" role="img" :aria-label="`${data.start} 至 ${data.end} 每日回复量`">
            <div v-for="(d, i) in data.days" :key="d.date" class="col" :title="`${d.date}：人工 ${d.manual}，AI 自动 ${d.auto}`">
              <span class="num">{{ d.manual + d.auto || '' }}</span>
              <div class="stack" :style="{ height: `${((d.manual + d.auto) / maxDay) * 100}%` }">
                <div class="seg auto" :style="{ flexGrow: d.auto }" />
                <div class="seg manual" :style="{ flexGrow: d.manual }" />
              </div>
              <span class="day">{{ i % showEvery === 0 ? dayLabel(d.date) : '' }}</span>
            </div>
          </div>
        </div>

        <!-- 客服明细 -->
        <div class="panel table-card">
          <div class="panel-head">
            客服绩效明细
            <span class="muted">{{ data.start }} 至 {{ data.end }} · {{ data.totals.staff }} 位客服；点表头可排序</span>
          </div>
          <el-table :data="tableRows" border show-summary :summary-method="summaryRow"
                    :row-class-name="({ row }: { row: Row }) => (row.is_ai ? 'ai-row' : '')" empty-text="这段时间没有客服回复记录">
            <el-table-column prop="actor" label="客服" min-width="120" fixed>
              <template #default="{ row }">
                <span class="actor"><span class="av" :class="{ ai: row.is_ai }">{{ row.is_ai ? 'AI' : row.actor.slice(0, 1) }}</span>{{ row.actor }}</span>
              </template>
            </el-table-column>
            <el-table-column label="工作量" align="center">
              <el-table-column prop="conversations" label="处理会话" width="88" align="right" sortable />
              <el-table-column prop="submitted" label="回复数" width="80" align="right" sortable />
              <el-table-column prop="sent" label="已发出" width="80" align="right" sortable />
              <el-table-column prop="recalled" label="撤回" width="70" align="right" sortable />
              <el-table-column prop="failed" label="发送失败" width="88" align="right" sortable>
                <template #default="{ row }"><span :class="{ bad: row.failed }">{{ row.failed }}</span></template>
              </el-table-column>
            </el-table-column>
            <el-table-column label="响应时效" align="center">
              <el-table-column prop="avg_response_seconds" label="平均响应" width="96" align="right" sortable>
                <template #default="{ row }">{{ dur(row.avg_response_seconds) }}</template>
              </el-table-column>
              <el-table-column prop="median_response_seconds" label="中位响应" width="96" align="right" sortable>
                <template #default="{ row }">{{ dur(row.median_response_seconds) }}</template>
              </el-table-column>
              <el-table-column prop="late" label="超时(>24h)" width="100" align="right" sortable>
                <template #default="{ row }"><span :class="{ bad: row.late }">{{ row.late }}</span></template>
              </el-table-column>
              <el-table-column prop="on_time_rate" label="24h内回复率" width="110" align="right" sortable>
                <template #default="{ row }"><span :class="rateClass(row.on_time_rate)">{{ pct(row.on_time_rate) }}</span></template>
              </el-table-column>
            </el-table-column>
            <el-table-column label="AI 草稿" align="center">
              <el-table-column prop="adopted" label="直接采纳" width="88" align="right" sortable />
              <el-table-column prop="edited" label="修改后发" width="88" align="right" sortable />
              <el-table-column prop="own" label="人工撰写" width="88" align="right" sortable />
              <el-table-column prop="adoption_rate" label="采纳率" width="84" align="right" sortable>
                <template #default="{ row }">{{ pct(row.adoption_rate) }}</template>
              </el-table-column>
            </el-table-column>
            <el-table-column label="其他处理" align="center">
              <el-table-column prop="marks" label="打标记" width="76" align="right" sortable />
              <el-table-column prop="aftersales" label="转售后" width="76" align="right" sortable />
              <el-table-column prop="handoffs" label="人工接管" width="88" align="right" sortable />
              <el-table-column prop="tickets" label="提交查件" width="88" align="right" sortable />
              <el-table-column prop="invoices" label="发发票" width="76" align="right" sortable />
            </el-table-column>
          </el-table>
          <div class="notes muted">
            口径：回复按客服提交到发件箱的时间统计（撤回的不算处理会话和响应时长）；响应时长 = 买家来信生成回复任务 → 客服提交回复；
            AI 采纳和该任务最后一版 AI 草稿比较（忽略空格差异）。只统计你负责的店铺。
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.pf {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 100%;
}
.bar {
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
}
.quick {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.quick button {
  height: 30px;
  padding: 0 14px;
  border: 0;
  background: var(--panel);
  font-family: inherit;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
}
.quick button + button {
  border-left: 1px solid var(--line);
}
.quick button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 200px;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}
.kpi {
  padding: 14px 16px;
  gap: 4px;
}
.k-label {
  font-size: 13px;
  color: var(--text-2);
}
.k-value {
  font-size: 26px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  line-height: 1.25;
}
.k-sub {
  font-size: 12px;
  color: var(--muted);
}
.good {
  color: var(--ok-text);
}
.ok {
  color: var(--warn-text);
}
.bad {
  color: var(--danger-text);
  font-weight: 600;
}
.legend {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-2);
}
.legend:first-of-type {
  margin-left: auto;
}
.sw {
  width: 10px;
  height: 10px;
  border-radius: 2px;
}
.sw.manual,
.seg.manual {
  background: var(--accent);
}
.sw.auto,
.seg.auto {
  background: #5b8def;
}
.chart {
  height: 200px;
  display: flex;
  align-items: stretch;
  gap: 4px;
  padding: 14px 16px 8px;
}
.col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  gap: 3px;
}
.num {
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  min-height: 14px;
}
.stack {
  width: min(70%, 34px);
  min-height: 1px;
  display: flex;
  flex-direction: column;
  border-radius: 3px 3px 0 0;
  overflow: hidden;
  background: var(--line-soft);
}
.seg {
  flex-basis: 0;
}
.day {
  height: 16px;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.table-card :deep(.el-table) {
  border-left: 0;
  border-right: 0;
}
.table-card :deep(.ai-row td) {
  background: #f4f7fe !important;
}
.actor {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.av {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.av.ai {
  background: #5b8def;
  font-size: 9px;
}
.notes {
  padding: 10px 14px 12px;
  line-height: 1.7;
}
</style>
