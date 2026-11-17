<script setup lang="ts">
// 售后管理的标签筛选（咨询 / 纠纷 / 退货 / 付款纠纷 / 取消 / 退款单 / 评价共用）：
// 搜索、勾选多个标签、全选、或 / 且，搜不到可以直接新建（所有人都可以）；
// 图钉 = 固定到列表上方的快捷标签栏，只有管理员能点；「标签管理」打开配置弹窗（改名、颜色、删除只有管理员）。
import { computed, ref } from 'vue'
import type { CaseTag } from '../caseCommon'
import { TAG_PALETTE, tagHex } from '../tagColor'

const props = defineProps<{
  tags: CaseTag[]
  modelValue: string[] // 选中的标签 id
  mode: 'any' | 'all'
  counts?: Record<string, number> | null
  isAdmin: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [v: string[]]
  'update:mode': [v: 'any' | 'all']
  create: [name: string, color: string, done: (t: CaseTag | null) => void]
  pin: [t: CaseTag, pinned: boolean]
  manage: []
  apply: []
}>()

const open = ref(false)
const q = ref('')
const creating = ref(false)
const shown = computed(() => {
  const k = q.value.trim().toLowerCase()
  return k ? props.tags.filter((t) => t.name.toLowerCase().includes(k)) : props.tags
})
const exact = computed(() => props.tags.some((t) => t.name.toLowerCase() === q.value.trim().toLowerCase()))
const picked = computed(() => new Set(props.modelValue))
const allShownPicked = computed(() => shown.value.length > 0 && shown.value.every((t) => picked.value.has(String(t.id))))

function toggle(t: CaseTag) {
  const id = String(t.id)
  emit('update:modelValue', picked.value.has(id) ? props.modelValue.filter((x) => x !== id) : [...props.modelValue, id])
}
function toggleAll() {
  const ids = shown.value.map((t) => String(t.id))
  emit('update:modelValue', allShownPicked.value
    ? props.modelValue.filter((x) => !ids.includes(x))
    : [...new Set([...props.modelValue, ...ids])])
}
function create() {
  const name = q.value.trim()
  if (!name || exact.value || creating.value) return
  creating.value = true
  const color = TAG_PALETTE[props.tags.length % TAG_PALETTE.length]
  emit('create', name, color, (t) => {
    creating.value = false
    if (!t) return
    q.value = ''
    emit('update:modelValue', [...props.modelValue, String(t.id)]) // 新建的顺手选上
  })
}
function onEnter() {
  const k = q.value.trim().toLowerCase()
  const hit = props.tags.find((t) => t.name.toLowerCase() === k)
  if (hit) toggle(hit)
  else create()
}
function apply() {
  open.value = false
  emit('apply')
}
function clear() {
  emit('update:modelValue', [])
}
const label = computed(() => {
  const names = props.modelValue.map((id) => props.tags.find((t) => String(t.id) === id)?.name).filter(Boolean)
  if (!names.length) return ''
  return names.length <= 2 ? names.join('、') : `${names[0]} 等 ${names.length} 个`
})
const nameColor = (t: CaseTag) => tagHex(t.color) || 'var(--text)'
</script>

