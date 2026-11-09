<script setup lang="ts">
// 议价：查哪些商品有“感兴趣的买家”（关注 / 加购过），勾选后给这些买家发折扣报价（eBay Negotiation API，一个商品一次）。
// 发报价是真实对外动作：只有开启了 eBay 真实收发的店铺能发；每次发送都有记录
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'

interface StoreOpt { id: string; name: string; marketplace_id: string; live_reason: string | null }
interface OfferRec {
  id: number; store_id: string; store_name?: string; marketplace_id: string; listing_id: string
  discount_percentage: string | null; price: { value: string; currency: string } | null; quantity: number
  duration_days: number; message: string | null; status: string; offers_count: number | null
  response: { offers: { offer_id: string; status: string; buyer: string }[] } | null; error: string | null
  sent_by: string; sent_at: string
}
interface Item { listing_id: string; last_offer: OfferRec | null }
interface Listing {
  listing_id: string; title?: string | null; price?: { value: string; currency: string } | null; picture_url?: string | null
  sku?: string | null; url?: string | null; quantity_available?: number | null; error: string | null
}
const SITE: Record<string, string> = {
  EBAY_US: '美国', EBAY_GB: '英国', EBAY_DE: '德国', EBAY_FR: '法国', EBAY_IT: '意大利', EBAY_ES: '西班牙',
  EBAY_AU: '澳大利亚', EBAY_CA: '加拿大', EBAY_AT: '奥地利',
}

// 默认留言按站点语言（意思都是“这是您以超值价格获得此物品的机会！”），弹窗里可以改
const OFFER_MSG: Record<string, string> = {
  en: 'This is your chance to get this item at a great price!',
  de: 'Das ist Ihre Chance, diesen Artikel zu einem super Preis zu bekommen!',
  fr: "C'est votre chance d'obtenir cet article à un prix exceptionnel !",
  it: "Questa è la tua occasione per avere questo articolo a un prezzo vantaggioso!",
  es: '¡Esta es tu oportunidad de conseguir este artículo a un precio excelente!',
}
const SITE_LANG: Record<string, string> = { EBAY_DE: 'de', EBAY_AT: 'de', EBAY_FR: 'fr', EBAY_IT: 'it', EBAY_ES: 'es' }
const LANG_NAME: Record<string, string> = { en: '英语', de: '德语', fr: '法语', it: '意大利语', es: '西班牙语' }
const siteLang = (m: string) => SITE_LANG[m] || 'en'

const tab = ref<'items' | 'history'>('items')
const conf = ref<{ stores: StoreOpt[]; marketplaces: string[]; duration_days: number; min_discount: number; max_discount: number; max_message: number } | null>(null)
const storeId = ref('')
const market = ref('EBAY_DE')
const store = computed(() => conf.value?.stores.find((s) => s.id === storeId.value) || null)
const items = ref<Item[]>([])
const liveReason = ref<string | null>(null)
const loading = ref(false)
const loadError = ref('')
const listings = reactive<Record<string, Listing | null>>({})

async function loadConf() {
  try {
    conf.value = await get('/internal/offers/stores')
    if (!storeId.value && conf.value?.stores.length) {
      storeId.value = conf.value.stores[0].id
      market.value = conf.value.stores[0].marketplace_id || 'EBAY_DE'
    }
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function loadItems(refresh = false) {
  if (!storeId.value) return
  loading.value = true
  loadError.value = ''
  try {
    const r = await get<{ items: Item[]; live_reason: string | null }>(
      `/internal/offers/eligible?store_id=${encodeURIComponent(storeId.value)}&marketplace_id=${market.value}${refresh ? '&refresh=true' : ''}`)
    items.value = r.items
    liveReason.value = r.live_reason
    selected.value = []
    page.value = 1
  } catch (e) {
    items.value = []
    loadError.value = errorText(e)
  } finally {
    loading.value = false
  }
}
watch(storeId, (id) => {
  const s = conf.value?.stores.find((x) => x.id === id)
  if (s?.marketplace_id) market.value = s.marketplace_id
  loadItems()
})
watch(market, () => loadItems())

// 搜索（Item ID / SKU / 标题）+ 分页 + 商品信息按需加载
const q = ref('')
const page = ref(1)
const pageSize = ref(20)
function ensureListing(id: string) {
  if (id in listings) return
  listings[id] = null
  get<Listing>(`/internal/offers/listing?store_id=${encodeURIComponent(storeId.value)}&listing_id=${id}`)
    .then((l) => (listings[id] = l))
    .catch(() => null)
}
const filtered = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return items.value
  return items.value.filter((i) => i.listing_id.includes(t) || (listings[i.listing_id]?.sku || '').toLowerCase().includes(t)
    || (listings[i.listing_id]?.title || '').toLowerCase().includes(t))
})
const pageRows = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(pageRows, (rows) => rows.forEach((r) => ensureListing(r.listing_id)))
// 按 SKU / 标题搜要先有商品信息：搜的时候把这个站点的商品信息都加载上（最多 300 个）
watch(q, (t) => {
  page.value = 1
  if (t.trim() && !/^\d+$/.test(t.trim())) items.value.slice(0, 300).forEach((i) => ensureListing(i.listing_id))
})
const selected = ref<Item[]>([])
const tableRef = ref<any>(null)

