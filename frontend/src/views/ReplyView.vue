<script setup lang="ts">
// 回复页面：左边当前消息箱的会话列表，中间聊天和回复，右边相关信息 / 客户资料 / 历史记录
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { errorText } from '../format'
import type { ConvContext, ConversationItem, CustomerPanelData, ListingCard } from '../types'
import ConversationPanel from '../components/ConversationPanel.vue'
import CustomerSidebar from '../components/CustomerSidebar.vue'

const props = defineProps<{
  items: ConversationItem[] // 列表页筛选后的会话（左栏）
  convId: number
  boxLabel: string
  boxCount?: number
  filterCount: number // 列表页生效的筛选项个数
}>()
const emit = defineEmits<{ select: [id: number]; close: []; changed: [] }>()

const current = computed(() => props.items.find((c) => c.id === props.convId) || null)
const idx = computed(() => props.items.findIndex((c) => c.id === props.convId))

function fmtRel(iso: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`
  return `${Math.floor(s / 86400)}天前`
}
// 头像颜色按买家名固定
const AVATAR = ['#8fd694', '#7cc8f2', '#f5b971', '#b9a3f0', '#f29b9b', '#6fd3c5', '#d4c16a']
function avatarColor(key: string) {
  let h = 0
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return AVATAR[h % AVATAR.length]
}
const initial = (s: string) => (s || '?').slice(0, 1).toUpperCase()

// ---- 会话面板与右栏“当前对话待处理”互通：右栏把建议回复填进回复框、审核后刷新聊天记录 ----
const panel = ref<InstanceType<typeof ConversationPanel> | null>(null)
const convCtx = ref<ConvContext | null>(null)
watch(() => props.convId, () => (convCtx.value = null))

// ---- 右栏数据 ----
const customer = ref<CustomerPanelData | null>(null)
const customerLoading = ref(false)
const customerError = ref('')
const listing = ref<ListingCard | null>(null)
let seq = 0
// 右侧数据后端缓存 1 小时（PANEL_CACHE_SECONDS）；force = 右侧栏点了“刷新”，强制重新调接口
async function loadSide(force = false) {
  const q = force === true ? '?refresh=true' : ''
  const my = ++seq
  customer.value = null
  listing.value = null
  customerError.value = ''
  customerLoading.value = true
  get<ListingCard>(`/internal/conversations/${props.convId}/listing${q}`)
    .then((r) => my === seq && (listing.value = r))
    .catch(() => null)
  try {
    const d = await get<CustomerPanelData>(`/internal/conversations/${props.convId}/customer${q}`)
    if (my === seq) customer.value = d
  } catch (e) {
    if (my === seq) customerError.value = errorText(e)
  } finally {
    if (my === seq) customerLoading.value = false
  }
}
watch(() => props.convId, () => loadSide(), { immediate: true })

const buyer = computed(() => current.value?.buyer_key || customer.value?.buyer_key || '')
const title = computed(() => current.value?.conversation_title || listing.value?.title || '')
const itemId = computed(() => current.value?.item_id || listing.value?.item_id || '')


function next() {
  const n = props.items[idx.value + 1]
  if (n) emit('select', n.id)
  else ElMessage.info('已经是列表里的最后一封')
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && !document.querySelector('.el-overlay:not([style*="display: none"])')) emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

const listEl = ref<HTMLElement | null>(null)
watch(() => props.convId, () => {
  requestAnimationFrame(() => listEl.value?.querySelector('.li.on')?.scrollIntoView({ block: 'nearest' }))
}, { immediate: true })
</script>

<template>
  <div class="rv">
    <!-- ======== 左：会话列表 ======== -->
    <aside class="left">
      <div class="l-head">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
          <path d="M4 6h16v12H4zM4 7l8 6 8-6" />
        </svg>
        <b>{{ boxLabel }}</b><span class="cnt">{{ boxCount ?? items.length }}</span>
      </div>
      <div class="l-filter">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M4 6h16M7 12h10M10 18h4" />
        </svg>
        筛选消息:<span class="accent">{{ filterCount ? `${filterCount} 个筛选项` : '无筛选项' }}</span>
      </div>
      <div ref="listEl" class="l-list">
        <button v-for="c in items" :key="c.id" class="li" :class="{ on: c.id === convId }" @click="emit('select', c.id)">
          <span class="av" :style="{ background: avatarColor(c.buyer_key) }">{{ initial(c.buyer_key) }}</span>
          <span class="li-main">
            <span class="li-top">
              <span class="li-name">{{ c.conversation_type === 'FROM_EBAY' ? c.conversation_title || 'eBay 通知' : c.buyer_key }}</span>
              <span class="li-time">{{ fmtRel(c.last_message_at) }}</span>
            </span>
            <span class="li-bottom">
              <span class="li-prev">{{ c.preview }}</span>
              <span v-if="c.unread_count" class="badge">{{ c.unread_count }}</span>
            </span>
          </span>
        </button>
        <div v-if="!items.length" class="muted empty">列表里没有会话</div>
      </div>
    </aside>

    <!-- ======== 中：聊天与回复 ======== -->
    <section class="mid">
      <header class="m-head">
        <span class="av big" :style="{ background: avatarColor(buyer) }">{{ initial(buyer) }}</span>
        <b class="m-buyer">{{ buyer || '会话' }}</b>
        <span class="pill"><span class="ebay-mini" aria-hidden="true"><i /><i /><i /><i /></span>eBay</span>
        <span v-if="current" class="pill">{{ current.store_name || current.store_id }}</span>
        <span class="spacer" />
        <el-button type="primary" @click="next">下一封</el-button>
      </header>
      <div v-if="title || itemId" class="about">
        关于: {{ buyer }} 发出了有关
        <a v-if="current?.item_url" :href="current.item_url" target="_blank" rel="noopener noreferrer">{{ title || itemId }}</a>
        <template v-else>{{ title || itemId }}</template>
        <template v-if="itemId">（物品编号: {{ itemId }}）</template>的讯息
      </div>
      <div class="m-body">
        <ConversationPanel ref="panel" :key="convId" :conv-id="convId" layout="page"
                           @changed="emit('changed')" @deleted="emit('close')" @context="(c: ConvContext) => (convCtx = c)" />
      </div>
    </section>

    <!-- ======== 右：相关信息 / 客户资料 / 历史记录 ======== -->
    <CustomerSidebar class="right" :data="customer" :loading="customerLoading" :error="customerError" :conv="current"
                     :listing="listing" :ctx="convCtx" @open="(id: number) => emit('select', id)" @reload="(f?: boolean) => loadSide(f === true)"
                     @changed="emit('changed')" @insert="(t: string) => panel?.setEditText(t)" @conv-changed="panel?.reload()" />

    <!-- 整个页面右上角的关闭按钮：回到列表（也可以按 Esc、点外面的灰色区域） -->
    <button class="rv-close" title="关闭，返回列表（Esc）" aria-label="关闭，返回列表" @click="emit('close')">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
        <path d="M6 6l12 12M18 6 6 18" />
      </svg>
    </button>
  </div>
</template>

<style scoped>
.rv {
  position: relative;
  height: 100%;
  display: grid;
  grid-template-columns: 272px minmax(0, 1fr) 500px;
  background: var(--panel);
}
@media (max-width: 1500px) {
  .rv {
    grid-template-columns: 240px minmax(0, 1fr) 420px;
  }
}
@media (max-width: 1100px) {
  .rv {
    grid-template-columns: minmax(0, 1fr);
    grid-auto-rows: minmax(0, auto);
    overflow: auto;
  }
  .left {
    max-height: 240px;
  }
}

/* ---- 左 ---- */
.left {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid var(--line);
}
.l-head {
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  font-size: 15px;
  border-bottom: 1px solid var(--line);
}
.l-head b {
  font-weight: 500;
}
.cnt {
  color: var(--text-2);
}
.l-filter {
  height: 40px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 18px;
  font-size: 12px;
  color: var(--text-2);
  border-bottom: 1px solid var(--line);
}
.accent {
  margin-left: 4px;
  color: var(--accent);
}
.l-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.li {
  width: 100%;
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px 14px;
  border: 0;
  border-bottom: 1px solid var(--line-soft);
  background: none;
  text-align: left;
  font-family: inherit;
  cursor: pointer;
}
.li:hover {
  background: var(--hover);
}
.li.on {
  background: var(--active-bg);
}
.av {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border-radius: 50%;
  color: #fff;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.av.big {
  width: 32px;
  height: 32px;
  font-size: 14px;
}
.li-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.li-top,
.li-bottom {
  display: flex;
  align-items: center;
  gap: 8px;
}
.li-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.li-time {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.li-prev {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--danger);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
  font-weight: 700;
}
.empty {
  padding: 20px;
  text-align: center;
}

/* ---- 中 ---- */
.mid {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.m-head {
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-bottom: 1px solid var(--line);
}
.m-buyer {
  font-size: 15px;
  font-weight: 500;
  white-space: nowrap;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 3px;
  font-size: 12px;
  color: var(--text);
  white-space: nowrap;
}
.ebay-mini {
  display: inline-grid;
  grid-template-columns: 5px 5px;
  gap: 1.5px;
}
.ebay-mini i {
  width: 5px;
  height: 5px;
  border-radius: 1px;
}
.ebay-mini i:nth-child(1) { background: #e53238; }
.ebay-mini i:nth-child(2) { background: #0064d2; }
.ebay-mini i:nth-child(3) { background: #f5af02; }
.ebay-mini i:nth-child(4) { background: #86b817; }
.spacer {
  flex: 1;
}
.rv-close {
  position: absolute;
  top: 8px;
  right: 10px;
  z-index: 5;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--panel);
  color: var(--text-2);
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s, border-color 0.15s;
}
.rv-close:hover {
  background: var(--danger);
  border-color: var(--danger);
  color: #fff;
}
/* 右栏页签给关闭按钮让出位置 */
.right :deep(.tabs) {
  padding-right: 56px;
}
.about {
  flex-shrink: 0;
  padding: 5px 14px;
  font-size: 12px;
  color: var(--muted);
  background: var(--sunken);
  border-bottom: 1px solid var(--line-soft);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.about a {
  color: var(--text-2);
}
.m-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.m-body > :deep(.panel) {
  flex: 1;
  border: 0;
  border-radius: 0;
}
.right {
  min-height: 0;
}
</style>
