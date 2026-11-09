<script setup lang="ts">
// 消息模板：系统预定义 / 企业模板 / 我的模板；可以添加、编辑、另存为、删除、星标，按标题内容、类型、用途、创建人、标签筛选
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get } from '../api'
import { errorText } from '../format'
import TemplateEditor from '../components/TemplateEditor.vue'
import type { Tpl, TplMeta } from '../components/TemplateEditor.vue'

const TABS = [
  { value: 'all', label: '全部' }, { value: 'system', label: '系统预定义' },
  { value: 'company', label: '企业模板' }, { value: 'mine', label: '我的模板' },
]
const SCOPE_LABEL: Record<string, string> = { SYSTEM: '系统预定义', COMPANY: '企业模板', PERSONAL: '个人模板' }
const USAGE_LABEL: Record<string, string> = { MESSAGE: '消息', FEEDBACK: '评价' }

const tab = ref('all')
const f = reactive({ q: '', qField: 'title', scope: '', usage: '', creator: '', tag: '', starred: false })
const items = ref<Tpl[]>([])
const counts = ref<Record<string, number>>({})
const meta = ref<TplMeta | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ scope: f.scope || tab.value })
    if (f.q.trim()) {
      q.set('q', f.q.trim())
      q.set('q_field', f.qField)
    }
    if (f.usage) q.set('usage', f.usage)
    if (f.creator) q.set('creator', f.creator)
    if (f.tag) q.set('tag', f.tag)
    if (f.starred) q.set('starred', 'true')
    const r = await get<{ items: Tpl[]; counts: Record<string, number> }>(`/internal/message-templates?${q}`)
    items.value = r.items
    counts.value = r.counts
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
async function loadMeta() {
  meta.value = await get<TplMeta>('/internal/message-templates/meta').catch(() => null)
}
watch(tab, () => {
  f.scope = ''
  page.value = 1
  load()
})
function search() {
  page.value = 1
  load()
}

const page = ref(1)
const pageSize = ref(20)
const pageRows = computed(() => items.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))