// 输入完整 Item ID 回车：这个站点没有就去其他站点找，找到了自动切过去并勾上
const searching = ref(false)
async function search() {
  const t = q.value.trim()
  if (!/^\d{9,}$/.test(t) || !storeId.value) return
  if (!items.value.some((i) => i.listing_id === t)) {
    searching.value = true
    let found = ''
    try {
      for (const m of (conf.value?.marketplaces || []).filter((x) => x !== market.value)) {
        try {
          const r = await get<{ items: Item[] }>(`/internal/offers/eligible?store_id=${encodeURIComponent(storeId.value)}&marketplace_id=${m}`)
          if (r.items.some((i) => i.listing_id === t)) { found = m; break }
        } catch { /* 这个站点查不了就跳过 */ }
      }
    } finally {
      searching.value = false
    }
    if (!found) return ElMessage.warning(`所有站点都没有 ${t} 的感兴趣买家（没人关注 / 加购，或刊登已结束），不能发报价`)
    ElMessage.success(`${t} 在${SITE[found] || found}站点（${found}），已切换过去`)
    market.value = found
    await nextTick()
    while (loading.value) await new Promise((ok) => setTimeout(ok, 100))
  }
  await nextTick()
  const row = items.value.find((i) => i.listing_id === t)
  if (row) tableRef.value?.toggleRowSelection(row, true)
}

// ---- 发报价 ----
const dialog = ref(false)
const form = reactive({ mode: 'discount' as 'discount' | 'price', discount: 5, price: '', currency: 'EUR', message: '' })
const sending = ref(false)
const results = ref<OfferRec[] | null>(null)
const firstListing = computed(() => (selected.value[0] ? listings[selected.value[0].listing_id] : null))
function openSend() {
  if (!selected.value.length) return ElMessage.warning('请先勾选商品')
  if (liveReason.value) return ElMessage.warning(`不能发报价：${liveReason.value}`)
  if (form.mode === 'price' && selected.value.length > 1) form.mode = 'discount'
  const p = firstListing.value?.price
  if (p) form.currency = p.currency
  // 没改过的默认留言换成这个站点的语言
  if (!form.message.trim() || Object.values(OFFER_MSG).includes(form.message)) form.message = OFFER_MSG[siteLang(market.value)]
  results.value = null
  dialog.value = true
}
const resent = computed(() => selected.value.filter((i) => i.last_offer).length)
async function doSend() {
  const what = form.mode === 'discount' ? `${form.discount}% 折扣` : `${form.price} ${form.currency}`
  try {
    await ElMessageBox.confirm(
      `给 ${selected.value.length} 个商品的感兴趣买家发 ${what} 的报价（买 1 件就能享受，有效 ${conf.value?.duration_days || 4} 天）？会真实发到 eBay，发出后不能撤回。` +
        (resent.value ? `\n其中 ${resent.value} 个商品 7 天内已经发过报价。` : ''),
      '确认发送报价', { type: 'warning', confirmButtonText: '发送' })
  } catch {
    return
  }
  sending.value = true
  try {
    const body: Record<string, unknown> = {
      store_id: storeId.value, marketplace_id: market.value, listing_ids: selected.value.map((i) => i.listing_id),
      message: form.message,
    }
    if (form.mode === 'discount') body.discount_percentage = String(form.discount)
    else body.price = { value: form.price, currency: form.currency }
    const r = await post<{ results: OfferRec[]; sent: number; failed: number }>('/internal/offers/send', body)
    results.value = r.results
    if (r.failed) ElMessage.warning(`发出 ${r.sent} 个，失败 ${r.failed} 个，见下方原因`)
    else ElMessage.success(`已发出 ${r.sent} 个商品的报价`)
    loadItems()
    loadConf() // eBay 改了有效期规则时，后端会记住新的天数
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    sending.value = false
  }
}

