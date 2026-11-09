<script setup lang="ts">
// 回复框里的“插入模板”：按类型、关键字找消息模板，点一下交给外面填进回复（占位符由后端按会话替换）
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, get } from '../api'
import { errorText } from '../format'
import type { Tpl } from './TemplateEditor.vue'

defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ pick: [t: Tpl] }>()

const open = ref(false)
const scope = ref('all')
const q = ref('')
const items = ref<Tpl[]>([])
const loading = ref(false)
const SCOPES = [
  { value: 'all', label: '全部模板' }, { value: 'mine', label: '我的模板' },
  { value: 'company', label: '企业模板' }, { value: 'system', label: '系统预定义' },
]

async function load() {
  loading.value = true
  try {
    const r = await get<{ items: Tpl[] }>(`/internal/message-templates?scope=${scope.value}&usage=MESSAGE`)
    items.value = r.items
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
// 星标的排前面，再按关键字过滤标题、内容、标签
const shown = computed(() => {
  const k = q.value.trim().toLowerCase()
  return items.value
    .filter((t) => !k || `${t.title}\n${t.content}\n${t.tags.join(' ')}`.toLowerCase().includes(k))
    .sort((a, b) => Number(b.starred) - Number(a.starred))
})
async function toggleStar(t: Tpl) {
  try {
    await api('PUT', `/internal/message-templates/${t.id}/star`, { starred: !t.starred })
    t.starred = !t.starred
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
function pick(t: Tpl) {
  open.value = false
  emit('pick', t)
}
</script>

<template>
  <el-popover v-model:visible="open" trigger="click" placement="top-start" :width="500" popper-class="tpl-picker"
              @show="load">
    <template #reference>
      <span class="trigger"><slot><el-button :disabled="disabled">插入模板</el-button></slot></span>
    </template>
    <div class="bar">
      <el-select v-model="scope" size="small" style="width: 118px" @change="load">
        <el-option v-for="s in SCOPES" :key="s.value" :label="s.label" :value="s.value" />
      </el-select>
      <el-input v-model="q" size="small" clearable placeholder="搜索标题、内容、标签" />
      <a class="manage" href="#/templates" target="_blank" rel="noopener">管理模板 ↗</a>
    </div>
    <div v-loading="loading" class="list">
      <div v-for="t in shown" :key="t.id" class="item" @click="pick(t)">
        <div class="t-head">
          <button class="star" :class="{ on: t.starred }" @click.stop="toggleStar(t)">{{ t.starred ? '★' : '☆' }}</button>
          <b class="t-title">{{ t.title }}</b>
          <span v-if="t.attachments.length" class="muted">🖼{{ t.attachments.length }}</span>
          <span class="muted scope">{{ { SYSTEM: '系统', COMPANY: '企业', PERSONAL: '个人' }[t.scope] }}</span>
        </div>
        <div class="t-body">{{ t.content }}</div>
      </div>
      <div v-if="!loading && !shown.length" class="empty muted">没有找到模板，可以在「智能客服 → 消息模板」里添加</div>
    </div>
    <div class="muted tip">点一下填进回复；<span v-pre>{{buyer}}</span> 等会换成这个会话的买家、订单、运单信息，取不到的会提示你补。</div>
  </el-popover>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.manage {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--accent);
  white-space: nowrap;
}
.list {
  max-height: 340px;
  min-height: 80px;
  overflow-y: auto;
  margin: 0 -12px;
}
.item {
  padding: 8px 12px;
  border-bottom: 1px solid var(--line-soft);
  cursor: pointer;
}
.item:hover {
  background: var(--accent-soft);
}
.t-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.t-title {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.scope {
  font-size: 11px;
}
.star {
  border: 0;
  background: none;
  padding: 0;
  color: var(--muted);
  cursor: pointer;
}
.star.on {
  color: #f5a623;
}
.t-body {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-line;
}
.empty {
  padding: 20px 12px;
  text-align: center;
}
.tip {
  margin-top: 8px;
  font-size: 11px;
}
</style>
