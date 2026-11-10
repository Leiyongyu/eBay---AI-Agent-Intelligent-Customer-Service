<script setup lang="ts">
// AI 草稿的生成依据：按 Agent 实际执行的步骤展示“读了什么、查了什么、依据什么写出这句话”。
// 步骤与证据来自运行记录（agent_steps、tool_calls、草稿的 claims）；顶部“回复思路”是模型自述，单独标注仅供参考。
import { computed } from 'vue'
import { fmtTime, intentLabel, isGateInfo, reasonLabel, toolTag } from '../format'
import type { ChatMessage, DraftTrace } from '../types'

const props = defineProps<{ trace: DraftTrace; messages: ChatMessage[]; live?: boolean; reasons?: string[] }>()
const emit = defineEmits<{ jump: [ebayMessageId: string] }>()

// ---- 意图判断（把握度、依据消息、沿用的问题）与会话状态卡 ----
const CONFIDENCE: Record<string, { label: string; type: 'success' | 'warning' | 'danger' }> = {
  high: { label: '把握高', type: 'success' }, medium: { label: '把握中', type: 'warning' }, low: { label: '把握低', type: 'danger' },
}
const card = computed(() => props.trace.state_card || null)
const basisMsgs = computed(() => (draft.value?.intent_basis || [])
  .map((id) => props.messages.find((m) => m.ebay_message_id === id) || { ebay_message_id: id, missing: true } as any))
const inherited = computed(() => card.value?.issues.find((i) => i.id === draft.value?.inherits_issue) || null)
const factLine = computed(() => {
  const f = card.value?.facts || {}
  return [f.order_ids?.length ? `订单 ${f.order_ids.join('、')}` : '', f.vin ? `VIN ${f.vin}` : '', f.vehicle ? `车型 ${f.vehicle}` : '',
    f.skus?.length ? `SKU ${f.skus.join('、')}` : '', f.media || ''].filter(Boolean)
})

const TOOL_LABELS: Record<string, string> = {
  get_listing_context: '刊登信息',
  get_item_details: '当前商品详情',
  get_authorized_order_context: '买家订单',
  get_package_status: '物流包裹',
  get_case_status: '退货 / 纠纷 / 取消',
  get_product_by_sku: '商品（按 SKU）',
  get_inventory: '库存',
}
const STEP_TITLES: Record<string, string> = {
  load_snapshot: '读取要回复的买家消息',
  load_memory: '带入聊天上下文',
  observe_attachments: '查看买家发来的图片',
  prefetch: '安全检查：买家订单与售后（程序自动查，只读）',
  load_knowledge: '加载公司知识条款',
  compose_reply: '模型查询并撰写回复',
  repair_output: '修正模型输出格式',
  validate_output: '核对依据、合规与风险',
  persist_candidate: '保存草稿',
}
// 程序固定做的安全检查（订单与售后）/ 模型撰写时自己调用的工具（request_id 以 model- 开头）
const prefetchCalls = computed(() => props.trace.tool_calls.filter((t) => !t.request_id?.startsWith('model-')))
const modelCalls = computed(() => props.trace.tool_calls.filter((t) => t.request_id?.startsWith('model-')))
const ACTION_LABELS: Record<string, string> = { answer: '直接回答', clarify: '向买家追问', handoff: '建议转人工' }
const STATUS_LABELS: Record<string, string> = {
  OK: '完成',
  NONE: '无',
  SKIPPED: '跳过',
  ERROR: '出错',
  PARSE_ERROR: '格式不合格',
  NEEDS_REPAIR: '需修正',
  STALE_DISCARDED: '已作废',
  REVIEW_REQUIRED: '待审核',
  AUTO_ELIGIBLE: '自动候选',
}

const draft = computed(() => props.trace.agent_draft)
const sourceMsgs = computed(() => props.messages.filter((m) => props.trace.sources.includes(m.ebay_message_id)))
const problems = computed(() => (props.reasons || []).filter((r) => !isGateInfo(r)))
const gateInfo = computed(() => (props.reasons || []).filter((r) => isGateInfo(r)))
const totalMs = computed(() => props.trace.steps.reduce((s, x) => s + x.duration_ms, 0))
const tokens = computed(() => draft.value?.token_usage?.total_tokens ?? props.trace.steps.find((s) => s.usage)?.usage?.total_tokens)

