<script setup lang="ts">
// 智能客服 → 自动发信：规则触发的主动通知（物流断更、取件点超时、退货标签未寄出、咨询 / 纠纷快到期）。
// 只生成模板草稿进工作台“未处理”，客服核对后再发（合规检查、5 分钟可撤回照旧），系统不自动发。
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, isAdmin, post } from '../api'
import { errorText } from '../format'

const emit = defineEmits<{ open: [convId: number] }>()
interface Rule { key: string; label: string; unit: string; enabled: boolean; threshold: number }
const cfg = ref<{ scan_minutes: number; cooldown_days: number; rules: Rule[] } | null>(null)
const notices = ref<any[]>([])
const preview = ref<Record<string, any> | null>(null)
const busy = ref(false)
const STATE: Record<string, string> = { REVIEW_REQUIRED: '待审核', SENDING: '发送中', SENT: '已发出', CANCELLED_STALE: '已作废',
  SEND_UNCERTAIN: '结果不确定', AUTO_ELIGIBLE: '待审核', FAILED_HANDOFF: '待审核' }
const RULE_HELP: Record<string, string> = {
  tracking_stalled: '轨迹多少天没有新动态（已签收、在取件点、丢失损坏、异常的不算）',
  pickup_overdue: '包裹在取件点放了多少天还没取',
  return_not_shipped: '退货标签提供后多少天买家还没寄出',
  case_due_soon: '咨询（未收到货）/ 纠纷离到期还剩多少小时、处理状态还是“待处理”',
}

async function load() {
  try {
    ;[cfg.value, notices.value] = await Promise.all([get<any>('/internal/proactive/rules'), get<any[]>('/internal/proactive/notices')])
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function save() {
  if (!cfg.value) return
  busy.value = true
  try {
    await api('PUT', '/internal/proactive/rules', { rules: Object.fromEntries(cfg.value.rules.map((r) => [r.key, { enabled: r.enabled, threshold: r.threshold }])) })
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}
async function scan(dryRun: boolean) {
  if (!dryRun) {
    try {
      await ElMessageBox.confirm('按开着的规则现在生成草稿？草稿进工作台“未处理”，要人工审核后才会发出。', '立即生成', { type: 'info' })
    } catch {
      return
    }
  }
  busy.value = true
  try {
    preview.value = await post<Record<string, any>>(`/internal/proactive/scan?dry_run=${dryRun}`, {})
    if (!dryRun) {
      ElMessage.success('已生成')
      await load()
    }
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}
async function dismiss(n: any) {
  try {
    await post(`/internal/proactive/notices/${n.job_id}/dismiss`, {})
    n.state = 'CANCELLED_STALE'
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
const labelOf = (k: string) => cfg.value?.rules.find((r) => r.key === k)?.label || k
onMounted(load)
</script>

<template>
  <div class="pv">
    <div class="panel">
      <div class="panel-head">主动发信规则
        <span class="muted">每 {{ cfg?.scan_minutes || 0 }} 分钟扫一次{{ cfg && cfg.scan_minutes <= 0 ? '（PROACTIVE_SCAN_MINUTES=0，不自动扫）' : '' }}；
          同一会话同一规则 {{ cfg?.cooldown_days }} 天最多触发一次；只生成草稿进“未处理”，人工审核后才发</span>
      </div>
      <div class="panel-body">
        <el-table v-if="cfg" :data="cfg.rules" size="small" border>
          <el-table-column label="规则" width="200"><template #default="{ row }"><b>{{ row.label }}</b></template></el-table-column>
          <el-table-column label="说明"><template #default="{ row }"><span class="muted">{{ RULE_HELP[row.key] }}</span></template></el-table-column>
          <el-table-column label="阈值" width="200">
            <template #default="{ row }">
              <el-input-number v-model="row.threshold" :min="1" :max="row.unit === '小时' ? 720 : 60" size="small" :disabled="!isAdmin()" />
              {{ row.unit }}
            </template>
          </el-table-column>
          <el-table-column label="开启" width="80"><template #default="{ row }"><el-switch v-model="row.enabled" :disabled="!isAdmin()" /></template></el-table-column>
        </el-table>
        <div v-if="isAdmin()" class="actions">
          <el-button type="primary" :loading="busy" @click="save">保存</el-button>
          <el-button :loading="busy" @click="scan(true)">预览（不生成）</el-button>
          <el-button :loading="busy" @click="scan(false)">立即生成草稿</el-button>
        </div>
        <div v-if="preview" class="preview">
          <div v-for="(v, k) in preview" :key="k" class="pv-rule">
            <b>{{ labelOf(String(k)) }}</b>：候选 {{ v.candidates }} 条，生成 {{ v.drafted }} 条
            <span v-for="(n, why) in v.skipped" :key="why" class="muted">　跳过 {{ n }}（{{ why }}）</span>
          </div>
          <div v-if="!Object.keys(preview).length" class="muted">没有开着的规则</div>
        </div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">最近触发</div>
      <div class="panel-body">
        <el-table :data="notices" size="small" border empty-text="还没有触发过">
          <el-table-column label="时间" width="160"><template #default="{ row }">{{ row.created_at?.slice(0, 16).replace('T', ' ') }}</template></el-table-column>
          <el-table-column label="规则" width="190"><template #default="{ row }">{{ row.rule_label }}</template></el-table-column>
          <el-table-column label="关联" prop="ref" />
          <el-table-column label="状态" width="110"><template #default="{ row }">{{ STATE[row.state] || row.state }}</template></el-table-column>
          <el-table-column label="操作" width="160">
            <template #default="{ row }">
              <el-button link type="primary" @click="emit('open', row.conversation_id)">打开会话</el-button>
              <el-button v-if="['REVIEW_REQUIRED', 'AUTO_ELIGIBLE', 'FAILED_HANDOFF'].includes(row.state)" link @click="dismiss(row)">不发了</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pv { display: flex; flex-direction: column; gap: 12px; }
.actions { margin-top: 10px; display: flex; gap: 8px; }
.preview { margin-top: 10px; padding: 8px 12px; background: var(--el-fill-color-light); border-radius: 4px; }
.pv-rule { margin: 2px 0; }
.muted { color: var(--el-text-color-secondary); }
</style>
