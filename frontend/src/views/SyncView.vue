<script setup lang="ts">
// 数据同步（只有管理员）：按功能模块看数据从哪个 eBay 接口来——触发方式 → 接口与参数 → 本地表 → 用在哪些页面，
// 相关配置（能在这里改的保存后立即生效，存系统配置、优先于 .env；其余只能改 .env），定时任务的运行状态与立即运行，接口调用次数
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post } from '../api'
import { errorText } from '../format'
import type { AppSettingItem } from '../types'

type Calls = { today: number; today_fail: number; total: number; fail: number; last_at: string | null; last_error: string | null; last_error_at: string | null }
type Api = { name: string; key: string; method: string; path: string; scope: string; params: string; when: string; calls?: Calls | null }
type Cfg = { key: string; env: string; label: string; value: any; editable: boolean }
type Module = { key: string; label: string; task: string | null; purpose: string; pages: string[]; trigger: string; stores: string
  tables: string[]; apis: Api[]; config: Cfg[] }
type Task = { key: string; label: string; enabled: boolean; interval: string; scope: string; running: boolean; worker_alive?: boolean
  last_at: string | null; last?: any; settings: string[]; stores?: any[]; push?: any; totals?: Record<string, number>; last_created_at?: string | null }
type SyncData = { modules: Module[]; calls: ({ key: string } & Calls)[]; ebay_ready: boolean; ebay_not_ready: string[]
  live_stores: { id: string; name: string; marketplace_id: string }[]; all_ebay_stores: number; tasks: Task[] }

const data = ref<SyncData | null>(null)
const settings = ref<Record<string, AppSettingItem>>({})
const draft = ref<Record<string, any>>({})
const active = ref('messages')
const loading = ref(false)
const saving = ref(false)
const showStores = ref(false)
const showCalls = ref(false)

