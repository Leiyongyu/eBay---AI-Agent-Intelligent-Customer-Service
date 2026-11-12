<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, loadMe, loadMeta, post, session, setUser } from './api'
import { errorText } from './format'
import { CASE_KIND, NAV, allLeaves, fmtCount, navCounts } from './nav'
import type { BoxKey, NavGroup, NavLeaf, ViewKey } from './nav'
import type { Store } from './types'
import NavIcon from './components/NavIcon.vue'
import WorkbenchView from './views/WorkbenchView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import StatusView from './views/StatusView.vue'
import UsageView from './views/UsageView.vue'
import AccountsView from './views/AccountsView.vue'
import InvoicesView from './views/InvoicesView.vue'
import SettingsView from './views/SettingsView.vue'
import EbayCasesView from './views/EbayCasesView.vue'
import CustomersView from './views/CustomersView.vue'
import SyncView from './views/SyncView.vue'
import BestOfferView from './views/BestOfferView.vue'
import AftersalesFilesView from './views/AftersalesFilesView.vue'
import LogisticsTicketsView from './views/LogisticsTicketsView.vue'
import ParcelFollowupsView from './views/ParcelFollowupsView.vue'
import AftersalesReasonsView from './views/AftersalesReasonsView.vue'
import ShipNoticesView from './views/ShipNoticesView.vue'
import AftersalesCasesView from './views/AftersalesCasesView.vue'
import PlaceholderView from './views/PlaceholderView.vue'
import PerformanceView from './views/PerformanceView.vue'
import LaunchView from './views/LaunchView.vue'
import MessageTemplatesView from './views/MessageTemplatesView.vue'
import OffersView from './views/OffersView.vue'
import LoginView from './views/LoginView.vue'

const VIEW_KEYS: ViewKey[] = ['workbench', 'knowledge', 'aftersales', 'tickets', 'shipnotices', 'cases', 'invoices', 'ebaycases', 'customers', 'sync',
  'usage', 'status', 'accounts', 'settings', 'todo', 'performance', 'launch', 'templates', 'offers', 'bestoffers', 'followups', 'reasons']
const ADMIN_VIEWS: ViewKey[] = ['usage', 'status', 'accounts', 'settings', 'sync']
const BOXES: BoxKey[] = ['pending', 'resolved', 'failed', 'unassigned', 'mine', 'auto', 'all', 'system',
  'system_unread', 'system_read']

const isAdmin = computed(() => session.user?.role === 'ADMIN')
// 菜单项是否对当前账号可见（管理员页面、功能权限点）
const visible = (l: NavLeaf) => (!l.adminOnly || isAdmin.value) && (!l.feature || !!session.user?.features?.[l.feature])
const groups = computed(() => NAV.filter((g) => (!g.adminOnly || isAdmin.value) && (!g.children || g.children.some(visible))))

// ---- 路由：#/workbench?box=pending&conv=12 / #/todo?m=best-offer ----
type Route = { view: ViewKey; box: BoxKey; conv: number | null; menu: string }
function defaultBox(): BoxKey {
  return session.user?.role === 'AFTERSALES' ? 'all' : 'pending'
}
function parseHash(): Route {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?')
  const p = new URLSearchParams(query || '')
  const view = (VIEW_KEYS.includes(path as ViewKey) ? path : 'workbench') as ViewKey
  const box = (BOXES.includes(p.get('box') as BoxKey) ? p.get('box') : defaultBox()) as BoxKey
  return { view, box, conv: Number(p.get('conv')) || null, menu: p.get('m') || '' }
}
const route = reactive<Route>(parseHash())

function go(view: ViewKey, opts: { box?: BoxKey; conv?: number | null; menu?: string } = {}) {
  const q = new URLSearchParams()
  if (view === 'workbench') {
    q.set('box', opts.box || route.box)
    if (opts.conv) q.set('conv', String(opts.conv))
  }
  if ((view === 'todo' || view === 'ebaycases' || view === 'customers') && opts.menu) q.set('m', opts.menu)
  const qs = q.toString()
  location.hash = `/${view}${qs ? `?${qs}` : ''}`
  Object.assign(route, { view, box: opts.box || route.box, conv: opts.conv || null, menu: opts.menu || '' })
}
function onHash() {
  Object.assign(route, parseHash())
}

