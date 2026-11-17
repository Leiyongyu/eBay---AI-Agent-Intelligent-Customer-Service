<script setup lang="ts">
// 评价管理：eBay 店铺收到的差评 / 中评 / 好评（定时拉取，只读）。本系统里可以认领、打标签、写内部备注、标处理状态；
// 回复评价、申请移除请到 eBay 后台
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText } from '../format'
import { copyText, downloadCsv, fmtFull, useCases } from '../caseCommon'
import type { CaseOptions, CaseRow, CaseTag, ViewCounts } from '../caseCommon'
import CaseTagDialog from '../components/CaseTagDialog.vue'
import CaseTagFilter from '../components/CaseTagFilter.vue'
import { dotStyle } from '../tagColor'

const emit = defineEmits<{ open: [id: number] }>()
const KIND = 'FEEDBACK'
const isAdmin = computed(() => session.user?.role === 'ADMIN')

const TABS = [{ value: 'negative', label: '差评' }, { value: 'neutral', label: '中评' }, { value: 'positive', label: '好评' }]
const SEARCH_FIELDS = [
  { value: 'item_id', label: 'Item ID' }, { value: 'order_id', label: '订单编号' },
  { value: 'buyer', label: '客户ID' }, { value: 'title', label: '商品标题' },
]
const tab = ref('negative')
const f = reactive({ q: '', qField: 'item_id', storeIds: [] as string[], operator: '', tagIds: [] as string[],
  tagMode: 'any' as 'any' | 'all', time: null as [string, string] | null, starred: false })
const applied = ref({ ...f })
const rows = ref<CaseRow[]>([])
const counts = ref<ViewCounts | null>(null)
const opts = ref<CaseOptions | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const a = applied.value
    const q = new URLSearchParams({ kind: KIND, rating: tab.value })
    if (a.q.trim()) {
      q.set('q', a.q)
      q.set('q_field', a.qField)
    }
    if (a.storeIds.length) q.set('store_ids', a.storeIds.join(','))
    if (a.operator) q.set('operator', a.operator)
    if (a.tagIds.length) {
      q.set('tag_ids', a.tagIds.join(','))
      q.set('tag_mode', a.tagMode)
    }
    if (a.starred) q.set('starred', 'true')
    if (a.time) {
      q.set('opened_from', a.time[0])
      q.set('opened_to', a.time[1])
    }
    const [list, c] = await Promise.all([
      get<CaseRow[]>(`/internal/ebay-cases?${q}`),
      get<ViewCounts>(`/internal/ebay-cases/view-counts?kind=${KIND}`),
    ])
    rows.value = list
    counts.value = c
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
const common = useCases(() => KIND, load)
const { tags, extras, busy } = common

function search() {
  applied.value = { ...f, storeIds: [...f.storeIds], tagIds: [...f.tagIds] }
  page.value = 1
  load()
}
/** 快捷标签栏：点一下只看这个标签，再点一下取消 */
function quickTag(id: string) {
  f.tagIds = applied.value.tagIds.length === 1 && applied.value.tagIds[0] === id ? [] : [id]
  search()
}
const pinnedTags = computed(() => tags.value.filter((t) => t.pinned))
async function createFromFilter(name: string, color: string, done: (t: CaseTag | null) => void) {
  done(await common.createTag(name, color))
}
watch(tab, () => {
  page.value = 1
  selected.value = []
  load()
})

const page = ref(1)
const pageSize = ref(20)
const pageRows = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(pageRows, (l) => common.want(l), { immediate: true })
const selected = ref<CaseRow[]>([])
const ids = () => selected.value.map((r) => r.id)