<template>
  <el-popover v-model:visible="open" trigger="click" placement="bottom-start" :width="300" popper-class="ctf-pop" :show-arrow="false">
    <template #reference>
      <button type="button" class="ctf-btn" :class="{ on: modelValue.length }" :title="label || '按标签筛选'">
        <span v-if="label" class="v">{{ label }}<em v-if="modelValue.length > 1">（{{ mode === 'all' ? '且' : '或' }}）</em></span>
        <span v-else class="ph">标签</span>
        <span v-if="modelValue.length" class="x" title="清空标签筛选" @click.stop="clear(); emit('apply')">×</span>
        <span v-else class="arr">▾</span>
      </button>
    </template>

    <div class="ctf">
      <div class="head">
        <el-input v-model="q" size="small" clearable placeholder="搜索标签，回车新建" @keyup.enter="onEnter" />
      </div>
      <div class="bar">
        <button type="button" class="lnk" :disabled="!shown.length" @click="toggleAll">{{ allShownPicked ? '全不选' : '全选' }}</button>
        <span class="seg" title="选了多个标签时：或 = 有其中一个就算；且 = 全都有才算">
          <button type="button" :class="{ on: mode === 'any' }" @click="emit('update:mode', 'any')">或</button>
          <button type="button" :class="{ on: mode === 'all' }" @click="emit('update:mode', 'all')">且</button>
        </span>
        <span class="sp" />
        <button type="button" class="lnk" @click="open = false; emit('manage')">标签管理</button>
      </div>
      <div class="list">
        <div v-for="t in shown" :key="t.id" class="row" :class="{ on: picked.has(String(t.id)) }" @click="toggle(t)">
          <el-checkbox :model-value="picked.has(String(t.id))" size="small" @click.stop @change="toggle(t)" />
          <span class="name" :style="{ color: nameColor(t) }">{{ t.name }}</span>
          <span v-if="counts" class="n">{{ counts[String(t.id)] || 0 }}</span>
          <span class="sp" />
          <span v-if="isAdmin" class="fix">{{ t.pinned ? '取消固定' : '固定' }}</span>
          <button v-if="isAdmin" type="button" class="pin" :class="{ on: t.pinned }"
                  :title="t.pinned ? '已固定在快捷标签栏，点击取消' : '固定到列表上方的快捷标签栏'" @click.stop="emit('pin', t, !t.pinned)">
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor"
              d="M14.5 2.5 21.5 9.5 19.4 11.6 18 10.2 13.8 14.4 14.5 19.3 12.4 21.4 8.5 17.5 3.9 22.1 1.9 20.1 6.5 15.5 2.6 11.6 4.7 9.5 9.6 10.2 13.8 6 12.4 4.6Z" /></svg>
          </button>
          <span v-else-if="t.pinned" class="pin on ro" title="已固定在快捷标签栏（只有管理员可以改）">
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor"
              d="M14.5 2.5 21.5 9.5 19.4 11.6 18 10.2 13.8 14.4 14.5 19.3 12.4 21.4 8.5 17.5 3.9 22.1 1.9 20.1 6.5 15.5 2.6 11.6 4.7 9.5 9.6 10.2 13.8 6 12.4 4.6Z" /></svg>
          </span>
        </div>
        <button v-if="q.trim() && !exact" type="button" class="row add" :disabled="creating" @click="create">
          + 新建标签「{{ q.trim() }}」
        </button>
        <div v-if="!shown.length && !q.trim()" class="empty">还没有标签，在上面输入名字回车即可新建</div>
      </div>
      <div class="foot">
        <span class="muted">已选 {{ modelValue.length }} 个</span>
        <span class="sp" />
        <el-button size="small" link :disabled="!modelValue.length" @click="clear">清空</el-button>
        <el-button size="small" type="primary" @click="apply">筛选</el-button>
      </div>
    </div>
  </el-popover>
</template>

<style scoped>
.ctf-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  min-width: 110px;
  max-width: 220px;
  padding: 0 10px;
  border: 1px solid var(--el-border-color, var(--line));
  border-radius: 4px;
  background: var(--panel);
  font: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.ctf-btn:hover,
.ctf-btn.on {
  border-color: var(--accent);
}
.ctf-btn .v {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
.ctf-btn .v em {
  font-style: normal;
  color: var(--muted);
}
.ctf-btn .ph {
  flex: 1;
  text-align: left;
  color: var(--el-text-color-placeholder, var(--muted));
}
.ctf-btn .arr,
.ctf-btn .x {
  color: var(--muted);
}
.ctf-btn .x:hover {
  color: var(--danger-text, #e5484d);
}
.ctf {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.sp {
  flex: 1;
}
.lnk {
  border: 0;
  padding: 0;
  background: none;
  font: inherit;
  color: var(--accent);
  cursor: pointer;
}
.lnk:disabled {
  color: var(--muted);
  cursor: default;
}
.seg {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 3px;
  overflow: hidden;
}
.seg button {
  height: 20px;
  padding: 0 8px;
  border: 0;
  background: var(--panel);
  font: inherit;
  color: var(--text-2);
  cursor: pointer;
}
.seg button + button {
  border-left: 1px solid var(--line);
}
.seg button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.list {
  max-height: 264px; /* 约 10 行，多了在框里滚动 */
  overflow-y: auto;
  border: 1px solid var(--line-soft);
  border-radius: 4px;
}
.row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  height: 26px;
  padding: 0 8px;
  border: 0;
  background: none;
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.row:hover,
.row.on {
  background: var(--sunken);
}
.row .name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row .n {
  font-size: 12px;
  color: var(--muted);
}
.row .fix {
  display: none;
  font-size: 12px;
  color: var(--muted);
}
.row:hover .fix {
  display: inline;
}
.pin {
  display: inline-flex;
  border: 0;
  padding: 2px;
  background: none;
  color: var(--line);
  cursor: pointer;
}
.pin:hover {
  color: var(--text-2);
}
.pin.on {
  color: var(--warn-text, #f59a23);
}
.pin.ro {
  cursor: default;
}
.row.add {
  color: var(--accent);
}
.empty {
  padding: 10px;
  font-size: 12px;
  color: var(--muted);
}
.foot {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.muted {
  color: var(--muted);
}
</style>