function openLeaf(l: NavLeaf) {
  if (l.caption) return
  if (l.view === 'workbench') go('workbench', { box: l.box })
  else if (l.view === 'todo' || l.view === 'ebaycases' || l.view === 'customers') go(l.view, { menu: l.id })
  else go(l.view)
}

// 当前高亮的菜单项
const activeId = computed(() => {
  const leaves = allLeaves()
  if (route.view === 'workbench') return leaves.find((l) => l.view === 'workbench' && l.box === route.box)?.id || ''
  if (route.view === 'todo' || route.view === 'ebaycases' || route.view === 'customers') return route.menu
  return leaves.find((l) => l.view === route.view)?.id || ''
})
const activeLeaf = computed(() => allLeaves().find((l) => l.id === activeId.value) || null)
const leafLabel = (l: NavLeaf) => (l.dynamicLabel === 'me' ? `${session.user?.display_name || '我'} (我)` : l.label)

// 一级分组展开状态（eBay 默认展开；当前页面所在分组自动展开）
const open = reactive<Record<string, boolean>>({ ebay: true })
function groupHas(g: NavGroup, id: string) {
  return (
    g.leaf?.id === id ||
    !!g.children?.some((c) => c.id === id) ||
    !!g.sections?.some((s) => s.parent?.id === id || s.children.some((c) => c.id === id))
  )
}
watch(activeId, (id) => {
  const g = NAV.find((x) => groupHas(x, id))
  if (g && !g.leaf) open[g.id] = true
}, { immediate: true })

// 面包屑：分组 / 小节 / 菜单
const crumbs = computed(() => crumbsOf(activeId.value))
function crumbsOf(id: string): string[] {
  for (const g of NAV) {
    if (g.leaf?.id === id) return [g.label]
    const c = g.children?.find((x) => x.id === id)
    if (c) return [g.label, c.label]
    for (const s of g.sections || []) {
      if (s.parent?.id === id) return [g.label, s.label, s.parent.label]
      const i = s.children.findIndex((x) => x.id === id)
      if (i >= 0) {
        const sc = s.children[i]
        const parent = sc.group ? null : s.children.slice(0, i).reverse().find((x) => x.group || x.caption)
        return [g.label, s.label, ...(parent?.group ? [parent.label] : []), leafLabel(sc)]
      }
    }
  }
  return ['eBay']
}

// ---- 顶部标签页：打开过的菜单各占一个标签，切换时页面保留（筛选、滚动位置、已加载的数据都在）----
type Tab = { key: string; view: ViewKey; box: BoxKey; menu: string }
const MENU_VIEWS: ViewKey[] = ['todo', 'ebaycases', 'customers']
const MAX_TABS = 15
const TABS_KEY = 'erp-open-tabs-v1'
const tabKey = (r: { view: ViewKey; box: BoxKey; menu: string }) =>
  r.view === 'workbench' ? `workbench:${r.box}` : MENU_VIEWS.includes(r.view) ? `${r.view}:${r.menu}` : r.view
