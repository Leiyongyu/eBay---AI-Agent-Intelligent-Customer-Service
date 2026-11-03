<script setup lang="ts">
// 售后附件：售后邮箱收到的买家照片、视频，按 店铺 → 订单 列出；只看得到自己负责店铺的；待认领只有管理员和全部店铺账号能看
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { errorText, fmtTime } from '../format'
import AftersalesMailCard from '../components/AftersalesMailCard.vue'
import type { AftersalesMailItem, Store } from '../types'

const emit = defineEmits<{ open: [convId: number] }>()

const stores = ref<Store[]>([])
const storeName = computed(() => Object.fromEntries(stores.value.map((s) => [s.id, s.name])))
const isAdmin = computed(() => session.user?.role === 'ADMIN')
const seesAll = computed(() => !!session.user?.all_stores)

const tab = ref<'stores' | 'unclaimed'>('stores')
const filters = ref({ store_id: '', q: '', range: [] as string[], source: '' })
const items = ref<AftersalesMailItem[]>([])
const total = ref(0)
const unclaimed = ref<number | null>(null)
const retention = ref(30)
const loading = ref(false)
const page = ref(1)
const PAGE = 30

// 按店铺分组显示（同一店铺里按邮件新到旧，每封邮件就是一个订单的一批文件）
const groups = computed(() => {
  const map = new Map<string, AftersalesMailItem[]>()
  for (const m of items.value) {
    const key = m.store_id || '_'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(m)
  }
  return [...map.entries()].map(([sid, mails]) => ({ sid, mails }))
})

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams({ limit: String(PAGE), offset: String((page.value - 1) * PAGE) })
    if (tab.value === 'unclaimed') q.set('unclaimed', 'true')
    if (filters.value.store_id) q.set('store_id', filters.value.store_id)
    if (filters.value.q.trim()) q.set('q', filters.value.q.trim())
    if (filters.value.source && tab.value === 'stores') q.set('source', filters.value.source)
    if (filters.value.range?.length === 2) {
      q.set('date_from', filters.value.range[0])
      q.set('date_to', filters.value.range[1])
    }
    const r = await get<{ total: number; items: AftersalesMailItem[]; unclaimed: number | null; retention_days: number }>(
      `/internal/aftersales-files?${q}`)
    items.value = r.items
    total.value = r.total
    unclaimed.value = r.unclaimed
    retention.value = r.retention_days
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

