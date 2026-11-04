<script setup lang="ts">
// 用量统计：模型调用次数、token、耗时，按日期 / 用途 / 模型 / 店铺 / 会话汇总（数据来自 llm_usage 表）
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { errorText, fmtTime } from '../format'
import type { Store, UsageSummary } from '../types'

const emit = defineEmits<{ open: [id: number] }>()

const RANGES = [
  { key: 1, label: '今天' },
  { key: 7, label: '近 7 天' },
  { key: 30, label: '近 30 天' },
  { key: 90, label: '近 90 天' },
  { key: 0, label: '自定义' },
]
const PURPOSE_COLORS: Record<string, string> = {
  reply: '#246b6b',
  repair: '#5b7fa6',
  vision: '#c9776b',
  translate: '#8fbc8f',
  embedding_query: '#d9a85b',
  embedding_docs: '#a8dadc',
  check: '#b4bcb9',
}

const range = ref(7)
const custom = ref<[string, string] | null>(null)
const storeId = ref('')
const stores = ref<Store[]>([])
const data = ref<UsageSummary | null>(null)
const loading = ref(false)

const fmt = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('zh-CN'))
function short(n: number) {
  if (n >= 1e8) return `${(n / 1e8).toFixed(1)} 亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(1)} 万`
  return String(n)
}