async function load() {
  loading.value = !data.value
  try {
    const [d, s] = await Promise.all([get<SyncData>('/internal/admin/sync'), get<{ items: AppSettingItem[] }>('/internal/admin/settings')])
    data.value = d
    const map: Record<string, AppSettingItem> = {}
    for (const it of s.items) map[it.key] = it
    settings.value = map
    for (const it of s.items) if (!(it.key in draft.value)) draft.value[it.key] = it.value
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

const mod = computed(() => data.value?.modules.find((m) => m.key === active.value) || null)
const task = computed(() => (mod.value?.task ? data.value?.tasks.find((t) => t.key === mod.value!.task) || null : null))
const todayCalls = (m: Module) => {
  const seen = new Set<string>()
  let n = 0
  for (const a of m.apis) if (a.key && a.calls && !seen.has(a.key)) { seen.add(a.key); n += a.calls.today }
  return n
}
const moduleFail = (m: Module) => m.apis.some((a) => a.calls?.today_fail)
const totalToday = computed(() => (data.value?.calls || []).reduce((n, c) => n + c.today, 0))
const totalFail = computed(() => (data.value?.calls || []).reduce((n, c) => n + c.today_fail, 0))
const taskOf = (m: Module) => (m.task ? data.value?.tasks.find((t) => t.key === m.task) : null)

// 配置：能在这里改的（系统配置项）
const editable = computed(() => (mod.value?.config || []).filter((c) => c.editable && settings.value[c.key]))
const readonly = computed(() => (mod.value?.config || []).filter((c) => !c.editable || !settings.value[c.key]))
const changed = computed(() => editable.value.filter((c) => draft.value[c.key] !== settings.value[c.key].value))

async function save() {
  if (!changed.value.length) return
  const warn = changed.value.map((c) => settings.value[c.key]).find((it) => it?.warn && draft.value[it.key] === true)
  if (warn) {
    try {
      await ElMessageBox.confirm(warn.warn!, `打开「${warn.label}」`, { type: 'warning' })
    } catch {
      return
    }
  }
  saving.value = true
  try {
    await api('PUT', '/internal/admin/settings', { values: Object.fromEntries(changed.value.map((c) => [c.key, draft.value[c.key]])) })
    ElMessage.success('已保存，立即生效')
    for (const c of changed.value) delete draft.value[c.key]
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

async function runNow(t: Task) {
  try {
    const r = await post<{ started: boolean }>(`/internal/admin/sync/${t.key}/run`)
    ElMessage.success(r.started ? '已开始运行，几秒后自动刷新结果' : '正在运行中，请稍候')
    setTimeout(load, 4000)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const pad = (n: number) => String(n).padStart(2, '0')
function fmt(v?: string | null) {
  if (!v) return '—'
  const d = new Date(v)
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function ago(v?: string | null) {
  if (!v) return ''
  const m = Math.round((Date.now() - Date.parse(v)) / 60000)
  return m < 1 ? '刚刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`
}
const show = (v: any) => (v === true ? '开' : v === false ? '关' : v === null || v === '' ? '（空）' : String(v))
const failedList = (t: Task) => Object.entries(t.last?.failed || {}) as [string, any][]

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(load, 15000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="sync" v-loading="loading">
    <!-- 顶部概览 -->
    <div class="panel top">
      <b>数据同步</b>
      <span class="muted small">每个功能的数据从哪个 eBay 接口来、带什么参数、怎么配置</span>
      <span class="spacer" />
      <span class="small">今天调用 eBay <b>{{ totalToday }}</b> 次<span v-if="totalFail" class="bad">，失败 {{ totalFail }}</span></span>
      <el-button size="small" @click="showCalls = !showCalls">{{ showCalls ? '收起' : '全部接口调用统计' }}</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>
    <el-alert v-if="data && !data.ebay_ready" type="error" :closable="false" show-icon
              :title="`eBay 凭证不齐，拉取不会运行：${data.ebay_not_ready.join('；')}`" />
    <div v-if="data" class="panel live small">
      <span class="muted">对接 eBay（真实收发、拉取消息）的店铺：</span>
      <el-tag v-for="s in data.live_stores" :key="s.id" size="small">{{ s.name }} · {{ s.marketplace_id.replace('EBAY_', '') }}</el-tag>
      <span v-if="!data.live_stores.length" class="warn">没有（消息不会拉取）</span>
      <span class="muted">· 共 {{ data.all_ebay_stores }} 个 eBay 店铺有秘钥 · 调用次数是本次启动 main.py 以来的统计</span>
    </div>

    <!-- 全部接口调用统计 -->
    <div v-if="showCalls && data" class="panel calls">
      <el-table :data="data.calls" size="small" max-height="320" empty-text="本次启动后还没有调用过 eBay">
        <el-table-column label="接口" prop="key" min-width="320"><template #default="{ row }"><span class="mono">{{ row.key }}</span></template></el-table-column>
        <el-table-column label="今天" prop="today" width="70" />
        <el-table-column label="今天失败" width="80"><template #default="{ row }"><span :class="{ bad: row.today_fail }">{{ row.today_fail }}</span></template></el-table-column>
        <el-table-column label="启动以来" prop="total" width="80" />
        <el-table-column label="最近调用" width="150"><template #default="{ row }">{{ fmt(row.last_at) }}</template></el-table-column>
        <el-table-column label="最近报错" min-width="240"><template #default="{ row }"><span class="bad small">{{ row.last_error || '' }}</span></template></el-table-column>
      </el-table>
    </div>

    <div v-if="data" class="body">
      <!-- 左：功能模块 -->
      <aside class="panel mods">
        <button v-for="m in data.modules" :key="m.key" :class="{ on: active === m.key }" @click="active = m.key; showStores = false">
          <span class="dot" :class="taskOf(m) ? (taskOf(m)!.running ? 'run' : taskOf(m)!.enabled ? 'ok' : 'off') : 'none'" />
          <span class="m-label">{{ m.label }}</span>
          <span class="m-calls" :class="{ bad: moduleFail(m) }" :title="'今天调用次数'">{{ todayCalls(m) || '' }}</span>
        </button>
        <div class="legend muted small"><span class="dot ok" />定时任务开启 <span class="dot off" />已关闭 <span class="dot none" />用到时才查</div>
      </aside>

      <!-- 右：模块详情 -->
      <section v-if="mod" class="panel detail">
        <div class="d-head">
          <b class="d-title">{{ mod.label }}</b>
          <template v-if="task">
            <el-tag size="small" :type="task.enabled ? 'success' : 'info'" effect="plain">{{ task.running ? '运行中' : task.enabled ? '已开启' : '已关闭' }}</el-tag>
            <span class="spacer" />
            <el-button v-if="task.key !== 'push'" size="small" :loading="task.running" @click="runNow(task)">立即运行</el-button>
          </template>
        </div>
        <div class="muted">{{ mod.purpose }}</div>

        <!-- 数据流向 -->
        <div class="flow">
          <div class="node"><div class="n-t">触发</div><div>{{ mod.trigger }}</div><div class="muted small">{{ mod.stores }}</div></div>
          <div class="arrow">→</div>
          <div class="node api-node"><div class="n-t">eBay 接口</div><div v-for="a in mod.apis" :key="a.name" class="small">{{ a.name }}</div></div>
          <div class="arrow">→</div>
          <div class="node"><div class="n-t">本地数据</div><div v-for="t in mod.tables" :key="t" class="small mono">{{ t }}</div></div>
          <div class="arrow">→</div>
          <div class="node"><div class="n-t">用在</div><div v-for="p in mod.pages" :key="p" class="small">{{ p }}</div></div>
        </div>

        <!-- 运行状态 -->
        <template v-if="task">
          <div class="sec-t">运行状态</div>
          <div v-if="task.key !== 'push'" class="status small">
            {{ task.interval }} · {{ task.scope }} · 上次运行 <b>{{ fmt(task.last_at) }}</b> <span class="muted">{{ ago(task.last_at) }}</span>
            <template v-if="task.last">
              <span v-if="task.key === 'messages'"> · {{ task.last.stores }} 个店铺，新消息 {{ task.last.messages_new }} 条</span>
              <span v-if="task.key === 'cases'"> · {{ task.last.stores }} 个店铺，新增 {{ task.last.new }}、状态变化 {{ task.last.changed }}、新订单 {{ task.last.orders_new }}</span>
              <span v-if="task.last.error" class="bad"> · 出错：{{ task.last.error }}</span>
            </template>
            <span v-else-if="!task.last_at" class="muted">（本次启动后还没运行）</span>
            <span v-if="task.totals" class="muted"> · 本地共 售后 {{ task.totals.cases }} 条、订单 {{ task.totals.orders }} 单</span>
            <span v-if="task.key === 'ship_notices' && task.last_created_at" class="muted"> · 最近生成 {{ fmt(task.last_created_at) }}</span>
            <div v-for="[name, err] in failedList(task)" :key="name" class="bad">失败：{{ name }} — {{ Array.isArray(err) ? err.join('、') : err }}</div>
          </div>
          <div v-else-if="task.push" class="status small">
            订阅 {{ task.push.subscriptions.enabled }}/{{ task.push.subscriptions.total }} 个店铺正常<span v-if="task.push.subscriptions.error" class="bad">，{{ task.push.subscriptions.error }} 个出错</span>
            · 24 小时收到 <b>{{ task.push.received_24h }}</b> 条 · 最近一次 {{ fmt(task.push.last_received_at) }} <span class="muted">{{ ago(task.push.last_received_at) }}</span>
            · 接收程序 {{ task.push.worker_running ? '运行中' : '没启动（要重启 main.py）' }} · 隧道 {{ task.push.tunnel?.running ? '已连接' : '未运行' }}
            <div v-if="task.push.reasons?.length" class="bad">推送没配好：{{ task.push.reasons.join('；') }}</div>
          </div>
          <template v-if="(task.stores || task.push?.stores)?.length">
            <button class="link small" @click="showStores = !showStores">{{ showStores ? '收起' : '展开' }}各店铺（{{ (task.stores || task.push.stores).length }}）</button>
            <el-table v-if="showStores && task.stores" :data="task.stores" size="small" max-height="320">
              <el-table-column label="店铺" prop="store_name" min-width="150" />
              <el-table-column label="上次成功" width="170"><template #default="{ row }">{{ fmt(row.last_success_at) }} <span class="muted">{{ ago(row.last_success_at) }}</span></template></el-table-column>
              <el-table-column label="上次统计" min-width="200"><template #default="{ row }"><span class="muted small">{{ row.stats ? JSON.stringify(row.stats) : '—' }}</span></template></el-table-column>
              <el-table-column label="报错" min-width="180"><template #default="{ row }"><span class="bad small">{{ row.last_error || '' }}</span></template></el-table-column>
            </el-table>
            <el-table v-if="showStores && !task.stores && task.push" :data="task.push.stores" size="small" max-height="320">
              <el-table-column label="店铺 ID" prop="store_id" min-width="150" />
              <el-table-column label="订阅状态" prop="status" width="100" />
              <el-table-column label="最近收到推送" width="160"><template #default="{ row }">{{ fmt(row.last_received_at) }}</template></el-table-column>
              <el-table-column label="报错" min-width="180"><template #default="{ row }"><span class="bad small">{{ row.error || '' }}</span></template></el-table-column>
            </el-table>
          </template>
        </template>

        <!-- 接口与参数 -->
        <div class="sec-t">eBay 接口与参数</div>
        <el-table :data="mod.apis" size="small" class="apis">
          <el-table-column label="接口" min-width="190">
            <template #default="{ row }">
              <b>{{ row.name }}</b>
              <div class="mono small"><span class="method" :class="row.method">{{ row.method === 'TRADING' ? 'Trading' : row.method }}</span> {{ row.path }}</div>
            </template>
          </el-table-column>
          <el-table-column label="权限" width="150"><template #default="{ row }"><span class="small">{{ row.scope }}</span></template></el-table-column>
          <el-table-column label="参数" min-width="260"><template #default="{ row }"><span class="small">{{ row.params }}</span></template></el-table-column>
          <el-table-column label="什么时候调用" width="150"><template #default="{ row }"><span class="small">{{ row.when }}</span></template></el-table-column>
          <el-table-column label="今天 / 失败" width="100">
            <template #default="{ row }">
              <template v-if="row.calls"><b>{{ row.calls.today }}</b> <span :class="row.calls.today_fail ? 'bad' : 'muted'">/ {{ row.calls.today_fail }}</span></template>
              <span v-else class="muted small">{{ row.key ? '0' : '—' }}</span>
              <div v-if="row.calls?.last_error" class="bad small" :title="row.calls.last_error">{{ fmt(row.calls.last_error_at) }} 出错</div>
            </template>
          </el-table-column>
        </el-table>

        <!-- 配置 -->
        <template v-if="mod.config.length">
          <div class="sec-t">配置</div>
          <div class="cfgs">
            <div v-for="c in editable" :key="c.key" class="cfg">
              <span class="c-label">{{ settings[c.key].label }}<div class="muted mono env">{{ c.env }}</div></span>
              <el-switch v-if="settings[c.key].type === 'bool'" v-model="draft[c.key]" size="small" />
              <el-input-number v-else-if="settings[c.key].type === 'int'" v-model="draft[c.key]" :min="0" size="small" controls-position="right" style="width: 130px" />
              <el-input v-else v-model="draft[c.key]" size="small" clearable style="width: 360px" />
              <span v-if="settings[c.key].help" class="muted small">{{ settings[c.key].help }}</span>
              <el-tag v-if="settings[c.key].source === 'page'" size="small" effect="plain">页面配置</el-tag>
            </div>
            <div v-if="changed.length" class="save-bar">
              <el-button size="small" type="primary" :loading="saving" @click="save">保存（{{ changed.length }} 项，立即生效）</el-button>
            </div>
            <div v-for="c in readonly" :key="c.key" class="cfg">
              <span class="c-label">{{ c.label }}<div class="muted mono env">{{ c.env }}</div></span>
              <span class="mono small ro">{{ show(c.value) }}</span>
              <span class="muted small">只能改 .env（改完重启 main.py）</span>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.sync {
  height: 100%;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 24px;
}
.sync > .panel,
.sync > .body {
  flex-shrink: 0;
}
.top,
.live {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
}
.calls {
  padding: 8px 12px;
}
.body {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.mods {
  width: 210px;
  flex-shrink: 0;
  padding: 6px 0;
  position: sticky;
  top: 0;
}
.mods button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 0;
  background: none;
  padding: 9px 14px;
  font-size: var(--fs-sm, 13px);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  color: var(--text);
}
.mods button:hover {
  background: var(--panel-soft, #f4f2ed);
}
.mods button.on {
  background: var(--panel-soft, #f4f2ed);
  color: var(--accent);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--accent);
}
.m-label {
  flex: 1;
}
.m-calls {
  font-size: 12px;
  color: var(--muted);
}
.legend {
  padding: 10px 14px 4px;
  border-top: 1px solid var(--line);
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  align-items: center;
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot.ok {
  background: #52c41a;
}
.dot.off {
  background: #bfbfbf;
}
.dot.run {
  background: #1677ff;
}
.dot.none {
  background: transparent;
  border: 1px solid #bfbfbf;
}
.detail {
  flex: 1;
  min-width: 0;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.d-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.d-title {
  font-size: 16px;
}
.flow {
  display: flex;
  align-items: stretch;
  gap: 6px;
  flex-wrap: wrap;
}
.node {
  flex: 1;
  min-width: 150px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  background: var(--panel-soft, #f7f9fb);
}
.api-node {
  border-color: var(--accent);
}
.n-t {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
}
.arrow {
  align-self: center;
  color: var(--muted);
}
.sec-t {
  font-weight: 600;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}
.status {
  background: var(--panel-soft, #f7f9fb);
  border-radius: 6px;
  padding: 6px 10px;
  line-height: 1.8;
}
.method {
  font-weight: 700;
  font-size: 11px;
  padding: 0 4px;
  border-radius: 3px;
  background: #e6f4ff;
  color: #0958d9;
}
.method.POST {
  background: #fff1f0;
  color: #cf1322;
}
.method.TRADING {
  background: #f6ffed;
  color: #389e0d;
}
.cfgs {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cfg {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.c-label {
  width: 210px;
  font-size: 13px;
}
.env {
  font-size: 11px;
}
.ro {
  background: var(--panel-soft, #f7f9fb);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 2px 8px;
  max-width: 420px;
  overflow-wrap: anywhere;
}
.save-bar {
  padding-left: 218px;
}
.spacer {
  flex: 1;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.bad {
  color: #a8071a;
}
.warn {
  color: #d4700a;
}
.link {
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0;
  align-self: flex-start;
}
@media (max-width: 900px) {
  .body {
    flex-direction: column;
  }
  .mods {
    width: 100%;
    position: static;
  }
  .c-label {
    width: 100%;
  }
  .save-bar {
    padding-left: 0;
  }
}
</style>
