<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { get, loadMe, loadMeta, post, session, setUser } from './api'
import { errorText } from './format'
import WorkbenchView from './views/WorkbenchView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import StatusView from './views/StatusView.vue'
import UsageView from './views/UsageView.vue'
import AccountsView from './views/AccountsView.vue'
import InvoicesView from './views/InvoicesView.vue'
import AftersalesFilesView from './views/AftersalesFilesView.vue'
import LogisticsTicketsView from './views/LogisticsTicketsView.vue'
import ShipNoticesView from './views/ShipNoticesView.vue'
import LoginView from './views/LoginView.vue'

type ViewKey = 'workbench' | 'knowledge' | 'aftersales' | 'tickets' | 'shipnotices' | 'invoices' | 'usage' | 'status' | 'accounts'
// adminOnly：只有管理员看得到的页面（后端同样只允许管理员）
const ALL_VIEWS: { key: ViewKey; label: string; adminOnly?: boolean; feature?: 'aftersales_files' }[] = [
  { key: 'workbench', label: '审核工作台' },
  { key: 'knowledge', label: '知识库' },
  { key: 'aftersales', label: '售后附件', feature: 'aftersales_files' },
  { key: 'tickets', label: '查件单' },
  { key: 'shipnotices', label: '发货通知' },
  { key: 'invoices', label: '发票' },
  { key: 'usage', label: '用量统计', adminOnly: true },
  { key: 'status', label: '系统状态', adminOnly: true },
  { key: 'accounts', label: '账号管理', adminOnly: true },
]
const VIEWS = computed(() =>
  ALL_VIEWS.filter(
    (v) => (!v.adminOnly || session.user?.role === 'ADMIN') && (!v.feature || session.user?.features?.[v.feature]),
  ),
)

function parseHash(): { view: ViewKey; conv: number | null } {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?')
  const view = (ALL_VIEWS.find((v) => v.key === path)?.key || 'workbench') as ViewKey
  const conv = Number(new URLSearchParams(query || '').get('conv')) || null
  return { view, conv }
}

const initial = parseHash()
const view = ref<ViewKey>(initial.view)
const focusConv = ref<number | null>(initial.conv)
const agentStatus = ref<{ ready: boolean; reasons: string[] }>({ ready: false, reasons: [] })

// 改密码：首次登录（管理员建的初始密码）强制修改，其他时候从右上角菜单进入
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

function go(key: ViewKey, conv: number | null = null) {
  view.value = key
  focusConv.value = conv
  location.hash = `/${key}${conv ? `?conv=${conv}` : ''}`
}

function onHash() {
  const h = parseHash()
  view.value = h.view
  focusConv.value = h.conv
}

async function refreshStatus() {
  try {
    const s = await get('/api/system/status')
    agentStatus.value = { ready: s.agent_ready, reasons: s.agent_not_ready_reasons }
  } catch {
    agentStatus.value = { ready: false, reasons: ['后端不可达'] }
  }
}

let timer: number | undefined
// 登录后（或打开页面时已登录）再加载元数据和状态；非管理员打开管理员页面时回到工作台
async function afterLogin() {
  if (!session.user) return
  if (!VIEWS.value.some((v) => v.key === view.value)) go('workbench')
  if (session.user.must_change_password) return
  await loadMeta()
  refreshStatus()
  clearInterval(timer)
  timer = window.setInterval(refreshStatus, 15000)
}
watch(() => [session.user?.id, session.user?.must_change_password], afterLogin)

onMounted(async () => {
  window.addEventListener('hashchange', onHash)
  await loadMe()
  afterLogin()
})
onUnmounted(() => {
  window.removeEventListener('hashchange', onHash)
  clearInterval(timer)
})
</script>

<template>
  <div v-if="!session.checked" class="shell" />
  <LoginView v-else-if="!session.user" />
  <div v-else class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true" />eBay 客服工作台</div>
      <nav class="nav">
        <button v-for="v in VIEWS" :key="v.key" :class="{ active: view === v.key }" @click="go(v.key)">
          {{ v.label }}
        </button>
      </nav>
      <div class="right">
        <el-tooltip :content="agentStatus.ready ? '回复模型已配置，可以生成草稿' : agentStatus.reasons.join('；')" placement="bottom">
          <span class="agent" :class="{ ready: agentStatus.ready, clickable: session.user?.role === 'ADMIN' }"
                @click="session.user?.role === 'ADMIN' && go('status')">
            <i class="dot" />{{ agentStatus.ready ? '模型就绪' : '模型未就绪' }}
          </span>
        </el-tooltip>
        <el-dropdown trigger="click" @command="onUserMenu">
          <span class="user">{{ session.user.display_name }}<span class="role">{{ session.user.role_label }}</span><span class="caret">▾</span></span>
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
    <main class="content" v-if="session.meta && !forcedPwd">
      <WorkbenchView v-if="view === 'workbench'" :focus="focusConv" @select="(id: number | null) => go('workbench', id)" />
      <KnowledgeView v-else-if="view === 'knowledge'" />
      <UsageView v-else-if="view === 'usage'" @open="(id: number) => go('workbench', id)" />
      <AccountsView v-else-if="view === 'accounts'" />
      <ShipNoticesView v-else-if="view === 'shipnotices'" @open="(id: number) => go('workbench', id)" />
      <LogisticsTicketsView v-else-if="view === 'tickets'" @open="(id: number) => go('workbench', id)" />
      <InvoicesView v-else-if="view === 'invoices'" @open="(id: number) => go('workbench', id)" />
      <AftersalesFilesView v-else-if="view === 'aftersales'" @open="(id: number) => go('workbench', id)" />
      <StatusView v-else @refreshed="refreshStatus" />
    </main>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.topbar {
  display: flex;
  align-items: stretch;
  gap: 28px;
  padding: 0 20px;
  height: 50px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  font-weight: 600;
  font-size: 15px;
  color: var(--brand);
  letter-spacing: 0.02em;
  white-space: nowrap;
}
/* 品牌标：两条错位的墨绿/鼠尾草色块，像一张折起的工单 */
.brand-mark {
  width: 14px;
  height: 18px;
  border-radius: 2px;
  background: linear-gradient(135deg, var(--brand) 0 62%, var(--sage) 62% 100%);
}
.nav {
  display: flex;
  gap: 2px;
  overflow-x: auto;
  scrollbar-width: none;
}
.nav button {
  position: relative;
  background: transparent;
  border: 0;
  color: var(--muted);
  padding: 0 12px;
  cursor: pointer;
  font-size: var(--fs-base);
  font-family: inherit;
  white-space: nowrap;
  transition: color 0.15s;
}
.nav button:hover {
  color: var(--text);
}
.nav button.active {
  color: var(--accent);
  font-weight: 600;
}
.nav button.active::after {
  content: '';
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: -1px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: var(--accent);
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
  gap: 6px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
  font-size: var(--fs-sm);
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
  padding: 14px;
  overflow: auto;
}
@media (max-width: 760px) {
  .topbar {
    height: auto;
    flex-wrap: wrap;
    padding: 8px 12px 0;
    gap: 6px 12px;
  }
  .brand {
    height: 32px;
  }
  .nav {
    order: 3;
    width: 100%;
    height: 38px;
  }
  .right {
    gap: 12px;
  }
  .role {
    display: none;
  }
}
</style>
