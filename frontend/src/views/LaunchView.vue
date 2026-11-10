<script setup lang="ts">
// 绩效管理 → 上线效果：智能客服上线后的来信与回复、AI 草稿效果、客服效率、售后与物流、大模型用量。
// 数据全部读库（/api/performance/launch），每个指标下面写着口径；只统计当前账号负责的店铺
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, isAdmin, post } from '../api'
import { errorText } from '../format'
import type { Store } from '../types'

interface Dist { key: string; label: string; count: number; share: number | null }
interface Staff {
  actor: string; reviewed: number; approved: number; edited: number; manual: number; rejected: number; no_reply: number
  submitted: number; recalled: number; failed: number; median_handling_seconds: number | null; avg_handling_seconds: number | null
}
interface Llm { purpose: string; label: string; calls: number; failed: number; input_tokens: number; output_tokens: number; total_tokens: number; estimated: number }
interface Launch {
  start: string; end: string; notes: Record<string, string>
  messages: {
    buyer_messages: number; conversations: number; replied_conversations: number; reply_rounds: number; replied_rounds: number
    first_response_median: number | null; first_response_p90: number | null; within_24h_rate: number | null
    unreplied_now: number; unreplied_over_24h: number; sent_via_system: Dist[]; sent_via_system_total: number
    days: { date: string; buyer: number; replies: number }[]
  }
  ai: {
    jobs: number; drafted: number; drafted_rate: number | null; review: Dist[]; review_sent_total: number
    adoption_rate: number | null; avg_edit_ratio: number | null; no_draft: Dist[]; no_draft_total: number
    intents: Dist[]; handoff_reasons: Dist[]
  }
  staff: Staff[]
  aftersales: {
    tickets: { count: number; replied: number; avg_reply_seconds: number | null; median_reply_seconds: number | null; results_sent: number; origin: Dist[] }
    feishu: { count: number; group_replied: number; avg_group_reply_seconds: number | null; avg_answer_seconds: number | null; answered: number; categories: Dist[] }
    parcels: { new: number; auto_new: number; done: number; open_now: number; avg_days_to_close: number | null; reasons: Dist[] }
    invoices: { count: number; auto: number; sent: number; failed: number; status: Dist[] }
  }
  llm: { by_purpose: Llm[]; total: { calls: number; failed: number; input_tokens: number; output_tokens: number; total_tokens: number } }
  quality: Quality
  baseline: {
    available: boolean; status: 'READY' | 'RUNNING' | 'NONE'; comparison: CompareRow[]
    data: (Quality & { period_start: string; period_end: string; source: 'snapshot' | 'raw'; computed_at: string | null }) | null
  }
}

interface Band { key: string; label: string; rounds: number; replied: number; median_seconds: number | null; p90_seconds: number | null }
interface Quality {
  rounds: number; replied_rounds: number; bands: Band[]
  on_time: { rate: number | null; on_time: number; late: number; observing: number; sla_hours: number }
  follow_up: { rate: number | null; followed: number; not_followed: number; observing: number; hours: number }
  escalation: { rate: number | null; escalated: number; not_escalated: number; observing: number; days: number }
  north_star: { rate: number | null; good: number; bad: number; observing: number }
  night_backlog: { count: number; pending: number; off_hours_rounds: number; rate: number | null; per_week: number | null }
  volume: { total: number; per_day: number | null; by_store: { key: string; count: number }[]; by_hour: { hour: number; count: number }[]
    by_weekday: { key: string; count: number }[] }
}
interface CompareRow {
  key: string; label: string; unit: 'seconds' | 'rate' | 'count'; baseline: number | null; live: number | null
  change: number | null; target: string; met: boolean | null; baseline_note: string | null
  baseline_sample: number | null; live_sample: number | null; small_sample: boolean; sample_note: string | null
  data_note?: string | null
}

