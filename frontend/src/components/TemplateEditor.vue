<script setup lang="ts">
// 编辑 / 新建消息模板：类型、用途、标题、内容（点右侧占位符插入到光标处）、图片附件、标签；保存 / 另存为
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, post } from '../api'
import { errorText } from '../format'

export interface Tpl {
  id: number
  scope: 'SYSTEM' | 'COMPANY' | 'PERSONAL'
  usage: 'MESSAGE' | 'FEEDBACK'
  title: string
  content: string
  tags: string[]
  attachments: { file: string; name: string | null; url: string }[]
  starred: boolean
  created_by: string | null
  updated_by: string | null
  created_at: string | null
  updated_at: string | null
  mine: boolean
  editable: boolean
}
export interface TplMeta {
  placeholders: { label: string; key: string }[]
  tags: string[]
  creators: string[]
  can_edit_system: boolean
  max_content: number
  max_attachments: number
}

const props = defineProps<{ modelValue: boolean; tpl: Tpl | null; meta: TplMeta | null; readonly?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean]; saved: []; tagsChanged: [] }>()

const form = ref({ scope: 'PERSONAL', usage: 'MESSAGE', title: '', content: '', tags: [] as string[],
  attachments: [] as { file: string; name: string | null; url: string }[] })
const saving = ref(false)
const isNew = computed(() => !props.tpl)
const canSave = computed(() => !props.readonly && (isNew.value || props.tpl!.editable))
const max = computed(() => props.meta?.max_content || 1800)

watch(() => [props.modelValue, props.tpl], () => {
  if (!props.modelValue) return
  const t = props.tpl
  form.value = t
    ? { scope: t.scope, usage: t.usage, title: t.title, content: t.content, tags: [...t.tags], attachments: [...t.attachments] }
    : { scope: 'PERSONAL', usage: 'MESSAGE', title: '', content: '', tags: [], attachments: [] }
}, { immediate: true })

// 点占位符：插到内容光标处
const area = ref<any>(null)
async function insert(key: string) {
  if (props.readonly) return
  const el: HTMLTextAreaElement | undefined = area.value?.textarea
  const token = `{{${key}}}`
  const c = form.value.content
  const start = el?.selectionStart ?? c.length
  const end = el?.selectionEnd ?? c.length
  form.value.content = (c.slice(0, start) + token + c.slice(end)).slice(0, max.value)
  await nextTick()
  el?.focus()
  el?.setSelectionRange(start + token.length, start + token.length)
}

// 图片附件
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
async function onFiles(e: Event) {
  const files = [...((e.target as HTMLInputElement).files || [])]
  ;(e.target as HTMLInputElement).value = ''
  const room = (props.meta?.max_attachments || 6) - form.value.attachments.length
  if (files.length > room) ElMessage.warning(`最多 ${props.meta?.max_attachments || 6} 张图片，多出来的没有加`)
  uploading.value = true
  try {
    for (const f of files.slice(0, Math.max(room, 0))) {
      if (f.size > 5 * 1024 * 1024) {
        ElMessage.warning(`${f.name} 超过 5 MB`)
        continue
      }
      const data = await new Promise<string>((res, rej) => {
        const r = new FileReader()
        r.onload = () => res(String(r.result))
        r.onerror = rej
        r.readAsDataURL(f)
      })
      const up = await post<{ file: string; name: string; url: string }>('/internal/message-templates/files', { name: f.name, data })
      form.value.attachments.push(up)
    }
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    uploading.value = false
  }
}

