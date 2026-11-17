<script setup lang="ts">
// 内部参考：按会话场景（售后类型、售后工单、AI 识别的意图）列出知识库里相关的“仅内部”条款，只给客服、售后看。
// 这些条款不发给买家，也不进 AI 提示词；条款的“适用场景”在知识库 Excel 里维护。
import { computed, ref, watch } from 'vue'
import { get } from '../api'
import DocLinks, { type DocRef } from './DocLinks.vue'

interface Ref { clause_id: string; title: string; text: string; priority: number; scenes: string[]; pinned: boolean; docs?: DocRef[] }
interface Refs { scenes: { scene: string; why: string }[]; items: Ref[]; pinned: Ref[]; more: number }

const props = defineProps<{ convId: number; refreshKey?: string }>()
const KEY = 'internalRefsOpen'
const data = ref<Refs | null>(null)
const open = ref(readOpen())
const expanded = ref<Set<string>>(new Set())

function readOpen() {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}
function toggleOpen() {
  open.value = !open.value
  try {
    localStorage.setItem(KEY, open.value ? '1' : '0')
  } catch {
    /* 浏览器禁用存储时只是不记住 */
  }
}
function toggle(id: string) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expanded.value = s
}

async function load() {
  try {
    data.value = await get<Refs>(`/internal/conversations/${props.convId}/internal-refs`)
  } catch {
    data.value = null
  }
}
watch(() => [props.convId, props.refreshKey], () => {
  expanded.value = new Set()
  load()
}, { immediate: true })

const list = computed(() => (data.value ? [...data.value.items, ...data.value.pinned] : []))
</script>

<template>
  <div v-if="data && list.length" class="ir-box">
    <div class="ir-head">
      <el-tag size="small" type="info" effect="dark">内部参考</el-tag>
      <span class="muted">不对买家说</span>
      <el-tag v-for="s in data.scenes" :key="s.scene" size="small" effect="plain" :title="s.why">{{ s.scene }}</el-tag>
      <span class="muted">{{ data.items.length }} 条相关<template v-if="data.more">（另有 {{ data.more }} 条见知识库）</template></span>
      <span class="spacer" />
      <el-button link size="small" @click="toggleOpen">{{ open ? '收起' : '展开' }}</el-button>
    </div>
    <div v-if="open" class="ir-body">
      <div v-if="!data.items.length" class="muted">这个会话还没识别出场景（客服标售后类型或 AI 起草后会自动匹配）</div>
      <div v-for="r in list" :key="r.clause_id" class="ir-item">
        <div class="ir-title" @click="toggle(r.clause_id)">
          <span class="arrow">{{ expanded.has(r.clause_id) ? '▾' : '▸' }}</span>
          <b>{{ r.title }}</b>
          <span class="muted">{{ r.clause_id }}<template v-if="r.pinned"> · 常驻</template></span>
        </div>
        <div v-if="expanded.has(r.clause_id)" class="ir-text">{{ r.text }}</div>
        <div v-if="expanded.has(r.clause_id) && r.docs?.length" class="ir-docs">
          <span class="muted">参考文档：</span>
          <DocLinks :docs="r.docs" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ir-box {
  border-bottom: 1px solid var(--line);
  background: #f6f8fb;
  padding: 6px 12px;
  font-size: 13px;
}
.ir-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.ir-body {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 320px;
  overflow: auto;
}
.ir-title {
  cursor: pointer;
  display: flex;
  gap: 6px;
  align-items: baseline;
}
.arrow {
  width: 12px;
  color: var(--el-text-color-secondary);
}
.ir-text {
  white-space: pre-wrap;
  line-height: 1.6;
  margin: 2px 0 6px 18px;
  padding: 6px 8px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.ir-docs {
  display: flex;
  gap: 4px;
  align-items: flex-start;
  margin: 4px 0 0 16px;
  font-size: 12px;
}
</style>