function onTag(cmd: string) {
  const [op, id] = cmd.split(':')
  common.batch(ids(), { action: op === 'add' ? 'tag_add' : 'tag_remove', tag_id: Number(id) }, op === 'add' ? '已加标签' : '已去标签')
}
function onState(cmd: string) {
  const label: Record<string, string> = { PROCESSING: '已标为跟进中', DONE: '已标为已处理', PENDING: '已改回待处理' }
  common.batch(ids(), { action: 'state', state: cmd }, label[cmd])
}
async function removeSelected() {
  if (!selected.value.length) return ElMessage.warning('请先勾选记录')
  try {
    await ElMessageBox.confirm(`删除勾选的 ${selected.value.length} 条评价？只在本系统里隐藏，eBay 上不受影响。`, '删除', { type: 'warning' })
  } catch {
    return
  }
  common.batch(ids(), { action: 'hide' }, '已删除')
}
function exportCsv() {
  const list = selected.value.length ? selected.value : rows.value
  const R: Record<string, string> = { Negative: '差评', Neutral: '中评', Positive: '好评' }
  downloadCsv(`评价_${fmtFull(new Date().toISOString()).slice(0, 10)}.csv`,
    ['评价', 'Item ID', '订单编号', '库存SKU', '平台SKU', '客户ID', '店铺', '评价内容', '是否已回复', '处理状态', '内部备注', '操作员', '评价时间'],
    list.map((r) => [R[r.rating || ''] || r.rating, r.item_id, r.order_id, extras[r.id]?.stock_sku, extras[r.id]?.sku, r.buyer,
      r.store_name, r.comment, r.detail?.responded ? '已回复' : '', stateLabel(r), r.note, r.operator, fmtFull(r.opened_at)]))
}

const syncing = ref(false)
async function syncNow() {
  syncing.value = true
  try {
    const r = await post<{ started: boolean }>('/internal/ebay-cases/sync')
    ElMessage.success(r.started ? '已开始从 eBay 拉取，完成后刷新即可看到' : '正在拉取中，请稍候')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    syncing.value = false
  }
}

// 内部备注：点“编辑”在格子里改
const editing = ref<number | null>(null)
const noteText = ref('')
function editNote(r: CaseRow) {
  editing.value = r.id
  noteText.value = r.note || ''
}
async function saveNote(r: CaseRow) {
  if (await common.saveNote(r, noteText.value)) editing.value = null
}
function stateLabel(r: CaseRow) {
  if (r.work_state === 'DONE') return '已处理'
  if (r.work_state === 'PROCESSING') return '跟进中'
  return r.detail?.responded ? '已回复' : ''
}
const stateType = (r: CaseRow) => (r.work_state === 'DONE' ? 'success' : r.work_state === 'PROCESSING' ? 'primary' : 'info')
const tagDialog = ref(false)

onMounted(async () => {
  common.loadTags()
  opts.value = await get<CaseOptions>(`/internal/ebay-cases/options?kind=${KIND}`).catch(() => null)
  await load()
})
</script>

