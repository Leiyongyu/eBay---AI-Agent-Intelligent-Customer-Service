<script setup lang="ts">
// 售后管理的快捷标签配置：新建、改颜色、改名、删除（删除会从所有记录上去掉）
import { ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { TAG_COLORS, TAG_DOT } from '../caseCommon'
import type { CaseTag } from '../caseCommon'

const props = defineProps<{ modelValue: boolean; tags: CaseTag[]; title: string }>()
const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  create: [name: string, color: string]
  update: [t: CaseTag, body: Record<string, unknown>]
  remove: [t: CaseTag]
}>()

const name = ref('')
const color = ref('primary')
function add() {
  if (!name.value.trim()) return
  emit('create', name.value.trim(), color.value)
  name.value = ''
}
async function rename(t: CaseTag) {
  try {
    const { value } = await ElMessageBox.prompt('新的标签名', '改名', { inputValue: t.name, inputPattern: /\S/, inputErrorMessage: '不能为空' })
    emit('update', t, { name: value })
  } catch {
    /* 取消 */
  }
}
async function remove(t: CaseTag) {
  try {
    await ElMessageBox.confirm(`删除标签「${t.name}」？已经打上这个标签的记录会一起去掉它。`, '删除标签', { type: 'warning' })
    emit('remove', t)
  } catch {
    /* 取消 */
  }
}
</script>

<template>
  <el-dialog :model-value="props.modelValue" :title="`${title}的快捷标签`" width="480px" append-to-body
             @update:model-value="(v: boolean) => emit('update:modelValue', v)">
    <!-- 最多显示 10 行，多了在框里上下滑动 -->
    <div class="list">
    <div v-for="t in tags" :key="t.id" class="row">
      <i class="dot" :style="{ background: TAG_DOT[t.color] || TAG_DOT.info }" />
      <span class="name">{{ t.name }}</span>
      <el-select :model-value="t.color" size="small" style="width: 72px" @update:model-value="(v: string) => emit('update', t, { color: v })">
        <el-option v-for="c in TAG_COLORS" :key="c.value" :label="c.label" :value="c.value" />
      </el-select>
      <el-button link size="small" @click="rename(t)">改名</el-button>
      <el-button link size="small" type="danger" @click="remove(t)">删除</el-button>
    </div>
    <div v-if="!tags.length" class="muted empty">还没有标签，在下面新建</div>
    </div>
    <div class="row add">
      <el-input v-model="name" size="small" maxlength="32" placeholder="新标签，如 不退回、已询问VIN" @keyup.enter="add" />
      <el-select v-model="color" size="small" style="width: 72px">
        <el-option v-for="c in TAG_COLORS" :key="c.value" :label="c.label" :value="c.value" />
      </el-select>
      <el-button size="small" type="primary" :disabled="!name.trim()" @click="add">新建</el-button>
    </div>
    <p class="muted">标签只在本系统，同事共用；点列表上方的标签可以快速筛选。</p>
  </el-dialog>
</template>

<style scoped>
.list {
  max-height: 360px; /* 10 行 × 36px */
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 10px;
  border-bottom: 1px solid var(--line-soft);
}
.list .row:last-child {
  border-bottom: 0;
}
.row.add {
  border-bottom: 0;
  margin-top: 10px;
  padding: 0;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.name {
  flex: 1;
}
.empty {
  padding: 10px;
}
</style>
