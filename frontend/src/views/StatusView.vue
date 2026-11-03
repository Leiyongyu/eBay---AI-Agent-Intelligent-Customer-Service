<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, session } from '../api'
import { STATE_LABELS, errorText } from '../format'

const emit = defineEmits<{ refreshed: [] }>()
const status = ref<any>(null)
const metrics = ref<any>(null)
const checking = ref(false)
const checkResult = ref<string>('')
const embChecking = ref(false)
const embResult = ref<string>('')

async function embeddingCheck() {
  embChecking.value = true
  embResult.value = ''
  try {
    const r = await post('/api/system/embedding-check')
    embResult.value = `调用成功：${r.model}，实际维度 ${r.dimension}`
    ElMessage.success('向量模型连通，维度一致')
  } catch (e) {
    embResult.value = errorText(e)
    ElMessage.error(embResult.value)
  } finally {
    embChecking.value = false
  }
}

async function load() {
  ;[status.value, metrics.value] = await Promise.all([get('/api/system/status'), get('/internal/metrics/workbench')])
  emit('refreshed')
}

async function llmCheck() {
  checking.value = true
  checkResult.value = ''
  try {
    const r = await post('/api/system/llm-check')
    checkResult.value = `调用成功：${r.content}（tokens ${r.usage?.total_tokens ?? '—'}）`
    ElMessage.success('模型连通')
  } catch (e) {
    checkResult.value = errorText(e)
    ElMessage.error(checkResult.value)
  } finally {
    checking.value = false
  }
}

const ebay = ref<any>(null)
const syncing = ref(false)
const syncResult = ref<any>(null)

const logistics = ref<any>(null)
const syncingTickets = ref(false)
async function syncTickets() {
  syncingTickets.value = true
  try {
    const r = await post('/api/system/logistics/sync-tickets')
    ElMessage.success(r.error ? `同步出错：${r.error}` : `已同步 ${r.tickets} 张查件单，新回复起草 ${r.suggested} 条`)
    loadLogistics()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    syncingTickets.value = false
  }
}
async function loadLogistics() {
  try {
    logistics.value = await get('/api/system/logistics')
  } catch {
    logistics.value = null
  }
}

async function loadEbay() {
  ebay.value = await get('/internal/ebay/status')
}

async function ebaySync() {
  syncing.value = true
  syncResult.value = null
  try {
    syncResult.value = await post('/internal/ebay/sync')
    if (syncResult.value.ok) ElMessage.success('eBay 消息同步完成')
    else ElMessage.warning('同步有失败项，见下方原因')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    syncing.value = false
    loadEbay()
  }
}

// 聊天记录保留期清理
const retention = ref<any>(null)
const cleaning = ref(false)
const cleanResult = ref<any>(null)
const RETENTION_LABELS: Record<string, string> = {
  messages: '消息',
  attachments: '附件',
  files: '图片文件',
  jobs: '回复任务',
  events: '会话事件',
  conversations: '整个会话',
}

async function loadRetention() {
  retention.value = await get('/internal/maintenance/retention')
}

async function runRetention(dryRun: boolean) {
  if (!session.reviewer) {
    ElMessage.warning('请先在右上角填写审核员名称')
    return
  }
  if (!dryRun) {
    try {
      await ElMessageBox.confirm(
        `将删除各店铺 ${retention.value.days} 天前的聊天记录（消息、附件图片、已结束的回复任务和草稿），删除后不能恢复。建议先点“试运行”看数量。`,
        '确认立即清理',
        { type: 'warning', confirmButtonText: '确认删除' },
      )
    } catch {
      return
    }
  }
  cleaning.value = true
  cleanResult.value = null
  try {
    cleanResult.value = await post('/internal/maintenance/retention/run', { dry_run: dryRun })
    ElMessage.success(dryRun ? '试运行完成（没有删除任何数据）' : '清理完成')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    cleaning.value = false
    loadRetention()
  }
}

const fmtTotals = (t: Record<string, number> | undefined) =>
  t ? Object.entries(RETENTION_LABELS).map(([k, label]) => `${label} ${t[k] ?? 0}`).join(' · ') : '—'