<template>
  <div class="fb">
    <div class="filters">
      <div class="f-row">
        <div class="search">
          <el-select v-model="f.qField" class="s-field">
            <el-option v-for="o in SEARCH_FIELDS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-input v-model="f.q" class="s-input" clearable placeholder="请输入关键字，批量输入用逗号或换行分隔" @keyup.enter="search" />
        </div>
        <el-select v-model="f.storeIds" class="f-sel w-store" placeholder="eBay账号" multiple collapse-tags clearable filterable>
          <el-option v-for="s in opts?.stores || []" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select v-model="f.operator" class="f-sel" placeholder="操作员" clearable filterable>
          <el-option label="未认领" value="unassigned" />
          <el-option v-for="o in opts?.operators || []" :key="o" :label="o" :value="o" />
        </el-select>
        <el-date-picker v-model="f.time" class="f-date" type="daterange" value-format="YYYY-MM-DD" range-separator="至"
                        start-placeholder="评价时间" end-placeholder="结束日期" />
        <CaseTagFilter v-model="f.tagIds" v-model:mode="f.tagMode" :tags="tags" :counts="counts?.tags" :is-admin="isAdmin"
                       @create="createFromFilter" @pin="(t, v) => common.updateTag(t, { pinned: v })" @manage="tagDialog = true" @apply="search" />
        <el-tooltip content="只看星标" placement="top">
          <button class="star-btn" :class="{ on: f.starred }" @click="f.starred = !f.starred; search()">★</button>
        </el-tooltip>
        <el-button plain type="primary" @click="search">搜索</el-button>
      </div>
      <div class="tag-row">
        <span class="tag-title">标签:</span>
        <button v-for="t in pinnedTags" :key="t.id" class="chip" :class="{ on: applied.tagIds.includes(String(t.id)) }" @click="quickTag(String(t.id))">
          <i class="dot" :style="dotStyle(t.color)" />{{ t.name }}<span class="n">({{ counts?.tags[String(t.id)] || 0 }})</span>
        </button>
        <span v-if="!pinnedTags.length" class="muted">{{ tags.length ? '没有固定的标签，' : '快速访问标签，' }}</span>
        <el-button link type="primary" size="small" @click="tagDialog = true">{{ tags.length ? '标签管理' : '新建标签' }}</el-button>
      </div>
    </div>

    <div class="tabs">
      <button v-for="t in TABS" :key="t.value" :class="{ on: tab === t.value }" @click="tab = t.value">
        {{ t.label }}<span class="n">({{ counts?.ratings[t.value] ?? 0 }})</span>
      </button>
      <span class="spacer" />
      <el-button v-if="isAdmin" size="small" :loading="syncing" @click="syncNow">⟳ 手动同步eBay</el-button>
    </div>

    <div class="toolbar">
      <el-button :disabled="busy" @click="common.batch(ids(), { action: 'claim' }, '已认领')">认领</el-button>
      <el-dropdown trigger="click" :disabled="busy" @command="onTag">
        <el-button>标签<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="t in tags" :key="`a${t.id}`" :command="`add:${t.id}`">
              <i class="dot" :style="dotStyle(t.color)" />加上「{{ t.name }}」
            </el-dropdown-item>
            <el-dropdown-item v-for="(t, i) in tags" :key="`r${t.id}`" :command="`remove:${t.id}`" :divided="i === 0">去掉「{{ t.name }}」</el-dropdown-item>
            <el-dropdown-item :divided="!!tags.length" @click="tagDialog = true">新建 / 管理标签…</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-dropdown trigger="click" :disabled="busy" @command="onState">
        <el-button>处理状态<span class="arr">▾</span></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="PROCESSING">标为跟进中</el-dropdown-item>
            <el-dropdown-item command="DONE">标为已处理</el-dropdown-item>
            <el-dropdown-item command="PENDING">清除状态</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button @click="exportCsv">按模板导出</el-button>
      <el-button :disabled="busy" @click="removeSelected">删除</el-button>
      <span v-if="selected.length" class="picked">已选 {{ selected.length }} 条</span>
      <span class="spacer" />
      <span class="total">共{{ rows.length }}条记录</span>
    </div>

    <div class="table-wrap">
      <el-table v-loading="loading && !rows.length" :data="pageRows" row-key="id" empty-text="没有评价"
                @selection-change="(v: CaseRow[]) => (selected = v)">
        <el-table-column type="selection" width="40" />
        <el-table-column label="图片" width="84">
          <template #default="{ row }">
            <img v-if="extras[row.id]?.picture_url" class="thumb" :src="extras[row.id]!.picture_url!" alt="" referrerpolicy="no-referrer" loading="lazy" />
            <span v-else class="thumb ph" aria-hidden="true" />
          </template>
        </el-table-column>
        <el-table-column label="Item ID / 价格" width="140">
          <template #default="{ row }">
            <div>{{ row.item_id || '—' }}<button v-if="row.item_id" class="copy" @click="copyText(row.item_id)">⧉</button></div>
            <div class="muted">{{ extras[row.id]?.price ? `${extras[row.id]!.price!.value} ${extras[row.id]!.price!.currency}` : '' }}</div>
            <el-tag v-if="stateLabel(row)" size="small" effect="dark" :type="stateType(row)">{{ stateLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="订单编号" width="130">
          <template #default="{ row }"><span class="dotted">{{ extras[row.id]?.order_id || row.order_id || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="SKU" width="160">
          <template #default="{ row }">
            <div class="kv"><span>库存:</span>{{ extras[row.id]?.stock_sku || '—' }}</div>
            <div class="kv"><span>平台:</span>{{ extras[row.id]?.sku || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="客户ID / 卖家账号" width="160">
          <template #default="{ row }">
            <div>{{ row.buyer || '—' }}
              <button v-if="row.conversation_id" class="contact" @click="emit('open', row.conversation_id)">联系TA</button>
            </div>
            <div class="muted">{{ row.store_name }}</div>
          </template>
        </el-table-column>
        <el-table-column label="评价内容" min-width="240">
          <template #default="{ row }">
            <span class="comment">{{ row.comment || '（没有文字）' }}</span>
            <span v-if="row.detail?.responded" class="replied">已回复</span>
            <div v-if="row.title" class="muted clamp">{{ row.title }}</div>
          </template>
        </el-table-column>
        <el-table-column label="内部备注" width="170">
          <template #default="{ row }">
            <template v-if="editing === row.id">
              <el-input v-model="noteText" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" maxlength="2000" />
              <div class="note-btns">
                <el-button size="small" type="primary" @click="saveNote(row)">保存</el-button>
                <el-button size="small" @click="editing = null">取消</el-button>
              </div>
            </template>
            <template v-else>
              <div v-if="row.note" class="note" :title="`${row.note_by || ''} ${fmtFull(row.note_at)}`">{{ row.note }}</div>
              <el-button link type="primary" size="small" @click="editNote(row)">{{ row.note ? '编辑' : '添加备注' }}</el-button>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作员" width="90">
          <template #default="{ row }">{{ row.operator || '' }}</template>
        </el-table-column>
        <el-table-column label="评价时间" width="150">
          <template #default="{ row }">
            <div>{{ fmtFull(row.opened_at) }}</div>
            <button class="star" :class="{ on: row.starred }" @click="common.star(row)">{{ row.starred ? '★ 已星标' : '☆ 星标' }}</button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="rows.length"
                     layout="total, sizes, prev, pager, next, jumper" background small />
    </div>
    <p class="muted foot">回复评价、申请移除请到 eBay 后台；这里的状态、标签、备注只在本系统，同事共用。</p>

    <CaseTagDialog v-model="tagDialog" :tags="tags" title="评价管理" :is-admin="isAdmin" @create="common.createTag"
                   @update="common.updateTag" @remove="common.deleteTag" />
  </div>
</template>

<style scoped>
.fb {
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  min-height: 100%;
}
.filters {
  padding: 12px 16px 6px;
}
.f-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.search {
  display: flex;
  align-items: center;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}
.search :deep(.el-select__wrapper),
.search :deep(.el-input__wrapper) {
  box-shadow: none !important;
  background: transparent;
}
.s-field {
  width: 104px;
}
.s-input {
  width: 260px;
  border-left: 1px solid var(--line);
}
.f-sel {
  width: 118px;
}
.f-sel.w-store {
  width: 170px;
}
.f-date {
  width: 230px !important;
}
.star-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--panel);
  color: var(--muted);
  cursor: pointer;
}
.star-btn.on {
  color: #f5a623;
  border-color: #f5a623;
}
.tag-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-top: 10px;
}
.tag-title {
  color: var(--muted);
  font-size: 13px;
}
.chip {
  height: 26px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--panel);
  font-family: inherit;
  font-size: 12px;
  color: var(--text-2);
  cursor: pointer;
}
.chip.on {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.chip .n {
  color: var(--muted);
}
.dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 4px;
}
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
}
.tabs > button {
  height: 40px;
  padding: 0 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: none;
  font-family: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.tabs > button .n {
  margin-left: 2px;
  font-size: 12px;
  color: var(--muted);
}
.tabs > button.on,
.tabs > button.on .n {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.spacer {
  flex: 1;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
}
.toolbar .el-button + .el-button,
.toolbar .el-dropdown + .el-button {
  margin-left: 0;
}
.arr {
  margin-left: 6px;
  font-size: 10px;
  color: var(--muted);
}
.picked {
  font-size: 12px;
  color: var(--accent);
}
.total {
  font-size: 12px;
  color: var(--muted);
}
.table-wrap {
  padding: 0 16px;
}
.table-wrap :deep(td.el-table__cell) {
  vertical-align: top;
}
.thumb {
  display: block;
  width: 60px;
  height: 60px;
  object-fit: contain;
  border-radius: 3px;
  border: 1px solid var(--line-soft);
  background: #fff;
}
.thumb.ph {
  background: var(--sunken);
}
.copy,
.contact {
  border: 0;
  background: none;
  padding: 0 0 0 4px;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
}
.contact {
  color: var(--accent);
}
.dotted {
  border-bottom: 1px dashed var(--muted);
}
.kv {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kv span {
  color: var(--muted);
  margin-right: 4px;
}
.rate {
  margin-right: 6px;
}
.comment {
  white-space: pre-wrap;
}
.replied {
  margin-left: 6px;
  font-size: 12px;
  color: var(--ok-text);
}
.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: 2px;
}
.note {
  white-space: pre-wrap;
  font-size: 12px;
}
.note-btns {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
.star {
  border: 0;
  background: none;
  padding: 0;
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}
.star.on {
  color: #f5a623;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px 4px;
}
.foot {
  padding: 0 16px 12px;
}
</style>
