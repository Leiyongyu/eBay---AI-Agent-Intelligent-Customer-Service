<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, get, isAdmin, post, session } from '../api'
import { errorText, fmtTime } from '../format'

interface Row {
  chunk_id: string
  clause_id: string
  import_id: number | null
  version: string
  title: string
  text: string
  store_id: string
  marketplace_id: string
  audience: string
  topic: string
  priority: number
  effective_at: string
  expires_at: string | null
  review_state: string
  applicable: boolean
  excluded_reason: string | null
  tokens: number
}
interface Issue {
  row: number | null
  column: string | null
  message: string
}
interface Preview {
  ok: boolean
  errors: Issue[]
  warnings: Issue[]
  rows: { row: number; clause_id: string; title: string; sites: string[]; audience: string; topic: string; priority: number; effective_at: string; expires_at: string | null; enabled: boolean; chars: number }[]
  counts: { rows: number; public: number; internal: number; disabled: number }
  site_tokens: Record<string, number>
  budget_tokens: number
  diff: { added: string[]; removed: string[]; changed: string[]; unchanged: number }
  current_import: { id: number; file_name: string; uploaded_at: string } | null
  file_sha256: string
  sites: string[]
}
interface IndexStatus {
  knowledge_mode: string
  backend: string
  pgvector_version: string | null
  reasons: string[]
  published_public: number
  missing_search_text: number
  indexed: number | null
  missing_vectors: number | null
  profile: { id: number; model: string; dimension: number } | null
}
interface Hit {
  chunk_id: string
  clause_id: string
  title: string
  rrf: number
  vector_rank?: number
  distance?: number
  keyword_rank?: number
  sku_rank?: number
  dropped?: string
  text: string | null
}
interface ImportRow {
  id: number
  file_name: string
  uploaded_by: string
  uploaded_at: string
  status: string
  row_count: number
  public_count: number
  internal_count: number
  note: string | null
}

const AUDIENCE_LABELS: Record<string, string> = { PUBLIC_REPLY: '可对买家说', INTERNAL_SOP: '仅内部' }
const STATE_LABELS: Record<string, string> = { PUBLISHED: '已发布', DRAFT: '未启用', RETIRED: '已下线', WITHDRAWN: '已下线' }
const IMPORT_LABELS: Record<string, [string, 'success' | 'info' | 'warning']> = {
  ACTIVE: ['当前生效', 'success'],
  SUPERSEDED: ['已被替换', 'info'],
  ROLLED_BACK: ['已回滚撤下', 'warning'],
}

const BACKEND_LABELS: Record<string, string> = { pgvector: 'PostgreSQL + pgvector', sqlite: 'SQLite（仅开发测试）', unavailable: '不可用' }

const stores = ref<{ id: string; name: string }[]>([])
const filter = reactive({ store_id: '', marketplace_id: '', on: '', only: 'all' })
const data = ref<{ date: string; items: Row[]; applicable_count: number; applicable_tokens: number; budget_tokens: number } | null>(null)
const keyword = ref('')
const imports = ref<ImportRow[]>([])

// 检索索引与检索测试
const index = ref<IndexStatus | null>(null)
const rebuilding = ref(false)
const query = ref('')
const searching = ref(false)
const searchResult = ref<{ query_tokens: string[]; candidates: number; tokens: number; hits: Hit[] } | null>(null)
const searchError = ref<string[]>([])

// 上传
const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<{ name: string; dataUrl: string } | null>(null)
const preview = ref<Preview | null>(null)
const previewing = ref(false)
const committing = ref(false)

async function load() {
  if (!filter.store_id) return
  const q = new URLSearchParams({ store_id: filter.store_id, marketplace_id: filter.marketplace_id })
  if (filter.on) q.set('on', filter.on)
  ;[data.value, imports.value, index.value] = await Promise.all([
    get(`/internal/knowledge?${q}`),
    get<ImportRow[]>(`/internal/knowledge/imports?store_id=${encodeURIComponent(filter.store_id)}`),
    get<IndexStatus>(`/internal/knowledge/index/status?store_id=${encodeURIComponent(filter.store_id)}`).catch(() => null),
  ])
}

