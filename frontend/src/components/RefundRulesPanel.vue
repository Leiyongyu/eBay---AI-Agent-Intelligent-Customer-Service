<script setup lang="ts">
// 系统配置 → 退款审核：按店铺 + 币种 + 金额范围定“直接退款 / 先审核”，从上到下第一条命中的生效，没命中的都先审核。
// 直接退款也只对管理员 / 售后提交的申请生效（客服提交的一律先审核）。纠纷升级后自动退款：不做，固定关闭
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get } from '../api'
import { errorText } from '../format'
import type { RefundRule } from '../refundRequest'

interface RulesResp { rules: RefundRule[]; stores: { id: string; name: string }[]; dispute_auto_refund: boolean }

const rules = ref<RefundRule[]>([])
const stores = ref<{ id: string; name: string }[]>([])
const loading = ref(false)
const saving = ref(false)
const dirty = ref(false)

async function load() {
  loading.value = true
  try {
    const d = await get<RulesResp>('/internal/refund-review-rules')
    rules.value = d.rules.map((r) => ({ ...r }))
    stores.value = d.stores
    dirty.value = false
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}
function add() {
  rules.value.push({ store_id: null, currency: 'EUR', min_amount: null, max_amount: 20, mode: 'direct', enabled: true, note: '' })
  dirty.value = true
}
function remove(i: number) {
  rules.value.splice(i, 1)
  dirty.value = true
}
function move(i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= rules.value.length) return
  const [x] = rules.value.splice(i, 1)
  rules.value.splice(j, 0, x)
  dirty.value = true
}
const storeName = (id: string | null) => (id ? stores.value.find((s) => s.id === id)?.name || id : '全部店铺')
const range = (r: RefundRule) => {
  const c = r.currency || ''
  if (r.min_amount == null && r.max_amount == null) return '任何金额'
  if (r.min_amount == null) return `≤ ${r.max_amount} ${c}`
  if (r.max_amount == null) return `≥ ${r.min_amount} ${c}`
  return `${r.min_amount} ~ ${r.max_amount} ${c}`
}
async function save() {
  const direct = rules.value.filter((r) => r.enabled && r.mode === 'direct')
  try {
    await ElMessageBox.confirm(
      direct.length
        ? `保存 ${rules.value.length} 条规则。其中 ${direct.length} 条是“直接退款”：管理员 / 售后提交的退款申请命中后会马上在 eBay 上退款，不再经过审核：\n`
          + direct.map((r) => `· ${storeName(r.store_id)} ${range(r)}`).join('\n')
        : `保存 ${rules.value.length} 条规则（都没有“直接退款”，所有退款都要先审核）。`,
      '保存退款审核规则', { type: direct.length ? 'warning' : 'info' })
  } catch {
    return
  }
  saving.value = true
  try {
    const d = await api<RulesResp>('PUT', '/internal/refund-review-rules', {
      rules: rules.value.map((r) => ({ ...r, currency: r.currency?.trim().toUpperCase() || null, store_id: r.store_id || null })),
    })
    rules.value = d.rules.map((r) => ({ ...r }))
    dirty.value = false
    ElMessage.success('已保存，立即生效')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="panel group rr">
    <div class="head">
      <h3>退款审核</h3>
      <span class="spacer" />
      <el-button size="small" @click="add">新增规则</el-button>
      <el-button size="small" type="primary" :disabled="!dirty" :loading="saving" @click="save">保存规则</el-button>
    </div>
    <p class="muted small">
      退款申请提交后，按下面的规则从上到下找第一条命中的（店铺、币种、金额都要对上）：“直接退款”= 提交后马上在 eBay 上退款，
      “先审核”= 管理员 / 售后审核通过后才退。<b>没有命中任何规则的都先审核</b>；客服提交的申请一律先审核（退款只能由管理员和售后执行）。
    </p>
    <el-table :data="rules" size="small" border empty-text="没有规则：所有退款都先审核">
      <el-table-column label="顺序" width="80">
        <template #default="{ $index }">
          <el-button link size="small" :disabled="$index === 0" @click="move($index, -1)">↑</el-button>
          <el-button link size="small" :disabled="$index === rules.length - 1" @click="move($index, 1)">↓</el-button>
        </template>
      </el-table-column>
      <el-table-column label="店铺" min-width="160">
        <template #default="{ row }">
          <el-select v-model="row.store_id" clearable filterable placeholder="全部店铺" size="small" @change="dirty = true">
            <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="币种" width="90">
        <template #default="{ row }">
          <el-input v-model="row.currency" size="small" maxlength="3" placeholder="任何" @input="dirty = true" />
        </template>
      </el-table-column>
      <el-table-column label="金额从（含）" width="130">
        <template #default="{ row }">
          <el-input-number v-model="row.min_amount" size="small" :min="0" :precision="2" controls-position="right" placeholder="不限"
                           @change="dirty = true" />
        </template>
      </el-table-column>
      <el-table-column label="到（含）" width="130">
        <template #default="{ row }">
          <el-input-number v-model="row.max_amount" size="small" :min="0" :precision="2" controls-position="right" placeholder="不限"
                           @change="dirty = true" />
        </template>
      </el-table-column>
      <el-table-column label="处理方式" width="120">
        <template #default="{ row }">
          <el-select v-model="row.mode" size="small" @change="dirty = true">
            <el-option label="直接退款" value="direct" /><el-option label="先审核" value="review" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }"><el-switch v-model="row.enabled" size="small" @change="dirty = true" /></template>
      </el-table-column>
      <el-table-column label="说明" min-width="140">
        <template #default="{ row }"><el-input v-model="row.note" size="small" maxlength="200" @input="dirty = true" /></template>
      </el-table-column>
      <el-table-column label="" width="60">
        <template #default="{ $index }"><el-button link size="small" type="danger" @click="remove($index)">删除</el-button></template>
      </el-table-column>
    </el-table>
    <div class="fixed">
      <span>纠纷升级后自动退款</span><el-switch :model-value="false" disabled size="small" />
      <span class="muted small">不做，固定关闭：纠纷升级后的退款都要人在系统里提交、审核</span>
    </div>
  </div>
</template>

<style scoped>
.rr {
  display: block;
  padding: 4px 16px 12px;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.head h3 {
  margin: 10px 0 6px;
  font-size: var(--fs-md, 15px);
}
.spacer {
  flex: 1;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.fixed {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 13px;
}
</style>
