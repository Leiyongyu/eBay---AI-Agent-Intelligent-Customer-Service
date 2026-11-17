<script setup lang="ts">
// 客户管理：客户列表（按买家汇总 eBay 订单、售后、会话）与黑名单（系统内名单：AI 不起草、界面标红）。
// eBay 没有开放“屏蔽买家”接口：要让买家不能再下单，需在各店铺 eBay 后台的“屏蔽买家”里手动加
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import { errorText } from '../format'
import type { Store } from '../types'

const props = defineProps<{ mode: 'list' | 'blacklist' }>()
const emit = defineEmits<{ open: [id: number] }>()

type BlEntry = { id: number; buyer: string; store_id: string | null; store_name?: string; reason: string | null; created_by: string | null; created_at: string | null }
type Customer = {
  buyer: string; country: string | null; orders: number; first_order_at: string | null; last_order_at: string | null
  amounts: Record<string, number>; stores: { id: string; name: string }[]; refunded_orders: number; cancelled_orders: number
  returns: number; disputes: number; negative_feedback: number; conversation_id: number | null; blacklist: BlEntry[]
}

const stores = ref<Store[]>([])
const storeId = ref('')
const keyword = ref('')
const onlyBlack = ref(false)
const sort = ref('recent')
const customers = ref<Customer[]>([])
const entries = ref<BlEntry[]>([])
const loading = ref(false)
const allStores = computed(() => session.user?.role === 'ADMIN' || !!(session.user as any)?.all_stores)

