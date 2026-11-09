<script setup lang="ts">
// Best offer：买家在刊登上出的报价。待处理的按过期时间排（48 小时过期）；接受 / 还价 / 拒绝会真实发到 eBay（只限对接 eBay 的店铺），
// 接受后买家即成交，所以每次都要二次确认。数据每 15 分钟从 eBay 拉一次（可在数据同步页改），也可点“马上刷新”
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'
import type { Store } from '../types'

const emit = defineEmits<{ open: [id: number] }>()

type Offer = {
  id: number; store_id: string; store_name: string; marketplace_id: string | null; best_offer_id: string; item_id: string | null
  title: string | null; picture_url: string | null; bin_price: number | null; currency: string | null; price: number | null
  quantity: number | null; percent: number | null; buyer: string | null; buyer_feedback: number | null; status: string | null
  status_label: string; pending: boolean; type_label: string | null; buyer_message: string | null; seller_message: string | null
  expires_at: string | null; hours_left: number | null; responded_action: string | null; responded_by: string | null
  responded_at: string | null; counter_price: number | null; response_message: string | null; response_error: string | null
  can_respond: boolean; respond_blocked: string | null; conversation_id: number | null; item_url: string | null
}
const TABS = [{ value: 'pending', label: '待处理' }, { value: 'done', label: '已处理' }, { value: 'all', label: '全部' }]
const tab = ref('pending')
const rows = ref<Offer[]>([])
const counts = ref<{ pending: number; done: number; all: number; sync: { last_at: string | null; running: boolean; last: any } } | null>(null)
const stores = ref<Store[]>([])
const storeId = ref('')
const keyword = ref('')
const loading = ref(false)