async function load() {
  const q = new URLSearchParams()
  if (range.value === 0) {
    if (!custom.value) return
    q.set('start', custom.value[0])
    q.set('end', custom.value[1])
  } else {
    q.set('days', String(range.value))
  }
  if (storeId.value) q.set('store_id', storeId.value)
  loading.value = true
  try {
    data.value = await get<UsageSummary>(`/api/usage/summary?${q}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

const purposes = computed(() => Object.keys(data.value?.purposes || {}))
const usedPurposes = computed(() => purposes.value.filter((p) => data.value?.by_day.some((d) => d.by_purpose[p])))
const maxDay = computed(() => Math.max(1, ...(data.value?.by_day.map((d) => d.total_tokens) || [0])))
// 日期太多时隔几天标一次
const labelEvery = computed(() => Math.max(1, Math.ceil((data.value?.by_day.length || 1) / 15)))
const peakDay = computed(() => data.value?.by_day.reduce((a, b) => (b.total_tokens > a.total_tokens ? b : a), data.value.by_day[0]))

function dayTip(d: UsageSummary['by_day'][number]) {
  const parts = purposes.value.filter((p) => d.by_purpose[p]).map((p) => `${data.value!.purposes[p]} ${fmt(d.by_purpose[p])}`)
  return `${d.date}：${fmt(d.total_tokens)} token，${d.calls} 次调用${d.failed ? `（失败 ${d.failed}）` : ''}${parts.length ? '\n' + parts.join('\n') : ''}`
}

watch([range, custom, storeId], load)
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores')
  load()
})
</script>

<template>
  <div class="usage">
    <div class="panel bar">
      <el-radio-group v-model="range" size="small">
        <el-radio-button v-for="r in RANGES" :key="r.key" :value="r.key">{{ r.label }}</el-radio-button>
      </el-radio-group>
      <el-date-picker v-if="range === 0" v-model="custom" type="daterange" size="small" value-format="YYYY-MM-DD"
        start-placeholder="开始日期" end-placeholder="结束日期" style="width: 240px" />
      <el-select v-model="storeId" size="small" filterable clearable placeholder="全部店铺" style="width: 220px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <span v-if="data" class="muted">{{ data.range.start }} 至 {{ data.range.end }}（按本机日期）</span>
    </div>

    <template v-if="data">
      <div class="kpis">
        <div class="panel kpi">
          <div class="k-label">总 token</div>
          <div class="k-value">{{ short(data.totals.total_tokens) }}</div>
          <div class="muted">{{ fmt(data.totals.total_tokens) }}<template v-if="data.totals.estimated_tokens"> · 其中估算 {{ fmt(data.totals.estimated_tokens) }}</template></div>
        </div>
        <div class="panel kpi">
          <div class="k-label">调用次数</div>
          <div class="k-value">{{ fmt(data.totals.calls) }}</div>
          <div class="muted" :class="{ bad: data.totals.failed }">失败 {{ data.totals.failed }} 次<template v-if="data.totals.calls"> · {{ ((data.totals.failed / data.totals.calls) * 100).toFixed(1) }}%</template></div>
        </div>
        <div class="panel kpi">
          <div class="k-label">输入 / 输出 token</div>
          <div class="k-value">{{ short(data.totals.input_tokens) }} / {{ short(data.totals.output_tokens) }}</div>
          <div class="muted">ChatGPT 通道只返回合计，不计入此项</div>
        </div>
        <div class="panel kpi">
          <div class="k-label">AI 草稿</div>
          <div class="k-value">{{ fmt(data.totals.drafts) }}</div>
          <div class="muted">每条平均 {{ data.totals.tokens_per_draft == null ? '—' : fmt(data.totals.tokens_per_draft) }} token</div>
        </div>
        <div class="panel kpi">
          <div class="k-label">平均每次</div>
          <div class="k-value">{{ fmt(data.totals.avg_tokens) }}</div>
          <div class="muted">token · 平均耗时 {{ data.totals.avg_seconds }} 秒</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          每日用量
          <span v-if="peakDay && peakDay.total_tokens" class="muted">最高 {{ peakDay.date }}：{{ fmt(peakDay.total_tokens) }} token</span>
          <span class="spacer" />
          <span v-for="p in usedPurposes" :key="p" class="legend"><i :style="{ background: PURPOSE_COLORS[p] }" />{{ data.purposes[p] }}</span>
        </div>
        <div class="panel-body">
          <el-empty v-if="!data.totals.calls" description="这段时间没有模型调用记录" :image-size="60" />
          <div v-else class="chart">
            <div class="axis muted"><span>{{ short(maxDay) }}</span><span>0</span></div>
            <div class="cols">
              <el-tooltip v-for="(d, i) in data.by_day" :key="d.date" placement="top" :show-after="80">
                <template #content><div class="tip">{{ dayTip(d) }}</div></template>
                <div class="col">
                  <div class="stack" :style="{ height: `${(d.total_tokens / maxDay) * 100}%` }">
                    <div v-for="p in usedPurposes" :key="p" class="seg"
                      :style="{ flexGrow: d.by_purpose[p] || 0, background: PURPOSE_COLORS[p] }" />
                  </div>
                  <div class="x muted">{{ i % labelEvery === 0 ? d.date.slice(5) : '' }}</div>
                </div>
              </el-tooltip>
            </div>
          </div>
        </div>
      </div>

      <div class="grid3">
        <div class="panel">
          <div class="panel-head">按用途</div>
          <el-table :data="data.by_purpose" size="small" empty-text="无">
            <el-table-column label="用途" min-width="100">
              <template #default="{ row }"><i class="dot" :style="{ background: PURPOSE_COLORS[row.purpose] }" />{{ row.label }}</template>
            </el-table-column>
            <el-table-column label="次数" prop="calls" width="64" align="right" />
            <el-table-column label="token" align="right" min-width="90">
              <template #default="{ row }">{{ fmt(row.total_tokens) }}</template>
            </el-table-column>
            <el-table-column label="平均" align="right" width="76">
              <template #default="{ row }">{{ fmt(row.avg_tokens) }}</template>
            </el-table-column>
            <el-table-column label="耗时" align="right" width="70">
              <template #default="{ row }">{{ row.avg_seconds }}s</template>
            </el-table-column>
          </el-table>
        </div>
        <div class="panel">
          <div class="panel-head">按模型</div>
          <el-table :data="data.by_model" size="small" empty-text="无">
            <el-table-column label="模型" min-width="150">
              <template #default="{ row }">
                <div>{{ row.model }}</div>
                <div class="muted">{{ row.provider_label }}</div>
              </template>
            </el-table-column>
            <el-table-column label="次数" align="right" width="70">
              <template #default="{ row }">{{ row.calls }}<span v-if="row.failed" class="bad"> / 败{{ row.failed }}</span></template>
            </el-table-column>
            <el-table-column label="token" align="right" min-width="90">
              <template #default="{ row }">{{ fmt(row.total_tokens) }}</template>
            </el-table-column>
            <el-table-column label="耗时" align="right" width="70">
              <template #default="{ row }">{{ row.avg_seconds }}s</template>
            </el-table-column>
          </el-table>
        </div>
        <div class="panel">
          <div class="panel-head">按店铺</div>
          <el-table :data="data.by_store" size="small" empty-text="无" max-height="300">
            <el-table-column label="店铺" prop="name" min-width="150" show-overflow-tooltip />
            <el-table-column label="次数" prop="calls" width="64" align="right" />
            <el-table-column label="token" align="right" min-width="90">
              <template #default="{ row }">{{ fmt(row.total_tokens) }}</template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <div class="grid2">
        <div class="panel">
          <div class="panel-head">用量最多的会话 <span class="muted">点击打开会话</span></div>
          <el-table :data="data.top_conversations" size="small" empty-text="无" row-class-name="clickable"
            @row-click="(row: UsageSummary['top_conversations'][number]) => emit('open', row.conversation_id)">
            <el-table-column label="会话" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                #{{ row.conversation_id }} <span class="muted">{{ row.store_name || row.store_id }}</span>
                <div class="muted ellipsis">{{ row.title || row.buyer_key }}</div>
              </template>
            </el-table-column>
            <el-table-column label="次数" prop="calls" width="64" align="right" />
            <el-table-column label="token" align="right" min-width="90">
              <template #default="{ row }">{{ fmt(row.total_tokens) }}</template>
            </el-table-column>
          </el-table>
        </div>
        <div class="panel">
          <div class="panel-head">最近调用 <span class="muted">最多 100 条</span></div>
          <el-table :data="data.recent" size="small" empty-text="无" max-height="420">
            <el-table-column label="时间" width="130">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="用途" width="90">
              <template #default="{ row }"><i class="dot" :style="{ background: PURPOSE_COLORS[row.purpose] }" />{{ row.purpose_label }}</template>
            </el-table-column>
            <el-table-column label="模型 / 店铺" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.model || '—' }}
                <div class="muted">
                  {{ row.store_name || row.store_id || '—' }}
                  <el-button v-if="row.conversation_id" link size="small" @click="emit('open', row.conversation_id)">#{{ row.conversation_id }}</el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="token" align="right" width="110">
              <template #default="{ row }">
                {{ fmt(row.total_tokens) }}<span v-if="row.estimated" class="muted">（估）</span>
                <div v-if="row.input_tokens != null && row.output_tokens != null" class="muted">{{ fmt(row.input_tokens) }} + {{ fmt(row.output_tokens) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="耗时" align="right" width="64">
              <template #default="{ row }">{{ row.duration_ms == null ? '—' : `${(row.duration_ms / 1000).toFixed(1)}s` }}</template>
            </el-table-column>
            <el-table-column label="结果" width="60">
              <template #default="{ row }">
                <el-tooltip v-if="!row.ok" :content="row.error || '失败'" placement="top"><el-tag size="small" type="danger">失败</el-tag></el-tooltip>
                <el-tag v-else size="small" type="success" effect="plain">成功</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <p class="muted note">
        说明：回复模型（百炼千问）按服务商返回的输入/输出 token 计费；早期 ChatGPT 账号（Codex）通道的历史记录只有合计 token。
        向量模型不返回用量，按字数估算（标“估”）。只统计本功能上线之后的调用；不记录任何提示词或买家原文。
      </p>
    </template>
    <div v-else class="panel" v-loading="true" style="height: 200px" />
  </div>
</template>

<style scoped>
.usage {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bar {
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
}
.spacer {
  flex: 1;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.kpi {
  padding: 12px 14px;
  gap: 2px;
}
.k-label {
  font-size: var(--fs-xs);
  color: var(--muted);
}
.k-value {
  font-size: 24px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.bad {
  color: var(--danger-text);
}
.legend {
  font-size: var(--fs-xs);
  font-weight: 400;
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.legend i,
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 4px;
}
.chart {
  display: flex;
  gap: 6px;
  height: 220px;
}
.axis {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  text-align: right;
  padding-bottom: 18px;
  min-width: 40px;
}
.cols {
  flex: 1;
  display: flex;
  align-items: stretch;
  gap: 3px;
  border-left: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  min-width: 0;
}
.col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  position: relative;
  padding-bottom: 18px;
  margin-bottom: -18px;
  cursor: default;
}
.col:hover .stack {
  opacity: 0.8;
}
.stack {
  display: flex;
  flex-direction: column-reverse;
  border-radius: 3px 3px 0 0;
  overflow: hidden;
  min-height: 0;
}
.seg {
  flex-basis: 0;
}
.x {
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  white-space: nowrap;
}
.tip {
  white-space: pre-line;
  line-height: 1.6;
}
.grid3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.grid2 {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr);
  gap: 12px;
}
.ellipsis {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
:deep(.clickable) {
  cursor: pointer;
}
.note {
  margin: 0;
  line-height: 1.7;
}
@media (max-width: 1100px) {
  .grid3,
  .grid2 {
    grid-template-columns: 1fr;
  }
}
</style>
