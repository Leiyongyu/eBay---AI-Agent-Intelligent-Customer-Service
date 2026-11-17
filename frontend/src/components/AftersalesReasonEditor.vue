<script setup lang="ts">
// 人工设置售后原因：下拉选 6 种 + 备注，或直接输入一行“产品问题 XXX”（以后端解析为准，这里只做预览）
import { computed, ref, watch } from 'vue'
import { REASONS, REASON_LABEL, parseReasonText } from '../aftersalesReason'
import type { AftersalesReasonCode } from '../types'

const props = defineProps<{ reason?: AftersalesReasonCode | null; note?: string | null; saving?: boolean; compact?: boolean }>()
const emit = defineEmits<{ save: [body: { reason: AftersalesReasonCode; note: string } | { text: string }]; cancel: [] }>()

const mode = ref<'pick' | 'text'>('pick')
const reason = ref<AftersalesReasonCode | ''>(props.reason || '')
const note = ref(props.note || '')
const text = ref(props.reason ? `${REASON_LABEL[props.reason]}${props.note ? ` ${props.note}` : ''}` : '')
watch(() => [props.reason, props.note], () => {
  reason.value = props.reason || ''
  note.value = props.note || ''
})
const parsed = computed(() => parseReasonText(text.value))
const canSave = computed(() => (mode.value === 'pick' ? !!reason.value : !!parsed.value))
function save() {
  if (!canSave.value) return
  if (mode.value === 'pick') emit('save', { reason: reason.value as AftersalesReasonCode, note: note.value.trim() })
  else emit('save', { text: text.value.trim() })
}
</script>

<template>
  <div class="are" :class="{ compact }">
    <div class="are-mode">
      <button :class="{ on: mode === 'pick' }" type="button" @click="mode = 'pick'">选择</button>
      <button :class="{ on: mode === 'text' }" type="button" @click="mode = 'text'">一行文本</button>
    </div>
    <template v-if="mode === 'pick'">
      <el-select v-model="reason" size="small" placeholder="售后原因" class="are-sel">
        <el-option v-for="r in REASONS" :key="r.code" :value="r.code" :label="r.label">
          <span>{{ r.label }}</span><span class="are-hint">{{ r.hint }}</span>
        </el-option>
      </el-select>
      <el-input v-model="note" size="small" maxlength="200" placeholder="备注（可空）" class="are-note" @keyup.enter="save" />
    </template>
    <template v-else>
      <el-input v-model="text" size="small" maxlength="220" placeholder="如：产品问题 漏油，装上一周就坏" class="are-text" @keyup.enter="save" />
      <div class="are-preview" :class="{ bad: text.trim() && !parsed }">
        <template v-if="parsed">识别为：<b>{{ REASON_LABEL[parsed.code] }}</b><span v-if="parsed.note"> · 备注“{{ parsed.note }}”</span></template>
        <template v-else-if="text.trim()">开头要是 6 种原因之一，后面空格再写备注</template>
        <template v-else>{{ REASONS.map((r) => r.label).join(' / ') }}</template>
      </div>
    </template>
    <div class="are-act">
      <el-button size="small" type="primary" :disabled="!canSave" :loading="saving" @click="save">保存</el-button>
      <el-button size="small" @click="emit('cancel')">取消</el-button>
    </div>
  </div>
</template>

<style scoped>
.are {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.are-mode {
  display: inline-flex;
  align-self: flex-start;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.are-mode button {
  border: 0;
  background: var(--panel);
  font: inherit;
  font-size: 12px;
  padding: 2px 10px;
  color: var(--text-2);
  cursor: pointer;
}
.are-mode button + button {
  border-left: 1px solid var(--line);
}
.are-mode button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.are-sel,
.are-note,
.are-text {
  width: 100%;
}
.are-hint {
  margin-left: 8px;
  font-size: 12px;
  color: var(--muted);
}
.are-preview {
  font-size: 12px;
  color: var(--muted);
}
.are-preview.bad {
  color: var(--danger-text);
}
.are-act {
  display: flex;
  gap: 6px;
}
</style>