async function load() {
  loading.value = !rows.value.length
  try {
    const q = new URLSearchParams({ tab: tab.value })
    if (storeId.value) q.set('store_id', storeId.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    const cq = new URLSearchParams()
    if (storeId.value) cq.set('store_id', storeId.value)
    const [r, c] = await Promise.all([get<Offer[]>(`/internal/best-offers?${q}`), get<any>(`/internal/best-offers/counts?${cq}`)])
    rows.value = r
    counts.value = c
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
let searchTimer: number | undefined
watch(keyword, () => {
  clearTimeout(searchTimer)
  searchTimer = window.setTimeout(load, 400)
})
watch([tab, storeId], load)

async function refresh() {
  try {
    const r = await post<{ started: boolean }>('/internal/best-offers/refresh')
    ElMessage.success(r.started ? '正在从 eBay 拉取全部店铺的报价（约 1 分钟），完成后自动刷新' : '正在拉取中，请稍候')
    setTimeout(load, 30000)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---- 回应 ----
const dialog = ref(false)
const current = ref<Offer | null>(null)
const action = ref<'Accept' | 'Counter' | 'Decline'>('Accept')
const counterPrice = ref<number | undefined>()
const counterQty = ref<number | undefined>()
const message = ref('')
const sending = ref(false)
const ACTION_LABEL = { Accept: '接受', Counter: '还价', Decline: '拒绝' } as const

function openRespond(o: Offer, a: 'Accept' | 'Counter' | 'Decline') {
  current.value = o
  action.value = a
  counterPrice.value = a === 'Counter' && o.price && o.bin_price ? Math.round(((o.price + o.bin_price) / 2) * 100) / 100 : undefined
  counterQty.value = o.quantity || 1
  message.value = ''
  dialog.value = true
}

const summary = computed(() => {
  const o = current.value
  if (!o) return ''
  const m = (v: number | null | undefined) => (v != null ? `${v.toFixed(2)} ${o.currency || ''}` : '—')
  if (action.value === 'Accept') return `接受买家 ${o.buyer} 的报价 ${m(o.price)} × ${o.quantity || 1}，买家将直接成交并付款。`
  if (action.value === 'Counter') return `向买家 ${o.buyer} 还价 ${m(counterPrice.value)} × ${counterQty.value || 1}（买家报价 ${m(o.price)}，一口价 ${m(o.bin_price)}）。`
  return `拒绝买家 ${o.buyer} 的报价 ${m(o.price)}。`
})

async function submit() {
  const o = current.value
  if (!o) return
  if (action.value === 'Counter' && !(counterPrice.value && counterPrice.value > 0)) return ElMessage.warning('请填写还价价格')
  try {
    await ElMessageBox.confirm(`${summary.value}\n\n这会真实发到 eBay，不能撤回。确定吗？`, `${ACTION_LABEL[action.value]}报价`,
                               { type: 'warning', confirmButtonText: `确定${ACTION_LABEL[action.value]}` })
  } catch {
    return
  }
  sending.value = true
  try {
    await post(`/internal/best-offers/${o.id}/respond`, {
      action: action.value, price: action.value === 'Counter' ? counterPrice.value : null,
      quantity: action.value === 'Counter' ? counterQty.value : null, message: message.value || null, confirmed: true,
    })
    ElMessage.success(`已${ACTION_LABEL[action.value]}`)
    dialog.value = false
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
    load()
  } finally {
    sending.value = false
  }
}

const pad = (n: number) => String(n).padStart(2, '0')
const fmt = (v: string | null) => {
  if (!v) return '—'
  const d = new Date(v)
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const money = (v: number | null, c: string | null) => (v != null ? `${v.toFixed(2)} ${c || ''}` : '—')
const left = (h: number | null) => (h == null ? '' : h <= 0 ? '已过期' : h < 1 ? `${Math.round(h * 60)} 分钟后过期` : `${Math.floor(h)} 小时后过期`)

let timer: number | undefined
onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  load()
  timer = window.setInterval(load, 60000)
})
onUnmounted(() => {
  clearInterval(timer)
  clearTimeout(searchTimer)
})
</script>

<template>
  <div class="panel bo">
    <div class="panel-head">
      Best offer
      <span class="muted small">买家在刊登上出的报价，48 小时内不处理就过期</span>
      <span class="spacer" />
      <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
        <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-input v-model="keyword" size="small" clearable placeholder="买家 / 商品号 / 标题" style="width: 200px" />
      <span class="muted small">更新于 {{ fmt(counts?.sync?.last_at || null) }}{{ counts?.sync?.running ? '（拉取中…）' : '' }}</span>
      <el-button size="small" :loading="counts?.sync?.running" @click="refresh">马上刷新</el-button>
    </div>
    <div class="tabs">
      <button v-for="t in TABS" :key="t.value" :class="{ on: tab === t.value }" @click="tab = t.value">
        {{ t.label }}<span class="n">{{ counts ? (counts as any)[t.value] : '' }}</span>
      </button>
      <span class="muted small tip">接受 / 还价 / 拒绝会真实发到 eBay，只有“对接 eBay 的店铺”能在这里操作，其余店铺请到 eBay 后台处理</span>
    </div>
    <div class="panel-body">
      <el-table :data="rows" v-loading="loading" size="small" :empty-text="tab === 'pending' ? '现在没有待处理的报价' : '没有记录'">
        <el-table-column label="商品" min-width="260">
          <template #default="{ row }">
            <div class="item">
              <img v-if="row.picture_url" :src="row.picture_url" alt="" referrerpolicy="no-referrer" />
              <div>
                <a v-if="row.item_url" :href="row.item_url" target="_blank" rel="noopener" class="title">{{ row.title || row.item_id }}</a>
                <div class="muted small">{{ row.store_name }} · {{ (row.marketplace_id || '').replace('EBAY_', '') }} · <span class="mono">{{ row.item_id }}</span></div>
                <div class="muted small">一口价 {{ money(row.bin_price, row.currency) }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="买家" width="150">
          <template #default="{ row }">
            <b class="mono">{{ row.buyer || '—' }}</b>
            <div class="muted small">信用 {{ row.buyer_feedback ?? '—' }}</div>
            <el-button v-if="row.conversation_id" link size="small" type="primary" @click="emit('open', row.conversation_id)">会话</el-button>
          </template>
        </el-table-column>
        <el-table-column label="报价" width="150">
          <template #default="{ row }">
            <b class="price">{{ money(row.price, row.currency) }}</b> × {{ row.quantity || 1 }}
            <div v-if="row.percent" class="small" :class="row.percent < 70 ? 'bad' : row.percent < 85 ? 'warn' : 'ok'">一口价的 {{ row.percent }}%</div>
            <div class="muted small">{{ row.type_label }}</div>
          </template>
        </el-table-column>
        <el-table-column label="买家留言" min-width="180"><template #default="{ row }"><span class="small">{{ row.buyer_message || '—' }}</span></template></el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="row.pending ? 'warning' : row.status === 'Accepted' ? 'success' : 'info'" effect="plain">{{ row.pending ? '待处理' : row.status_label }}</el-tag>
            <div v-if="row.pending" class="small" :class="(row.hours_left ?? 99) < 6 ? 'bad' : 'muted'">{{ left(row.hours_left) }}</div>
            <div v-if="row.responded_action" class="muted small">
              {{ row.responded_by }} {{ fmt(row.responded_at) }}<span v-if="row.counter_price"> · 还价 {{ money(row.counter_price, row.currency) }}</span>
            </div>
            <div v-if="row.response_error" class="bad small">{{ row.response_error }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170">
          <template #default="{ row }">
            <template v-if="row.pending && row.can_respond">
              <el-button size="small" type="success" plain @click="openRespond(row, 'Accept')">接受</el-button>
              <el-button size="small" plain @click="openRespond(row, 'Counter')">还价</el-button>
              <el-button size="small" type="danger" plain @click="openRespond(row, 'Decline')">拒绝</el-button>
            </template>
            <span v-else-if="row.pending" class="muted small">{{ row.respond_blocked }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" :title="`${ACTION_LABEL[action]}报价`" width="460px">
      <div v-if="current" class="dlg">
        <div class="small">{{ current.title || current.item_id }}</div>
        <div class="small muted">买家 {{ current.buyer }} 报价 {{ money(current.price, current.currency) }} × {{ current.quantity || 1 }} · 一口价 {{ money(current.bin_price, current.currency) }}</div>
        <el-form label-width="80px" class="mt">
          <template v-if="action === 'Counter'">
            <el-form-item label="还价价格"><el-input-number v-model="counterPrice" :min="0.01" :precision="2" :step="1" style="width: 180px" /> <span class="muted small">{{ current.currency }}</span></el-form-item>
            <el-form-item label="数量"><el-input-number v-model="counterQty" :min="1" style="width: 120px" /></el-form-item>
          </template>
          <el-form-item label="给买家留言"><el-input v-model="message" type="textarea" :rows="2" maxlength="250" show-word-limit placeholder="可空，买家能看到" /></el-form-item>
        </el-form>
        <div class="warn small">{{ summary }} 这会真实发到 eBay，不能撤回。</div>
      </div>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button :type="action === 'Decline' ? 'danger' : action === 'Accept' ? 'success' : 'primary'" :loading="sending" @click="submit">确定{{ ACTION_LABEL[action] }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bo {
  height: 100%;
}
.spacer {
  flex: 1;
}
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 14px 0;
  border-bottom: 1px solid var(--line);
}
.tabs button {
  border: 0;
  background: none;
  padding: 6px 12px 8px;
  font-size: var(--fs-sm, 13px);
  font-family: inherit;
  cursor: pointer;
  color: var(--muted);
  border-bottom: 2px solid transparent;
}
.tabs button.on {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}
.n {
  margin-left: 4px;
  font-size: 12px;
}
.tip {
  margin-left: auto;
}
.item {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.item img {
  width: 52px;
  height: 52px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--line);
  flex-shrink: 0;
}
.title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.price {
  font-size: 14px;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.ok {
  color: #237804;
}
.warn {
  color: #d4700a;
}
.bad {
  color: #a8071a;
}
.mt {
  margin-top: 10px;
}
.dlg {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
</style>