const TYPE_LABELS: Record<string, string> = { FROM_MEMBERS: '买家会话', FROM_EBAY: 'eBay 系统消息' }

// eBay 实时推送：买家发消息后 eBay 推到隧道地址，收到后立刻拉取该店铺
const push = ref<any>(null)
const pushing = ref(false)
const pushResult = ref<any>(null)
const fmtTime = (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—')

async function loadPush() {
  push.value = await get('/internal/ebay/push')
}

async function setupPush(sendTest: boolean) {
  if (!session.reviewer) return ElMessage.warning('请先在顶栏填写审核员名称')
  pushing.value = true
  pushResult.value = null
  try {
    pushResult.value = await post('/internal/ebay/push/setup', { send_test: sendTest })
    if (pushResult.value.ok) ElMessage.success(`已订阅 ${pushResult.value.subscribed} 个店铺`)
    else ElMessage.warning(pushResult.value.error || '部分店铺订阅失败，见下方')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    pushing.value = false
    loadPush()
  }
}

const pct = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)

onMounted(() => {
  loadLogistics()
  load()
  loadEbay()
  loadRetention()
  loadPush()
})
</script>

<template>
  <div v-if="status" class="grid">
    <div class="panel">
      <div class="panel-head">
        Agent 就绪状态
        <el-tag :type="status.agent_ready ? 'success' : 'danger'">{{ status.agent_status }}</el-tag>
        <el-tag :type="status.rag_status === 'READY' ? 'success' : 'warning'">知识 {{ status.rag_status }}</el-tag>
        <el-tag :type="status.worker_alive ? 'success' : 'danger'" effect="plain">Worker {{ status.worker_alive ? '运行中' : '未运行' }}</el-tag>
      </div>
      <div class="panel-body">
        <el-alert v-for="r in status.agent_not_ready_reasons" :key="r" :title="r" type="error" :closable="false" class="gap" />
        <el-alert v-for="r in status.rag_not_ready_reasons" :key="r" :title="r" type="warning" :closable="false" class="gap" />
        <p v-if="!status.agent_ready" class="muted">
          在项目根目录的 <span class="mono">.env</span> 填写 LLM_PROVIDER=openai_compatible、LLM_BASE_URL、LLM_CHAT_MODEL、LLM_API_KEY 后重启
          main.py。未配置期间仍可收消息、查看预取事实、人工撰写并发送。
        </p>
        <div class="row">
          <el-button type="primary" :loading="checking" :disabled="!status.agent_ready" @click="llmCheck">测试大模型</el-button>
          <el-button :loading="embChecking" :disabled="!status.embedding_configured" @click="embeddingCheck">测试向量模型</el-button>
          <el-button @click="load">刷新</el-button>
        </div>
        <p v-if="checkResult" class="mono">{{ checkResult }}</p>
        <p v-if="embResult" class="mono">{{ embResult }}</p>
        <el-alert v-for="r in status.embedding_missing" :key="r" :title="`向量模型：${r}`" type="info" :closable="false" class="gap" />
        <div class="section-title">基础设施</div>
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="数据库">
            <el-tag size="small" :type="status.database.dialect === 'postgresql' ? 'success' : 'warning'">{{ status.database.dialect }}</el-tag>
            {{ status.database.display }}<br /><span class="muted">{{ status.database.server }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Redis">
            <template v-if="!status.redis.configured">未配置（进程内唤醒）</template>
            <template v-else-if="status.redis.ok"><el-tag size="small" type="success">已连接</el-tag> 版本 {{ status.redis.server_version }}</template>
            <template v-else><el-tag size="small" type="danger">连不上</el-tag> {{ status.redis.error }}（已退回进程内唤醒）</template>
          </el-descriptions-item>
          <el-descriptions-item v-if="logistics" label="谷仓 OMS">
            <el-tag size="small" :type="logistics.goodcang_ready ? 'success' : 'info'">{{ logistics.goodcang_ready ? '已配置' : '未配置' }}</el-tag>
            <span class="muted"> 按 eBay 订单号查出库单和物流号（只读）</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="logistics" label="17TRACK">
            <template v-if="logistics.track17_reasons.length"><el-tag size="small" type="info">未开启</el-tag> <span class="muted">{{ logistics.track17_reasons.join('；') }}</span></template>
            <template v-else-if="logistics.error"><el-tag size="small" type="danger">出错</el-tag> {{ logistics.error }}</template>
            <template v-else-if="logistics.quota">
              <el-tag size="small" :type="logistics.quota.quota_remain > 20 ? 'success' : 'warning'">剩余 {{ logistics.quota.quota_remain }} 单</el-tag>
              <span class="muted"> 共 {{ logistics.quota.quota_total }}，已用 {{ logistics.quota.quota_used }}，今天 {{ logistics.quota.today_used }}（每个新物流号扣 1 单）</span>
            </template>
          </el-descriptions-item>
          <el-descriptions-item v-if="logistics?.goodcang_ready" label="谷仓查件单">
            每天 {{ logistics.tickets.sync_time }} 同步<template v-if="!logistics.tickets.running">（后台任务未运行，重启 main.py）</template>
            <template v-if="logistics.tickets.last">
              <span class="muted"> · 上次 {{ logistics.tickets.last_at?.slice(0, 16).replace('T', ' ') }}：{{ logistics.tickets.last.error || `${logistics.tickets.last.tickets} 张，新回复起草 ${logistics.tickets.last.suggested} 条，需人工 ${logistics.tickets.last.needs_human} 条` }}</span>
            </template>
            <el-button link size="small" type="primary" :loading="syncingTickets" @click="syncTickets">立即同步</el-button>
          </el-descriptions-item>
        </el-descriptions>
        <div class="section-title">版本</div>
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item v-for="(v, k) in status.versions" :key="k" :label="String(k)">{{ v }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">配置（脱敏，只显示是否已填写）</div>
      <div class="panel-body">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="LLM">
            {{ status.config.llm.provider || '未填' }} / {{ status.config.llm.model || '未填' }} · Base URL
            {{ status.config.llm.base_url_configured ? '已填' : '未填' }} · Key {{ status.config.llm.api_key_configured ? '已填' : '未填' }} ·
            结构化输出 {{ status.config.llm.structured_output }}
          </el-descriptions-item>
          <el-descriptions-item label="Embedding">
            {{ status.config.embedding.provider || '未填' }} / {{ status.config.embedding.model || '未填' }} · 维度
            {{ status.config.embedding.dimension || '未填' }} · Key {{ status.config.embedding.api_key_configured ? '已填' : '未填' }}（全量注入模式暂不使用）
          </el-descriptions-item>
          <el-descriptions-item label="视觉模型">
            {{ status.config.vision.provider || '未填' }} / {{ status.config.vision.model || '未填' }} · Key
            {{ status.config.vision.api_key_configured ? '已填' : '未填' }}（P4 图片理解，尚未接入流程）
          </el-descriptions-item>
          <el-descriptions-item label="数据库">{{ status.config.database }}</el-descriptions-item>
          <el-descriptions-item label="业务数据源">{{ status.config.business_source }}</el-descriptions-item>
          <el-descriptions-item label="eBay 渠道">{{ status.config.ebay_channel }}</el-descriptions-item>
          <el-descriptions-item label="Agent 模式">{{ status.config.agent_mode }}</el-descriptions-item>
          <el-descriptions-item label="知识模式">
            {{ status.config.knowledge_mode }} · 预算 {{ status.config.knowledge_full_context_max_tokens }} token · 生效日 {{ status.config.knowledge_date }}
          </el-descriptions-item>
          <el-descriptions-item label="输入预算">{{ status.config.input_token_budget }} token</el-descriptions-item>
          <el-descriptions-item label="合并窗口">{{ status.config.merge_window_seconds }} 秒</el-descriptions-item>
          <el-descriptions-item label="自动发送">{{ status.config.auto_send_enabled ? '开启' : '关闭（P2 全部人工审核）' }}</el-descriptions-item>
          <el-descriptions-item label="知识条款数">{{ status.knowledge_chunks }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </div>
    <div class="panel" v-if="ebay">
      <div class="panel-head">
        eBay 消息对接（只读）
        <el-tag :type="ebay.ready ? 'success' : 'warning'">{{ ebay.ready ? '凭证齐全' : '未就绪' }}</el-tag>
        <el-tag v-if="ebay.ready" :type="ebay.scheduler_running ? 'success' : 'info'" effect="plain">
          {{ ebay.scheduler_running ? `定时拉取中（每 ${ebay.sync_interval_seconds} 秒）` : '定时拉取未运行' }}
        </el-tag>
      </div>
      <div class="panel-body">
        <el-alert v-for="r in ebay.not_ready_reasons" :key="r" :title="r" type="warning" :closable="false" class="gap" />
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="环境 / 对接店铺">{{ ebay.env }} · {{ ebay.stores?.length ?? 0 }} 个店铺</el-descriptions-item>
          <el-descriptions-item label="App ID / Cert ID">
            {{ ebay.client_id_configured ? '已填' : '未填' }} / {{ ebay.client_secret_configured ? '已填' : '未填' }}
          </el-descriptions-item>
          <el-descriptions-item label="店铺秘钥来源">
            {{ ebay.stores?.some((s: any) => s.source === 'credentials') ? '店铺凭证表（加密存储，每店各用各的秘钥）' : `.env：${ebay.refresh_token}` }}
          </el-descriptions-item>
          <el-descriptions-item label="已同步">{{ ebay.conversations }} 个会话 · {{ ebay.messages }} 条消息</el-descriptions-item>
          <el-descriptions-item label="交给 AI 处理">{{ ebay.create_reply_jobs ? '是' : '否（只入库展示）' }}</el-descriptions-item>
        </el-descriptions>
        <el-table v-if="ebay.stores?.length" :data="ebay.stores" size="small" border max-height="420" style="margin-top: 10px">
          <el-table-column prop="name" label="店铺" min-width="150" />
          <el-table-column label="站点" width="70">
            <template #default="{ row }">{{ row.marketplace_id.replace('EBAY_', '') }}</template>
          </el-table-column>
          <el-table-column prop="conversations" label="会话" width="60" />
          <el-table-column v-for="ct in ['FROM_MEMBERS', 'FROM_EBAY']" :key="ct" :label="`${TYPE_LABELS[ct]}上次成功`" min-width="150">
            <template #default="{ row }">
              <template v-for="st in row.states.filter((x: any) => x.conversation_type === ct)" :key="ct">
                {{ st.last_success_at ? new Date(st.last_success_at).toLocaleString('zh-CN', { hour12: false }) : '—' }}
                <div v-if="st.last_error" class="err">{{ st.last_error }}</div>
              </template>
              <span v-if="!row.states.some((x: any) => x.conversation_type === ct)" class="muted">未同步</span>
            </template>
          </el-table-column>
        </el-table>
        <div class="row" style="margin-top: 10px">
          <el-button type="primary" :loading="syncing" :disabled="!ebay.ready" @click="ebaySync">立即同步</el-button>
          <el-button @click="loadEbay">刷新</el-button>
        </div>
        <template v-if="syncResult">
          <div v-if="syncResult.error" class="err">{{ syncResult.error }}</div>
          <div v-if="syncResult.stores" class="mono">
            共 {{ Object.keys(syncResult.stores).length }} 个店铺，新消息
            {{ Object.values(syncResult.stores).reduce((n: number, s: any) => n + Object.values(s.types).reduce((m: number, r: any) => m + (r.messages_new || 0), 0), 0) }} 条，
            API 调用 {{ syncResult.api_calls }} 次
          </div>
          <div v-for="(s, sid) in syncResult.stores" :key="sid">
            <div v-for="(r, t) in s.ok ? {} : s.types" :key="t" class="err">
              {{ s.name }} · {{ TYPE_LABELS[String(t)] || t }}：{{ r.ok ? '成功' : r.error }}
            </div>
          </div>
        </template>
      </div>
    </div>
    <div class="panel" v-if="push">
      <div class="panel-head">
        eBay 实时推送
        <el-tag v-if="push.reasons.length" type="info" effect="plain">未配置</el-tag>
        <el-tag v-else :type="push.subscriptions.enabled ? 'success' : 'warning'" effect="plain">
          {{ push.subscriptions.enabled ? `已订阅 ${push.subscriptions.enabled} 个店铺` : '还没有订阅' }}
        </el-tag>
      </div>
      <div class="panel-body">
        <div v-for="r in push.reasons" :key="r" class="err">{{ r }}</div>
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="接收地址"><span class="mono">{{ push.endpoint || '—' }}</span></el-descriptions-item>
          <el-descriptions-item label="Cloudflare 隧道">
            <template v-if="push.tunnel.running">
              <el-tag size="small" :type="push.tunnel.connections ? 'success' : 'warning'">
                {{ push.tunnel.connections ? `已连接（${push.tunnel.connections} 条连接）` : '已启动，正在连接' }}
              </el-tag>
              <span class="muted"> 跟随本程序启停 · PID {{ push.tunnel.pid }}</span>
            </template>
            <template v-else>
              <el-tag size="small" type="info">未运行</el-tag>
              <span class="muted"> {{ push.tunnel.reasons.join('；') || '进程已退出，见 data/logs/cloudflared.log' }}</span>
            </template>
            <div v-if="push.tunnel.last_lines.length" class="mono muted tunnel-log">{{ push.tunnel.last_lines.slice(-2).join(' ｜ ') }}</div>
          </el-descriptions-item>
          <el-descriptions-item label="接收处理">{{ push.worker_running ? '运行中：收到推送后立刻拉取对应店铺' : '未运行' }}</el-descriptions-item>
          <el-descriptions-item label="订阅">
            共 {{ push.subscriptions.total }} 个店铺，启用 {{ push.subscriptions.enabled }}，失败 {{ push.subscriptions.error }}
          </el-descriptions-item>
          <el-descriptions-item label="最近收到">
            {{ fmtTime(push.last_received_at) }}<template v-if="push.last_status">（{{ push.last_status }}）</template> · 24 小时内 {{ push.received_24h }} 条
          </el-descriptions-item>
        </el-descriptions>
        <p class="muted">
          推送只用来触发：收到后立刻拉取该店铺，正文、图片、已读、AI 自动回复都和定时拉取一样处理；定时拉取保留作兜底（电脑关机、隧道断开期间漏掉的推送）。
          需要 main.py 和 Cloudflare 隧道都在运行。
        </p>
        <div v-for="x in push.stores.filter((x: any) => x.error)" :key="x.store_id" class="err">{{ x.store_id }}：{{ x.error }}</div>
        <div class="row" style="margin-top: 10px">
          <el-button type="primary" :loading="pushing" :disabled="push.reasons.length > 0" @click="setupPush(false)">启用 / 更新推送订阅</el-button>
          <el-button :loading="pushing" :disabled="push.reasons.length > 0 || !push.subscriptions.enabled" @click="setupPush(true)">让 eBay 发测试推送</el-button>
          <el-button @click="loadPush">刷新</el-button>
        </div>
        <div v-if="pushResult" class="mono" style="margin-top: 6px">
          {{ pushResult.ok ? '完成' : '有失败' }}：目的地 {{ pushResult.destination || '—' }}，订阅 {{ pushResult.subscribed ?? 0 }} 个店铺
          <div v-if="pushResult.error" class="err">{{ pushResult.error }}</div>
        </div>
      </div>
    </div>
    <div class="panel" v-if="retention">
      <div class="panel-head">
        聊天记录清理
        <el-tag :type="retention.scheduler_running ? 'success' : 'info'" effect="plain">
          {{ retention.scheduler_running ? `每天 ${retention.cleanup_time} 自动运行` : '定时清理未运行' }}
        </el-tag>
        <el-tag v-if="retention.dry_run" type="warning">试运行：只统计不删除</el-tag>
      </div>
      <div class="panel-body">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="保留期">
            {{ retention.days > 0 ? `每个店铺保留最近 ${retention.days} 天（按消息在 eBay 的发送时间）` : '不清理' }}
            <span v-if="retention.stores.some((x: any) => x.retention_days !== null)" class="muted">
              ；单独设置：{{ retention.stores.filter((x: any) => x.retention_days !== null).map((x: any) => `${x.name} ${x.retention_days || '不清理'}${x.retention_days ? ' 天' : ''}`).join('、') }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="下次自动运行">{{ retention.next_run }}（当时没开机的话，下次启动后补跑）</el-descriptions-item>
          <el-descriptions-item label="审计日志">同步流水保留 {{ retention.days }} 天，其他操作记录保留 {{ retention.audit_days }} 天</el-descriptions-item>
        </el-descriptions>
        <p class="muted">
          删除早于保留期的消息、附件图片、已结束的回复任务与草稿；消息删光的会话一并删除。还没处理完的会话（待审核、发送中、正被认领）先跳过。
          拉取 eBay 消息时也不再入库保留期以前的消息。
          <template v-if="retention.dry_run">确认试运行数字没问题后，把 .env 里的 <span class="mono">RETENTION_DRY_RUN</span> 改为 false 并重启，定时清理才会真正删除。</template>
        </p>
        <div class="section-title">最近运行</div>
        <el-empty v-if="!retention.runs.length" :image-size="40" description="还没有运行过" />
        <div v-for="(r, i) in retention.runs" :key="i" class="run">
          <el-tag size="small" :type="r.dry_run ? 'info' : 'success'">{{ r.dry_run ? '试运行' : '已删除' }}</el-tag>
          {{ new Date(r.at).toLocaleString('zh-CN', { hour12: false }) }} · {{ r.actor === 'retention_scheduler' ? '定时' : r.actor }}
          <div class="mono">{{ fmtTotals(r.totals) }}</div>
          <div v-if="r.ok === false" class="err">有店铺清理失败：{{ Object.values(r.stores || {}).filter((x: any) => x.error).map((x: any) => x.error).join('；') }}</div>
        </div>
        <div class="row" style="margin-top: 10px">
          <el-button :loading="cleaning" @click="runRetention(true)">试运行（只统计）</el-button>
          <el-button type="danger" plain :loading="cleaning" :disabled="retention.days <= 0" @click="runRetention(false)">立即清理</el-button>
          <el-button @click="loadRetention">刷新</el-button>
        </div>
        <div v-if="cleanResult" class="mono" style="margin-top: 6px">
          {{ cleanResult.dry_run ? '将删除' : '已删除' }}：{{ fmtTotals(cleanResult.totals) }}
        </div>
      </div>
    </div>
    <div class="panel" v-if="metrics">
      <div class="panel-head">工作台效率（第 2.2 节口径）</div>
      <div class="panel-body">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="已审核草稿">{{ metrics.reviewed_drafts }}</el-descriptions-item>
          <el-descriptions-item label="草稿采纳率">{{ pct(metrics.adoption_rate) }}</el-descriptions-item>
          <el-descriptions-item label="平均修改幅度">{{ pct(metrics.avg_edit_ratio) }}</el-descriptions-item>
          <el-descriptions-item label="审核时长中位数">{{ metrics.median_handling_ms ? `${(metrics.median_handling_ms / 1000).toFixed(1)} 秒` : '—' }}</el-descriptions-item>
          <el-descriptions-item label="人工撰写">{{ metrics.manual_replies }}</el-descriptions-item>
        </el-descriptions>
        <div class="section-title">任务状态分布</div>
        <div class="row">
          <el-tag v-for="(n, s) in metrics.jobs_by_state" :key="s" effect="plain">{{ STATE_LABELS[String(s)] || s }}：{{ n }}</el-tag>
        </div>
        <div class="section-title">修改/拒绝原因</div>
        <div class="row">
          <span v-if="!Object.keys(metrics.reason_distribution).length" class="muted">暂无</span>
          <el-tag v-for="(n, r) in metrics.reason_distribution" :key="r" type="warning" effect="plain">{{ r }}：{{ n }}</el-tag>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 12px;
  align-items: start;
}
.gap {
  margin-bottom: 8px;
}
.err {
  color: #c45656;
  font-size: 12px;
}
.run {
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.provider {
  padding: 6px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.provider .el-tag {
  margin-left: 8px;
}
.provider .reason {
  padding-left: 24px;
}
.tunnel-log {
  white-space: pre-wrap;
  font-size: 11px;
  word-break: break-all;
}
</style>
