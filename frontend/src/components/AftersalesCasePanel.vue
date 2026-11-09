<script setup lang="ts">
// 飞书售后群工单：AI 按划分规则起草（质量问题、破损错发缺件、安装技术问题、发 VIN 核对适配），客服核对后点“发到售后群”；
// 群里回复后 AI 起草买家回复，点“填入回复框”审核修改后再发送（一律人工审核）。发出后在群里话题回帖“已回复买家”。
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post } from '../api'
import { errorText, fmtTime } from '../format'

interface CaseReply { message_id: string; sender: string; text: string; at: string; via: string; files: string[] }
interface Case {
  id: number; title: string; status: string; status_label: string; category: string; case_type: string
  problem_zh: string; fault_info: string | null; original_text: string; missing: string[]
  attachments: { id: number; file_url: string }[]; replies: CaseReply[]
  draft_reply: string | null; draft_reply_zh: string | null; sent_by: string | null; sent_at: string | null
  origin: string | null
}

const props = defineProps<{ convId: number; refreshKey?: string }>()
const emit = defineEmits<{ insert: [text: string] }>()

const c = ref<Case | null>(null)
const ready = ref(false)
const open = ref(true)
const busy = ref(false)
const editing = ref(false)
const form = ref({ problem_zh: '', fault_info: '', original_text: '', attachment_ids: [] as number[] })
const TYPE: Record<string, 'warning' | 'primary' | 'danger' | 'success' | 'info'> = {
  DRAFT: 'warning', SENT: 'primary', REPLIED: 'danger', ANSWERED: 'success', CLOSED: 'info',
}
const CATEGORIES = ['质量问题', '破损错发缺件', '安装技术问题', '适配核对']
const newCategory = ref('质量问题')

async function load() {
  try {
    const r = await get<{ case: Case | null; feishu_ready: boolean }>(`/internal/conversations/${props.convId}/aftersales-case`)
    c.value = r.case
    ready.value = r.feishu_ready
  } catch {
    c.value = null
  }
}

function startEdit() {
  if (!c.value) return
  form.value = {
    problem_zh: c.value.problem_zh, fault_info: c.value.fault_info || '', original_text: c.value.original_text,
    attachment_ids: c.value.attachments.map((a) => a.id),
  }
  editing.value = true
}