function reasonsOf(e: unknown): string[] {
  const d = e instanceof ApiError ? (e.detail as { reasons?: string[] } | null) : null
  return d?.reasons?.length ? d.reasons : [errorText(e)]
}

async function rebuild() {
  rebuilding.value = true
  try {
    const r = await post('/internal/knowledge/index/rebuild', { store_id: filter.store_id })
    ElMessage.success(`索引已补齐：新生成 ${r.embedded} 条向量，复用 ${r.reused} 条`)
    index.value = { ...r.status, knowledge_mode: index.value?.knowledge_mode || '' }
  } catch (e) {
    ElMessage.error(reasonsOf(e).join('；'))
  } finally {
    rebuilding.value = false
  }
}

async function runSearch() {
  if (!query.value.trim()) return
  searching.value = true
  searchError.value = []
  searchResult.value = null
  try {
    searchResult.value = await post('/internal/knowledge/search', {
      store_id: filter.store_id,
      marketplace_id: filter.marketplace_id,
      query: query.value,
    })
  } catch (e) {
    searchError.value = reasonsOf(e)
  } finally {
    searching.value = false
  }
}

const rows = computed(() =>
  (data.value?.items || []).filter((r) => {
    if (filter.only === 'applicable' && !r.applicable) return false
    if (filter.only === 'excluded' && r.applicable) return false
    const k = keyword.value.trim().toLowerCase()
    return !k || `${r.clause_id} ${r.title} ${r.text}`.toLowerCase().includes(k)
  }),
)
const usage = computed(() => (data.value ? Math.min(100, Math.round((data.value.applicable_tokens / data.value.budget_tokens) * 100)) : 0))
const templateUrl = computed(() => `/internal/knowledge/template?store_id=${encodeURIComponent(filter.store_id)}`)
const storeName = computed(() => stores.value.find((s) => s.id === filter.store_id)?.name || filter.store_id)

function pickFile() {
  fileInput.value?.click()
}

function onFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  if (!f) return
  if (!f.name.toLowerCase().endsWith('.xlsx')) {
    ElMessage.warning('请上传 .xlsx 文件（可先下载模板填写）')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    file.value = { name: f.name, dataUrl: reader.result as string }
    runPreview()
  }
  reader.readAsDataURL(f)
}

async function runPreview() {
  if (!file.value) return
  previewing.value = true
  preview.value = null
  try {
    preview.value = await post<Preview>('/internal/knowledge/upload/preview', {
      store_id: filter.store_id,
      file_name: file.value.name,
      data_base64: file.value.dataUrl,
    })
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    previewing.value = false
  }
}

async function commit() {
  if (!file.value || !preview.value?.ok) return
  if (!session.reviewer) {
    ElMessage.warning('请先在右上角填写审核员名称')
    return
  }
  const d = preview.value.diff
  try {
    await ElMessageBox.confirm(
      `将用“${file.value.name}”整体替换【${storeName.value}】的知识：新增 ${d.added.length}、修改 ${d.changed.length}、删除 ${d.removed.length}、不变 ${d.unchanged}。旧版本会保留，可在“历史版本”回滚。`,
      '确认替换',
      { type: 'warning', confirmButtonText: '确认替换' },
    )
  } catch {
    return
  }
  committing.value = true
  try {
    const r = await post('/internal/knowledge/upload/commit', {
      store_id: filter.store_id,
      file_name: file.value.name,
      data_base64: file.value.dataUrl,
      expected_sha256: preview.value.file_sha256,
    })
    const idx = r.index || {}
    const note = idx.embedded !== undefined ? `，向量新生成 ${idx.embedded} 条、复用 ${idx.reused} 条` : idx.skipped || idx.error ? `（检索索引未生成：${idx.skipped || idx.error}）` : ''
    ElMessage.success(`已生效：版本 #${r.import_id}${note}`)
    file.value = null
    preview.value = null
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    committing.value = false
  }
}