const LAUNCH_DAY = '2026-11-09' // 正式上线日
const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
function back(days: number) {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d
}
type Quick = 'today' | '7' | '30' | 'launch' | 'custom'
const QUICKS: { key: Quick; label: string }[] = [
  { key: 'today', label: '今天' }, { key: '7', label: '近7天' }, { key: '30', label: '近30天' }, { key: 'launch', label: '上线以来' },
]
const quick = ref<Quick>('7')
const range = ref<[string, string]>([ymd(back(6)), ymd(new Date())])
function pick(q: Quick) {
  quick.value = q
  const t = ymd(new Date())
  if (q === 'today') range.value = [t, t]
  else if (q === '7') range.value = [ymd(back(6)), t]
  else if (q === '30') range.value = [ymd(back(29)), t]
  else if (q === 'launch') range.value = [LAUNCH_DAY < t ? LAUNCH_DAY : t, t]
}
function onRange(v: [string, string] | null) {
  if (!v) return pick('7')
  quick.value = 'custom'
  range.value = v
}

const stores = ref<Store[]>([])
const storeId = ref('')
const data = ref<Launch | null>(null)
const loading = ref(false)
async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ start: range.value[0], end: range.value[1] })
    if (storeId.value) q.set('store_id', storeId.value)
    data.value = await get<Launch>(`/api/performance/launch?${q}`)
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
  if (s < 60) return `${Math.round(s)}秒`
  if (s < 3600) return `${Math.round(s / 60)}分钟`
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h >= 48 ? `${(s / 86400).toFixed(1)}天` : `${h}小时${m ? `${m}分` : ''}`
}
const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(v === 1 || v === 0 ? 0 : 1)}%`)
const num = (v: number | null | undefined) => (v == null ? '—' : v.toLocaleString('zh-CN'))
const note = (k: string) => data.value?.notes[k] || ''
const barMax = (rows: Dist[]) => Math.max(1, ...rows.map((r) => r.count))

// ---- 第 2 章指标与基线对比 ----
const storeName = (id: string) => stores.value.find((s) => s.id === id)?.name || id
function cell(r: CompareRow, v: number | null) {
  if (v == null) return '—'
  return r.unit === 'seconds' ? dur(v) : r.unit === 'rate' ? pct(v) : num(v)
}
function changeText(r: CompareRow) {
  if (r.change == null) return '—'
  const v = (r.change * 100).toFixed(Math.abs(r.change) >= 0.1 ? 0 : 1)
  return `${r.change > 0 ? '+' : ''}${v}%`
}
const hourMax = computed(() => Math.max(1, ...(data.value?.quality.volume.by_hour || []).map((h) => h.count)))
const BASELINE_STATUS: Record<string, string> = { NONE: '基线未采集', RUNNING: '基线采集中' }
// 管理员：补拉新批次或售后单后重新计算基线快照（原始消息被清理后不会覆盖）
const refreshing = ref(false)
async function refreshBaseline() {
  refreshing.value = true
  try {
    const r = await post<{ results: { store_id: string; action: string; reason?: string | null }[] }>('/api/performance/baseline/refresh')
    const kept = r.results.filter((x) => x.reason)
    if (kept.length) ElMessage.warning(`部分基线原始消息已被清理，保留原快照（${kept.length} 份）`)
    else ElMessage.success(`已重新计算 ${r.results.filter((x) => x.action === 'computed').length} 份基线快照`)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    refreshing.value = false
  }
}

// ---- 每日来信 / 回复（并排柱） ----
const maxDay = computed(() => Math.max(1, ...(data.value?.messages.days || []).flatMap((d) => [d.buyer, d.replies])))
const dayLabel = (s: string) => s.slice(5).replace('-', '/')
const showEvery = computed(() => Math.max(1, Math.ceil((data.value?.messages.days.length || 1) / 15)))
</script>

<template>
  <div class="lv">
    <!-- 筛选 -->
    <div class="bar panel">
      <div class="quick">
        <button v-for="q in QUICKS" :key="q.key" :class="{ on: quick === q.key }" @click="pick(q.key)">{{ q.label }}</button>
      </div>
      <el-date-picker :model-value="range" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                      start-placeholder="开始日期" end-placeholder="结束日期" :clearable="false" class="range"
                      @update:model-value="onRange" />
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable class="store">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <span class="spacer" />
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div v-loading="loading && !data" class="body">
      <template v-if="data">
        <!-- ======== 来信与回复 ======== -->
        <section class="panel sec">
          <div class="panel-head">来信与回复<span class="muted">{{ data.start }} 至 {{ data.end }}</span></div>
          <div class="kpis">
            <div class="kpi"><div class="k-label">买家来信</div><div class="k-value">{{ num(data.messages.buyer_messages) }}</div>
              <div class="k-sub">{{ note('buyer_messages') }}</div></div>
            <div class="kpi"><div class="k-label">来信会话</div><div class="k-value">{{ num(data.messages.conversations) }}</div>
              <div class="k-sub">{{ note('conversations') }}</div></div>
            <div class="kpi"><div class="k-label">已回复会话</div><div class="k-value">{{ num(data.messages.replied_conversations) }}</div>
              <div class="k-sub">{{ note('replied_conversations') }}</div></div>
            <div class="kpi"><div class="k-label">首次回复（中位数 / P90）</div>
              <div class="k-value">{{ dur(data.messages.first_response_median) }}<span class="k-small"> / {{ dur(data.messages.first_response_p90) }}</span></div>
              <div class="k-sub">{{ note('first_response') }}（{{ data.messages.replied_rounds }} / {{ data.messages.reply_rounds }} 轮已回复）</div></div>
            <div class="kpi"><div class="k-label">24 小时内回复率</div><div class="k-value">{{ pct(data.messages.within_24h_rate) }}</div>
              <div class="k-sub">{{ note('within_24h') }}</div></div>
            <div class="kpi"><div class="k-label">当前待回复</div>
              <div class="k-value" :class="{ bad: data.messages.unreplied_now }">{{ num(data.messages.unreplied_now) }}</div>
              <div class="k-sub">{{ note('unreplied_now') }}；其中超过 24 小时 {{ data.messages.unreplied_over_24h }} 个</div></div>
          </div>

          <div class="sub-head">每日来信与回复
            <span class="legend"><i class="sw buyer" />买家来信</span><span class="legend"><i class="sw reply" />店铺回复</span>
          </div>
          <div class="chart" role="img" :aria-label="`${data.start} 至 ${data.end} 每日买家来信与店铺回复`">
            <div v-for="(d, i) in data.messages.days" :key="d.date" class="col" :title="`${d.date}：来信 ${d.buyer}，回复 ${d.replies}`">
              <div class="pair">
                <div class="b buyer" :style="{ height: `${(d.buyer / maxDay) * 100}%` }" />
                <div class="b reply" :style="{ height: `${(d.replies / maxDay) * 100}%` }" />
              </div>
              <span class="day">{{ i % showEvery === 0 ? dayLabel(d.date) : '' }}</span>
            </div>
          </div>

          <div class="sub-head">经系统发出的消息<span class="muted">共 {{ data.messages.sent_via_system_total }} 条</span></div>
          <div class="note muted">{{ note('sent_via_system') }}</div>
          <div class="dist">
            <div v-for="r in data.messages.sent_via_system" :key="r.key" class="drow">
              <span class="dl">{{ r.label }}</span>
              <span class="track"><i :style="{ width: `${(r.count / barMax(data.messages.sent_via_system)) * 100}%` }" /></span>
              <span class="dn">{{ r.count }}</span>
            </div>
            <div v-if="!data.messages.sent_via_system.length" class="muted empty">这段时间没有经系统发出的消息</div>
          </div>
        </section>

        <!-- ======== AI 草稿效果 ======== -->
        <section class="panel sec">
          <div class="panel-head">AI 草稿效果</div>
          <div class="kpis">
            <div class="kpi"><div class="k-label">回复任务</div><div class="k-value">{{ num(data.ai.jobs) }}</div>
              <div class="k-sub">{{ note('jobs') }}</div></div>
            <div class="kpi"><div class="k-label">AI 起草率</div><div class="k-value">{{ pct(data.ai.drafted_rate) }}</div>
              <div class="k-sub">{{ note('drafted') }}（{{ data.ai.drafted }} 个起草，{{ data.ai.no_draft_total }} 个没起草）</div></div>
            <div class="kpi"><div class="k-label">AI 草稿采纳率</div><div class="k-value">{{ pct(data.ai.adoption_rate) }}</div>
              <div class="k-sub">{{ note('adoption_rate') }}</div></div>
            <div class="kpi"><div class="k-label">平均修改比例</div><div class="k-value">{{ pct(data.ai.avg_edit_ratio) }}</div>
              <div class="k-sub">{{ note('avg_edit_ratio') }}</div></div>
          </div>
          <div class="grid2">
            <div>
              <div class="sub-head">审核后的发出方式<span class="muted">共 {{ data.ai.review_sent_total }} 条发出</span></div>
              <div class="note muted">{{ note('review') }}</div>
              <div class="dist">
                <div v-for="r in data.ai.review" :key="r.key" class="drow">
                  <span class="dl">{{ r.label }}</span>
                  <span class="track"><i :style="{ width: `${(r.count / barMax(data.ai.review)) * 100}%` }" /></span>
                  <span class="dn">{{ r.count }}<em v-if="r.share != null">{{ pct(r.share) }}</em></span>
                </div>
              </div>
            </div>
            <div>
              <div class="sub-head">AI 没起草的原因</div>
              <div class="note muted">{{ note('no_draft') }}</div>
              <div class="dist">
                <div v-for="r in data.ai.no_draft" :key="r.key" class="drow">
                  <span class="dl" :title="r.label">{{ r.label }}</span>
                  <span class="track"><i :style="{ width: `${(r.count / barMax(data.ai.no_draft)) * 100}%` }" /></span>
                  <span class="dn">{{ r.count }}</span>
                </div>
                <div v-if="!data.ai.no_draft.length" class="muted empty">这段时间的任务 AI 都起草了</div>
              </div>
            </div>
            <div>
              <div class="sub-head">买家问题类型</div>
              <div class="note muted">{{ note('intents') }}</div>
              <div class="dist">
                <div v-for="r in data.ai.intents" :key="r.key" class="drow">
                  <span class="dl">{{ r.label }}</span>
                  <span class="track"><i :style="{ width: `${(r.count / barMax(data.ai.intents)) * 100}%` }" /></span>
                  <span class="dn">{{ r.count }}<em>{{ pct(r.share) }}</em></span>
                </div>
                <div v-if="!data.ai.intents.length" class="muted empty">暂无</div>
              </div>
            </div>
            <div>
              <div class="sub-head">需要人工审核的原因</div>
              <div class="note muted">{{ note('handoff_reasons') }}</div>
              <div class="dist">
                <div v-for="r in data.ai.handoff_reasons" :key="r.key" class="drow">
                  <span class="dl" :title="r.label">{{ r.label }}</span>
                  <span class="track"><i :style="{ width: `${(r.count / barMax(data.ai.handoff_reasons)) * 100}%` }" /></span>
                  <span class="dn">{{ r.count }}<em>{{ pct(r.share) }}</em></span>
                </div>
                <div v-if="!data.ai.handoff_reasons.length" class="muted empty">暂无</div>
              </div>
            </div>
          </div>
        </section>

        <!-- ======== 客服效率 ======== -->
        <section class="panel sec">
          <div class="panel-head">客服效率</div>
          <div class="note muted">{{ note('staff') }}</div>
          <el-table :data="data.staff" border size="small" empty-text="这段时间没有客服审核或发送记录">
            <el-table-column prop="actor" label="客服" min-width="100" fixed />
            <el-table-column prop="reviewed" label="审核草稿" width="84" align="right" sortable />
            <el-table-column prop="approved" label="原样发" width="74" align="right" sortable />
            <el-table-column prop="edited" label="改后发" width="74" align="right" sortable />
            <el-table-column prop="manual" label="人工撰写" width="84" align="right" sortable />
            <el-table-column prop="rejected" label="拒绝" width="64" align="right" sortable />
            <el-table-column prop="median_handling_seconds" label="处理时长(中位)" width="116" align="right" sortable>
              <template #default="{ row }">{{ dur(row.median_handling_seconds) }}</template>
            </el-table-column>
            <el-table-column prop="submitted" label="提交发送" width="84" align="right" sortable />
            <el-table-column prop="recalled" label="撤回" width="64" align="right" sortable />
            <el-table-column prop="failed" label="发送失败" width="84" align="right" sortable>
              <template #default="{ row }"><span :class="{ bad: row.failed }">{{ row.failed }}</span></template>
            </el-table-column>
          </el-table>
        </section>

        <!-- ======== 售后与物流 ======== -->
        <section class="panel sec">
          <div class="panel-head">售后与物流</div>
          <div class="kpis">
            <div class="kpi"><div class="k-label">谷仓查件单</div><div class="k-value">{{ num(data.aftersales.tickets.count) }}</div>
              <div class="k-sub">平均回复 {{ dur(data.aftersales.tickets.avg_reply_seconds) }} · 中位 {{ dur(data.aftersales.tickets.median_reply_seconds) }}；
                已回复 {{ data.aftersales.tickets.replied }}，结果已发买家 {{ data.aftersales.tickets.results_sent }}<br>{{ note('tickets') }}</div></div>
            <div class="kpi"><div class="k-label">飞书售后工单</div><div class="k-value">{{ num(data.aftersales.feishu.count) }}</div>
              <div class="k-sub">群内答复 {{ dur(data.aftersales.feishu.avg_group_reply_seconds) }} · 回复买家 {{ dur(data.aftersales.feishu.avg_answer_seconds) }}
                <br>{{ note('feishu') }}</div></div>
            <div class="kpi"><div class="k-label">异常包裹跟进</div>
              <div class="k-value">{{ num(data.aftersales.parcels.new) }}<span class="k-small"> 新增</span></div>
              <div class="k-sub">完结 {{ data.aftersales.parcels.done }} · 跟进中 {{ data.aftersales.parcels.open_now }} ·
                平均 {{ data.aftersales.parcels.avg_days_to_close ?? '—' }} 天；系统自动建 {{ data.aftersales.parcels.auto_new }}<br>{{ note('parcels') }}</div></div>
            <div class="kpi"><div class="k-label">发票</div><div class="k-value">{{ num(data.aftersales.invoices.count) }}</div>
              <div class="k-sub">AI 自动 {{ data.aftersales.invoices.auto }} · 已发邮件 {{ data.aftersales.invoices.sent }} ·
                发送失败 {{ data.aftersales.invoices.failed }}<br>{{ note('invoices') }}</div></div>
          </div>
        </section>

        <!-- ======== 回复质量（开发方案第 2 章） ======== -->
        <section class="panel sec">
          <div class="panel-head">回复质量<span class="muted">开发方案第 2 章指标 · 共 {{ data.quality.rounds }} 轮来信</span></div>
          <div class="kpis">
            <div class="kpi"><div class="k-label">按时且有效（北极星）</div><div class="k-value">{{ pct(data.quality.north_star.rate) }}</div>
              <div class="k-sub">达标 {{ data.quality.north_star.good }} · 不达标 {{ data.quality.north_star.bad }} · 观察中 {{ data.quality.north_star.observing }}
                <br>{{ note('north_star') }}</div></div>
            <div class="kpi"><div class="k-label">{{ data.quality.on_time.sla_hours }} 小时内首次回复</div><div class="k-value">{{ pct(data.quality.on_time.rate) }}</div>
              <div class="k-sub">按时 {{ data.quality.on_time.on_time }} · 超时 {{ data.quality.on_time.late }} · 观察中 {{ data.quality.on_time.observing }}
                <br>{{ note('on_time') }}</div></div>
            <div class="kpi"><div class="k-label">买家追问率</div><div class="k-value">{{ pct(data.quality.follow_up.rate) }}</div>
              <div class="k-sub">追问 {{ data.quality.follow_up.followed }} · 没追问 {{ data.quality.follow_up.not_followed }} · 观察中 {{ data.quality.follow_up.observing }}
                <br>{{ note('follow_up') }}</div></div>
            <div class="kpi"><div class="k-label">沟通后升级率</div><div class="k-value">{{ pct(data.quality.escalation.rate) }}</div>
              <div class="k-sub">升级 {{ data.quality.escalation.escalated }} · 没升级 {{ data.quality.escalation.not_escalated }} · 观察中 {{ data.quality.escalation.observing }}
                <br>{{ note('escalation') }}</div></div>
            <div class="kpi"><div class="k-label">非工作时段积压</div><div class="k-value">{{ num(data.quality.night_backlog.count) }}<span class="k-small"> 轮</span></div>
              <div class="k-sub">占非工作时段来信 {{ pct(data.quality.night_backlog.rate) }} · 每周 {{ data.quality.night_backlog.per_week ?? '—' }} 轮 ·
                非工作时段到达还没回 {{ data.quality.night_backlog.pending }}<br>{{ note('night_backlog') }}</div></div>
          </div>
          <div class="sub-head">首次响应（按到达时段）</div>
          <div class="note muted">{{ note('bands') }}</div>
          <el-table :data="data.quality.bands" border size="small">
            <el-table-column prop="label" label="时段" min-width="190" />
            <el-table-column prop="rounds" label="来信轮次" width="90" align="right" />
            <el-table-column prop="replied" label="已回复" width="80" align="right" />
            <el-table-column label="中位数" width="110" align="right"><template #default="{ row }">{{ dur(row.median_seconds) }}</template></el-table-column>
            <el-table-column label="P90" width="110" align="right"><template #default="{ row }">{{ dur(row.p90_seconds) }}</template></el-table-column>
          </el-table>
          <div class="sub-head">买家消息量<span class="muted">共 {{ num(data.quality.volume.total) }} 条 · 日均 {{ data.quality.volume.per_day ?? '—' }}</span></div>
          <div class="note muted">{{ note('volume') }}</div>
          <div class="hours" role="img" aria-label="买家消息按北京时间每小时分布">
            <div v-for="h in data.quality.volume.by_hour" :key="h.hour" class="hcol" :title="`${h.hour}:00–${h.hour + 1}:00：${h.count} 条`">
              <div class="hbar" :class="{ work: h.hour >= 9 && h.hour < 18 }" :style="{ height: `${(h.count / hourMax) * 100}%` }" />
              <span class="day">{{ h.hour % 3 === 0 ? h.hour : '' }}</span>
            </div>
          </div>
          <div class="grid2">
            <div class="dist">
              <div v-for="w in data.quality.volume.by_weekday" :key="w.key" class="drow">
                <span class="dl">{{ w.key }}</span>
                <span class="track"><i :style="{ width: `${(w.count / Math.max(1, ...data.quality.volume.by_weekday.map((x) => x.count))) * 100}%` }" /></span>
                <span class="dn">{{ w.count }}</span>
              </div>
            </div>
            <div class="dist">
              <div v-for="st in data.quality.volume.by_store" :key="st.key" class="drow">
                <span class="dl" :title="storeName(st.key)">{{ storeName(st.key) }}</span>
                <span class="track"><i :style="{ width: `${(st.count / Math.max(1, data.quality.volume.by_store[0]?.count || 1)) * 100}%` }" /></span>
                <span class="dn">{{ st.count }}</span>
              </div>
              <div v-if="!data.quality.volume.by_store.length" class="muted empty">这段时间没有买家消息</div>
            </div>
          </div>
        </section>

        <!-- ======== 上线前基线 vs 上线后 ======== -->
        <section class="panel sec">
          <div class="panel-head">上线前基线 vs 上线后
            <span v-if="data.baseline.data" class="muted">基线 {{ data.baseline.data.period_start }} 至 {{ data.baseline.data.period_end }}（{{ data.baseline.data.rounds }} 轮，
              {{ data.baseline.data.source === 'snapshot' ? `快照 ${data.baseline.data.computed_at?.slice(0, 10) || ''}` : '现算' }}）·
              上线后 {{ data.start }} 至 {{ data.end }}</span>
            <el-tag v-else size="small" type="info" effect="plain">{{ BASELINE_STATUS[data.baseline.status] || '基线未采集' }}</el-tag>
            <el-button v-if="isAdmin() && data.baseline.data" size="small" class="refresh-base" :loading="refreshing" @click="refreshBaseline">
              重新计算基线
            </el-button>
          </div>
          <div class="note muted">{{ note('baseline') }}</div>
          <el-table :data="data.baseline.comparison" border size="small">
            <el-table-column label="指标" min-width="200" fixed>
              <template #default="{ row }">{{ row.label }}<div v-if="row.data_note" class="small-sample">{{ row.data_note }}</div></template>
            </el-table-column>
            <el-table-column label="基线" width="150" align="right">
              <template #default="{ row }"><span v-if="row.baseline_note && row.baseline == null" class="muted">{{ row.baseline_note }}</span>
                <template v-else>{{ cell(row, row.baseline) }}</template></template>
            </el-table-column>
            <el-table-column label="上线后" width="110" align="right"><template #default="{ row }">{{ cell(row, row.live) }}</template></el-table-column>
            <el-table-column label="变化" width="84" align="right"><template #default="{ row }">{{ changeText(row) }}</template></el-table-column>
            <el-table-column label="样本（基线 / 上线后）" width="150" align="right">
              <template #default="{ row }">
                {{ row.baseline_sample ?? '—' }} / {{ row.live_sample ?? '—' }}
                <div v-if="row.sample_note" class="small-sample">{{ row.sample_note }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="target" label="第 2.2 节目标" min-width="150" />
            <el-table-column label="是否达到" width="90" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.met === true" size="small" type="success">达到</el-tag>
                <el-tag v-else-if="row.met === false" size="small" type="danger">未达到</el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </section>

        <!-- ======== 大模型用量 ======== -->
        <section class="panel sec">
          <div class="panel-head">大模型用量
            <span class="muted">共 {{ num(data.llm.total.calls) }} 次调用 · {{ num(data.llm.total.total_tokens) }} token · 失败 {{ data.llm.total.failed }} 次</span>
          </div>
          <div class="note muted">{{ note('llm') }}；费用金额暂无数据来源（系统里没有各模型单价）。</div>
          <el-table :data="data.llm.by_purpose" border size="small" empty-text="这段时间没有调用大模型">
            <el-table-column prop="label" label="用途" min-width="110" fixed />
            <el-table-column prop="calls" label="调用次数" width="90" align="right" sortable />
            <el-table-column prop="failed" label="失败" width="70" align="right" sortable>
              <template #default="{ row }"><span :class="{ bad: row.failed }">{{ row.failed }}</span></template>
            </el-table-column>
            <el-table-column prop="input_tokens" label="输入 token" width="110" align="right" sortable>
              <template #default="{ row }">{{ num(row.input_tokens) }}</template>
            </el-table-column>
            <el-table-column prop="output_tokens" label="输出 token" width="110" align="right" sortable>
              <template #default="{ row }">{{ num(row.output_tokens) }}</template>
            </el-table-column>
            <el-table-column prop="total_tokens" label="合计 token" width="116" align="right" sortable>
              <template #default="{ row }">{{ num(row.total_tokens) }}<span v-if="row.estimated" class="muted">（含估算）</span></template>
            </el-table-column>
          </el-table>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.lv {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 100%;
  min-width: 0;
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
  flex-wrap: wrap;
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
.range {
  width: 260px;
  max-width: 100%;
}
.store {
  width: 190px;
  max-width: 100%;
}
.spacer {
  flex: 1;
}
.body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 200px;
  min-width: 0;
}
.sec {
  min-width: 0;
  padding-bottom: 12px;
}
.sec > .note,
.sec :deep(.el-table) {
  margin: 0 14px;
}
.sec :deep(.el-table) {
  width: auto;
  max-width: calc(100% - 28px);
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px;
  padding: 12px 14px 4px;
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  background: var(--sunken);
  min-width: 0;
}
.k-label {
  font-size: 13px;
  color: var(--text-2);
}
.k-value {
  font-size: 24px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  line-height: 1.25;
}
.k-small {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-2);
}
.k-sub {
  font-size: 12px;
  line-height: 1.55;
  color: var(--muted);
}
.bad {
  color: var(--danger-text);
  font-weight: 600;
}
.sub-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 14px 14px 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.sub-head .muted {
  font-weight: 400;
}
.note {
  font-size: 12px;
  line-height: 1.6;
  padding: 0 14px 6px;
}
.sec > .note {
  padding: 0 0 8px;
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
.sw.buyer,
.b.buyer {
  background: #5b8def;
}
.sw.reply,
.b.reply {
  background: var(--accent);
}
.chart {
  height: 180px;
  display: flex;
  align-items: stretch;
  gap: 3px;
  padding: 8px 14px 4px;
  min-width: 0;
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
.pair {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  gap: 2px;
}
.b {
  width: min(40%, 16px);
  min-height: 1px;
  border-radius: 2px 2px 0 0;
}
.day {
  height: 16px;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.grid2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 0 18px;
  min-width: 0;
}
.dist {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 4px 14px 6px;
}
.drow {
  display: grid;
  grid-template-columns: minmax(0, 11em) minmax(40px, 1fr) auto;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.dl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-2);
}
.track {
  height: 10px;
  border-radius: 5px;
  background: var(--line-soft);
  overflow: hidden;
}
.track i {
  display: block;
  height: 100%;
  min-width: 2px;
  border-radius: 5px;
  background: var(--accent);
}
.dn {
  font-variant-numeric: tabular-nums;
  color: var(--text);
  white-space: nowrap;
}
.dn em {
  font-style: normal;
  margin-left: 6px;
  font-size: 12px;
  color: var(--muted);
}
.empty {
  font-size: 12px;
}
.refresh-base {
  margin-left: auto;
}
.small-sample {
  font-size: 11px;
  color: var(--warn-text);
}
.hours {
  height: 120px;
  display: flex;
  align-items: stretch;
  gap: 2px;
  padding: 8px 14px 4px;
  min-width: 0;
}
.hcol {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  gap: 3px;
}
.hbar {
  width: 70%;
  min-height: 1px;
  border-radius: 2px 2px 0 0;
  background: #5b8def;
}
.hbar.work {
  background: var(--accent);
}
@media (max-width: 640px) {
  .bar {
    padding: 10px;
  }
  .kpis {
    grid-template-columns: minmax(0, 1fr);
  }
  .grid2 {
    grid-template-columns: minmax(0, 1fr);
  }
  .drow {
    grid-template-columns: minmax(0, 8em) minmax(30px, 1fr) auto;
  }
}
</style>
