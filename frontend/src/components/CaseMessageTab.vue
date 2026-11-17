<script setup lang="ts">
// 纠纷处理弹窗的“给买家发信”：可让 AI 先起草（用会话现有的草稿流程，上下文里有这条售后单的状态），
// 提交后进发件箱（合规检查、审核记录、延迟发送可撤回），到时间发到 eBay 售后单的留言（Post-Order send_message）
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'
import TemplatePicker from './TemplatePicker.vue'
import type { Tpl } from './TemplateEditor.vue'

const props = defineProps<{ convId: number | null; blocked: string | null; busy: boolean }>()
const emit = defineEmits<{ submit: [payload: { text: string; text_zh: string | null }] }>()

const text = ref('')
const textZh = ref<string | null>(null)
const drafting = ref(false)

function pickTemplate(t: Tpl) {
  text.value = text.value ? `${text.value}\n${t.content}` : t.content
  textZh.value = null
}

/** 让 AI 起草：在会话上生成草稿（覆盖店铺最后一次回复之后的买家消息），等生成完填进输入框，不发送 */
async function aiDraft() {
  if (!props.convId) return
  drafting.value = true
  try {
    const job = await post<{ job_id: number; run_generation: number }>(`/internal/conversations/${props.convId}/generate-draft`, {})
    for (let i = 0; i < 45; i++) {
      await new Promise((r) => setTimeout(r, 2000))
      const d = await get<any>(`/internal/conversations/${props.convId}`)
      const st = d.job?.id === job.job_id ? d.job.state : null
      if (st && ['RECEIVED', 'RUNNING'].includes(st)) continue
      const draft = d.agent_draft
      if (draft && draft.job_id === job.job_id && draft.run_generation === job.run_generation && draft.text) {
        text.value = draft.text
        textZh.value = draft.text_zh || null
        ElMessage.success('AI 草稿已填进输入框，请核对后再提交')
      } else {
        ElMessage.warning(d.job?.last_error ? `AI 没有生成草稿：${d.job.last_error}` : 'AI 没有生成草稿（可能判断要转人工），请人工撰写')
      }
      return
    }
    ElMessage.warning('AI 起草超时，可稍后到会话里查看草稿')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    drafting.value = false
  }
}
function submit() {
  emit('submit', { text: text.value, text_zh: textZh.value })
}
defineExpose({ clear: () => { text.value = ''; textZh.value = null } })
</script>

<template>
  <div class="msg-wrap">
    <el-form label-width="90px" class="plan-form grow">
      <el-form-item label="发信内容" required>
        <el-input v-model="text" type="textarea" :rows="10" maxlength="1000" show-word-limit placeholder="用买家的语言写"
                  @input="textZh = null" />
        <div v-if="textZh" class="zh muted small">中文对照：{{ textZh }}</div>
      </el-form-item>
      <el-form-item>
        <el-tooltip :disabled="!!convId" content="这张售后单对不上买家会话，不能让 AI 起草">
          <span><el-button :loading="drafting" :disabled="!convId" @click="aiDraft">AI 起草</el-button></span>
        </el-tooltip>
        <el-tooltip :disabled="!blocked" :content="blocked || ''">
          <span><el-button type="success" :loading="busy" :disabled="!!blocked || !text.trim()" @click="submit">提交</el-button></span>
        </el-tooltip>
        <span class="muted small hint">提交后进发件箱，延迟发出、之前可在会话里撤回；违规内容会被拦下</span>
      </el-form-item>
    </el-form>
    <div class="tpl"><span class="muted small">消息模板</span> <TemplatePicker @pick="pickTemplate" /></div>
  </div>
</template>

<style scoped>
.msg-wrap { display: flex; gap: 20px; }
.grow { flex: 1; }
.plan-form { padding-top: 6px; }
.tpl { width: 260px; padding-top: 6px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.hint { margin-left: 10px; }
.zh { white-space: pre-wrap; margin-top: 4px; line-height: 1.5; }
</style>