async function save(asNew: boolean) {
  const f = form.value
  if (!f.title.trim() || !f.content.trim()) return ElMessage.warning('标题和内容都要填')
  let body: Record<string, unknown> = { ...f, attachments: f.attachments.map((a) => ({ file: a.file, name: a.name })) }
  if (asNew) {
    try {
      const { value } = await ElMessageBox.prompt('新模板的标题', '另存为', { inputValue: `${f.title}（副本）`, inputPattern: /\S/ })
      body = { ...body, title: value.trim(), scope: f.scope === 'SYSTEM' ? 'PERSONAL' : f.scope }
    } catch {
      return
    }
  }
  saving.value = true
  try {
    if (asNew || isNew.value) await post('/internal/message-templates', body)
    else await api('PUT', `/internal/message-templates/${props.tpl!.id}`, body)
    ElMessage.success(asNew ? '已另存为新模板' : '已保存')
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

// 标签管理：改名 / 删除（只改你能改的模板）
const tagMgr = ref(false)
async function renameTag(name: string) {
  try {
    const { value } = await ElMessageBox.prompt(`把标签「${name}」改成`, '标签改名', { inputValue: name, inputPattern: /\S/ })
    const r = await post<{ updated: number }>('/internal/message-templates/tags/rename', { old: name, new: value.trim() })
    form.value.tags = form.value.tags.map((t) => (t === name ? value.trim() : t))
    ElMessage.success(`已改 ${r.updated} 个模板`)
    emit('tagsChanged')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}
async function deleteTag(name: string) {
  try {
    await ElMessageBox.confirm(`从你能改的模板上去掉标签「${name}」？`, '删除标签', { type: 'warning' })
    const r = await post<{ updated: number }>('/internal/message-templates/tags/delete', { name })
    form.value.tags = form.value.tags.filter((t) => t !== name)
    ElMessage.success(`已从 ${r.updated} 个模板上去掉`)
    emit('tagsChanged')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}
const title = computed(() => (props.readonly ? '查看模板' : isNew.value ? '添加模板' : '编辑模板'))
</script>

<template>
  <el-dialog :model-value="modelValue" fullscreen :show-close="false" class="tpl-dialog" append-to-body
             @update:model-value="(v: boolean) => emit('update:modelValue', v)">
    <template #header>
      <div class="hd">
        <b>{{ title }}</b>
        <span v-if="tpl && !tpl.editable && !readonly" class="muted">（这个模板你不能直接改，可以“另存为”自己的模板）</span>
        <span class="spacer" />
        <template v-if="!readonly">
          <el-button v-if="canSave" type="primary" :loading="saving" @click="save(false)">保存</el-button>
          <el-button v-if="!isNew" type="primary" plain :loading="saving" @click="save(true)">另存为</el-button>
        </template>
        <button class="x" aria-label="关闭" @click="emit('update:modelValue', false)">×</button>
      </div>
    </template>

    <el-form label-width="70px" class="form" :disabled="readonly">
      <el-form-item label="类型">
        <el-radio-group v-model="form.scope">
          <el-radio v-if="meta?.can_edit_system || form.scope === 'SYSTEM'" value="SYSTEM" :disabled="!meta?.can_edit_system">系统预定义</el-radio>
          <el-radio value="COMPANY">企业模板</el-radio>
          <el-radio value="PERSONAL">个人模板</el-radio>
        </el-radio-group>
        <span class="hint">企业模板全员可见；个人模板只有你自己看得到</span>
      </el-form-item>
      <el-form-item label="用途">
        <el-radio-group v-model="form.usage">
          <el-radio value="MESSAGE">消息</el-radio>
          <el-radio value="FEEDBACK">评价</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="标题" required>
        <el-input v-model="form.title" maxlength="200" style="max-width: 460px" placeholder="如 FRD-70361-4-0734 编程步骤" />
      </el-form-item>
      <el-form-item label="内容" required>
        <div class="content-row">
          <el-input ref="area" v-model="form.content" type="textarea" class="content" :rows="14" :maxlength="max" show-word-limit
                    placeholder="用买家语言写；可以点右边的标签插入 {{buyer}} 等，发送时会换成实际内容" />
          <div class="ph-box">
            <div class="ph-title">使用模板标签制作通用短语<span class="muted">（点一下插入到光标处）</span>:</div>
            <div class="ph-grid">
              <button v-for="p in meta?.placeholders || []" :key="p.key" type="button" class="ph" :disabled="readonly"
                      @click="insert(p.key)"><span class="muted">{{ p.label }}:</span> {{ p.key }}</button>
            </div>
          </div>
        </div>
      </el-form-item>
      <el-form-item label="附件">
        <div class="atts">
          <div v-for="(a, i) in form.attachments" :key="a.file" class="att">
            <el-image :src="a.url" fit="cover" :preview-src-list="form.attachments.map((x) => x.url)" :initial-index="i" preview-teleported />
            <button v-if="!readonly" class="att-x" title="移除" @click="form.attachments.splice(i, 1)">×</button>
          </div>
          <button v-if="!readonly && form.attachments.length < (meta?.max_attachments || 6)" type="button" class="add" :disabled="uploading"
                  @click="fileInput?.click()">{{ uploading ? '上传中…' : '+ 添加图片' }}</button>
          <input ref="fileInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden @change="onFiles" />
        </div>
      </el-form-item>
      <el-form-item label="标签">
        <el-select v-model="form.tags" multiple filterable allow-create default-first-option :reserve-keyword="false"
                   placeholder="选择或输入新标签后回车" style="width: 320px">
          <el-option v-for="t in meta?.tags || []" :key="t" :label="t" :value="t" />
        </el-select>
        <el-button v-if="!readonly" link type="primary" class="ml" @click="tagMgr = true">标签管理</el-button>
      </el-form-item>
      <el-form-item v-if="tpl" label="记录">
        <span class="muted">{{ tpl.created_by || '—' }} 创建<template v-if="tpl.updated_by"> · {{ tpl.updated_by }} 最后修改</template></span>
      </el-form-item>
    </el-form>

    <el-dialog v-model="tagMgr" title="标签管理" width="420px" append-to-body>
      <div v-for="t in meta?.tags || []" :key="t" class="tag-row">
        <el-tag>{{ t }}</el-tag>
        <span class="spacer" />
        <el-button link size="small" @click="renameTag(t)">改名</el-button>
        <el-button link size="small" type="danger" @click="deleteTag(t)">删除</el-button>
      </div>
      <p v-if="!meta?.tags.length" class="muted">还没有标签；在“标签”框里输入新名字回车就能新建。</p>
      <p class="muted">改名、删除只作用于你能改的模板。</p>
    </el-dialog>
  </el-dialog>
</template>

<style scoped>
.hd {
  display: flex;
  align-items: center;
  gap: 10px;
}
.hd b {
  font-size: 17px;
}
.spacer {
  flex: 1;
}
.x {
  margin-left: 10px;
  border: 0;
  background: none;
  font-size: 22px;
  color: var(--muted);
  cursor: pointer;
}
.x:hover {
  color: var(--text);
}
.form {
  max-width: 1200px;
  padding: 20px 40px;
}
.hint {
  margin-left: 14px;
  font-size: 12px;
  color: var(--muted);
}
.content-row {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  width: 100%;
  flex-wrap: wrap;
}
.content {
  flex: 1 1 520px;
  max-width: 600px;
}
.content :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 13px;
}
.ph-box {
  flex: 0 1 420px;
  padding: 14px 16px;
  background: #fdf9f2;
  border: 1px solid #f3e6cf;
  border-radius: 4px;
}
.ph-title {
  font-size: 13px;
  margin-bottom: 8px;
}
.ph-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 12px;
}
.ph {
  text-align: left;
  border: 0;
  background: none;
  padding: 6px 4px;
  border-radius: 3px;
  font-family: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.ph:hover:not(:disabled) {
  background: var(--accent-soft);
  color: var(--accent);
}
.atts {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.att {
  position: relative;
  width: 100px;
  height: 90px;
}
.att .el-image {
  width: 100%;
  height: 100%;
  border-radius: 4px;
  border: 1px solid var(--line);
}
.att-x {
  position: absolute;
  top: -7px;
  right: -7px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 0;
  background: var(--danger);
  color: #fff;
  cursor: pointer;
  line-height: 18px;
}
.add {
  width: 100px;
  height: 90px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--panel);
  color: var(--accent);
  font-family: inherit;
  cursor: pointer;
}
.add:hover {
  border-color: var(--accent);
}
.ml {
  margin-left: 10px;
}
.tag-row {
  display: flex;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid var(--line-soft);
}
</style>

<style>
.tpl-dialog .el-dialog__header {
  padding: 12px 20px;
  margin: 0;
  border-bottom: 1px solid var(--line);
}
</style>
