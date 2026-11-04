<script setup lang="ts">
// 发票：所有发票申请统一在这里审核、发送（买家要发票时自动登记；客服、售后看自己负责的店铺）；
// 管理员另有发票留底（每份生成的 PDF）、开票主体（按店铺分配）、企业邮箱发送设置与测试
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import InvoiceRequestDrawer from '../components/InvoiceRequestDrawer.vue'
import { errorText, fmtTime } from '../format'
import type { InvoiceInfo, InvoiceIssuer, InvoiceRequestRow, Store } from '../types'

const emit = defineEmits<{ open: [convId: number] }>()
const isAdmin = computed(() => session.user?.role === 'ADMIN')
const tab = ref<'requests' | 'archive' | 'issuers' | 'mail'>('requests')

// ---------------------------------------------------------------- 发票申请（所有角色，按负责的店铺）
const reqs = ref<InvoiceRequestRow[]>([])
const reqTotal = ref(0)
const reqLoading = ref(false)
const reqFilter = ref({ status: 'active', store_id: '', q: '' })
const counts = ref({ open: 0, waiting: 0 })
const openReq = ref<number | null>(null)

async function loadRequests() {
  reqLoading.value = true
  try {
    const q = new URLSearchParams({ status: reqFilter.value.status, limit: '100' })
    if (reqFilter.value.store_id) q.set('store_id', reqFilter.value.store_id)
    if (reqFilter.value.q.trim()) q.set('q', reqFilter.value.q.trim())
    const [r, c] = await Promise.all([
      get<{ total: number; items: InvoiceRequestRow[] }>(`/internal/invoice-requests?${q}`),
      get<{ open: number; waiting: number }>('/internal/invoice-requests/count'),
    ])
    reqs.value = r.items
    reqTotal.value = r.total
    counts.value = c
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    reqLoading.value = false
  }
}

function fromHash() {
  const m = location.hash.match(/[?&]req=(\d+)/)
  if (m) {
    tab.value = 'requests'
    openReq.value = Number(m[1])
  }
}
function closeReq() {
  openReq.value = null
  if (location.hash.includes('req=')) history.replaceState(null, '', '#/invoices')
}
function openConversation(id: number) {
  closeReq()
  emit('open', id)
}
const REQ_TYPE: Record<string, 'warning' | 'info' | 'success'> = {
  OPEN: 'warning', WAITING_EMAIL: 'info', DONE: 'success', CANCELLED: 'info',
}
const stores = ref<Store[]>([])
const storeName = computed(() => Object.fromEntries(stores.value.map((s) => [s.id, s.name])))

// ---------------------------------------------------------------- 发票留底
const rows = ref<InvoiceInfo[]>([])
const total = ref(0)
const loading = ref(false)
const filters = ref({ store_id: '', q: '', status: '', range: [] as string[] })
const page = ref(1)
const PAGE = 50
const statusLabels = ref<Record<string, string>>({})

function query() {
  const f = filters.value
  const q = new URLSearchParams()
  if (f.store_id) q.set('store_id', f.store_id)
  if (f.q.trim()) q.set('q', f.q.trim())
  if (f.status) q.set('status', f.status)
  if (f.range?.length === 2) {
    q.set('date_from', f.range[0])
    q.set('date_to', f.range[1])
  }
  return q
}