async function rollback(imp: ImportRow) {
  try {
    await ElMessageBox.confirm(`回滚到版本 #${imp.id}（${imp.file_name}）？当前生效的版本会被撤下，之后也可以再切回来。`, '确认回滚', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await post(`/internal/knowledge/imports/${imp.id}/rollback`, {})
    ElMessage.success(`已回滚到版本 #${imp.id}`)
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function issueText(i: Issue) {
  return `${i.row ? `第 ${i.row} 行` : ''}${i.column ? `「${i.column}」` : ''}${i.row || i.column ? '：' : ''}${i.message}`
}

onMounted(async () => {
  const [storeList, ebay] = await Promise.all([get('/internal/stores'), get('/internal/ebay/status').catch(() => null)])
  stores.value = storeList
  filter.store_id = ebay?.store_id && storeList.some((s: { id: string }) => s.id === ebay.store_id) ? ebay.store_id : storeList[0]?.id || ''
  filter.marketplace_id = session.meta?.marketplaces.includes('EBAY_DE') ? 'EBAY_DE' : session.meta?.marketplaces[0] || 'EBAY_US'
  load()
})
</script>

<template>
  <div class="kb">
    <div class="row filters">
      <span class="muted">店铺</span>
      <el-select v-model="filter.store_id" style="width: 200px" @change="(file = null), (preview = null), load()">
        <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
      </el-select>
      <span class="muted">只有你上传并启用的条款会给 AI 使用；大模型只用来回复，不整理知识库</span>
    </div>

    <div class="top">
      <!-- 上传（只有管理员） -->
      <div v-if="isAdmin()" class="panel">
        <div class="panel-head">上传正式知识库（Excel）</div>
        <div class="panel-body">
          <div class="row">
            <a :href="templateUrl"><el-button>下载模板</el-button></a>
            <el-button type="primary" :loading="previewing" @click="pickFile">选择 .xlsx 文件</el-button>
            <input ref="fileInput" type="file" accept=".xlsx" style="display: none" @change="onFile" />
            <span v-if="file" class="mono">{{ file.name }}</span>
          </div>
          <p class="muted">上传后先校验预览，不会立即生效；确认后整体替换【{{ storeName }}】当前全部知识，旧版本保留可回滚。</p>

          <template v-if="preview">
            <div class="row stats">
              <el-tag :type="preview.ok ? 'success' : 'danger'">{{ preview.ok ? '校验通过' : `有 ${preview.errors.length} 处错误` }}</el-tag>
              <span>共 {{ preview.counts.rows }} 条：可对买家说 {{ preview.counts.public }}，仅内部 {{ preview.counts.internal }}，未启用 {{ preview.counts.disabled }}</span>
            </div>
            <div class="row stats">
              <span class="muted">与当前版本相比：</span>
              <el-tag type="success" effect="plain">新增 {{ preview.diff.added.length }}</el-tag>
              <el-tag type="warning" effect="plain">修改 {{ preview.diff.changed.length }}</el-tag>
              <el-tag type="danger" effect="plain">删除 {{ preview.diff.removed.length }}</el-tag>
              <el-tag type="info" effect="plain">不变 {{ preview.diff.unchanged }}</el-tag>
            </div>
            <div class="section-title">各站点可对买家说的条款长度（预算 {{ preview.budget_tokens }} token）</div>
            <div v-for="(n, site) in preview.site_tokens" :key="site" class="row site">
              <span class="mono site-name">{{ site }}</span>
              <el-progress :percentage="Math.min(100, Math.round((n / preview.budget_tokens) * 100))" :status="n > preview.budget_tokens ? 'exception' : undefined" style="flex: 1" />
              <span class="muted">{{ n }}</span>
            </div>
            <template v-if="preview.errors.length">
              <div class="section-title err">错误（必须修改后重新上传）</div>
              <div v-for="(e, i) in preview.errors" :key="'e' + i" class="issue err">{{ issueText(e) }}</div>
            </template>
            <template v-if="preview.warnings.length">
              <div class="section-title warn">提醒</div>
              <div v-for="(w, i) in preview.warnings" :key="'w' + i" class="issue warn">{{ issueText(w) }}</div>
            </template>
            <div class="row" style="margin-top: 12px">
              <el-button type="danger" :disabled="!preview.ok" :loading="committing" @click="commit">确认替换</el-button>
              <el-button @click="(file = null), (preview = null)">取消</el-button>
            </div>
          </template>
        </div>
      </div>

      <!-- 历史版本 -->
      <div class="panel">
        <div class="panel-head">历史版本</div>
        <div class="panel-body">
          <el-empty v-if="!imports.length" :image-size="50" description="还没有上传过知识库" />
          <div v-for="imp in imports" :key="imp.id" class="imp">
            <div class="row">
              <b>#{{ imp.id }}</b>
              <el-tag size="small" :type="IMPORT_LABELS[imp.status]?.[1] || 'info'">{{ IMPORT_LABELS[imp.status]?.[0] || imp.status }}</el-tag>
              <span class="mono">{{ imp.file_name }}</span>
              <el-button v-if="imp.status !== 'ACTIVE' && isAdmin()" link size="small" @click="rollback(imp)">回滚到此版本</el-button>
            </div>
            <div class="muted">
              {{ fmtTime(imp.uploaded_at) }} · {{ imp.uploaded_by }} · {{ imp.row_count }} 条（对外 {{ imp.public_count }}，内部 {{ imp.internal_count }}）
            </div>
            <div v-if="imp.note" class="muted">{{ imp.note }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 检索索引与检索测试 -->
    <div class="panel">
      <div class="panel-head">
        知识检索（RAG）
        <span class="muted">回复时按买家问题从知识库里找最相关的几条：向量相似 + 关键词全文 + SKU 精确，三路合并排序</span>
      </div>
      <div class="panel-body">
        <div v-if="index" class="row stats">
          <span>当前模式：<b>{{ index.knowledge_mode === 'retrieval' ? '检索（RAG）' : '全量注入' }}</b></span>
          <span>向量库：<b>{{ BACKEND_LABELS[index.backend] || index.backend }}</b><span v-if="index.pgvector_version" class="muted"> v{{ index.pgvector_version }}</span></span>
          <span v-if="index.profile" class="muted">向量模型 {{ index.profile.model }} · {{ index.profile.dimension }} 维</span>
          <span>已发布可对买家说 {{ index.published_public }} 条<template v-if="index.indexed !== null">，已生成向量 {{ index.indexed }}，缺 <b :class="{ err: index.missing_vectors }">{{ index.missing_vectors }}</b></template></span>
          <el-button v-if="isAdmin()" size="small" :loading="rebuilding" :disabled="!!index.reasons.length" @click="rebuild">补齐索引</el-button>
        </div>
        <el-alert v-if="index?.reasons.length" type="warning" :closable="false" show-icon title="检索未就绪" style="margin: 8px 0">
          <div v-for="(r, i) in index.reasons" :key="i">{{ r }}</div>
        </el-alert>
        <div class="row" style="margin-top: 8px; align-items: flex-start">
          <el-input v-model="query" type="textarea" :rows="2" placeholder="输入一句买家的问题测试检索效果，如：Wann wird meine Bestellung versendet?" style="flex: 1" @keydown.ctrl.enter="runSearch" />
          <el-button type="primary" :loading="searching" @click="runSearch">检索（{{ filter.marketplace_id }}）</el-button>
        </div>
        <el-alert v-if="searchError.length" type="error" :closable="false" style="margin-top: 8px">
          <div v-for="(r, i) in searchError" :key="i">{{ r }}</div>
        </el-alert>
        <template v-if="searchResult">
          <div class="muted" style="margin: 6px 0">
            适用条款 {{ searchResult.candidates }} 条 · 分词：{{ searchResult.query_tokens.join(' / ') || '（无）' }} · 放入提示约 {{ searchResult.tokens }} token
          </div>
          <el-table :data="searchResult.hits" size="small" border row-key="chunk_id" empty-text="没有检索到条款">
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="text">{{ row.text || '（超出检索预算，未放入提示）' }}</div>
              </template>
            </el-table-column>
            <el-table-column type="index" label="#" width="40" />
            <el-table-column prop="clause_id" label="编号" width="120" />
            <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
            <el-table-column label="向量" width="170">
              <template #default="{ row }">{{ row.vector_rank ? `第 ${row.vector_rank}（距离 ${row.distance}）` : '—' }}</template>
            </el-table-column>
            <el-table-column label="关键词" width="70">
              <template #default="{ row }">{{ row.keyword_rank ? `第 ${row.keyword_rank}` : '—' }}</template>
            </el-table-column>
            <el-table-column label="SKU" width="60">
              <template #default="{ row }">{{ row.sku_rank ? '命中' : '—' }}</template>
            </el-table-column>
            <el-table-column prop="rrf" label="综合分" width="80" />
            <el-table-column label="" width="110">
              <template #default="{ row }"><el-tag v-if="row.dropped" type="info" size="small">{{ row.dropped }}</el-tag></template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </div>

    <!-- 当前条款 -->
    <div class="panel">
      <div class="panel-head">
        当前条款
        <span class="muted">按站点、日期筛选，查看哪些条款会放进 AI 的回复提示</span>
      </div>
      <div class="panel-body">
        <div class="row filters">
          <el-select v-model="filter.marketplace_id" style="width: 130px" @change="load">
            <el-option v-for="m in session.meta?.marketplaces || []" :key="m" :value="m" :label="m" />
            <el-option value="UNKNOWN" label="UNKNOWN" />
          </el-select>
          <el-date-picker v-model="filter.on" type="date" value-format="YYYY-MM-DD" placeholder="生效日期（默认今天）" @change="load" />
          <el-radio-group v-model="filter.only" size="small">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="applicable">会给 AI 用</el-radio-button>
            <el-radio-button value="excluded">不会用</el-radio-button>
          </el-radio-group>
          <el-input v-model="keyword" placeholder="搜索编号、标题、正文" clearable style="width: 220px" />
        </div>
        <div v-if="data" class="summary">
          {{ data.date }} {{ filter.marketplace_id }} 会给 AI 用 <b>{{ data.applicable_count }}</b> 条，约 <b>{{ data.applicable_tokens }}</b> token / 预算
          {{ data.budget_tokens }}
          <el-progress :percentage="usage" :status="data.applicable_tokens > data.budget_tokens ? 'exception' : undefined" style="width: 260px" />
        </div>
        <el-table :data="rows" size="small" border stripe row-key="chunk_id" style="width: 100%" empty-text="暂无条款，请先上传知识库">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="text">{{ row.text }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="clause_id" label="编号" width="120" />
          <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
          <el-table-column label="站点" width="100">
            <template #default="{ row }">{{ row.marketplace_id === 'ALL' ? '全部' : row.marketplace_id }}</template>
          </el-table-column>
          <el-table-column label="能否对买家说" width="110">
            <template #default="{ row }">{{ AUDIENCE_LABELS[row.audience] || row.audience }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">{{ STATE_LABELS[row.review_state] || row.review_state }}</template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="70" />
          <el-table-column label="生效期" width="170">
            <template #default="{ row }">{{ row.effective_at }} ~ {{ row.expires_at || '' }}</template>
          </el-table-column>
          <el-table-column label="版本" width="70">
            <template #default="{ row }">{{ row.import_id ? `#${row.import_id}` : '—' }}</template>
          </el-table-column>
          <el-table-column label="给 AI 用" width="200">
            <template #default="{ row }">
              <el-tag v-if="row.applicable" type="success" size="small">会用</el-tag>
              <el-tag v-else type="info" size="small">{{ row.excluded_reason }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kb {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.top {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}
.filters {
  margin-bottom: 4px;
}
.stats {
  margin-top: 8px;
  font-size: var(--fs-sm);
}
.site {
  font-size: var(--fs-xs);
  margin-bottom: 4px;
}
.site-name {
  width: 70px;
}
.issue {
  font-size: var(--fs-xs);
  padding: 3px 8px;
  border-radius: 4px;
  margin-bottom: 3px;
}
.issue.err {
  background: var(--danger-soft);
}
.issue.warn {
  background: var(--warn-soft);
}
.section-title.err {
  color: var(--danger-text);
}
.section-title.warn {
  color: var(--warn-text);
}
b.err {
  color: var(--danger-text);
}
.imp {
  border-bottom: 1px solid var(--line);
  padding: 6px 0;
}
.summary {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 8px 0;
  font-size: var(--fs-sm);
  flex-wrap: wrap;
}
.text {
  white-space: pre-wrap;
  padding: 6px 16px;
  font-size: var(--fs-sm);
}
@media (max-width: 1000px) {
  .top {
    grid-template-columns: 1fr;
  }
}
</style>