async function load() {
  loading.value = true
  try {
    if (props.mode === 'list') {
      const q = new URLSearchParams({ sort: sort.value })
      if (storeId.value) q.set('store_id', storeId.value)
      if (keyword.value.trim()) q.set('q', keyword.value.trim())
      if (onlyBlack.value) q.set('blacklisted', 'true')
      customers.value = (await get<{ items: Customer[] }>(`/internal/customers?${q}`)).items
    } else {
      const q = new URLSearchParams()
      if (keyword.value.trim()) q.set('q', keyword.value.trim())
      entries.value = await get<BlEntry[]>(`/internal/blacklist?${q}`)
    }
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
let timer: number | undefined
watch(keyword, () => {
  clearTimeout(timer)
  timer = window.setTimeout(load, 400)
})
watch([storeId, onlyBlack, sort, () => props.mode], load)

// 加入黑名单
const dialog = ref(false)
const form = ref({ buyer: '', store_id: '' as string, reason: '' })
function openAdd(c?: Customer) {
  form.value = { buyer: c?.buyer || '', store_id: c?.stores.length === 1 ? c.stores[0].id : allStores.value ? '' : (c?.stores[0]?.id || ''), reason: '' }
  dialog.value = true
}
async function add() {
  if (!form.value.buyer.trim()) return
  try {
    await post('/internal/blacklist', { buyer: form.value.buyer.trim(), store_id: form.value.store_id || null, reason: form.value.reason })
    dialog.value = false
    ElMessage.success('已加入黑名单。要让买家不能再下单，请到对应店铺的 eBay 后台“屏蔽买家”里也加上')
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function remove(e: BlEntry) {
  try {
    await ElMessageBox.confirm(`把 ${e.buyer} 移出黑名单（${e.store_name || (e.store_id ? '该店铺' : '全部店铺')}）？`, '移出黑名单', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api('DELETE', `/internal/blacklist/${e.id}`)
    ElMessage.success('已移出')
    load()
  } catch (err) {
    ElMessage.error(errorText(err))
  }
}

const fmt = (v: string | null) => (v ? new Date(v).toLocaleDateString('zh-CN') : '—')
const fmtTime = (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—')
const money = (m: Record<string, number>) => Object.entries(m).map(([c, v]) => `${v.toFixed(2)} ${c}`).join(' / ') || '—'

onMounted(async () => {
  stores.value = await get<Store[]>('/internal/stores').catch(() => [])
  load()
})
</script>

<template>
  <div class="panel cust">
    <div class="panel-head">
      {{ props.mode === 'list' ? '客户列表' : '黑名单' }}
      <span class="muted small">{{ props.mode === 'list' ? `${customers.length} 位买家（按订单汇总，近 180 天起）` : `${entries.length} 条` }}</span>
      <span class="spacer" />
      <template v-if="props.mode === 'list'">
        <el-select v-model="storeId" placeholder="全部店铺" clearable filterable size="small" style="width: 200px">
          <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select v-model="sort" size="small" style="width: 130px">
          <el-option label="最近下单" value="recent" /><el-option label="下单次数" value="orders" />
        </el-select>
        <el-checkbox v-model="onlyBlack" size="small">只看黑名单</el-checkbox>
      </template>
      <el-input v-model="keyword" size="small" clearable placeholder="搜索买家账号" style="width: 200px" />
      <el-button size="small" type="danger" plain @click="openAdd()">加入黑名单</el-button>
    </div>
    <div v-if="props.mode === 'blacklist'" class="tip muted small">
      这是本系统内的黑名单：名单里的买家来信 AI 不起草回复、不发“已收到”回执，直接交给人工，会话和售后记录上标红。
      eBay 没有开放“屏蔽买家”的接口，要让买家不能再下单或发消息，请到对应店铺的 eBay 后台 → 账户设置 → 屏蔽买家 里手动添加。
    </div>
    <div class="panel-body">
      <el-table v-if="props.mode === 'list'" :data="customers" v-loading="loading" size="small" empty-text="没有客户（订单会随售后管理定时拉取）">
        <el-table-column label="买家" min-width="170">
          <template #default="{ row }">
            <b class="mono">{{ row.buyer }}</b>
            <el-tag v-if="row.blacklist.length" size="small" type="danger" class="ml" :title="row.blacklist.map((e: BlEntry) => e.reason || '').join('；')">黑名单</el-tag>
            <div class="muted small">{{ row.country || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="店铺" min-width="160">
          <template #default="{ row }">{{ row.stores.map((s: any) => s.name).join('、') }}</template>
        </el-table-column>
        <el-table-column label="订单" width="70" prop="orders" />
        <el-table-column label="金额" min-width="130"><template #default="{ row }">{{ money(row.amounts) }}</template></el-table-column>
        <el-table-column label="最近下单" width="100"><template #default="{ row }">{{ fmt(row.last_order_at) }}</template></el-table-column>
        <el-table-column label="售后" min-width="170">
          <template #default="{ row }">
            <span v-if="row.returns" class="tag-w">退货 {{ row.returns }}</span>
            <span v-if="row.disputes" class="tag-r">纠纷 {{ row.disputes }}</span>
            <span v-if="row.negative_feedback" class="tag-r">差评 {{ row.negative_feedback }}</span>
            <span v-if="row.refunded_orders" class="tag-w">退款 {{ row.refunded_orders }}</span>
            <span v-if="row.cancelled_orders" class="muted">取消 {{ row.cancelled_orders }}</span>
            <span v-if="!row.returns && !row.disputes && !row.negative_feedback && !row.refunded_orders && !row.cancelled_orders" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button v-if="row.conversation_id" link size="small" type="primary" @click="emit('open', row.conversation_id)">会话</el-button>
            <el-button v-if="!row.blacklist.length" link size="small" type="danger" @click="openAdd(row)">加黑名单</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-table v-else :data="entries" v-loading="loading" size="small" empty-text="黑名单是空的">
        <el-table-column label="买家" min-width="160"><template #default="{ row }"><b class="mono">{{ row.buyer }}</b></template></el-table-column>
        <el-table-column label="范围" width="160" prop="store_name" />
        <el-table-column label="原因" min-width="220"><template #default="{ row }">{{ row.reason || '—' }}</template></el-table-column>
        <el-table-column label="加入人" width="110" prop="created_by" />
        <el-table-column label="加入时间" width="150"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }"><el-button link size="small" @click="remove(row)">移出</el-button></template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" title="加入黑名单" width="460px">
      <el-form label-width="80px">
        <el-form-item label="买家账号"><el-input v-model="form.buyer" placeholder="eBay 用户名" /></el-form-item>
        <el-form-item label="范围">
          <el-select v-model="form.store_id" filterable style="width: 100%" :placeholder="allStores ? '全部店铺' : '选择店铺'">
            <el-option v-if="allStores" label="全部店铺" value="" />
            <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="原因"><el-input v-model="form.reason" type="textarea" :rows="2" placeholder="如：恶意退货、辱骂、敲诈差评" /></el-form-item>
      </el-form>
      <div class="muted small">加入后这个买家来信 AI 不起草回复，直接交人工。要让他不能再下单，请同时到 eBay 后台“屏蔽买家”里添加。</div>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="danger" :disabled="!form.buyer.trim() || (!allStores && !form.store_id)" @click="add">加入黑名单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cust {
  height: 100%;
}
.spacer {
  flex: 1;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.ml {
  margin-left: 6px;
}
.tip {
  padding: 8px 14px;
  border-bottom: 1px solid var(--line);
}
.tag-w,
.tag-r {
  margin-right: 8px;
}
.tag-w {
  color: #d4700a;
}
.tag-r {
  color: var(--danger, #d03050);
  font-weight: 600;
}
</style>