function leafIdOf(t: Tab): string {
  const leaves = allLeaves()
  if (t.view === 'workbench') return leaves.find((l) => l.view === 'workbench' && l.box === t.box)?.id || ''
  if (MENU_VIEWS.includes(t.view)) return t.menu
  return leaves.find((l) => l.view === t.view)?.id || ''
}
// 标签名：菜单名；“买家消息 / 平台消息”下面的同名消息箱带上父级，如 平台消息·未处理
function tabTitle(t: Tab): string {
  const c = crumbsOf(leafIdOf(t))
  if (c.length >= 4) return `${c[c.length - 2]}·${c[c.length - 1]}`
  return c[c.length - 1] || '页面'
}
const tabAllowed = (t: Tab) => VIEW_KEYS.includes(t.view) && (!ADMIN_VIEWS.includes(t.view) || isAdmin.value)
function loadTabs(): Tab[] {
  try {
    const raw = JSON.parse(localStorage.getItem(TABS_KEY) || '[]')
    return Array.isArray(raw) ? raw.filter((t) => t && VIEW_KEYS.includes(t.view)).slice(0, MAX_TABS) : []
  } catch {
    return []
  }
}
const tabs = ref<Tab[]>(loadTabs())
const activeKey = computed(() => tabKey(route))
function saveTabs() {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(tabs.value.map(({ key, view, box, menu }) => ({ key, view, box, menu }))))
  } catch {
    /* 浏览器不让存就算了，只是下次不恢复 */
  }
}
// 打开菜单（或从别的页面跳过来）：没有这个标签就加一个，太多时关掉最早的
watch(() => [route.view, route.box, route.menu, session.user?.id], () => {
  if (!session.user) return
  const t: Tab = { key: tabKey(route), view: route.view, box: route.box, menu: route.menu }
  if (!tabs.value.some((x) => x.key === t.key)) {
    tabs.value.push(t)
    while (tabs.value.length > MAX_TABS) {
      const i = tabs.value.findIndex((x) => x.key !== t.key)
      tabs.value.splice(i, 1)
    }
  }
  tabs.value = tabs.value.filter(tabAllowed)
  saveTabs()
}, { immediate: true })
function switchTab(t: Tab) {
  if (t.key !== activeKey.value) go(t.view, { box: t.box, menu: t.menu })
}
function closeTab(t: Tab) {
  const i = tabs.value.findIndex((x) => x.key === t.key)
  if (i < 0 || tabs.value.length === 1) return // 至少留一个
  tabs.value.splice(i, 1)
  saveTabs()
  if (t.key === activeKey.value) switchTab(tabs.value[Math.min(i, tabs.value.length - 1)])
}
function onTabMenu(cmd: string) {
  const cur = tabs.value.find((x) => x.key === activeKey.value)
  if (cmd === 'others' && cur) tabs.value = [cur]
  else if (cmd === 'right' && cur) tabs.value = tabs.value.slice(0, tabs.value.indexOf(cur) + 1)
  else if (cmd === 'all') {
    const first: Tab = { key: `workbench:${defaultBox()}`, view: 'workbench', box: defaultBox(), menu: '' }
    tabs.value = [first]
    switchTab(first)
  }
  saveTabs()
}
// 当前标签滚动到看得见的位置
const tabsEl = ref<HTMLElement | null>(null)
watch(activeKey, () => requestAnimationFrame(() => tabsEl.value?.querySelector('.tab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })))

// 菜单项 id 是否属于第 gi 个分组父级下面（到下一个分隔线 / 父级为止）
function inGroup(list: NavLeaf[], gi: number, id: string) {
  for (let i = gi + 1; i < list.length && !list[i].group && !list[i].caption; i++) if (list[i].id === id) return true
  return false
}

// 消息分组（买家消息 / 平台消息）的子级默认收起：点父级展开；当前页面所在的分组自动展开
function groupOf(list: NavLeaf[], i: number): string | null {
  for (let j = i - 1; j >= 0; j--) {
    if (list[j].group) return list[j].id
    if (list[j].caption) return null
  }
  return null
}
const subOpen = reactive<Record<string, boolean>>({})
watch(activeId, (id) => {
  for (const g of NAV)
    for (const s of g.sections || []) {
      const i = s.children.findIndex((c) => c.id === id)
      const gid = i >= 0 ? (s.children[i].group ? s.children[i].id : groupOf(s.children, i)) : null
      if (gid) subOpen[gid] = true
    }
}, { immediate: true })
// 二级小节（消息管理 / 售后管理 / 智能客服 / 绩效管理）：点标题收起展开，默认展开；当前页面所在的小节自动展开
const secClosed = reactive<Record<string, boolean>>({})
watch(activeId, (id) => {
  for (const g of NAV)
    for (const s of g.sections || []) if (s.parent?.id === id || s.children.some((c) => c.id === id)) secClosed[s.id] = false
}, { immediate: true })
// 点父级：收起时只收起；展开时同时打开这个分组的全部消息
function toggleGroup(c: NavLeaf) {
  if (subOpen[c.id]) subOpen[c.id] = false
  else {
    subOpen[c.id] = true
    openLeaf(c)
  }
}

const collapsed = ref(false)

// ---- 侧边栏数字：店铺未读 + 各消息箱数量（工作台打开时也会刷新）----
// 定时刷新时先问会话列表的变更标记，没变化（且不到 60 秒）就不重新算；标签页隐藏时不刷新
let countsVersion = ''
let countsAt = 0
async function refreshCounts(force = true) {
  if (!session.user || session.user.must_change_password) return
  try {
    if (!force) {
      const v = await get<{ version: string; changed: boolean }>(`/internal/conversations/version?since=${countsVersion}`)
      if (!v.changed && Date.now() - countsAt < 60_000) return
      countsVersion = v.version
    }
    countsAt = Date.now()
    const [stores, c] = await Promise.all([
      get<Store[]>('/internal/stores'),
      get<{ views?: Record<string, number>; boxes?: Record<string, number> }>('/internal/conversations/filter-counts'),
    ])
    navCounts.buyerUnread = stores.reduce((n, s) => n + (s.unread_conversations || 0), 0)
    navCounts.system = stores.reduce((n, s) => n + (s.unread_system || 0), 0)
    navCounts.pending = c.views?.pending
    for (const k of ['resolved', 'failed', 'unassigned', 'mine'] as const) navCounts[k] = c.boxes?.[k]
  } catch {
    /* 数字刷不出来不影响使用 */
  }
}

// ---- 模型状态 ----
const agentStatus = ref<{ ready: boolean; reasons: string[] }>({ ready: false, reasons: [] })
async function refreshStatus() {
  try {
    const s = await get('/api/system/status')
    agentStatus.value = { ready: s.agent_ready, reasons: s.agent_not_ready_reasons }
  } catch {
    agentStatus.value = { ready: false, reasons: ['后端不可达'] }
  }
}

// ---- 改密码 / 退出 ----
const pwdDialog = ref(false)
const pwd = reactive({ old: '', next: '', again: '' })
const pwdSaving = ref(false)
const forcedPwd = computed(() => !!session.user?.must_change_password)

async function changePassword() {
  if (pwd.next !== pwd.again) return ElMessage.warning('两次输入的新密码不一致')
  pwdSaving.value = true
  try {
    await post('/api/auth/password', { old_password: pwd.old, new_password: pwd.next })
    ElMessage.success('密码已修改')
    Object.assign(pwd, { old: '', next: '', again: '' })
    pwdDialog.value = false
    await loadMe()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    pwdSaving.value = false
  }
}
async function logout() {
  try {
    await post('/api/auth/logout')
  } finally {
    setUser(null)
  }
}
function onUserMenu(cmd: string) {
  if (cmd === 'password') pwdDialog.value = true
  else if (cmd === 'logout') logout()
}

let timer: number | undefined
// 登录后再加载元数据和状态；非管理员打开管理员页面时回到工作台
async function afterLogin() {
  if (!session.user) return
  if (ADMIN_VIEWS.includes(route.view) && !isAdmin.value) go('workbench', { box: defaultBox() })
  if (!location.hash.includes('box=') && route.view === 'workbench') route.box = defaultBox()
  if (session.user.must_change_password) return
  await loadMeta()
  refreshStatus()
  refreshCounts()
  clearInterval(timer)
  timer = window.setInterval(() => {
    if (document.hidden) return
    refreshStatus()
    refreshCounts(false)
  }, 15000)
}
watch(() => [session.user?.id, session.user?.must_change_password], afterLogin)

// 切回这个浏览器标签页时马上查一次（不用等下一个 15 秒）
function onVisible() {
  if (document.hidden || !session.user || session.user.must_change_password) return
  refreshStatus()
  refreshCounts(false)
}
onMounted(async () => {
  window.addEventListener('hashchange', onHash)
  document.addEventListener('visibilitychange', onVisible)
  await loadMe()
  afterLogin()
})
onUnmounted(() => {
  window.removeEventListener('hashchange', onHash)
  document.removeEventListener('visibilitychange', onVisible)
  clearInterval(timer)
})
</script>

<template>
  <div v-if="!session.checked" class="shell" />
  <LoginView v-else-if="!session.user" />
  <div v-else class="shell" :class="{ collapsed }">
    <!-- ================= 左侧菜单 ================= -->
    <aside class="side">
      <div class="brand">
        <img class="brand-mark" src="/logo-512.png" alt="" aria-hidden="true" />
        <span>Ebay智能AI客服</span>
      </div>
      <nav class="menu">
        <template v-for="g in groups" :key="g.id">
          <!-- 没有子菜单的一级菜单 -->
          <button v-if="g.leaf" class="g-head" :class="{ active: activeId === g.leaf.id }" @click="openLeaf(g.leaf)">
            <NavIcon :name="g.icon" /><span class="g-label">{{ g.label }}</span>
          </button>
          <template v-else>
            <button class="g-head" :class="{ platform: g.id === 'ebay' }" @click="open[g.id] = !open[g.id]">
              <span v-if="g.id === 'ebay'" class="ebay-logo" aria-hidden="true"><i /><i /><i /><i /></span>
              <NavIcon v-else :name="g.icon" />
              <span class="g-label">{{ g.label }}</span>
              <svg class="chev" :class="{ up: open[g.id] }" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
              </svg>
            </button>
            <div v-show="open[g.id]" class="g-body">
              <!-- 普通分组 -->
              <template v-for="c in g.children || []" :key="c.id">
                <button v-if="visible(c)" class="item" :class="{ active: activeId === c.id }" @click="openLeaf(c)">
                  <span class="label">{{ leafLabel(c) }}</span>
                  <span v-if="c.count && navCounts[c.count]" class="cnt">{{ fmtCount(navCounts[c.count]) }}</span>
                </button>
              </template>
              <!-- eBay：二级小节 -->
              <section v-for="s in g.sections || []" :key="s.id" class="sec">
                <button class="sec-head" :aria-expanded="!secClosed[s.id]" @click="secClosed[s.id] = !secClosed[s.id]">
                  <NavIcon :name="s.icon" /><span class="g-label">{{ s.label }}</span>
                  <svg class="chev" :class="{ up: !secClosed[s.id] }" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                    <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                  </svg>
                </button>
                <div v-show="!secClosed[s.id]">
                <button v-if="s.parent" class="item parent"
                        :class="{ current: route.view === 'workbench' && s.children.some((c) => c.id === activeId), active: activeId === s.parent.id }"
                        @click="openLeaf(s.parent)">
                  <span class="label">{{ s.parent.label }}</span>
                  <span v-if="s.parent.count && navCounts[s.parent.count]" class="cnt">{{ fmtCount(navCounts[s.parent.count]) }}</span>
                </button>
                <template v-for="(c, ci) in s.children" :key="c.id">
                  <div v-if="c.caption && !c.label" class="sep" />
                  <!-- 消息分组父级：买家消息 / 平台消息 -->
                  <button v-else-if="c.group" class="item parent"
                          :class="{ current: route.view === 'workbench' && inGroup(s.children, ci, activeId), active: activeId === c.id }"
                          :aria-expanded="!!subOpen[c.id]" @click="toggleGroup(c)">
                    <span class="label">{{ c.label }}</span>
                    <span v-if="c.count && navCounts[c.count]" class="cnt">{{ fmtCount(navCounts[c.count]) }}</span>
                    <svg class="chev sub-chev" :class="{ up: subOpen[c.id] }" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                      <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                    </svg>
                  </button>
                  <div v-else-if="c.caption" class="item caption" :class="{ sub: !!s.parent }">
                    <NavIcon v-if="c.icon" :name="c.icon" :size="15" />
                    <span class="label">{{ c.label }}</span>
                    <el-tooltip v-if="c.addable" content="团队成员在「系统管理 → 账号管理」里添加" placement="right">
                      <span class="add" @click.stop="isAdmin && go('accounts')">+</span>
                    </el-tooltip>
                  </div>
                  <button v-else-if="visible(c) && (!groupOf(s.children, ci) || subOpen[groupOf(s.children, ci)!])"
                          class="item" :class="{ active: activeId === c.id, sub: !!c.icon }"
                          @click="openLeaf(c)">
                    <NavIcon v-if="c.icon" :name="c.icon" :size="15" />
                    <span class="label">{{ leafLabel(c) }}</span>
                    <span v-if="c.count && navCounts[c.count]" class="cnt" :class="{ plain: !!c.icon }">{{ fmtCount(navCounts[c.count]) }}</span>
                  </button>
                </template>
                </div>
              </section>
            </div>
          </template>
        </template>
      </nav>
    </aside>
    <button class="collapse" :title="collapsed ? '展开菜单' : '收起菜单'" @click="collapsed = !collapsed">
      <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">
        <path :d="collapsed ? 'M4.5 3 7.5 6 4.5 9' : 'M7.5 3 4.5 6 7.5 9'" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
      </svg>
    </button>

    <!-- ================= 右侧 ================= -->
    <div class="main">
      <header class="topbar">
        <div class="crumbs">
          <template v-for="(c, i) in crumbs" :key="i">
            <span v-if="i" class="slash">/</span><span :class="{ last: i === crumbs.length - 1 }">{{ c }}</span>
          </template>
        </div>
        <div class="right">
          <el-tooltip :content="agentStatus.ready ? '回复模型已配置，可以生成草稿' : agentStatus.reasons.join('；')" placement="bottom">
            <span class="agent" :class="{ ready: agentStatus.ready, clickable: isAdmin }" @click="isAdmin && go('status')">
              <i class="dot" />{{ agentStatus.ready ? '模型就绪' : '模型未就绪' }}
            </span>
          </el-tooltip>
          <el-dropdown trigger="click" @command="onUserMenu">
            <span class="user">
              <span class="avatar">{{ session.user.display_name.slice(0, 1) }}</span>
              {{ session.user.display_name }}<span class="role">{{ session.user.role_label }}</span><span class="caret">▾</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>
      <el-dialog :model-value="pwdDialog || forcedPwd" :title="forcedPwd ? '首次登录，请修改初始密码' : '修改密码'" width="400px"
                 :close-on-click-modal="!forcedPwd" :close-on-press-escape="!forcedPwd" :show-close="!forcedPwd"
                 @close="pwdDialog = false">
        <el-form label-width="80px" @submit.prevent="changePassword">
          <el-form-item label="原密码"><el-input v-model="pwd.old" type="password" show-password /></el-form-item>
          <el-form-item label="新密码"><el-input v-model="pwd.next" type="password" show-password placeholder="至少 8 位，含字母和数字" /></el-form-item>
          <el-form-item label="再输一次"><el-input v-model="pwd.again" type="password" show-password /></el-form-item>
        </el-form>
        <template #footer>
          <el-button v-if="forcedPwd" @click="logout">退出登录</el-button>
          <el-button type="primary" :loading="pwdSaving" @click="changePassword">确认修改</el-button>
        </template>
      </el-dialog>
      <!-- 顶部标签页：点击切换，× 关闭（鼠标中键也能关）；切换时页面保留 -->
      <div v-if="session.meta && !forcedPwd" class="tabbar">
        <div ref="tabsEl" class="tabs-scroll">
          <div v-for="t in tabs" :key="t.key" class="tab" :class="{ on: t.key === activeKey }" :title="tabTitle(t)"
               @click="switchTab(t)" @mousedown.middle.prevent="closeTab(t)">
            <span class="tab-label">{{ tabTitle(t) }}</span>
            <button v-if="tabs.length > 1" class="tab-x" title="关闭" aria-label="关闭标签" @click.stop="closeTab(t)">×</button>
          </div>
        </div>
        <el-dropdown trigger="click" @command="onTabMenu">
          <button class="tab-more" title="标签操作">⋯</button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="others">关闭其他标签</el-dropdown-item>
              <el-dropdown-item command="right">关闭右侧标签</el-dropdown-item>
              <el-dropdown-item command="all" divided>关闭全部标签</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <main class="content" v-if="session.meta && !forcedPwd">
        <div v-for="t in tabs" v-show="t.key === activeKey" :key="t.key" class="pane" :class="{ flush: t.view === 'workbench' }">
            <WorkbenchView v-if="t.view === 'workbench'" :box="t.box" :focus="t.key === activeKey ? route.conv : null"
                           @select="(id: number | null) => go('workbench', { conv: id })" @counts="refreshCounts" />
            <KnowledgeView v-else-if="t.view === 'knowledge'" />
            <UsageView v-else-if="t.view === 'usage'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <AccountsView v-else-if="t.view === 'accounts'" />
            <SettingsView v-else-if="t.view === 'settings' && isAdmin" />
            <SyncView v-else-if="t.view === 'sync' && isAdmin" />
            <CustomersView v-else-if="t.view === 'customers'" :mode="t.menu === 'customer-blacklist' ? 'blacklist' : 'list'"
                           @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <EbayCasesView v-else-if="t.view === 'ebaycases'" :kind="CASE_KIND[t.menu] || 'INQUIRY'"
                           @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <AftersalesCasesView v-else-if="t.view === 'cases'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <ShipNoticesView v-else-if="t.view === 'shipnotices'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <LogisticsTicketsView v-else-if="t.view === 'tickets'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <ParcelFollowupsView v-else-if="t.view === 'followups'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <AftersalesReasonsView v-else-if="t.view === 'reasons'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <InvoicesView v-else-if="t.view === 'invoices'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <AftersalesFilesView v-else-if="t.view === 'aftersales'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <PerformanceView v-else-if="t.view === 'performance'" />
            <LaunchView v-else-if="t.view === 'launch'" />
            <MessageTemplatesView v-else-if="t.view === 'templates'" />
            <OffersView v-else-if="t.view === 'offers'" />
            <BestOfferView v-else-if="t.view === 'bestoffers'" @open="(id: number) => go('workbench', { box: 'all', conv: id })" />
            <PlaceholderView v-else-if="t.view === 'todo'" :title="tabTitle(t)" :path="crumbsOf(leafIdOf(t))" />
            <StatusView v-else @refreshed="refreshStatus" />
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  --side-w: 184px;
  height: 100%;
  display: flex;
  position: relative;
}

/* ---------------- 左侧菜单 ---------------- */
.side {
  width: var(--side-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-right: 1px solid var(--line);
  transition: width 0.18s, opacity 0.18s;
  overflow: hidden;
}
.shell.collapsed .side {
  width: 0;
  opacity: 0;
  border-right: 0;
}
.brand {
  height: 48px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  font-weight: 700;
  font-size: 14px;
  color: var(--text);
  white-space: nowrap;
  border-bottom: 1px solid var(--line-soft);
}
.brand-mark {
  width: 22px;
  height: 22px;
}
.menu {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 0 24px;
}
.menu button {
  font-family: inherit;
  border: 0;
  background: none;
  cursor: pointer;
  text-align: left;
}
.g-head {
  width: 100%;
  height: 40px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  font-size: 14px;
  color: var(--text);
  white-space: nowrap;
  border-bottom: 1px solid var(--line-soft);
}
.g-head:hover {
  background: var(--hover);
}
.g-head.active {
  color: var(--accent);
  background: var(--accent-soft);
}
.g-head .nav-icon {
  color: var(--muted);
}
.g-head.active .nav-icon {
  color: var(--accent);
}
.g-head.platform {
  font-weight: 700;
  font-size: 15px;
}
.g-label {
  flex: 1;
}
.chev {
  color: var(--muted);
  transition: transform 0.15s;
}
.chev.up {
  transform: rotate(180deg);
}
/* eBay 平台标：四色小方块（不使用官方商标） */
.ebay-logo {
  display: inline-grid;
  grid-template-columns: 7px 7px;
  gap: 2px;
}
.ebay-logo i {
  width: 7px;
  height: 7px;
  border-radius: 2px;
}
.ebay-logo i:nth-child(1) { background: #e53238; }
.ebay-logo i:nth-child(2) { background: #0064d2; }
.ebay-logo i:nth-child(3) { background: #f5af02; }
.ebay-logo i:nth-child(4) { background: #86b817; }

.g-body {
  padding: 2px 0 6px;
  border-bottom: 1px solid var(--line-soft);
}
.sec + .sec {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px solid var(--line-soft);
}
.sec-head {
  width: 100%;
  height: 38px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  font-size: 14px;
  color: var(--text);
}
.sec-head:hover {
  background: var(--hover);
}
.sec-head .nav-icon {
  color: var(--muted);
}
.item {
  position: relative;
  width: calc(100% - 16px);
  margin: 1px 8px;
  height: 30px;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 10px 0 26px;
  border-radius: 4px;
  font-size: 13px;
  color: var(--text-2);
  white-space: nowrap;
}
.item.sub {
  padding-left: 18px;
}
.item:not(.caption):hover {
  background: var(--hover);
  color: var(--text);
}
/* 父级（买家消息 / 平台消息）悬停时保持橙色，只加浅底 */
.item.parent:hover {
  color: var(--accent);
  background: var(--accent-soft);
}
.item.active {
  background: var(--active-bg);
  color: var(--text);
  font-weight: 600;
}
.sub-chev {
  margin-left: auto;
}
/* 父级（买家消息 / 平台消息）：平时黑色；鼠标放上去、或者选中它 / 它下面的消息箱时变橙色 */
.item.parent {
  margin-top: 4px;
  color: var(--text);
  font-weight: 500;
}
.item.parent.current,
.item.parent.active {
  color: var(--accent);
  font-weight: 600;
}
.item.parent.active {
  background: var(--accent-soft);
}
.item .nav-icon {
  color: var(--muted);
}
.item.active .nav-icon {
  color: var(--text);
}
.item.caption {
  color: var(--muted);
  cursor: default;
}
.label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.cnt {
  margin-left: 2px;
  padding: 0 5px;
  height: 17px;
  line-height: 15px;
  font-size: 11px;
  border-radius: 3px;
  color: var(--accent);
  border: 1px solid var(--accent-line);
  background: var(--accent-soft);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
/* 带图标的消息箱：数字直接跟在名称后面 */
.cnt.plain {
  border: 0;
  background: none;
  color: var(--text);
  padding: 0 2px;
  font-size: 12px;
}
.add {
  margin-left: auto;
  width: 18px;
  height: 18px;
  line-height: 16px;
  text-align: center;
  border-radius: 3px;
  font-size: 15px;
  color: var(--muted);
  cursor: pointer;
}
.add:hover {
  background: var(--active-bg);
  color: var(--accent);
}
.sep {
  height: 1px;
  margin: 6px 14px;
  background: var(--line-soft);
}

.collapse {
  position: absolute;
  z-index: 20;
  top: 50%;
  left: calc(var(--side-w) - 1px);
  transform: translateY(-50%);
  width: 12px;
  height: 46px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line);
  border-left: 0;
  border-radius: 0 6px 6px 0;
  background: var(--panel);
  color: var(--muted);
  cursor: pointer;
  transition: left 0.18s;
}
.collapse:hover {
  color: var(--accent);
}
.shell.collapsed .collapse {
  left: 0;
}

/* ---------------- 右侧 ---------------- */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  height: 48px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 0 18px 0 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
.crumbs {
  font-size: 13px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.crumbs .slash {
  margin: 0 8px;
  color: var(--line);
}
.crumbs .last {
  color: var(--text);
  font-weight: 600;
}
.right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 18px;
}
.agent {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-xs);
  color: var(--danger-text);
  white-space: nowrap;
}
.agent.ready {
  color: var(--muted);
}
.agent.clickable {
  cursor: pointer;
}
.agent .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--danger);
  box-shadow: 0 0 0 3px var(--danger-soft);
}
.agent.ready .dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px var(--el-color-success-light-9);
}
.user {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
  font-size: var(--fs-sm);
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.role {
  font-size: var(--fs-xs);
  color: var(--muted);
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--sunken);
}
.caret {
  color: var(--muted);
  font-size: 11px;
}
.content {
  flex: 1;
  min-height: 0;
  position: relative;
}
.pane {
  height: 100%;
  padding: 14px;
  overflow: auto;
}
.pane.flush {
  padding: 0;
}
/* ---------------- 顶部标签页 ---------------- */
.tabbar {
  height: 36px;
  flex-shrink: 0;
  display: flex;
  align-items: stretch;
  background: var(--sunken);
  border-bottom: 1px solid var(--line);
}
.tabs-scroll {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: flex-end;
  gap: 2px;
  padding: 0 8px;
  overflow-x: auto;
  scrollbar-width: none;
}
.tabs-scroll::-webkit-scrollbar {
  display: none;
}
.tab {
  height: 30px;
  max-width: 180px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px 0 12px;
  border: 1px solid transparent;
  border-bottom: 0;
  border-radius: 6px 6px 0 0;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
}
.tab:hover {
  background: var(--hover);
  color: var(--text);
}
.tab.on {
  position: relative;
  top: 1px;
  height: 31px;
  background: var(--panel);
  border-color: var(--line);
  color: var(--accent);
  font-weight: 600;
}
.tab.on::before {
  content: '';
  position: absolute;
  left: -1px;
  right: -1px;
  top: -1px;
  height: 2px;
  border-radius: 6px 6px 0 0;
  background: var(--accent);
}
.tab-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tab-x {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: none;
  font-size: 14px;
  line-height: 16px;
  color: var(--muted);
  cursor: pointer;
  visibility: hidden;
}
.tab:hover .tab-x,
.tab.on .tab-x {
  visibility: visible;
}
.tab-x:hover {
  background: var(--danger);
  color: #fff;
}
.tab-more {
  width: 36px;
  border: 0;
  border-left: 1px solid var(--line);
  background: none;
  font-size: 16px;
  color: var(--muted);
  cursor: pointer;
}
.tab-more:hover {
  color: var(--accent);
}
@media (max-width: 760px) {
  .shell:not(.collapsed) .side {
    position: absolute;
    z-index: 30;
    height: 100%;
    box-shadow: var(--el-box-shadow);
  }
  .role,
  .agent {
    display: none;
  }
}
</style>
