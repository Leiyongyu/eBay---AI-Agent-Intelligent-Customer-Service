<script setup lang="ts">
// 上线效果 → 质量评测：第一层规则评测 / 第二层 LLM 评审的趋势、人工修改回流（改进建议、知识缺口）、意图把握度校准
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, isAdmin, post } from '../api'
import { errorText } from '../format'

const tab = ref('trend')
const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const TRIGGER: Record<string, string> = { manual: '手动', startup: '启动', ci: 'CI', nightly: '每晚' }
const STATUS: Record<string, string> = { PROPOSED: '待确认', ACCEPTED: '已采纳', REJECTED: '不采纳' }

// ---- 评测趋势
const runs = ref<any[]>([])
const gate = ref<any>(null)
async function loadTrend() {
  ;[runs.value, gate.value] = await Promise.all([get<any[]>('/internal/eval/runs?limit=60'), get<any>('/internal/eval/gate')])
}
const rulesRuns = computed(() => runs.value.filter((r) => r.kind === 'rules').slice().reverse())
const judgeRuns = computed(() => runs.value.filter((r) => r.kind === 'judge').slice().reverse())
const bar = (v: number | null) => `${Math.max(2, Math.round((v ?? 0) * 100))}%`

// ---- 改进建议
const edits = ref<any>(null)
const suggestions = ref<any[]>([])
const generating = ref(false)
async function loadSuggestions() {
  ;[edits.value, suggestions.value] = await Promise.all([get<any>('/internal/eval/edits?days=7'), get<any[]>('/internal/eval/suggestions')])
}
async function generate() {
  generating.value = true
  try {
    await post('/internal/eval/suggestions/generate', {})
    await loadSuggestions()
    ElMessage.success('已生成本周建议')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    generating.value = false
  }
}
async function decide(s: any, status: string) {
  try {
    const { value } = await ElMessageBox.prompt(status === 'ACCEPTED' ? '采纳后改了哪里（提示词 / 知识条款编号）？' : '不采纳的原因（可不填）',
      STATUS[status], { inputValue: s.note || '', confirmButtonText: '确定', cancelButtonText: '取消' })
    Object.assign(s, await post(`/internal/eval/suggestions/${s.id}/decide`, { status, note: value || null }))
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(errorText(e))
  }
}

// ---- 知识缺口
const gapDays = ref(30)
const gaps = ref<any[]>([])
async function loadGaps() {
  gaps.value = await get<any[]>(`/internal/eval/knowledge-gaps?days=${gapDays.value}`)
}
function exportGaps() {
  window.open(`/internal/eval/knowledge-gaps/export?days=${gapDays.value}`, '_blank')
}

// ---- 置信度校准
const calDays = ref(30)
const cal = ref<any>(null)
async function loadCal() {
  cal.value = await get<any>(`/internal/eval/calibration?days=${calDays.value}`)
}
const BUCKET: Record<string, string> = { high: '高（明确）', medium: '中（结合上下文）', low: '低（看不准）', none: '没填' }