async function loadArchive() {
  loading.value = true
  try {
    const q = query()
    q.set('limit', String(PAGE))
    q.set('offset', String((page.value - 1) * PAGE))
    const r = await get<{ total: number; items: InvoiceInfo[]; status_labels: Record<string, string> }>(`/api/invoices?${q}`)
    rows.value = r.items
    total.value = r.total
    statusLabels.value = r.status_labels
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  loadArchive()
}

const detail = ref<InvoiceInfo | null>(null)
async function openDetail(row: InvoiceInfo) {
  detail.value = await get<InvoiceInfo>(`/api/invoices/${row.id}`)
}

async function voidInvoice(row: InvoiceInfo) {
  try {
    const { value } = await ElMessageBox.prompt(`作废发票 ${row.number}？号码和留底文件会保留。`, '作废发票', {
      inputPlaceholder: '作废原因', inputValidator: (v) => !!v?.trim() || '请写原因',
    })
    await post(`/internal/invoices/${row.id}/void`, { reason: value })
    loadArchive()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'primary'> = {
  ISSUED: 'primary', SENT: 'success', SIMULATED: 'warning', SEND_FAILED: 'danger', VOID: 'info',
}

// ---------------------------------------------------------------- 开票主体
const issuers = ref<InvoiceIssuer[]>([])
const defaultRates = ref<Record<string, number>>({})
const dialog = ref(false)
const form = ref<InvoiceIssuer & { vatText: string; rateText: string }>(blank())
const saving = ref(false)
const prefillStore = ref('')

function blank() {
  return { name: '', address: '', postcode: '', phone: '', email: '', vat_numbers: {}, vat_rates: {}, footer: '',
           is_active: false, store_ids: [] as string[], vatText: '', rateText: '' } as InvoiceIssuer & { vatText: string; rateText: string }
}
const toText = (m: Record<string, string | number>) => Object.entries(m).map(([k, v]) => `${k}=${v}`).join('\n')
function parse(text: string) {
  const out: Record<string, string> = {}
  for (const line of text.split(/[\n,;]+/)) {
    const [k, ...rest] = line.split(/[=:：]/)
    if (k?.trim() && rest.join('').trim()) out[k.trim().toUpperCase()] = rest.join('').trim()
  }
  return out
}

async function loadIssuers() {
  const r = await get<{ items: InvoiceIssuer[]; default_vat_rates: Record<string, number> }>('/api/invoice-issuers')
  issuers.value = r.items
  defaultRates.value = r.default_vat_rates
}

function openIssuer(i?: InvoiceIssuer) {
  form.value = i
    ? { ...i, store_ids: [...i.store_ids], vatText: toText(i.vat_numbers), rateText: toText(i.vat_rates) }
    : { ...blank(), rateText: toText(defaultRates.value) }
  prefillStore.value = ''
  dialog.value = true
}

async function prefill() {
  if (!prefillStore.value) return
  try {
    const r = await get<{ name: string; address: string; postcode: string; phone: string }>(
      `/api/invoice-issuers/prefill?store_id=${encodeURIComponent(prefillStore.value)}`)
    Object.assign(form.value, { name: r.name || form.value.name, address: r.address || form.value.address,
                                postcode: r.postcode || form.value.postcode, phone: r.phone || form.value.phone })
    if (!form.value.store_ids.includes(prefillStore.value)) form.value.store_ids.push(prefillStore.value)
    ElMessage.success('已从 eBay 注册资料填入；税号 eBay 不提供，请手动填写')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function saveIssuer() {
  const f = form.value
  const rates = Object.fromEntries(Object.entries(parse(f.rateText)).map(([k, v]) => [k, Number(v)]))
  if (Object.values(rates).some((v) => Number.isNaN(v))) return ElMessage.warning('税率要填数字，如 DE=19')
  saving.value = true
  try {
    const body = { name: f.name, address: f.address, postcode: f.postcode, phone: f.phone, email: f.email,
                   footer: f.footer, is_active: f.is_active, store_ids: f.store_ids,
                   vat_numbers: parse(f.vatText), vat_rates: rates }
    if (f.id) await api('PATCH', `/api/invoice-issuers/${f.id}`, body)
    else await post('/api/invoice-issuers', body)
    ElMessage.success('已保存')
    dialog.value = false
    loadIssuers()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

const assigned = computed(() => new Set(issuers.value.flatMap((i) => i.store_ids)))

// ---------------------------------------------------------------- 邮箱
const mail = ref<{ enabled: boolean; not_ready_reasons: string[]; host: string; port: number; user: string | null;
                   password_configured: boolean; bcc: string | null; from_name: string | null } | null>(null)
const testTo = ref('')
const testing = ref(false)
async function loadMail() {
  mail.value = await get('/api/invoices/mail-status')
}
async function testMail() {
  testing.value = true
  try {
    await post('/api/invoices/mail-test', { to: testTo.value })
    ElMessage.success('测试邮件已发出，请到收件箱确认')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    testing.value = false
  }
}

onMounted(async () => {
  fromHash()
  window.addEventListener('hashchange', fromHash)
  stores.value = await get<Store[]>('/internal/stores')
  loadRequests()
  if (isAdmin.value) {
    loadArchive()
    loadIssuers()
    loadMail()
  }
})
onUnmounted(() => window.removeEventListener('hashchange', fromHash))
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      发票
      <span class="muted">买家要发票时自动登记到这里；人工审核后发到买家邮箱，并在 eBay 消息里提醒买家查收。每份发票都留底，已发出的只有管理员能看</span>
    </div>
    <div class="panel-body">
      <el-tabs v-model="tab">
        <el-tab-pane :label="`发票申请${counts.open ? `（待审核 ${counts.open}）` : ''}`" name="requests">
          <div class="bar">
            <el-radio-group v-model="reqFilter.status" size="small" @change="loadRequests">
              <el-radio-button value="active">待处理（{{ counts.open + counts.waiting }}）</el-radio-button>
              <el-radio-button value="OPEN">待审核发送（{{ counts.open }}）</el-radio-button>
              <el-radio-button value="WAITING_EMAIL">等买家邮箱（{{ counts.waiting }}）</el-radio-button>
              <el-radio-button value="DONE">已发送</el-radio-button>
              <el-radio-button value="CANCELLED">已取消</el-radio-button>
              <el-radio-button value="all">全部</el-radio-button>
            </el-radio-group>
            <el-select v-model="reqFilter.store_id" placeholder="全部店铺" clearable filterable size="small" style="width: 180px" @change="loadRequests">
              <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
            </el-select>
            <el-input v-model="reqFilter.q" placeholder="买家 / 订单号 / 邮箱" size="small" clearable style="width: 200px" @keyup.enter="loadRequests" />
            <el-button size="small" @click="loadRequests">刷新</el-button>
          </div>
          <el-table :data="reqs" v-loading="reqLoading" size="small" border class="clickable" @row-click="(r: InvoiceRequestRow) => (openReq = r.id)">
            <el-table-column label="状态" width="120">
              <template #default="{ row }"><el-tag size="small" :type="REQ_TYPE[row.status]">{{ row.status_label }}</el-tag></template>
            </el-table-column>
            <el-table-column label="买家" min-width="150">
              <template #default="{ row }">
                <b class="mono">{{ row.buyer_key }}</b>
                <el-tag v-if="row.test_conversation" size="small" type="info">测试</el-tag>
                <div class="muted small">{{ storeName[row.store_id] || row.store_id }} · {{ row.marketplace_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="买家邮箱" min-width="170">
              <template #default="{ row }">{{ row.email || '—' }}</template>
            </el-table-column>
            <el-table-column label="订单" min-width="130">
              <template #default="{ row }">
                {{ row.order_id || '待选' }}
                <div class="muted small">{{ row.invoice_type === 'VAT' ? '要增值税发票' : row.invoice_type === 'NORMAL' ? '要普通发票' : '种类未问清' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="发票" min-width="180">
              <template #default="{ row }">
                <template v-if="row.invoice">
                  <span class="mono">{{ row.invoice.number }}</span> · {{ row.invoice.total.toFixed(2) }} {{ row.invoice.currency }}
                  <div class="muted small">{{ row.invoice.status_label }}</div>
                </template>
                <span v-else class="muted">未生成</span>
                <div v-if="row.note" class="warn small">{{ row.note }}</div>
              </template>
            </el-table-column>
            <el-table-column label="更新" width="130">
              <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column width="90">
              <template #default="{ row }">
                <el-button size="small" :type="row.status === 'OPEN' ? 'primary' : 'default'" @click.stop="openReq = row.id">
                  {{ row.status === 'OPEN' ? '审核' : '查看' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="muted small">共 {{ reqTotal }} 条。买家在消息里要发票时 AI 会请买家给邮箱，这里先登记为“等买家邮箱”；买家回了邮箱就变成“待审核发送”，能确定订单的会先把发票生成好。</p>
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" label="发票留底" name="archive">
          <div class="bar">
            <el-select v-model="filters.store_id" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
              <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
            </el-select>
            <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" size="small"
              start-placeholder="开票日期从" end-placeholder="到" style="width: 260px" />
            <el-select v-model="filters.status" placeholder="全部状态" clearable size="small" style="width: 150px">
              <el-option v-for="(label, k) in statusLabels" :key="k" :value="k" :label="label" />
            </el-select>
            <el-input v-model="filters.q" placeholder="发票号 / 订单号 / 邮箱" size="small" clearable style="width: 200px"
              @keyup.enter="search" />
            <el-button size="small" type="primary" @click="search">查询</el-button>
            <a class="export" :href="`/api/invoices/export?${query()}`">导出 Excel</a>
          </div>
          <el-table :data="rows" v-loading="loading" size="small" border>
            <el-table-column label="发票号" min-width="150">
              <template #default="{ row }"><a class="mono" @click="openDetail(row)">{{ row.number }}</a></template>
            </el-table-column>
            <el-table-column label="开票日期" prop="issue_date" width="100" />
            <el-table-column label="种类" prop="kind_label" width="96" />
            <el-table-column label="店铺" min-width="130">
              <template #default="{ row }">{{ storeName[row.store_id] || row.store_id }}</template>
            </el-table-column>
            <el-table-column label="订单号" prop="order_id" min-width="130" />
            <el-table-column label="买方" min-width="140">
              <template #default="{ row }">{{ row.buyer }} <span class="muted">{{ row.ship_country }}</span></template>
            </el-table-column>
            <el-table-column label="金额" width="120" align="right">
              <template #default="{ row }">{{ row.total.toFixed(2) }} {{ row.currency }}</template>
            </el-table-column>
            <el-table-column label="增值税" width="110" align="right">
              <template #default="{ row }">
                <span v-if="row.vat_rate != null">{{ row.vat_amount?.toFixed(2) }}（{{ row.vat_rate }}%）</span>
                <span v-else class="muted">未列</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="150">
              <template #default="{ row }">
                <el-tag size="small" :type="STATUS_TYPE[row.status]">{{ row.status_label }}</el-tag>
                <div v-if="row.sent_to" class="muted small">→ {{ row.sent_to }} · {{ row.sent_by }}</div>
                <div v-if="row.void_reason" class="muted small">{{ row.void_reason }}</div>
              </template>
            </el-table-column>
            <el-table-column label="开票" min-width="120">
              <template #default="{ row }">{{ row.created_by }}<div class="muted small">{{ fmtTime(row.created_at) }}</div></template>
            </el-table-column>
            <el-table-column label="" width="150">
              <template #default="{ row }">
                <a :href="`/internal/invoices/${row.id}/pdf`" target="_blank" rel="noopener">查看</a>
                <a :href="`/internal/invoices/${row.id}/pdf?download=true`" class="ml">下载</a>
                <el-button v-if="row.status !== 'VOID'" link type="danger" size="small" class="ml" @click="voidInvoice(row)">作废</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination v-if="total > PAGE" v-model:current-page="page" :page-size="PAGE" :total="total"
            layout="total, prev, pager, next" class="pager" @current-change="loadArchive" />
        </el-tab-pane>

        <el-tab-pane v-if="isAdmin" label="开票主体" name="issuers">
          <div class="bar">
            <span class="muted">发票上的卖方公司；一个主体可以给多个店铺用。启用（确认信息无误）后，对应店铺才能开票。</span>
            <el-button type="primary" size="small" style="margin-left: auto" @click="openIssuer()">新建开票主体</el-button>
          </div>
          <el-table :data="issuers" size="small" border>
            <el-table-column label="公司" min-width="200">
              <template #default="{ row }">
                <b>{{ row.name }}</b>
                <div class="muted small pre">{{ row.address }}</div>
              </template>
            </el-table-column>
            <el-table-column label="增值税号" min-width="170">
              <template #default="{ row }"><div v-for="(v, k) in row.vat_numbers" :key="k" class="mono small">{{ k }}: {{ v }}</div></template>
            </el-table-column>
            <el-table-column label="税率" min-width="120">
              <template #default="{ row }">{{ Object.entries(row.vat_rates).map(([k, v]) => `${k} ${v}%`).join('、') }}</template>
            </el-table-column>
            <el-table-column label="店铺" min-width="200">
              <template #default="{ row }">
                <span v-if="!row.store_ids.length" class="warn">未分配店铺</span>
                {{ row.store_ids.map((id: string) => storeName[id] || id).join('、') }}
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '已启用' : '未启用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="" width="70">
              <template #default="{ row }"><el-button link type="primary" size="small" @click="openIssuer(row)">编辑</el-button></template>
            </el-table-column>
          </el-table>
          <p class="muted small">没分配开票主体的店铺：{{ stores.filter((s) => !assigned.has(s.id)).length }} 个</p>
        </el-tab-pane>

        <el-tab-pane v-if="isAdmin" label="邮件发送" name="mail">
          <template v-if="mail">
            <el-descriptions :column="1" border size="small" style="max-width: 640px">
              <el-descriptions-item label="发送状态">
                <el-tag :type="mail.not_ready_reasons.length ? 'warning' : 'success'" size="small">
                  {{ mail.not_ready_reasons.length ? '只模拟发送（不会真正发出）' : '已开启，会真正发邮件' }}
                </el-tag>
                <div v-for="r in mail.not_ready_reasons" :key="r" class="muted small">{{ r }}</div>
              </el-descriptions-item>
              <el-descriptions-item label="SMTP 服务器">{{ mail.host }}:{{ mail.port }}</el-descriptions-item>
              <el-descriptions-item label="发件邮箱">{{ mail.user || '未配置' }}</el-descriptions-item>
              <el-descriptions-item label="客户端专用密码">{{ mail.password_configured ? '已配置' : '未配置' }}</el-descriptions-item>
              <el-descriptions-item label="发件人名称">{{ mail.from_name || '（用开票主体名称）' }}</el-descriptions-item>
              <el-descriptions-item label="密送留底">{{ mail.bcc || '无' }}</el-descriptions-item>
            </el-descriptions>
            <p class="muted small">
              配置在 .env 的 INVOICE_SMTP_*（企业微信邮箱：smtp.exmail.qq.com，端口 465；密码用企业邮箱里生成的“客户端专用密码”）。
              改完重启程序，先发测试邮件，成功后把 INVOICE_MAIL_ENABLED 改成 true。
            </p>
            <div class="bar">
              <el-input v-model="testTo" placeholder="测试收件邮箱" size="small" style="width: 260px" />
              <el-button size="small" :loading="testing" :disabled="!testTo" @click="testMail">发测试邮件</el-button>
            </div>
          </template>
        </el-tab-pane>
      </el-tabs>
    </div>

    <InvoiceRequestDrawer :req-id="openReq" @close="closeReq" @changed="loadRequests" @open-conversation="openConversation" />

    <el-dialog v-model="dialog" :title="form.id ? '编辑开票主体' : '新建开票主体'" width="620px">
      <el-form label-width="96px">
        <el-form-item v-if="!form.id" label="从 eBay 读取">
          <el-select v-model="prefillStore" filterable placeholder="选店铺，读它的 eBay 注册资料" size="small" style="width: 260px">
            <el-option v-for="s in stores.filter((x) => x.ebay_user_id)" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
          <el-button size="small" class="ml" :disabled="!prefillStore" @click="prefill">读取</el-button>
        </el-form-item>
        <el-alert type="info" :closable="false" class="mb-s"
          title="发票抬头（公司名、注册人、地址、邮编）开票时自动取订单所属店铺的 eBay 注册资料；这里主要填税号和税率，公司名和地址只在读不到 eBay 资料时备用。" />
        <el-form-item label="主体名称"><el-input v-model="form.name" placeholder="方便自己认，如 湘彦；也是读不到 eBay 资料时的备用抬头" /></el-form-item>
        <el-form-item label="备用地址"><el-input v-model="form.address" type="textarea" :rows="2" placeholder="可空" /></el-form-item>
        <el-form-item label="邮编"><el-input v-model="form.postcode" style="width: 160px" /></el-form-item>
        <el-form-item label="电话"><el-input v-model="form.phone" style="width: 240px" /></el-form-item>
        <el-form-item label="邮箱"><el-input v-model="form.email" placeholder="印在发票上（可空）" /></el-form-item>
        <el-form-item label="增值税号">
          <el-input v-model="form.vatText" type="textarea" :rows="4" placeholder="每行一个：国家=税号&#10;DE=DE329615047&#10;GB=GB345543105" />
          <div class="muted small">照抄 eBay 后台账户设置里的“增值税编号”即可（不带国家前缀也行，发票上自动补成 DE329615047 这种格式）；
            发票按收货国家取税号，没有就用站点国家的。不要填 DE200081785——那是 eBay 的 OSS 税号</div>
        </el-form-item>
        <el-form-item label="税率 %">
          <el-input v-model="form.rateText" type="textarea" :rows="4" placeholder="每行一个：国家=税率&#10;DE=19" />
          <div class="muted small">收货国家没配税率的订单，发票不列增值税</div>
        </el-form-item>
        <el-form-item label="页脚"><el-input v-model="form.footer" type="textarea" :rows="2" placeholder="可空，印在发票最下面" /></el-form-item>
        <el-form-item label="用于店铺">
          <el-select v-model="form.store_ids" multiple filterable style="width: 100%">
            <el-option v-for="s in stores" :key="s.id" :value="s.id" :label="s.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
          <span class="muted small ml">信息核对无误后再启用；未启用的主体不能开票</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!form.name" @click="saveIssuer">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="!!detail" :title="`发票 ${detail?.number || ''}`" width="640px" @close="detail = null">
      <template v-if="detail?.snapshot">
        <el-alert v-for="w in detail.warnings" :key="w" :title="w" type="warning" :closable="false" style="margin-bottom: 6px" />
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="订单">{{ detail.order_id }}</el-descriptions-item>
          <el-descriptions-item label="语言">{{ detail.language }}</el-descriptions-item>
          <el-descriptions-item label="收票人" :span="2"><span class="pre">{{ detail.snapshot.bill_to.join('\n') }}</span></el-descriptions-item>
          <el-descriptions-item label="收货地址" :span="2"><span class="pre">{{ detail.snapshot.ship_to.join('\n') }}</span></el-descriptions-item>
          <el-descriptions-item label="商品金额">{{ detail.snapshot.subtotal }}</el-descriptions-item>
          <el-descriptions-item label="运费">{{ detail.snapshot.shipping }}</el-descriptions-item>
          <el-descriptions-item label="折扣">{{ detail.snapshot.discount || '—' }}</el-descriptions-item>
          <el-descriptions-item label="总额">{{ detail.snapshot.total }} {{ detail.currency }}</el-descriptions-item>
          <el-descriptions-item label="卖方税号">{{ detail.snapshot.seller.vat_number || '—' }}</el-descriptions-item>
          <el-descriptions-item label="发送">{{ detail.sent_to || '未发送' }} {{ detail.sent_at ? fmtTime(detail.sent_at) : '' }}</el-descriptions-item>
        </el-descriptions>
        <p><a :href="`/internal/invoices/${detail.id}/pdf`" target="_blank" rel="noopener">打开 PDF ↗</a></p>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.export {
  margin-left: auto;
}
.small {
  font-size: var(--fs-xs);
}
.pre {
  white-space: pre-wrap;
}
.ml {
  margin-left: 8px;
}
.warn {
  color: var(--warn-text);
}
.mb-s {
  margin-bottom: 10px;
}
.pager {
  margin-top: 10px;
}
a {
  cursor: pointer;
}
.clickable :deep(tr) {
  cursor: pointer;
}
</style>