// ---- 发送记录 ----
const history = ref<OfferRec[]>([])
const histLoading = ref(false)
async function loadHistory() {
  histLoading.value = true
  try {
    history.value = await get<OfferRec[]>(`/internal/offers/history?days=30${storeId.value ? `&store_id=${encodeURIComponent(storeId.value)}` : ''}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    histLoading.value = false
  }
}
watch(tab, (t) => t === 'history' && loadHistory())

const pad = (n: number) => String(n).padStart(2, '0')
function fmt(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const offerText = (r: OfferRec) => (r.discount_percentage ? `${r.discount_percentage}% 折扣` : r.price ? `${r.price.value} ${r.price.currency}` : '—')

onMounted(async () => {
  await loadConf()
  if (storeId.value) loadItems()
})
</script>

<template>
  <div class="of">
    <div class="bar">
      <el-select v-model="storeId" placeholder="选择 eBay 账号" filterable style="width: 220px">
        <el-option v-for="s in conf?.stores || []" :key="s.id" :label="s.name" :value="s.id">
          <span>{{ s.name }}</span><span v-if="s.live_reason" class="opt-muted">（只能查看）</span>
        </el-option>
      </el-select>
      <el-select v-model="market" style="width: 150px">
        <el-option v-for="m in conf?.marketplaces || []" :key="m" :label="`${SITE[m] || m} · ${m.replace('EBAY_', '')}`" :value="m" />
      </el-select>
      <el-button :loading="loading" @click="loadItems(true)">重新查询</el-button>
      <span class="muted">站点要和刊登站点一致，选错了会查不到</span>
      <span class="spacer" />
      <div class="tabs">
        <button :class="{ on: tab === 'items' }" @click="tab = 'items'">可议价商品<span v-if="items.length" class="n">({{ items.length }})</span></button>
        <button :class="{ on: tab === 'history' }" @click="tab = 'history'">发送记录</button>
      </div>
    </div>

    <el-alert v-if="!conf?.stores.length && conf" type="info" :closable="false" show-icon
              title="没有可议价的店铺：需要先在「账号管理」导入店铺的 eBay 秘钥" />
    <el-alert v-else-if="liveReason && tab === 'items'" type="warning" :closable="false" show-icon
              :title="`可以查看，但不能发报价：${liveReason}`" />

    <!-- ======== 可议价商品 ======== -->
    <template v-if="tab === 'items'">
      <div class="toolbar">
        <el-button type="primary" :disabled="!selected.length || !!liveReason" @click="openSend">
          发送报价{{ selected.length ? `（${selected.length}）` : '' }}
        </el-button>
        <el-input v-model="q" clearable placeholder="Item ID / SKU / 标题，输入 Item ID 回车可跨站点查找" style="width: 330px"
                  @keyup.enter="search">
          <template #append><el-button :loading="searching" @click="search">查找</el-button></template>
        </el-input>
        <span class="muted">这些商品有买家关注或加入购物车，报价会发给这些感兴趣的买家</span>
        <span class="spacer" />
        <span class="total">共{{ filtered.length }}条记录</span>
      </div>
      <el-alert v-if="loadError" type="error" :closable="false" show-icon :title="loadError" />
      <el-table ref="tableRef" v-loading="loading" :data="pageRows" row-key="listing_id"
                :empty-text="loading ? '查询中…' : q ? '这个站点没有找到，输入完整 Item ID 回车可去其他站点找' : '这个站点暂时没有可议价的商品'"
                @selection-change="(v: Item[]) => (selected = v)">
        <el-table-column type="selection" width="44" />
        <el-table-column label="图片" width="84">
          <template #default="{ row }">
            <img v-if="listings[row.listing_id]?.picture_url" class="thumb" :src="listings[row.listing_id]!.picture_url!" alt="" referrerpolicy="no-referrer" loading="lazy" />
            <span v-else class="thumb ph" aria-hidden="true" />
          </template>
        </el-table-column>
        <el-table-column label="Item ID / 标题" min-width="360">
          <template #default="{ row }">
            <a v-if="listings[row.listing_id]?.url" :href="listings[row.listing_id]!.url!" target="_blank" rel="noopener noreferrer" class="lid">{{ row.listing_id }} ↗</a>
            <span v-else class="lid">{{ row.listing_id }}</span>
            <div class="title">{{ listings[row.listing_id]?.title || (listings[row.listing_id] === null ? '加载中…' : '') }}</div>
          </template>
        </el-table-column>
        <el-table-column label="SKU" width="170"><template #default="{ row }">{{ listings[row.listing_id]?.sku || '—' }}</template></el-table-column>
        <el-table-column label="价格" width="120">
          <template #default="{ row }">{{ listings[row.listing_id]?.price ? `${listings[row.listing_id]!.price!.value} ${listings[row.listing_id]!.price!.currency}` : '—' }}</template>
        </el-table-column>
        <el-table-column label="可售" width="80" align="right">
          <template #default="{ row }">{{ listings[row.listing_id]?.quantity_available ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="最近 7 天报价" width="220">
          <template #default="{ row }">
            <template v-if="row.last_offer">
              <el-tag size="small" type="success">已发 {{ offerText(row.last_offer) }}</el-tag>
              <div class="muted">{{ row.last_offer.sent_by }} · {{ fmt(row.last_offer.sent_at) }}<template v-if="row.last_offer.offers_count != null"> · {{ row.last_offer.offers_count }} 位买家</template></div>
            </template>
            <span v-else class="muted">没发过</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="filtered.length"
                       layout="total, sizes, prev, pager, next" background small />
      </div>
    </template>

    <!-- ======== 发送记录 ======== -->
    <template v-else>
      <div class="toolbar"><span class="muted">最近 30 天{{ store ? `「${store.name}」` : '' }}的报价记录</span><span class="spacer" />
        <el-button size="small" :loading="histLoading" @click="loadHistory">刷新</el-button></div>
      <el-table v-loading="histLoading" :data="history" empty-text="还没有发过报价">
        <el-table-column label="时间" width="150"><template #default="{ row }">{{ fmt(row.sent_at) }}</template></el-table-column>
        <el-table-column label="店铺 / 站点" width="190"><template #default="{ row }">{{ row.store_name }}<div class="muted">{{ row.marketplace_id }}</div></template></el-table-column>
        <el-table-column prop="listing_id" label="Item ID" width="140" />
        <el-table-column label="报价" width="130"><template #default="{ row }">{{ offerText(row) }} × {{ row.quantity }}<div class="muted">有效 {{ row.duration_days }} 天</div></template></el-table-column>
        <el-table-column label="结果" min-width="260">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'SENT' ? 'success' : 'danger'">{{ row.status === 'SENT' ? '已发出' : '没发出' }}</el-tag>
            <span v-if="row.status === 'SENT'" class="ml">发给 {{ row.offers_count ?? 0 }} 位买家
              <span v-if="row.response?.offers.length" class="muted">（{{ row.response.offers.map((o: any) => o.buyer).filter(Boolean).join('、') }}）</span></span>
            <div v-if="row.error" class="err">{{ row.error }}</div>
          </template>
        </el-table-column>
        <el-table-column label="留言" min-width="200"><template #default="{ row }"><div class="msg">{{ row.message || '—' }}</div></template></el-table-column>
        <el-table-column prop="sent_by" label="操作人" width="100" />
      </el-table>
    </template>

    <!-- ======== 发报价 ======== -->
    <el-dialog v-model="dialog" title="发送报价" width="560px" append-to-body>
      <el-form label-width="90px">
        <el-form-item label="商品">
          <span>{{ selected.length }} 个商品 · {{ store?.name }} · {{ SITE[market] || market }}</span>
          <div v-if="resent" class="warn">其中 {{ resent }} 个 7 天内已经发过报价</div>
        </el-form-item>
        <el-form-item label="报价方式">
          <el-radio-group v-model="form.mode">
            <el-radio value="discount">按折扣</el-radio>
            <el-radio value="price" :disabled="selected.length > 1">指定价格<span v-if="selected.length > 1" class="muted">（只能一个商品）</span></el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.mode === 'discount'" label="折扣">
          <el-input-number v-model="form.discount" :min="conf?.min_discount || 5" :max="conf?.max_discount || 80" :step="5" />
          <span class="ml">% off</span>
          <span v-if="firstListing?.price && selected.length === 1" class="muted ml">
            ≈ {{ (Number(firstListing.price.value) * (100 - form.discount) / 100).toFixed(2) }} {{ firstListing.price.currency }}
          </span>
        </el-form-item>
        <el-form-item v-else label="报价金额">
          <el-input v-model="form.price" placeholder="如 89.90" style="width: 160px" />
          <el-input v-model="form.currency" style="width: 80px; margin-left: 8px" />
          <span v-if="firstListing?.price" class="muted ml">原价 {{ firstListing.price.value }} {{ firstListing.price.currency }}</span>
        </el-form-item>
        <el-form-item label="数量">
          <span>1 件</span>
          <span class="muted ml">买家买 1 件就能按报价成交<template v-if="selected.length === 1 && firstListing?.quantity_available != null">（可售 {{ firstListing.quantity_available }} 件）</template>；卖完的商品不会发</span>
        </el-form-item>
        <el-form-item label="有效期">
          <span>{{ conf?.duration_days || 4 }} 天</span>
          <span class="muted ml">eBay 规定的天数；不允许买家还价</span>
        </el-form-item>
        <el-form-item label="留言">
          <el-input v-model="form.message" type="textarea" :rows="4" :maxlength="conf?.max_message || 2000" show-word-limit
                    placeholder="给买家的留言（用买家的语言）" />
          <div class="muted">默认留言按站点用{{ LANG_NAME[siteLang(market)] }}</div>
        </el-form-item>
      </el-form>
      <div v-if="results" class="results">
        <div v-for="r in results" :key="r.id" class="res">
          <span class="mono">{{ r.listing_id }}</span>
          <el-tag size="small" :type="r.status === 'SENT' ? 'success' : 'danger'">{{ r.status === 'SENT' ? `已发给 ${r.offers_count ?? 0} 位买家` : '没发出' }}</el-tag>
          <span v-if="r.error" class="err">{{ r.error }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="dialog = false">{{ results ? '关闭' : '取消' }}</el-button>
        <el-button v-if="!results" type="primary" :loading="sending" @click="doSend">发送报价</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.of {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 14px 16px;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bar,
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.spacer {
  flex: 1;
}
.tabs {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}
.tabs button {
  height: 30px;
  padding: 0 14px;
  border: 0;
  background: var(--panel);
  font-family: inherit;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}
.tabs button + button {
  border-left: 1px solid var(--line);
}
.tabs button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.n {
  margin-left: 2px;
  font-size: 12px;
}
.total {
  font-size: 12px;
  color: var(--muted);
}
.opt-muted {
  margin-left: 4px;
  font-size: 12px;
  color: var(--muted);
}
.thumb {
  display: block;
  width: 56px;
  height: 56px;
  object-fit: contain;
  border-radius: 3px;
  border: 1px solid var(--line-soft);
  background: #fff;
}
.thumb.ph {
  background: var(--sunken);
}
.lid {
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
a.lid:hover {
  color: var(--accent);
}
.title {
  font-size: 12px;
  color: var(--text-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pager {
  display: flex;
  justify-content: flex-end;
}
.ml {
  margin-left: 8px;
}
.warn {
  width: 100%;
  font-size: 12px;
  color: var(--warn-text);
}
.err {
  font-size: 12px;
  color: var(--danger-text);
}
.msg {
  font-size: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.results {
  border-top: 1px solid var(--line-soft);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.res {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
</style>