const loading = ref(false)
async function load() {
  loading.value = true
  try {
    if (tab.value === 'trend') await loadTrend()
    else if (tab.value === 'suggest') await loadSuggestions()
    else if (tab.value === 'gaps') await loadGaps()
    else await loadCal()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
watch(tab, load)
watch([gapDays], () => tab.value === 'gaps' && load())
watch([calDays], () => tab.value === 'cal' && load())
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="qp">
    <el-tabs v-model="tab">
      <el-tab-pane label="评测趋势" name="trend">
        <div v-if="gate" class="gate" :class="{ red: gate.red }">
          门槛 {{ pct(gate.threshold) }}<template v-if="gate.baseline"> · 基线 {{ pct(gate.baseline.pass_rate) }}（#{{ gate.baseline.id }}）</template>
          · 最近一次 <b>{{ pct(gate.last?.pass_rate) }}</b>
          <span v-if="gate.changed_since_last.length"> · 版本有变化：{{ gate.changed_since_last.join('、') }}（还没重跑）</span>
        </div>
        <div class="sub">第一层规则评测（黄金集 mock 回放，程序侧规则回归）</div>
        <div v-if="!rulesRuns.length" class="muted">还没有记录：在项目目录运行 python scripts/eval_rules.py</div>
        <div class="bars">
          <div v-for="r in rulesRuns" :key="r.id" class="bar-col" :title="`#${r.id} ${r.created_at} ${TRIGGER[r.trigger] || r.trigger} ${r.judged}/${r.cases} 条`">
            <div class="bar" :class="{ below: r.below, base: r.is_baseline }" :style="{ height: bar(r.pass_rate) }" />
            <div class="bar-label">{{ pct(r.pass_rate) }}</div>
            <div class="bar-date">{{ r.created_at?.slice(5, 10) }}</div>
          </div>
        </div>
        <div class="sub">第二层 LLM 评审（忠实度 / 语气 / 越权承诺，1–5 分，综合分 = 平均分 / 5）</div>
        <div v-if="!judgeRuns.length" class="muted">还没有记录（真实调用模型，要用户同意后再开 EVAL_JUDGE_ENABLED 或运行 scripts/eval_judge.py --live）</div>
        <el-table v-else :data="judgeRuns.slice().reverse()" size="small" border>
          <el-table-column label="时间" width="150"><template #default="{ row }">{{ row.created_at?.slice(0, 16).replace('T', ' ') }}</template></el-table-column>
          <el-table-column label="触发" width="70"><template #default="{ row }">{{ TRIGGER[row.trigger] || row.trigger }}</template></el-table-column>
          <el-table-column label="条数" width="70"><template #default="{ row }">{{ row.judged }}/{{ row.cases }}</template></el-table-column>
          <el-table-column label="综合" width="80"><template #default="{ row }">{{ pct(row.pass_rate) }}</template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="改进建议" name="suggest">
        <div v-if="edits" class="gate">
          近 7 天审核了 {{ edits.reviewed }} 条 AI 草稿，被改 {{ edits.edited }} 条（{{ pct(edits.edited_rate) }}）；
          <span v-for="(v, k) in edits.by_type" :key="k" class="gap">{{ v.label }} {{ v.n }}</span>
          <el-button v-if="isAdmin()" size="small" :loading="generating" class="right" @click="generate">生成本周建议</el-button>
        </div>
        <div class="muted small">每周一自动生成（规则，不调模型）；人工确认后才算采纳——采纳只是记录，提示词和知识库仍由人去改。</div>
        <el-table :data="suggestions" size="small" border class="mt">
          <el-table-column label="周" prop="week" width="100" />
          <el-table-column label="意图" width="130"><template #default="{ row }">{{ row.intent_label }}</template></el-table-column>
          <el-table-column label="修改类型" width="90"><template #default="{ row }">{{ row.edit_type_label }}</template></el-table-column>
          <el-table-column label="条数" prop="count" width="60" />
          <el-table-column label="建议">
            <template #default="{ row }">
              <div>{{ row.suggestion }}</div>
              <el-collapse v-if="row.examples.length">
                <el-collapse-item :title="`例子（${row.examples.length}）`">
                  <div v-for="(x, i) in row.examples" :key="i" class="ex">
                    <div><span class="muted">AI：</span>{{ x.draft }}</div>
                    <div><span class="muted">发出：</span>{{ x.final }}</div>
                    <div class="muted small">修改比例 {{ pct(x.edit_ratio) }} · 任务 {{ x.job_id }}</div>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <div :class="row.status === 'ACCEPTED' ? 'green' : row.status === 'REJECTED' ? 'muted' : ''">{{ STATUS[row.status] }}</div>
              <div v-if="row.note" class="muted small">{{ row.note }}</div>
              <template v-if="isAdmin()">
                <el-button size="small" link type="success" @click="decide(row, 'ACCEPTED')">采纳</el-button>
                <el-button size="small" link @click="decide(row, 'REJECTED')">不采纳</el-button>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="知识缺口" name="gaps">
        <div class="gate">
          知识库没命中（检索没结果 / 分数低 / 草稿没引用知识条款）且被人工大改的会话，近
          <el-select v-model="gapDays" size="small" style="width: 90px"><el-option v-for="d in [7, 30, 90]" :key="d" :label="`${d} 天`" :value="d" /></el-select>
          共 {{ gaps.length }} 条
          <el-button v-if="isAdmin() && gaps.length" size="small" type="primary" class="right" @click="exportGaps">导出成知识库 Excel</el-button>
        </div>
        <div class="muted small">导出的行“启用”为“否”：请把人工回复改写成通用说法、去掉个案信息后再到知识库导入并启用。</div>
        <el-table :data="gaps" size="small" border class="mt">
          <el-table-column label="意图" prop="intent" width="120" />
          <el-table-column label="买家问" prop="question" min-width="220" show-overflow-tooltip />
          <el-table-column label="人工回复（候选条款正文）" prop="answer" min-width="260" show-overflow-tooltip />
          <el-table-column label="修改比例" width="90"><template #default="{ row }">{{ pct(row.edit_ratio) }}</template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="置信度校准" name="cal">
        <template v-if="cal">
          <div class="gate">
            近 <el-select v-model="calDays" size="small" style="width: 90px"><el-option v-for="d in [7, 30, 90]" :key="d" :label="`${d} 天`" :value="d" /></el-select>
            审核了 {{ cal.reviewed }} 条；
            <span v-if="cal.monotonic === true" class="green">把握度越高被改得越少（可信）</span>
            <span v-else-if="cal.monotonic === false" class="red">高把握度反而改得更多：模型自报的把握度不可信</span>
            <span v-else class="muted">样本不够，看不出</span>
          </div>
          <el-table :data="Object.entries(cal.buckets).map(([k, v]: any) => ({ k, ...v }))" size="small" border class="mt">
            <el-table-column label="意图把握度" width="160"><template #default="{ row }">{{ BUCKET[row.k] }}</template></el-table-column>
            <el-table-column label="条数" prop="n" width="80" />
            <el-table-column :label="`被改（≥${pct(cal.edit_threshold)}）`"><template #default="{ row }">{{ pct(row.edited_rate) }}</template></el-table-column>
            <el-table-column :label="`大改（≥${pct(cal.heavy_threshold)}）`"><template #default="{ row }">{{ pct(row.heavy_rate) }}</template></el-table-column>
            <el-table-column label="原样发出"><template #default="{ row }">{{ pct(row.unchanged_rate) }}</template></el-table-column>
            <el-table-column label="平均修改比例"><template #default="{ row }">{{ pct(row.avg_edit_ratio) }}</template></el-table-column>
          </el-table>
          <div class="sub">自动发送候选：{{ cal.auto_send_candidates.join('、') || '暂无' }}</div>
          <div class="muted small">{{ cal.rule }}</div>
          <el-table :data="Object.entries(cal.per_intent).map(([k, v]: any) => ({ k, ...v }))" size="small" border class="mt">
            <el-table-column label="意图" prop="k" width="150" />
            <el-table-column v-for="b in ['high', 'medium', 'low', 'none']" :key="b" :label="BUCKET[b]">
              <template #default="{ row }">
                <span v-if="row[b]">{{ row[b].n }} 条 · 被改 {{ pct(row[b].edited_rate) }}</span><span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.qp { padding: 4px 12px 16px; }
.gate { padding: 8px 12px; background: var(--el-fill-color-light); border-radius: 4px; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.gate.red { background: var(--el-color-danger-light-9); }
.right { margin-left: auto; }
.sub { font-weight: 600; margin: 14px 0 6px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.mt { margin-top: 8px; }
.gap { margin-right: 10px; }
.red { color: var(--el-color-danger); }
.green { color: var(--el-color-success); }
.ex { border-top: 1px dashed var(--el-border-color-lighter); padding: 6px 0; }
.bars { display: flex; align-items: flex-end; gap: 6px; height: 160px; padding: 8px 0; overflow-x: auto; }
.bar-col { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; min-width: 38px; }
.bar { width: 22px; background: var(--el-color-primary-light-5); border-radius: 3px 3px 0 0; }
.bar.below { background: var(--el-color-danger-light-5); }
.bar.base { outline: 2px solid var(--el-color-success); }
.bar-label { font-size: 11px; }
.bar-date { font-size: 11px; color: var(--el-text-color-secondary); }
</style>
