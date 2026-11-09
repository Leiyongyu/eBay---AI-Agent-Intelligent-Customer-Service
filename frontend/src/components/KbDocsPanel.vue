<script setup lang="ts">
// 知识库页“参考文档”页签：第三方文件和内部资料（报表、手册、供应商资料、物流手册），知识条款在 Excel「参考文档」列按编号引用。
// 管理员可上传、编辑、替换新版本（旧版本保留）、归档、删除（被引用时拦住并列出引用）；其他人只看自己负责店铺的，敏感文档只看到标题。
// 文档只给人看：AI 不读，也不会发给买家。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, api, get, isAdmin, post } from '../api'
import { errorText, fmtTime } from '../format'

interface Doc {
  id: number
  doc_no: string
  title: string
  category: string
  sensitive: boolean
  status: string
  store_ids: string[]
  stores: string[]
  ref_count: number
  can_download: boolean
  description?: string | null
  version?: number
  file_name?: string
  size?: number
  scan_status?: string | null
  file_url?: string
  uploaded_by?: string
  uploaded_at?: string
  updated_by?: string | null
  updated_at?: string
}
interface Version { version: number; file_name: string; size: number; uploaded_by: string; uploaded_at: string; file_url: string }
interface Ref { store_id: string; store_name: string; clause_id: string; title: string }

const props = defineProps<{ stores: { id: string; name: string }[] }>()

const items = ref<Doc[]>([])
const categories = ref<string[]>(['报表', '手册', '供应商资料', '物流', '其他'])
const allowedExt = ref<string[]>([])
const maxMb = ref(30)
const loading = ref(false)
const f = reactive({ q: '', category: '', archived: false })

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ status: f.archived ? 'ALL' : 'ACTIVE' })
    if (f.q.trim()) q.set('q', f.q.trim())
    if (f.category) q.set('category', f.category)
    const r = await get<{ items: Doc[]; categories: string[]; allowed_ext: string[]; max_mb: number }>(`/internal/kb-docs?${q}`)
    items.value = r.items
    categories.value = r.categories
    allowedExt.value = r.allowed_ext
    maxMb.value = r.max_mb
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

const accept = computed(() => allowedExt.value.map((x) => `.${x}`).join(','))
const size = (n?: number) => (n == null ? '' : n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)
const SCAN: Record<string, string> = { CLEAN: '已扫描', SKIPPED: '未扫描', ERROR: '扫描失败' }

async function copy(t: string) {
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success(`已复制 ${t}`)
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(file)
  })
}