// 编辑器
const editorOpen = ref(false)
const editing = ref<Tpl | null>(null)
const readonly = ref(false)
function openNew() {
  editing.value = null
  readonly.value = false
  editorOpen.value = true
}
function openEdit(t: Tpl, view = false) {
  editing.value = t
  readonly.value = view
  editorOpen.value = true
}
async function onSaved() {
  await Promise.all([load(), loadMeta()])
}
async function remove(t: Tpl) {
  try {
    await ElMessageBox.confirm(`删除模板「${t.title}」？删除后不能恢复。`, '删除模板', { type: 'warning' })
    await api('DELETE', `/internal/message-templates/${t.id}`)
    ElMessage.success('已删除')
    onSaved()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}
async function toggleStar(t: Tpl) {
  try {
    await api('PUT', `/internal/message-templates/${t.id}/star`, { starred: !t.starred })
    t.starred = !t.starred
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function copyContent(t: Tpl) {
  try {
    await navigator.clipboard.writeText(t.content)
    ElMessage.success('内容已复制')
  } catch {
    ElMessage.warning('复制失败，请打开模板手动复制')
  }
}

onMounted(() => {
  loadMeta()
  load()
})
</script>

<template>
  <div class="mt">
    <div class="top">
      <el-button type="primary" @click="openNew">+ 添加模板</el-button>
    </div>
    <div class="f-row">
      <div class="search">
        <el-select v-model="f.qField" class="s-field">
          <el-option label="标题" value="title" />
          <el-option label="内容" value="content" />
          <el-option label="标题或内容" value="any" />
        </el-select>
        <el-input v-model="f.q" class="s-input" clearable :placeholder="f.qField === 'content' ? '请输入内容关键字' : '请输入标题'"
                  @keyup.enter="search" @clear="search" />
      </div>
      <el-select v-model="f.scope" class="f-sel" placeholder="类型" clearable @change="search">
        <el-option label="系统预定义" value="system" />
        <el-option label="企业模板" value="company" />
        <el-option label="我的模板" value="mine" />
      </el-select>
      <el-select v-model="f.usage" class="f-sel" placeholder="用途" clearable @change="search">
        <el-option label="消息" value="MESSAGE" />
        <el-option label="评价" value="FEEDBACK" />
      </el-select>
      <el-select v-model="f.creator" class="f-sel" placeholder="创建人" clearable filterable @change="search">
        <el-option v-for="c in meta?.creators || []" :key="c" :label="c" :value="c" />
      </el-select>
      <el-select v-model="f.tag" class="f-sel" placeholder="标签" clearable filterable @change="search">
        <el-option v-for="t in meta?.tags || []" :key="t" :label="t" :value="t" />
      </el-select>
      <el-tooltip content="只看星标" placement="top">
        <button class="star-btn" :class="{ on: f.starred }" @click="f.starred = !f.starred; search()">★</button>
      </el-tooltip>
      <el-button plain type="primary" @click="search">搜索</el-button>
    </div>
    <div class="tabs">
      <button v-for="t in TABS" :key="t.value" :class="{ on: tab === t.value && !f.scope }" @click="tab = t.value">
        {{ t.label }}<span v-if="counts[t.value] != null" class="n">({{ counts[t.value] }})</span>
      </button>
      <span class="spacer" />
      <span class="total">共{{ items.length }}条记录</span>
    </div>

    <el-table v-loading="loading && !items.length" :data="pageRows" row-key="id" empty-text="没有模板" class="tbl">
      <el-table-column label="标题" min-width="300">
        <template #default="{ row }">
          <div class="title-cell">
            <button class="star" :class="{ on: row.starred }" :title="row.starred ? '取消星标' : '加星标'" @click="toggleStar(row)">
              {{ row.starred ? '★' : '☆' }}
            </button>
            <a class="title" @click="openEdit(row, !row.editable)">{{ row.title }}</a>
            <button class="view" @click="openEdit(row, true)">查看</button>
            <span v-if="row.attachments.length" class="muted">🖼 {{ row.attachments.length }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="内容" min-width="380">
        <template #default="{ row }"><div class="content">{{ row.content }}</div></template>
      </el-table-column>
      <el-table-column label="用途 / 类型" width="130">
        <template #default="{ row }">
          <div>{{ USAGE_LABEL[row.usage] || row.usage }}</div>
          <div class="muted">{{ SCOPE_LABEL[row.scope] }}</div>
        </template>
      </el-table-column>
      <el-table-column label="标签" width="160">
        <template #default="{ row }">
          <el-tag v-for="t in row.tags" :key="t" size="small" class="tag" @click="f.tag = t; search()">{{ t }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建人" width="110">
        <template #default="{ row }">{{ row.scope === 'SYSTEM' ? '系统' : row.created_by }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.editable" link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button v-else link type="primary" size="small" @click="openEdit(row)">另存为</el-button>
          <el-button link size="small" @click="copyContent(row)">复制</el-button>
          <el-button v-if="row.editable" link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="pager">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="items.length"
                     layout="total, sizes, prev, pager, next" background small />
    </div>

    <TemplateEditor v-model="editorOpen" :tpl="editing" :meta="meta" :readonly="readonly" @saved="onSaved" @tags-changed="onSaved" />
  </div>
</template>

<style scoped>
.mt {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 14px 16px;
  min-height: 100%;
}
.top {
  margin-bottom: 12px;
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
  width: 110px;
}
.s-input {
  width: 260px;
  border-left: 1px solid var(--line);
}
.f-sel {
  width: 116px;
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
.tabs {
  display: flex;
  align-items: center;
  margin: 14px 0 8px;
}
.tabs button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line);
  background: var(--panel);
  font-family: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.tabs button + button {
  border-left: 0;
}
.tabs button:first-child {
  border-radius: 3px 0 0 3px;
}
.tabs button:last-of-type {
  border-radius: 0 3px 3px 0;
}
.tabs button.on {
  color: var(--accent);
  border-color: var(--accent);
  position: relative;
}
.tabs button.on + button {
  border-left: 1px solid var(--accent);
}
.n {
  margin-left: 3px;
  font-size: 12px;
}
.spacer {
  flex: 1;
}
.total {
  font-size: 12px;
  color: var(--muted);
}
.tbl :deep(td.el-table__cell) {
  vertical-align: top;
}
.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.star {
  border: 0;
  background: none;
  padding: 0;
  font-size: 15px;
  color: var(--muted);
  cursor: pointer;
}
.star.on {
  color: #f5a623;
}
.title {
  color: var(--text);
  cursor: pointer;
}
.title:hover {
  color: var(--accent);
}
.view {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 2px;
  padding: 0 6px;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
}
.view:hover {
  color: var(--accent);
  border-color: var(--accent-line);
}
.content {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 13px;
  color: var(--text-2);
  white-space: pre-line;
}
.tag {
  margin: 0 4px 4px 0;
  cursor: pointer;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}
</style>