function d(step: { detail: unknown }): Record<string, any> {
  return (step.detail as Record<string, any>) || {}
}
function secs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} 毫秒`
}
function stepType(status: string) {
  if (['ERROR', 'PARSE_ERROR'].includes(status)) return 'danger'
  if (['SKIPPED', 'NEEDS_REPAIR', 'STALE_DISCARDED'].includes(status)) return 'warning'
  return 'success'
}
function snippet(text: string, n = 160) {
  return text.length > n ? `${text.slice(0, n)}…` : text
}
// 查包裹工具：单个包裹是对象，多个是数组
function pkgList(data: unknown): any[] {
  return Array.isArray(data) ? data : [data]
}
function trackTag(status?: string) {
  if (status === 'Delivered') return 'success'
  if (['Exception', 'DeliveryFailure', 'Expired'].includes(status || '')) return 'danger'
  return 'primary'
}
function args(a: Record<string, unknown> | null) {
  const entries = Object.entries(a || {}).filter(([k]) => k !== 'store_id')
  return entries.length ? entries.map(([k, v]) => `${k}=${v}`).join('，') : '无参数'
}

// 证据 ID → 它指向的原始内容
type Evidence = { kind: string; title: string; body: string; json?: unknown }
function evidence(id: string): Evidence {
  if (id.startsWith('kb:')) {
    const c = props.trace.cited_knowledge.find((x) => x.evidence_id === id)
    return c ? { kind: '知识条款', title: c.title, body: c.text } : { kind: '知识条款', title: id, body: '条款原文已不存在（可能已被替换）' }
  }
  if (id.startsWith('img:')) {
    const att = props.messages.flatMap((m) => m.attachments).find((a) => `img:${a.id}` === id)
    return { kind: '买家图片', title: att?.media_name || id, body: att?.observation?.observation?.description || '识图结果见聊天记录中的图片', json: att?.observation?.observation }
  }
  const t = props.trace.tool_calls.find((x) => x.evidence_ref === id)
  return t
    ? { kind: '业务数据', title: TOOL_LABELS[t.tool_name] || t.tool_name, body: t.message || '', json: t.data }
    : { kind: '证据', title: id, body: '本次运行中找不到这条证据（校验时会标为问题）' }
}
function evidenceTag(id: string) {
  return id.startsWith('kb:') ? 'warning' : id.startsWith('img:') ? 'danger' : 'primary'
}
</script>

<template>
  <div class="trace">
    <div class="summary">
      <template v-if="draft">
        <span>意图 <b>{{ intentLabel(draft.intent) }}</b></span>
        <span>· {{ ACTION_LABELS[draft.action || ''] || draft.action }}</span>
        <span>· 语言 {{ draft.language }}</span>
        <el-tag v-if="draft.risk_level" size="small" effect="plain">风险 {{ draft.risk_level }}</el-tag>
      </template>
      <span class="muted">
        共 {{ trace.steps.length }} 步 · {{ secs(totalMs) }}<template v-if="tokens"> · {{ tokens }} token</template>
        <template v-if="trace.run"> · {{ fmtTime(trace.run.started_at) }}</template>
      </span>
    </div>

    <div v-if="draft && (draft.intent_confidence || draft.intent_basis?.length || draft.inherits_issue)" class="intent-box">
      <div class="ib-row">
        <span class="ib-k">意图判断</span>
        <el-tag v-if="draft.intent_confidence" size="small" :type="CONFIDENCE[draft.intent_confidence]?.type" effect="plain">
          {{ CONFIDENCE[draft.intent_confidence]?.label || draft.intent_confidence }}
        </el-tag>
        <span v-if="draft.intents?.length > 1" class="muted">同时识别：{{ draft.intents.map(intentLabel).join('、') }}</span>
      </div>
      <div class="ib-row">
        <span class="ib-k">沿用问题</span>
        <span v-if="inherited">{{ inherited.summary_zh }}<span class="muted">（{{ inherited.source === 'system' ? '系统' : '对话' }}{{ inherited.order_id ? ` · ${inherited.order_id}` : '' }}）</span></span>
        <span v-else-if="draft.inherits_issue" class="muted">{{ draft.inherits_issue }}（已不在状态卡里）</span>
        <span v-else class="muted">新话题</span>
      </div>
      <div v-if="basisMsgs.length" class="ib-row">
        <span class="ib-k">依据消息</span>
        <span class="basis">
          <button v-for="m in basisMsgs" :key="m.ebay_message_id" class="bm" :disabled="m.missing"
                  :title="m.missing ? '这条消息不在当前会话里' : '点击定位到这条消息'" @click="emit('jump', m.ebay_message_id)">
            <template v-if="!m.missing">#{{ m.seq }} {{ snippet(m.body, 40) || '（无文字）' }}</template>
            <template v-else>{{ m.ebay_message_id }}</template>
          </button>
        </span>
      </div>
    </div>

    <div v-if="card && (card.topic || card.issues.length || factLine.length)" class="state-card">
      <div class="sc-title">会话状态卡<span v-if="card.topic">：{{ card.topic }}</span>
        <span class="muted">{{ trace.state_updated_at ? fmtTime(trace.state_updated_at) : '' }}</span></div>
      <div v-if="card.issues.length" class="sc-issues">
        <div v-for="i in card.issues" :key="i.id" class="sc-issue" :class="{ on: i.id === draft?.inherits_issue }">
          <el-tag size="small" :type="i.source === 'system' ? 'primary' : 'info'" effect="plain">{{ i.source === 'system' ? '系统' : '对话' }}</el-tag>
          <span class="sc-sum">{{ i.summary_zh || intentLabel(i.intent) }}</span>
          <span class="muted">{{ [i.order_id, i.status, i.since].filter(Boolean).join(' · ') }}</span>
        </div>
      </div>
      <div v-else class="muted">没有未解决的问题</div>
      <div v-if="factLine.length" class="sc-line"><span class="ib-k">关键事实</span>{{ factLine.join('；') }}</div>
      <div v-if="card.promises?.length" class="sc-line"><span class="ib-k">答应过</span>{{ card.promises.join('；') }}</div>
      <div v-if="card.resolved?.length" class="sc-line muted"><span class="ib-k">已解决</span>{{ card.resolved.map((r) => r.summary_zh).join('；') }}</div>
    </div>

    <div v-if="draft?.reasoning_zh" class="reasoning">
      <div class="reasoning-title">回复思路 <span class="muted">（模型自述，仅供参考；以下方步骤和证据为准）</span></div>
      <div class="reasoning-body">{{ draft.reasoning_zh }}</div>
    </div>

    <el-timeline class="tl">
      <el-timeline-item v-for="(s, i) in trace.steps" :key="i" :type="stepType(s.status)" size="large" hide-timestamp>
        <div class="step-head">
          <b>{{ i + 1 }}. {{ STEP_TITLES[s.node] || s.node }}</b>
          <el-tag size="small" :type="stepType(s.status)" effect="plain">{{ STATUS_LABELS[s.status] || s.status }}</el-tag>
          <span class="muted">{{ secs(s.duration_ms) }}</span>
        </div>

        <!-- 1 读取买家消息 -->
        <div v-if="s.node === 'load_snapshot'" class="step-body">
          <div v-for="m in sourceMsgs" :key="m.id" class="quote">
            <span class="muted">#{{ m.seq }} {{ fmtTime(m.sent_at) }}</span> {{ snippet(m.body) || '（无文字）' }}
            <span v-if="m.attachments.length" class="muted">（{{ m.attachments.length }} 个附件）</span>
          </div>
          <div v-if="!sourceMsgs.length" class="muted">{{ d(s).sources ?? 0 }} 条（消息已不在当前会话中）</div>
        </div>

        <div v-else-if="s.node === 'load_memory'" class="step-body muted">
          连同本次消息共 {{ d(s).total_messages }} 条聊天记录交给模型，约 {{ d(s).tokens }} token{{ d(s).truncated ? '；更早的消息因超出预算未带入' : '' }}
        </div>

        <div v-else-if="s.node === 'observe_attachments'" class="step-body muted">
          <template v-if="s.status === 'NONE'">本次消息没有图片</template>
          <template v-else>
            {{ d(s).attachments }} 个附件，识别 {{ d(s).observed }} 个
            <div v-for="(r, j) in d(s).not_read || []" :key="j">· 未识别：{{ r }}</div>
          </template>
        </div>

        <div v-else-if="s.node === 'prefetch'" class="step-body">
          <div v-if="!prefetchCalls.length" class="muted">没有需要查询的数据</div>
          <el-collapse v-else class="tools">
            <el-collapse-item v-for="t in prefetchCalls" :key="t.id" :name="t.id">
              <template #title>
                <div class="tool-title">
                  <el-tag :type="toolTag(t.status)" size="small">{{ t.status }}</el-tag>
                  <span>{{ TOOL_LABELS[t.tool_name] || t.tool_name }}</span>
                  <span class="muted ellipsis">{{ args(t.arguments) }}</span>
                </div>
              </template>
              <div class="muted">
                工具 <span class="mono">{{ t.tool_name }}</span> · 数据源 {{ t.source || '—' }} · {{ t.duration_ms }} 毫秒
                <template v-if="t.evidence_ref"> · 证据 ID <span class="mono">{{ t.evidence_ref }}</span></template>
              </div>
              <div v-if="t.message" class="note">{{ t.message }}</div>
              <div v-if="t.tool_name === 'get_package_status' && t.data" class="pkgs">
                <div v-for="(p, i) in pkgList(t.data)" :key="i" class="pkg">
                  <div>
                    <b>{{ p.carrier || p.tracking?.carrier || '承运商未知' }}</b>
                    <span class="mono"> {{ p.tracking_number || '无物流号' }}</span>
                    <el-tag v-if="p.tracking_status" size="small" :type="trackTag(p.tracking?.status)" effect="plain">{{ p.tracking_status }}</el-tag>
                    <span v-if="p.tracking?.sub_status_label" class="muted"> {{ p.tracking.sub_status_label }}</span>
                    <span v-if="p.source" class="muted"> · 物流号来自{{ p.source === 'goodcang' ? '谷仓' : 'eBay' }}</span>
                  </div>
                  <div v-if="p.warehouse" class="muted">
                    谷仓出库单 {{ p.warehouse.order_code }} · {{ p.warehouse.status_label }} · {{ p.warehouse.warehouse_code }}<template v-if="p.warehouse.date_shipping"> · 出库 {{ p.warehouse.date_shipping }}</template>
                  </div>
                  <div v-if="p.assessment" class="muted">
                    物流状况：<b :class="{ danger: p.assessment.needs_human }">{{ p.assessment.stage_label }}</b>
                    <template v-if="p.assessment.days_without_update != null"> · {{ p.assessment.days_without_update }} 天未更新</template>
                    <template v-if="p.assessment.needs_human"> · 已转人工</template>
                  </div>
                  <div v-for="tk in p.tickets || []" :key="tk.io_code" class="muted">谷仓查件单 {{ tk.io_code }} · {{ tk.type }} · {{ tk.status }}</div>
                  <div v-for="(e, j) in p.tracking?.events || []" :key="j" class="ev-line">
                    <span class="muted">{{ e.time }}</span> {{ e.location }} {{ e.description }}
                  </div>
                  <div v-if="p.tracking_note" class="note">{{ p.tracking_note }}</div>
                </div>
              </div>
              <details v-if="t.data"><summary class="muted">原始数据</summary><pre class="json">{{ JSON.stringify(t.data, null, 2) }}</pre></details>
            </el-collapse-item>
          </el-collapse>
        </div>

        <div v-else-if="s.node === 'load_knowledge'" class="step-body muted">
          <template v-if="s.status === 'ERROR'">
            <div v-for="(r, j) in d(s).reasons || []" :key="j" class="err">{{ r }}</div>
          </template>
          <template v-else-if="d(s).mode === 'retrieval'">
            按问题检索：候选 {{ d(s).candidates }} 条，选用 {{ d(s).clauses }} 条（约 {{ d(s).tokens }} token）
          </template>
          <template v-else>店铺适用条款全部带入：{{ d(s).clauses }} 条（约 {{ d(s).tokens }} token）</template>
          <div>其中回复实际引用 {{ trace.cited_knowledge.length }} 条，见下方“每句话的依据”</div>
        </div>

        <div v-else-if="s.node === 'compose_reply' || s.node === 'repair_output'" class="step-body muted">
          <template v-if="s.status === 'SKIPPED'">没有调用模型：{{ problems.map(reasonLabel).join('、') || '前置条件不满足' }}</template>
          <template v-else>
            <span v-if="d(s).model">模型 {{ d(s).model }} · </span>
            <span v-if="d(s).prompt_tokens_est">输入约 {{ d(s).prompt_tokens_est }} token · </span>
            <span v-if="s.usage?.total_tokens">实际用量 {{ s.usage.total_tokens }} token</span>
            <div v-if="d(s).error" class="err">{{ d(s).error }}</div>
          </template>
          <template v-if="s.node === 'compose_reply' && modelCalls.length">
            <div>模型自己调用了 {{ modelCalls.length }} 个工具（{{ d(s).tool_rounds || 1 }} 轮），查到后作答：</div>
            <el-collapse class="tools">
            <el-collapse-item v-for="t in modelCalls" :key="t.id" :name="t.id">
              <template #title>
                <div class="tool-title">
                  <el-tag :type="toolTag(t.status)" size="small">{{ t.status }}</el-tag>
                  <span>{{ TOOL_LABELS[t.tool_name] || t.tool_name }}</span>
                  <span class="muted ellipsis">{{ args(t.arguments) }}</span>
                </div>
              </template>
              <div class="muted">
                工具 <span class="mono">{{ t.tool_name }}</span> · 数据源 {{ t.source || '—' }} · {{ t.duration_ms }} 毫秒
                <template v-if="t.evidence_ref"> · 证据 ID <span class="mono">{{ t.evidence_ref }}</span></template>
              </div>
              <div v-if="t.message" class="note">{{ t.message }}</div>
              <div v-if="t.tool_name === 'get_package_status' && t.data" class="pkgs">
                <div v-for="(p, i) in pkgList(t.data)" :key="i" class="pkg">
                  <div>
                    <b>{{ p.carrier || p.tracking?.carrier || '承运商未知' }}</b>
                    <span class="mono"> {{ p.tracking_number || '无物流号' }}</span>
                    <el-tag v-if="p.tracking_status" size="small" :type="trackTag(p.tracking?.status)" effect="plain">{{ p.tracking_status }}</el-tag>
                    <span v-if="p.tracking?.sub_status_label" class="muted"> {{ p.tracking.sub_status_label }}</span>
                    <span v-if="p.source" class="muted"> · 物流号来自{{ p.source === 'goodcang' ? '谷仓' : 'eBay' }}</span>
                  </div>
                  <div v-if="p.warehouse" class="muted">
                    谷仓出库单 {{ p.warehouse.order_code }} · {{ p.warehouse.status_label }} · {{ p.warehouse.warehouse_code }}<template v-if="p.warehouse.date_shipping"> · 出库 {{ p.warehouse.date_shipping }}</template>
                  </div>
                  <div v-if="p.assessment" class="muted">
                    物流状况：<b :class="{ danger: p.assessment.needs_human }">{{ p.assessment.stage_label }}</b>
                    <template v-if="p.assessment.days_without_update != null"> · {{ p.assessment.days_without_update }} 天未更新</template>
                    <template v-if="p.assessment.needs_human"> · 已转人工</template>
                  </div>
                  <div v-for="tk in p.tickets || []" :key="tk.io_code" class="muted">谷仓查件单 {{ tk.io_code }} · {{ tk.type }} · {{ tk.status }}</div>
                  <div v-for="(e, j) in p.tracking?.events || []" :key="j" class="ev-line">
                    <span class="muted">{{ e.time }}</span> {{ e.location }} {{ e.description }}
                  </div>
                  <div v-if="p.tracking_note" class="note">{{ p.tracking_note }}</div>
                </div>
              </div>
              <details v-if="t.data"><summary class="muted">原始数据</summary><pre class="json">{{ JSON.stringify(t.data, null, 2) }}</pre></details>
            </el-collapse-item>
          </el-collapse>
          </template>
        </div>

        <div v-else-if="s.node === 'validate_output'" class="step-body">
          <div class="muted">
            闸门结论 {{ d(s).decision }} · 风险 {{ d(s).risk }}
            <template v-if="draft?.compliance"> · 合规 {{ draft.compliance.result }}</template>
          </div>
          <div v-if="problems.length" class="row reasons">
            <el-tag v-for="r in problems" :key="r" type="danger" effect="plain" size="small">{{ reasonLabel(r) }}</el-tag>
          </div>
          <div v-if="gateInfo.length" class="row reasons">
            <el-tag v-for="r in gateInfo" :key="r" type="info" size="small">{{ reasonLabel(r) }}</el-tag>
          </div>
          <div v-for="h in draft?.compliance?.hits || []" :key="h.start" class="muted">
            · {{ h.severity }} {{ h.rule }}：“{{ h.text }}” {{ h.note }}
          </div>
        </div>

        <div v-else-if="s.node === 'persist_candidate'" class="step-body muted">
          {{ draft ? `草稿 v${draft.version}（第 ${draft.run_generation} 次生成）已填入下方输入框` : '没有生成草稿，请人工撰写' }}
        </div>
      </el-timeline-item>

      <el-timeline-item v-if="live" type="primary" size="large" hide-timestamp>
        <div class="step-head"><b>{{ trace.steps.length + 1 }}. 进行中…</b><el-icon class="is-loading"><i class="spin" /></el-icon></div>
      </el-timeline-item>
    </el-timeline>

    <template v-if="draft && !live">
      <div class="section-title">每句话的依据</div>
      <div v-if="!draft.claims.length" class="muted">模型没有给出需要证据支撑的陈述（如纯寒暄、追问）</div>
      <div v-for="(c, i) in draft.claims" :key="i" class="claim">
        <div>{{ c.text }}</div>
        <div class="row">
          <el-popover v-for="e in c.evidence_ids" :key="e" placement="top" :width="420" trigger="click">
            <template #reference>
              <el-tag :type="evidenceTag(e)" size="small" effect="plain" class="ev">{{ evidence(e).kind }} · {{ evidence(e).title }}</el-tag>
            </template>
            <div class="ev-pop">
              <div><b>{{ evidence(e).kind }}：{{ evidence(e).title }}</b></div>
              <div class="mono muted">{{ e }}</div>
              <div v-if="evidence(e).body" class="ev-body">{{ evidence(e).body }}</div>
              <pre v-if="evidence(e).json" class="json">{{ JSON.stringify(evidence(e).json, null, 2) }}</pre>
            </div>
          </el-popover>
          <span v-if="!c.evidence_ids.length" class="muted">（未给出证据）</span>
        </div>
      </div>
      <div v-if="draft.missing_facts.length" class="note">缺少的事实：{{ draft.missing_facts.join('；') }}</div>
      <div v-if="draft.conflicts.length" class="note err">相互冲突的证据：{{ draft.conflicts.join('、') }}</div>
    </template>
    <div v-if="trace.run?.error" class="note err">{{ trace.run.error }}</div>
  </div>
</template>

<style scoped>
.trace {
  font-size: var(--fs-sm);
}
.intent-box,
.state-card {
  border: 1px solid var(--line-soft);
  border-radius: 4px;
  background: var(--sunken);
  padding: 8px 10px;
  margin-bottom: 10px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.ib-row,
.sc-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}
.ib-k {
  flex: none;
  color: var(--muted);
  min-width: 4.5em;
}
.basis {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}
.bm {
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--panel);
  color: var(--text-2);
  font: inherit;
  padding: 1px 6px;
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bm:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}
.sc-title {
  font-weight: 600;
  color: var(--text);
}
.sc-title .muted {
  font-weight: 400;
  margin-left: 6px;
}
.sc-issues {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.sc-issue {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 2px 4px;
  border-radius: 3px;
}
.sc-issue.on {
  background: var(--accent-soft);
}
.sc-sum {
  color: var(--text);
}
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-bottom: 10px;
}
.reasoning {
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  border-radius: 4px;
  padding: 6px 10px;
  margin-bottom: 12px;
}
.reasoning-title {
  font-weight: 600;
  margin-bottom: 2px;
}
.reasoning-body {
  white-space: pre-wrap;
  line-height: 1.6;
}
.tl {
  padding-left: 2px;
}
.tl :deep(.el-timeline-item) {
  padding-bottom: 12px;
}
.step-head {
  display: flex;
  gap: 8px;
  align-items: center;
}
.step-body {
  margin-top: 4px;
  line-height: 1.6;
}
.quote {
  border-left: 3px solid var(--warn);
  padding: 2px 8px;
  margin-bottom: 4px;
  background: var(--warn-soft);
  white-space: pre-wrap;
}
.tools {
  border-top: none;
}
.tools :deep(.el-collapse-item__header) {
  height: 32px;
  line-height: 32px;
}
.tool-title {
  display: flex;
  gap: 6px;
  align-items: center;
  min-width: 0;
  overflow: hidden;
}
.ellipsis {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.reasons {
  margin-top: 4px;
}
.claim {
  border-left: 3px solid var(--aqua);
  padding: 2px 8px;
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ev {
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ev-pop {
  font-size: var(--fs-sm);
}
.ev-body {
  margin-top: 6px;
  white-space: pre-wrap;
}
.note {
  font-size: var(--fs-xs);
  background: var(--sunken);
  border-radius: 4px;
  padding: 4px 8px;
  margin-top: 6px;
}
.note.err,
.err {
  color: var(--danger-text);
}
.note.err {
  background: var(--danger-soft);
}
.spin {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--accent);
  border-right-color: transparent;
  border-radius: 50%;
}
.pkg {
  border-left: 3px solid var(--aqua);
  padding: 2px 8px;
  margin: 6px 0;
  line-height: 1.6;
}
.ev-line {
  font-size: var(--fs-xs);
}
.danger {
  color: var(--danger-text);
}
</style>