async function claim(mail: AftersalesMailItem) {
  try {
    const { value } = await ElMessageBox.prompt(
      mail.store_id ? '改成关联到哪个订单？文件会挪到该订单所属店铺的文件夹并改名。' : '填订单号认领：系统查到订单所属店铺后，把文件挪过去并按规则改名。',
      mail.store_id ? '改关联' : '认领', { inputPlaceholder: '如 12-34567-89012', inputValue: mail.order_id || '',
        inputValidator: (v) => /^\d{2}-\d{5}-\d{5}$/.test((v || '').trim()) || '订单号格式如 12-34567-89012' })
    await post(`/internal/aftersales-mails/${mail.id}/claim`, { order_id: value.trim() })
    ElMessage.success('已关联')
    load()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}

// 管理员：邮箱状态、马上收信、马上清理
const status = ref<Record<string, any> | null>(null)
async function loadStatus() {
  try {
    status.value = await get('/internal/aftersales-files/status')
  } catch {
    status.value = null
  }
}
async function pollNow() {
  try {
    const r = await post<Record<string, any>>('/internal/aftersales-files/poll')
    if (r.error) ElMessage.error(r.error)
    else if (r.skipped) ElMessage.warning(r.skipped.join('；'))
    else ElMessage.success(`处理了 ${r.processed} 封${r.failed ? `，失败 ${r.failed} 封` : ''}`)
    load()
    loadStatus()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function cleanupNow() {
  try {
    await ElMessageBox.confirm(`删除满 ${retention.value} 天的附件文件（记录保留），并彻底删除“已处理”里满 ${retention.value} 天的邮件？`, '马上清理')
    const r = await post<Record<string, any>>('/internal/aftersales-files/cleanup')
    ElMessage.success(`清理了 ${r.files} 个文件、${r.mailbox_deleted} 封邮件`)
    load()
    loadStatus()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}
const gb = (n: number) => (n / 1024 ** 3).toFixed(2)

onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores')
  load()
  loadStatus()
})
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      售后附件
      <span class="muted">售后邮箱收到的买家照片、视频，保存 {{ retention }} 天后自动清理</span>
      <span class="spacer" />
      <template v-if="status">
        <el-tag size="small" :type="status.enabled ? 'success' : 'warning'">{{ status.enabled ? '收信中' : '邮箱未开启' }}</el-tag>
        <span class="muted small">{{ status.files }} 个文件 · {{ gb(status.bytes) }} GB</span>
      </template>
      <template v-if="isAdmin">
        <el-button size="small" @click="pollNow">马上收信</el-button>
        <el-button size="small" @click="cleanupNow">马上清理</el-button>
      </template>
    </div>
    <div class="panel-body">
      <el-alert v-if="isAdmin && status && !status.enabled" type="warning" :closable="false" show-icon class="mb"
        title="售后邮箱还没开启，不会收信" :description="(status.not_ready_reasons || []).join('；') + '。在 .env 填 AFTERSALES_MAIL_USER、AFTERSALES_MAIL_PASSWORD（客户端专用密码），把 AFTERSALES_MAIL_ENABLED 改成 true 后重启。'" />
      <div v-if="isAdmin && status?.last_poll_at" class="muted small mb">
        最近收信 {{ fmtTime(status.last_poll_at) }}：{{ JSON.stringify(status.last_poll) }} · 文件夹 {{ status.dir }}
      </div>
      <el-tabs v-model="tab" @tab-change="search">
        <el-tab-pane label="按店铺" name="stores" />
        <el-tab-pane v-if="seesAll" :label="`待认领${unclaimed ? `（${unclaimed}）` : ''}`" name="unclaimed" />
      </el-tabs>
      <div class="bar">
        <el-select v-if="tab === 'stores'" v-model="filters.store_id" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
          <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" size="small"
          start-placeholder="收到日期从" end-placeholder="到" style="width: 260px" />
        <el-radio-group v-if="tab === 'stores'" v-model="filters.source" size="small" @change="search">
          <el-radio-button value="">全部来源</el-radio-button>
          <el-radio-button value="mail">售后邮箱</el-radio-button>
          <el-radio-button value="upload">上传页</el-radio-button>
        </el-radio-group>
        <el-input v-model="filters.q" placeholder="订单号 / 主题 / 发件邮箱 / 售后编号" size="small" clearable style="width: 240px" @keyup.enter="search" />
        <el-button size="small" type="primary" @click="search">查询</el-button>
      </div>
      <div v-loading="loading">
        <el-empty v-if="!items.length" :description="tab === 'unclaimed' ? '没有待认领的邮件' : '还没有售后附件'" />
        <div v-for="g in groups" :key="g.sid" class="group">
          <h4 v-if="tab === 'stores'">{{ storeName[g.sid] || g.sid }} <span class="muted small">{{ g.sid }}</span></h4>
          <AftersalesMailCard v-for="m in g.mails" :key="`${m.kind}-${m.id}`" :mail="m" show-conversation
            @claim="claim" @open="(id) => emit('open', id)" />
        </div>
      </div>
      <el-pagination v-if="total > PAGE" v-model:current-page="page" :page-size="PAGE" :total="total"
        layout="total, prev, pager, next" @current-change="load" />
    </div>
  </div>
</template>

<style scoped>
.spacer {
  flex: 1;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.mb {
  margin-bottom: 10px;
}
.small {
  font-size: 12px;
}
.group h4 {
  margin: 12px 0 6px;
}
</style>
