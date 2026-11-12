<script setup lang="ts">
// 售后管理的标签管理：所有人都能新建；改名、改颜色、固定到快捷标签栏、删除（会从所有记录上去掉）只有管理员
import { ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import type { CaseTag } from '../caseCommon'
import { TAG_PALETTE, dotStyle, pickerValue } from '../tagColor'

const props = defineProps<{ modelValue: boolean; tags: CaseTag[]; title: string; isAdmin: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  create: [name: string, color: string]
  update: [t: CaseTag, body: Record<string, unknown>]
  remove: [t: CaseTag]
}>()

const name = ref('')
const color = ref<string>(TAG_PALETTE[0])
function add() {
  if (!name.value.trim()) return
  emit('create', name.value.trim(), color.value || TAG_PALETTE[0])
  name.value = ''
  color.value = TAG_PALETTE[(props.tags.length + 1) % TAG_PALETTE.length]
}
async function rename(t: CaseTag) {
  try {
    const { value } = await ElMessageBox.prompt('新的标签名', '改名', { inputValue: t.name, inputPattern: /\S/, inputErrorMessage: '不能为空' })
    if (value.trim() !== t.name) emit('update', t, { name: value.trim() })
  } catch {
    /* 取消 */
  }
}
function recolor(t: CaseTag, v: string | null) {
  if (v && v.toLowerCase() !== pickerValue(t.color).toLowerCase()) emit('update', t, { color: v.toLowerCase() })
}
async function remove(t: CaseTag) {
  try {
    await ElMessageBox.confirm(`删除标签「${t.name}」？已经打上这个标签的记录会一起去掉它，不能恢复。`, '删除标签',
      { type: 'warning', confirmButtonText: '删除' })
    emit('remove', t)
  } catch {
    /* 取消 */
  }
}
</script>

<template>
  <el-dialog :model-value="props.modelValue" :title="`${title}的标签管理`" width="min(520px, calc(100vw - 24px))" append-to-body
             @update:model-value="(v: boolean) => emit('update:modelValue', v)">
    <!-- 最多显示 10 行，多了在框里上下滑动 -->
    <div class="list">
      <div v-for="t in tags" :key="t.id" class="row">
        <el-color-picker v-if="isAdmin" :model-value="pickerValue(t.color)" size="small" :predefine="TAG_PALETTE" title="改颜色"
                         @change="(v: string | null) => recolor(t, v)" />
        <i v-else class="dot" :style="dotStyle(t.color)" />
        <span class="name">{{ t.name }}</span>
        <span v-if="t.created_by" class="by" :title="`由 ${t.created_by} 新建`">{{ t.created_by }}</span>
        <template v-if="isAdmin">
          <el-switch :model-value="t.pinned" size="small" inline-prompt active-text="固定" inactive-text="不固定"
                     title="固定 = 显示在列表上方的快捷标签栏" @change="(v: string | number | boolean) => emit('update', t, { pinned: !!v })" />
          <el-button link size="small" @click="rename(t)">改名</el-button>
          <el-button link size="small" type="danger" @click="remove(t)">删除</el-button>
        </template>
        <span v-else-if="t.pinned" class="by">已固定</span>
      </div>
      <div v-if="!tags.length" class="muted empty">还没有标签，在下面新建</div>
    </div>
    <div class="row add">
      <el-color-picker v-model="color" size="small" :predefine="TAG_PALETTE" title="标签颜色" />
      <el-input v-model="name" size="small" maxlength="32" placeholder="新标签，如 妥投未收到、已提供POD" @keyup.enter="add" />
      <el-button size="small" type="primary" :disabled="!name.trim()" @click="add">新建</el-button>
    </div>
    <p class="muted">
      标签只在本系统，同事共用；所有人都可以新建。<template v-if="isAdmin">改名、改颜色、固定、删除只有管理员能做。</template>
      <template v-else>改名、改颜色、固定、删除请找管理员。</template>
      固定的标签显示在列表上方，点一下就能筛选。
    </p>
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
  min-height: 36px;
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
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.by {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.empty {
  padding: 10px;
}
</style>