async function save() {
  if (!c.value) return
  try {
    c.value = await api<Case>('PATCH', `/internal/aftersales-cases/${c.value.id}`, form.value)
    editing.value = false
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function create() {
  busy.value = true
  try {
    c.value = await post<Case>(`/internal/conversations/${props.convId}/aftersales-case`, { category: newCategory.value })
    open.value = true
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function send() {
  if (!c.value) return
  if (c.value.missing.length) {
    try {
      await ElMessageBox.confirm(`资料还缺：${c.value.missing.join('、')}。建议先向买家要齐再发。仍然发到售后群？`, '资料不全', { type: 'warning' })
    } catch {
      return
    }
  }
  busy.value = true
  try {
    c.value = await post<Case>(`/internal/aftersales-cases/${c.value.id}/send`, {})
    ElMessage.success('已发到飞书售后群')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function action(path: 'redraft' | 'close') {
  if (!c.value) return
  busy.value = true
  try {
    c.value = await post<Case>(`/internal/aftersales-cases/${c.value.id}/${path}`, {})
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

const canSend = computed(() => !!c.value && c.value.status === 'DRAFT' && ready.value)
watch(() => [props.convId, props.refreshKey], load, { immediate: true })
</script>

<template>
  <div class="ac-box">
    <div class="ac-head">
      <el-tag size="small" type="danger" effect="dark">售后群工单</el-tag>
      <template v-if="c">
        <b>{{ c.title }}</b>
        <el-tag size="small" effect="plain" :type="TYPE[c.status]">{{ c.status_label }}</el-tag>
        <span class="muted">{{ c.category }}<template v-if="c.origin === 'AI'"> · AI 建议</template></span>
      </template>
      <span v-else class="muted">这个会话还没有工单</span>
      <span class="spacer" />
      <template v-if="!c || c.status === 'CLOSED'">
        <el-select v-model="newCategory" size="small" style="width: 130px">
          <el-option v-for="x in CATEGORIES" :key="x" :label="x" :value="x" />
        </el-select>
        <el-button size="small" :loading="busy" @click="create">起草工单</el-button>
      </template>
      <el-button v-if="c" link size="small" @click="open = !open">{{ open ? '收起' : '展开' }}</el-button>
    </div>
    <div v-if="c && open" class="ac-body">
      <template v-if="!editing">
        <div><span class="muted">问题（中文）：</span><span class="pre">{{ c.problem_zh || '（未翻译，发群时自动翻译）' }}</span></div>
        <div><span class="muted">故障码 / VIN：</span><span class="pre">{{ c.fault_info || '未提供' }}</span></div>
        <div v-if="c.attachments.length" class="thumbs">
          <a v-for="a in c.attachments" :key="a.id" :href="a.file_url" target="_blank"><img :src="a.file_url" alt="" /></a>
        </div>
        <div v-if="c.missing.length && c.status === 'DRAFT'" class="warn">资料还缺：{{ c.missing.join('、') }}（建议先向买家要齐）</div>
      </template>
      <div v-else class="edit">
        <el-input v-model="form.problem_zh" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="问题（中文）" />
        <el-input v-model="form.fault_info" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" placeholder="故障码 / VIN" />
        <el-input v-model="form.original_text" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="买家原文" />
        <div v-if="c.attachments.length" class="muted">发到群里的图片：
          <el-checkbox-group v-model="form.attachment_ids">
            <el-checkbox v-for="a in c.attachments" :key="a.id" :value="a.id"><img :src="a.file_url" class="mini" alt="" /></el-checkbox>
          </el-checkbox-group>
        </div>
      </div>
      <div v-if="c.status === 'DRAFT'" class="row">
        <template v-if="editing">
          <el-button size="small" type="primary" @click="save">保存</el-button>
          <el-button size="small" @click="editing = false">取消</el-button>
        </template>
        <template v-else>
          <el-button size="small" type="primary" :disabled="!canSend" :loading="busy" @click="send">发到售后群</el-button>
          <el-button size="small" @click="startEdit">修改</el-button>
          <span v-if="!ready" class="warn">飞书售后群没有配置好（.env FEISHU_CASE_ENABLED / FEISHU_CASE_CHAT_ID）</span>
        </template>
      </div>
      <div v-if="c.sent_at" class="muted">{{ c.sent_by }} 于 {{ fmtTime(c.sent_at) }} 发到售后群</div>

      <div v-if="c.replies.length" class="replies">
        <div class="muted">群里的回复（内部信息，不要原样发给买家）：</div>
        <div v-for="r in c.replies" :key="r.message_id" class="reply">
          <b>{{ r.sender }}</b><span class="muted"> · {{ fmtTime(r.at) }}{{ r.via === 'mention' ? ' · @机器人' : '' }}</span>
          <div class="pre">{{ r.text }}</div>
          <div v-if="r.files.length" class="thumbs">
            <a v-for="f in r.files" :key="f" :href="f" target="_blank"><img :src="f" alt="" /></a>
          </div>
        </div>
      </div>
      <div v-if="c.draft_reply" class="sugg">
        <div class="muted">AI 按群里回复起草的买家回复（需人工审核后发送）：</div>
        <div class="pre">{{ c.draft_reply }}</div>
        <div v-if="c.draft_reply_zh" class="muted pre">中文：{{ c.draft_reply_zh }}</div>
        <div class="row">
          <el-button size="small" type="primary" plain @click="emit('insert', c.draft_reply!)">填入回复框</el-button>
          <el-button size="small" :loading="busy" @click="action('redraft')">重新起草</el-button>
        </div>
      </div>
      <div v-if="c.status !== 'DRAFT' && c.status !== 'CLOSED'" class="row">
        <el-button size="small" :loading="busy" @click="action('close')">完结工单</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ac-box {
  border-bottom: 1px solid var(--line);
  background: #fff5f5;
  padding: 6px 12px;
  font-size: 13px;
}
.ac-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.ac-body {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  line-height: 1.6;
  max-height: 360px;
  overflow: auto;
}
.pre {
  white-space: pre-wrap;
}
.warn {
  color: #d97706;
}
.thumbs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.thumbs img {
  max-height: 90px;
  max-width: 140px;
  border-radius: 4px;
  border: 1px solid var(--line);
}
.mini {
  max-height: 48px;
  vertical-align: middle;
}
.edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.replies {
  border-left: 3px solid #e6a23c;
  padding-left: 8px;
}
.reply + .reply {
  margin-top: 4px;
}
.sugg {
  background: #fff;
  border: 1px dashed #e6a23c;
  border-radius: 6px;
  padding: 6px 8px;
}
</style>