// ---- 上传 / 编辑 ----
const form = reactive({ open: false, id: 0, title: '', description: '', category: '其他', store_ids: [] as string[], sensitive: false })
const pickedFile = ref<File | null>(null)
const saving = ref(false)
function openNew() {
  Object.assign(form, { open: true, id: 0, title: '', description: '', category: '其他', store_ids: [], sensitive: false })
  pickedFile.value = null
}
function openEdit(d: Doc) {
  Object.assign(form, { open: true, id: d.id, title: d.title, description: d.description || '', category: d.category,
                        store_ids: [...d.store_ids], sensitive: d.sensitive })
  pickedFile.value = null
}
function onPick(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0] || null
  ;(e.target as HTMLInputElement).value = ''
  if (file && file.size > maxMb.value * 1024 * 1024) {
    ElMessage.error(`文件超过 ${maxMb.value} MB`)
    return
  }
  pickedFile.value = file
  if (file && !form.title) form.title = file.name.replace(/\.[^.]+$/, '')
}
async function save() {
  if (!form.title.trim()) return ElMessage.warning('请填写标题')
  if (!form.id && !pickedFile.value) return ElMessage.warning('请选择文件')
  saving.value = true
  const meta = { title: form.title.trim(), description: form.description, category: form.category,
                 store_ids: form.store_ids, sensitive: form.sensitive }
  try {
    if (form.id) {
      await api('PUT', `/internal/kb-docs/${form.id}`, meta)
      ElMessage.success('已保存')
    } else {
      const d = await post<Doc>('/internal/kb-docs', { ...meta, file_name: pickedFile.value!.name,
                                                       data_base64: await readFile(pickedFile.value!) })
      ElMessage.success(`已上传，编号 ${d.doc_no}（在知识库 Excel 的「参考文档」列填这个编号）`)
    }
    form.open = false
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

// ---- 替换新版本 ----
const replaceInput = ref<HTMLInputElement | null>(null)
const replacing = ref<Doc | null>(null)
function pickReplace(d: Doc) {
  replacing.value = d
  replaceInput.value?.click()
}
async function onReplace(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  const d = replacing.value
  if (!file || !d) return
  try {
    await ElMessageBox.confirm(`用“${file.name}”替换 ${d.doc_no}（${d.title}）？编号不变，当前版本 v${d.version} 保留在历史版本里。`,
                               '替换新版本', { type: 'warning', confirmButtonText: '替换' })
  } catch {
    return
  }
  try {
    const r = await post<Doc>(`/internal/kb-docs/${d.id}/replace`, { file_name: file.name, data_base64: await readFile(file) })
    ElMessage.success(`已替换为 v${r.version}`)
    load()
  } catch (err) {
    ElMessage.error(errorText(err))
  }
}

// ---- 归档 / 恢复 / 删除 ----
async function setArchived(d: Doc, archived: boolean) {
  try {
    await post(`/internal/kb-docs/${d.id}/${archived ? 'archive' : 'restore'}`, {})
    ElMessage.success(archived ? '已归档（列表默认不显示，已有引用仍能打开）' : '已恢复')
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function remove(d: Doc) {
  try {
    await ElMessageBox.confirm(`删除 ${d.doc_no}（${d.title}）？文件和全部历史版本一起删除，不能恢复；编号以后也不会再用。`,
                               '删除文档', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
  } catch {
    return
  }
  try {
    await api('DELETE', `/internal/kb-docs/${d.id}`)
    ElMessage.success('已删除')
    load()
  } catch (e) {
    const detail = e instanceof ApiError ? (e.detail as { message?: string; refs?: { store_id: string; clause_id: string }[] } | null) : null
    if (detail?.refs?.length) {
      const names = Object.fromEntries(props.stores.map((s) => [s.id, s.name]))
      const lines = detail.refs.slice(0, 30).map((r) => `${names[r.store_id] || r.store_id}：${r.clause_id}`)
      ElMessageBox.alert(`${detail.message}\n\n${lines.join('\n')}${detail.refs.length > 30 ? `\n……共 ${detail.refs.length} 条` : ''}`,
                         '不能删除', { type: 'warning', customStyle: { whiteSpace: 'pre-wrap' } })
    } else {
      ElMessage.error(errorText(e))
    }
  }
}

// ---- 历史版本、引用 ----
const side = reactive({ open: false, doc: null as Doc | null, versions: [] as Version[], refs: [] as Ref[] })
async function openSide(d: Doc) {
  Object.assign(side, { open: true, doc: d, versions: [], refs: [] })
  try {
    ;[side.versions, side.refs] = await Promise.all([get<Version[]>(`/internal/kb-docs/${d.id}/versions`),
                                                    get<Ref[]>(`/internal/kb-docs/${d.id}/refs`)])
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">参考文档</div>
    <div class="panel-body">
      <p class="muted intro">
        报表、手册、供应商资料、物流手册等文件放在这里，每个文件有一个编号（如 DOC-0001）。在知识库 Excel 的「参考文档」列填编号，
        知识库页的条款列表和会话页“内部参考”里就能直接点开。<b>文档只给人看：AI 不读，也不会发给买家。</b>
      </p>
      <div class="row filters">
        <el-input v-model="f.q" placeholder="搜索编号、标题、说明、文件名" clearable style="width: 260px" @keyup.enter="load" @clear="load" />
        <el-select v-model="f.category" placeholder="全部分类" clearable style="width: 130px" @change="load">
          <el-option v-for="c in categories" :key="c" :value="c" :label="c" />
        </el-select>
        <el-checkbox v-model="f.archived" @change="load">显示已归档</el-checkbox>
        <el-button @click="load">搜索</el-button>
        <span class="spacer" />
        <el-button v-if="isAdmin()" type="primary" @click="openNew">上传文档</el-button>
      </div>
      <el-table v-loading="loading" :data="items" size="small" border stripe row-key="id" empty-text="还没有参考文档">
        <el-table-column label="编号" width="110">
          <template #default="{ row }">
            <span class="mono">{{ row.doc_no }}</span>
            <button class="icon-btn" title="复制编号" @click="copy(row.doc_no)">⧉</button>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="220">
          <template #default="{ row }">
            <a v-if="row.file_url" :href="row.file_url" target="_blank" rel="noopener" title="pdf、图片在新标签页预览，其他文件下载">{{ row.title }}</a>
            <span v-else>{{ row.title }}</span>
            <el-tag v-if="row.status === 'ARCHIVED'" size="small" type="info" class="ml">已归档</el-tag>
            <div v-if="row.description" class="muted ellipsis" :title="row.description">{{ row.description }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="分类" width="90" />
        <el-table-column label="适用店铺" width="140">
          <template #default="{ row }">
            <span v-if="!row.store_ids.length">全部店铺</span>
            <span v-else class="ellipsis" :title="row.stores.join('、')">{{ row.stores.join('、') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="敏感" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.sensitive" size="small" type="danger" effect="plain">敏感</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="文件" min-width="170">
          <template #default="{ row }">
            <template v-if="row.can_download">
              <div class="ellipsis" :title="row.file_name">{{ row.file_name }}</div>
              <div class="muted">{{ size(row.size) }} · v{{ row.version }}<template v-if="row.scan_status"> · {{ SCAN[row.scan_status] || row.scan_status }}</template></div>
            </template>
            <span v-else class="muted">敏感文档，仅管理员可看</span>
          </template>
        </el-table-column>
        <el-table-column label="被引用" width="80">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openSide(row)">{{ row.ref_count }} 条</el-button>
          </template>
        </el-table-column>
        <el-table-column label="上传" width="150">
          <template #default="{ row }">
            <template v-if="row.uploaded_at">
              <div>{{ row.updated_by || row.uploaded_by }}</div>
              <div class="muted">{{ fmtTime(row.updated_at || row.uploaded_at) }}</div>
            </template>
          </template>
        </el-table-column>
        <el-table-column v-if="isAdmin()" label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button link size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link size="small" @click="pickReplace(row)">替换</el-button>
            <el-button link size="small" @click="openSide(row)">版本</el-button>
            <el-button v-if="row.status === 'ACTIVE'" link size="small" @click="setArchived(row, true)">归档</el-button>
            <el-button v-else link size="small" @click="setArchived(row, false)">恢复</el-button>
            <el-button link size="small" type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <input ref="replaceInput" type="file" :accept="accept" style="display: none" @change="onReplace" />
    </div>

    <!-- 上传 / 编辑 -->
    <el-dialog v-model="form.open" :title="form.id ? '编辑文档信息' : '上传参考文档'" width="560px">
      <el-form label-width="90px">
        <el-form-item v-if="!form.id" label="文件" required>
          <label class="pick">
            <input type="file" :accept="accept" style="display: none" @change="onPick" />
            <el-button tag="span">选择文件</el-button>
            <span v-if="pickedFile" class="mono">{{ pickedFile.name }}（{{ size(pickedFile.size) }}）</span>
            <span v-else class="muted">{{ allowedExt.join('、') }}，不超过 {{ maxMb }} MB</span>
          </label>
        </el-form-item>
        <el-form-item label="标题" required><el-input v-model="form.title" maxlength="200" /></el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="3" maxlength="4000" placeholder="这份文档是什么、什么时候用" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width: 160px">
            <el-option v-for="c in categories" :key="c" :value="c" :label="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="适用店铺">
          <el-select v-model="form.store_ids" multiple filterable collapse-tags collapse-tags-tooltip placeholder="不选 = 全部店铺" style="width: 100%">
            <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="敏感">
          <el-checkbox v-model="form.sensitive">含买家个人信息等：只有管理员能打开，其他人只看到标题</el-checkbox>
        </el-form-item>
      </el-form>
      <p class="muted">文件不要含买家原话、地址、姓名等个人信息；需要保留的标为“敏感”。</p>
      <template #footer>
        <el-button @click="form.open = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ form.id ? '保存' : '上传' }}</el-button>
      </template>
    </el-dialog>

    <!-- 历史版本与引用 -->
    <el-drawer v-model="side.open" :title="side.doc ? `${side.doc.doc_no} ${side.doc.title}` : ''" size="520px">
      <template v-if="side.doc">
        <div class="section-title">被这些当前条款引用（{{ side.refs.length }}）</div>
        <div v-if="!side.refs.length" class="muted">没有条款引用它</div>
        <div v-for="r in side.refs" :key="r.store_id + r.clause_id" class="ref-row">
          <span class="mono">{{ r.clause_id }}</span> {{ r.title }} <span class="muted">· {{ r.store_name }}</span>
        </div>
        <div class="section-title">版本</div>
        <div v-if="side.doc.can_download" class="ref-row">
          <b>v{{ side.doc.version }}（当前）</b>
          <a :href="side.doc.file_url" target="_blank" rel="noopener">{{ side.doc.file_name }}</a>
          <span class="muted">{{ size(side.doc.size) }} · {{ side.doc.updated_by || side.doc.uploaded_by }}</span>
        </div>
        <div v-for="v in side.versions" :key="v.version" class="ref-row">
          <b>v{{ v.version }}</b>
          <a :href="v.file_url" target="_blank" rel="noopener">{{ v.file_name }}</a>
          <span class="muted">{{ size(v.size) }} · {{ v.uploaded_by }} · {{ fmtTime(v.uploaded_at) }}</span>
        </div>
        <div v-if="!side.doc.can_download" class="muted">敏感文档，只有管理员能看版本和下载</div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.intro {
  margin: 0 0 10px;
  line-height: 1.7;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.spacer {
  flex: 1;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.ml {
  margin-left: 6px;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.icon-btn {
  border: 0;
  background: none;
  padding: 0 4px;
  color: var(--muted);
  cursor: pointer;
}
.icon-btn:hover {
  color: var(--accent);
}
.pick {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.section-title {
  margin: 14px 0 6px;
  font-weight: 600;
  font-size: 13px;
}
.ref-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
  padding: 4px 0;
  font-size: 13px;
  border-bottom: 1px solid var(--line-soft);
}
</style>
