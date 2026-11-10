export interface Store {
  id: string
  name: string
  default_marketplace_id: string
  timezone: string
  auto_send_enabled: boolean
  ebay_user_id: string | null // 有值即真实 eBay 店铺（名称取自 eBay 原始店铺名）；演示店铺为空
  unread_conversations: number // 有未读消息的买家会话数
  unread_system: number // 有未读的 eBay 系统消息会话数
}

export interface JobInfo {
  id: number
  state: string
  risk_level: string | null
  run_generation: number
  decision_reasons: string[]
  ready_at: string | null
  attempt: number
  last_error: string | null
  created_at: string | null
  updated_at: string | null
  rule_version: string | null
  model_version: string | null
}

export interface Claim {
  claimed_by: string
  expires_at: string
}

/** AI 起草时给出的会话情况：阶段 + 一句中文概括 */
export interface SituationInfo {
  stage: 'presale' | 'shipping' | 'aftersales' | 'invoice' | 'other'
  label: string | null
  at: string | null
}

export interface ConversationItem {
  id: number
  store_id: string
  store_name?: string // 店铺名（列表卡片显示）
  marketplace_id: string
  buyer_key: string
  item_id: string | null
  handoff_state: string
  channel_source: string | null
  conversation_type: string
  conversation_title: string | null
  last_message_at: string | null
  preview: string
  preview_role: string | null
  waiting_seconds: number | null
  job: JobInfo | null
  intent: string | null
  claim: Claim | null
  auto_replied: boolean // 当前任务由 AI 自动回复发出
  aftersales: AftersalesInfo | null
  situation?: SituationInfo | null // AI 判断的会话情况（阶段 + 一句话概括）
  mark: MessageMarkInfo | null // 第一个会话标记（兼容旧字段）
  marks?: MessageMarkInfo[] // 会话上的全部标记（可以多个）
  starred?: boolean // 星标（同事共用）
  invoice_status: 'WAITING_EMAIL' | 'OPEN' | 'DONE' | 'CANCELLED' | null // 发票申请：OPEN 待审核发送
  outbox_pending?: number // 发件箱里还没发出的条数（可撤回）
  blacklist?: { id: number; reason: string | null; created_by: string | null } | null // 黑名单买家
  outbox_problem?: boolean // 有发不出或结果不确定的消息
  item_url: string | null
  unread_count: number // 团队未读条数（收到的消息）
  platform_unread_count: number | null // eBay 后台的未读条数（只读同步）
  read_by: string | null
  read_at: string | null
}

/** 会话商品卡片（GET /internal/conversations/{id}/listing），给运营看，含具体库存数 */
export interface ListingCard {
  item_id: string | null
  url: string | null
  error: string | null
  title?: string | null
  sku?: string | null
  price?: { value: string; currency: string } | null
  condition?: string | null
  picture_url?: string | null
  listing_status?: string | null
  marketplace_id?: string | null
  quantity_available?: number | null
  variations?: number
  compatibility_total?: number
}

export interface ChatMessage {
  id: number
  ebay_message_id: string
  seq: number
  role: 'BUYER' | 'SELLER' | 'SYSTEM' | 'UNKNOWN'
  ack?: boolean // 转人工后自动发的“已收到”回执（不算正式回复）
  direction: string
  body: string
  zh_translation: string | null
  translating?: boolean // 正在后台自动翻译
  sent_at: string
  subject: string | null
  sender_key: string | null
  media: { mediaName?: string; mediaType?: string; mediaUrl?: string; fileId?: number }[] // fileId：店铺发出的附件（如查件结果 POD），存在售后附件里
  attachments: Attachment[]
  /** 工作台模拟发送的店铺消息（没有发到 eBay） */
  simulated: boolean
  /** 有 eBay 原始 HTML 邮件（系统通知）；正文 body 已是纯文本 */
  has_html: boolean
  /** 由哪个任务、哪一代次的草稿发出；from_ai 为 true 时可查看生成依据 */
  trace: { job_id: number; generation: number; from_ai: boolean; auto: boolean } | null // auto：AI 自动回复发出
}

export interface ImageObservationOutput {
  image_kind: string
  quality: string
  description: string
  visible_text: string[]
  part_numbers: string[]
  error_codes: string[]
  vin_masked: string
  damage_observed: boolean
  damage_description: string
  uncertain: string[]
  contains_personal_info: boolean
}

export interface ImageObservationRecord {
  id: number
  status: 'OK' | 'FAILED'
  observation: ImageObservationOutput | null
  error: string | null
  cached: boolean
  review_status: 'UNREVIEWED' | 'CONFIRMED' | 'CORRECTED'
  review_note: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  created_at: string
}

export interface Attachment {
  id: number
  seq: number
  media_name: string | null
  media_type: string
  source: string
  status: 'PENDING' | 'STORED' | 'SKIPPED' | 'FAILED' | 'EXPIRED'
  status_reason: string | null
  content_type: string | null
  size_bytes: number | null
  width: number | null
  height: number | null
  file_url: string | null
  expires_at: string | null
  observation: ImageObservationRecord | null
}

export interface ComplianceHit {
  rule: string
  severity: 'BLOCK' | 'REVIEW'
  start: number
  end: number
  text: string
  note: string
}

export interface ComplianceResult {
  result: 'PASS' | 'REVIEW' | 'BLOCK'
  rules_version: string
  hits: ComplianceHit[]
}

export interface Draft {
  id: number
  job_id: number
  run_generation: number
  version: number
  author: string
  status: string
  conversation_row_version: number
  text: string
  text_zh: string | null
  /** 模型自述的中文回复思路（仅供审核员参考） */
  reasoning_zh: string | null
  language: string | null
  intent: string | null
  intents: string[]
  action: string | null
  risk_level: string | null
  decision: string | null
  claims: { text: string; evidence_ids: string[] }[]
  evidence_ids: string[]
  missing_facts: string[]
  conflicts: string[]
  review_reasons: string[]
  compliance: ComplianceResult | null
  prompt_version: string | null
  model_config_version: string | null
  knowledge_versions: string[]
  token_usage: Record<string, number> | null
  created_at: string
  intent_confidence?: 'high' | 'medium' | 'low' | null // 意图把握度
  intent_basis?: string[] // 判断意图依据的消息（eBay 消息 ID）
  inherits_issue?: string | null // 沿用的状态卡未解决问题 ID
}

export interface ToolCallInfo {
  id: number
  request_id: string
  tool_name: string
  tool_version: string
  arguments: Record<string, unknown>
  status: 'FOUND' | 'NOT_FOUND' | 'AMBIGUOUS' | 'STALE' | 'ERROR'
  data: unknown
  message: string | null
  source: string | null
  evidence_ref: string | null
  duration_ms: number
  queried_at: string
}

export interface CitedClause {
  evidence_id: string
  title: string
  text: string
  store_id: string
  marketplace_id: string
  priority: number
  version: string
}

export interface AgentStepInfo {
  node: string
  status: string
  detail: unknown
  usage: Record<string, number> | null
  duration_ms: number
}

export interface RunInfo {
  id: number
  state: string
  thread_id: string
  mode: string
  prompt_version: string
  model_config_version: string
  error: string | null
  started_at: string | null
  finished_at: string | null
}

/** 某任务某一代次的生成依据（GET /internal/jobs/{id}/trace，会话详情里是当前代次） */
/** 会话状态卡（conversations.state_card） */
export interface StateIssue {
  id: string
  source: 'system' | 'model'
  intent: string
  summary_zh: string
  order_id: string | null
  since: string | null
  status: string | null
  refs: string[]
  flags: string[]
}
export interface StateCard {
  topic: string
  issues: StateIssue[]
  resolved: { summary_zh: string; intent: string | null; at: string }[]
  facts: { order_ids?: string[]; vin?: string; vehicle?: string; skus?: string[]; media?: string }
  promises: string[]
}

export interface DraftTrace {
  state_card?: StateCard | null
  state_updated_at?: string | null
  sources: string[]
  tool_calls: ToolCallInfo[]
  steps: AgentStepInfo[]
  run: RunInfo | null
  agent_draft: Draft | null
  cited_knowledge: CitedClause[]
}

export interface JobTrace extends DraftTrace {
  job: JobInfo
  generation: number
}

/** 对话里的查件记录（发起查件 / 收到反馈），按时间插在聊天记录中 */
/** 会话页把右侧栏“当前对话待处理”需要的状态传上去 */
export interface ConvContext {
  jobKey: string // 回复任务 id-状态：变了就刷新查件单、发票、售后工单
  messages: number
  af: AftersalesInfo | null // 客服标记的售后状态
}

export interface TicketTimelineItem {
  kind: 'opened' | 'replied'
  key: string
  at: string
  ticket_id: number
  io_code: string
  type: string | null
  tracking_no: string | null
  desc?: string | null
  by?: string | null
  status?: string | null
  draft?: boolean
  reply?: string | null
  attachments?: { name: string | null; file_id: number | null }[]
  send_status?: string | null
  send_error?: string | null
  sent_at?: string | null
  needs_human?: boolean | null
}

/** 对话里的发票（生成、发到邮箱），按时间插在聊天记录中 */
export interface InvoiceTimelineItem {
  kind: 'invoice'
  key: string
  at: string
  invoice_id: number
  number: string
  invoice_kind: string
  kind_label: string
  order_id: string
  total: number
  currency: string
  status: 'ISSUED' | 'SENT' | 'SIMULATED' | 'SEND_FAILED' | 'VOID'
  status_label: string
  send_error: string | null
  sent_to: string | null
  buyer_email: string | null
  sent_by: string | null
  created_by: string | null
  created_at: string | null
  sent_at: string | null
  pdf_url: string | null
}

/** 发件箱里的消息：延迟发出，发出前可撤回 */
export interface OutboxItem {
  id: number
  conversation_id: number
  body: string
  zh_translation: string | null
  source: string
  actor: string
  state: 'PENDING' | 'SENDING' | 'SENT' | 'RECALLED' | 'FAILED' | 'UNCERTAIN'
  send_after: string
  created_at: string
  sent_at: string | null
  channel: string | null
  error: string | null
  recalled_by: string | null
  recalled_at: string | null
  job_id: number | null
  has_media: boolean
}

/** 系统配置页的一项 */
export interface AppSettingItem {
  group: string
  key: string
  env: string
  label: string
  help: string
  type: 'bool' | 'int' | 'str' | 'secret'
  restart: boolean
  warn: string | null
  source: 'page' | 'env'
  updated_by: string | null
  updated_at: string | null
  value: string | number | boolean | null
  is_set?: boolean
  choices?: string[]
}

export interface ConversationDetail extends DraftTrace {
  conversation: {
    id: number
    store_id: string
    store_name?: string // 店铺名（会话头部显示）
    ebay_conversation_id: string
    conversation_type: string
    marketplace_id: string
    marketplace_source: string | null
    buyer_key: string
    item_id: string | null
    handoff_state: string
    row_version: number
    channel_source: string | null
    conversation_title: string | null
    platform_status: string | null
    aftersales: AftersalesInfo | null // 客服标记转售后的状态
    situation?: SituationInfo | null // AI 判断的会话情况
    mark: MessageMarkInfo | null // 第一个会话标记（兼容旧字段）
    marks?: MessageMarkInfo[] // 会话上的全部标记
    item_url: string | null // 在 eBay 打开商品（按刊登站点选域名）
  }
  messages: ChatMessage[]
  ticket_timeline?: TicketTimelineItem[]
  invoice_timeline?: InvoiceTimelineItem[]
  outbox?: OutboxItem[]
  blacklist?: { id: number; reason: string | null; created_by: string | null } | null
  events: { id: number; event_type: string; source: string; case_id: string | null; detail: string | null; occurred_at: string }[]
  jobs: { id: number; state: string; run_generation: number; created_at: string }[]
  job: JobInfo | null
  claim: Claim | null
  drafts: Draft[]
  send_intent: { id: number; state: string; actor: string; platform_message_id: string | null; last_error: string | null; sent_at: string | null } | null
}

export interface Meta {
  intents: Record<string, { name: string; default_risk: string }>
  reason_labels: Record<string, string>
  gate_info_reasons: string[]
  review_reason_codes: Record<string, string>
  marketplaces: string[]
  send_delay_seconds?: number // 发出前可撤回的时长（秒）
  aftersales: { types: string[]; status_labels: Record<string, string>; intent_hint: Record<string, string> }
  roles: Record<string, string>
}

/** 当前登录账号 */
export interface Me {
  id: number
  username: string
  display_name: string
  role: 'ADMIN' | 'CS' | 'AFTERSALES'
  role_label: string
  all_stores: boolean
  can_aftersales_files?: boolean // 可看售后附件（管理员始终可看）
  store_ids: string[]
  must_change_password: boolean
  features?: { aftersales_files: boolean } // 功能权限点（菜单显示用）
}

/** 消息标记项（管理员维护） */
export interface MessageTag {
  id: number
  name: string
  color: string // #rrggbb / none（无色）；早期的 primary / success / warning / danger / info 也认
  sort: number
  is_active: boolean
  created_by?: string | null
}

/** 会话上的标记 */
export interface MessageMarkInfo {
  tag_id: number
  tag: string
  color: MessageTag['color']
  note: string | null
  marked_by: string
  marked_at: string | null
}

/** 客服标记转售后的状态 */
export interface AftersalesInfo {
  status: 'PENDING' | 'IN_PROGRESS' | 'DONE'
  status_label: string
  type: string | null
  note: string | null
  marked_by: string | null
  marked_at: string | null
  handled_by: string | null
  updated_at: string | null
}

/** 管理员看到的账号 */
export interface UserRow extends Omit<Me, 'must_change_password'> {
  is_active: boolean
  must_change_password: boolean
  locked: boolean
  last_login_at: string | null
  created_by: string | null
  created_at: string | null
}

export interface UsageBucket {
  calls: number
  failed: number
  total_tokens: number
  input_tokens: number
  output_tokens: number
  avg_tokens: number
  avg_seconds: number
}

/** GET /api/usage/summary */
export interface UsageSummary {
  range: { start: string; end: string; days: number }
  totals: UsageBucket & { estimated_tokens: number; drafts: number; tokens_per_draft: number | null }
  by_day: (UsageBucket & { date: string; by_purpose: Record<string, number> })[]
  by_purpose: (UsageBucket & { purpose: string; label: string })[]
  by_model: (UsageBucket & { provider: string | null; provider_label: string; model: string })[]
  by_store: (UsageBucket & { store_id: string | null; name: string })[]
  top_conversations: (UsageBucket & {
    conversation_id: number
    store_id: string | null
    store_name: string | null
    title: string | null
    buyer_key: string | null
  })[]
  recent: {
    id: number
    created_at: string
    purpose: string
    purpose_label: string
    provider: string | null
    model: string | null
    store_id: string | null
    store_name: string | null
    conversation_id: number | null
    job_id: number | null
    input_tokens: number | null
    output_tokens: number | null
    total_tokens: number | null
    estimated: boolean
    duration_ms: number | null
    ok: boolean
    error: string | null
  }[]
  purposes: Record<string, string>
}

/** 发票（会话里看到的；管理员额外有 snapshot） */
export interface InvoiceInfo {
  id: number
  number: string
  store_id: string
  conversation_id: number | null
  order_id: string
  issue_date: string
  kind: 'NORMAL' | 'VAT' // 普通发票（同 eBay 订单单据）/ 增值税发票（店铺 VAT 模板）
  kind_label: string
  language: string
  currency: string
  total: number
  vat_rate: number | null
  vat_amount: number | null
  status: 'ISSUED' | 'SENT' | 'SIMULATED' | 'SEND_FAILED' | 'VOID'
  status_label: string
  created_by: string
  created_at: string | null
  sent_to: string | null
  sent_by: string | null
  sent_at: string | null
  send_error: string | null
  void_reason: string | null
  warnings: string[]
  buyer?: string // 管理员列表
  ship_country?: string | null
  snapshot?: Record<string, any>
}

export interface InvoiceRequestInfo {
  id: number
  email: string | null
  order_id: string | null
  buyer_company: string | null
  buyer_vat: string | null
  invoice_type: 'NORMAL' | 'VAT' | null // 买家要的发票种类；空 = 还没问清
  status: 'WAITING_EMAIL' | 'OPEN' | 'DONE' | 'CANCELLED'
  status_label: string
  source: 'agent' | 'manual'
  note: string | null
  updated_at: string | null
}

/** GET /internal/conversations/{id}/invoice */
export interface ConversationInvoice {
  request: InvoiceRequestInfo | null
  invoices: InvoiceInfo[]
  orders: { order_id: string; created_at: string | null; total: { value: string; currency: string } | null; title: string | null; line_items: number | null }[]
  orders_error: string | null
  test_conversation: boolean
  issuer: { id: number; name: string; vat_ready: boolean } | null // 开票主体（税号、税率）
  mail: { enabled: boolean; not_ready_reasons: string[] }
  languages: string[]
}

/** 开票主体（管理员） */
export interface InvoiceIssuer {
  id?: number
  name: string
  address: string
  postcode: string | null
  phone: string | null
  email: string | null
  vat_numbers: Record<string, string>
  vat_rates: Record<string, number>
  footer: string | null
  is_active: boolean
  store_ids: string[]
  updated_by?: string | null
  updated_at?: string | null
}

/** 售后邮件附件 */
export interface AftersalesFileItem {
  id: number
  mail_id: number
  store_id: string | null
  order_id: string | null
  conversation_id: number | null
  source: 'ATTACHMENT' | 'WETRANSFER' | 'LINK' | 'UPLOAD'
  original_name: string | null
  content_type: string | null
  size_bytes: number | null
  file_name: string | null
  link_url: string | null
  status: 'STORED' | 'INFECTED' | 'TOO_LARGE' | 'BLOCKED_TYPE' | 'DOWNLOAD_FAILED' | 'PURGED' | 'UPLOADING' | 'WAITING_FETCH'
  status_label: string
  status_reason: string | null
  scan_result: string | null
  received_at: string | null
  purged_at: string | null
  days_left: number | null
  kind: 'image' | 'pdf' | 'video' | 'download'
  claimed_by: string | null
}

export interface AftersalesMailItem {
  kind?: 'mail' | 'upload' // upload：买家上传页提交的（下面 code 起的字段只有它有）
  id: number
  subject: string | null
  sender: string | null
  received_at: string | null
  processed_at: string | null
  order_id: string | null
  store_id: string | null
  conversation_id: number | null
  match: string | null
  status: 'PROCESSED' | 'UNMATCHED' | 'FAILED'
  files_saved: number
  body_excerpt: string | null
  files: AftersalesFileItem[]
  code?: string | null // 售后编号，如 AS-20261103-0007
  buyer_key?: string | null
  item_title?: string | null
  issue_label?: string | null
  description?: string | null
  vehicle?: Record<string, string> | null
  installed?: string | null
  submitted_at?: string | null
  status_label?: string
}

/** 发票页的一条发票申请（GET /internal/invoice-requests） */
export interface InvoiceRequestRow extends InvoiceRequestInfo {
  store_id: string
  conversation_id: number
  buyer_key: string | null
  marketplace_id: string | null
  conversation_title: string | null
  test_conversation: boolean
  created_at: string | null
  invoice: InvoiceInfo | null
}

/** GET /internal/conversations/{id}/logistics-tickets */
export interface LogisticsTicketItem {
  id: number
  io_code: string
  order_id: string | null
  tracking_no: string | null
  type: string | null
  status: string | null
  trail_status: string | null
  sm_name: string | null
  warehouse_code: string | null
  reply: string | null
  reject_reason: string | null
  attachments: { name: string; file_id: number | null }[]
  added_at: string | null
  replied_at: string | null
  suggestion: string | null
  suggestion_zh: string | null
  needs_human: boolean | null
  suggested_at: string | null
  handled_at: string | null
  handled_by: string | null
  is_draft: boolean
  origin: string | null
  io_desc: string | null
  created_by: string | null
  created_at: string | null
  is_local_draft: boolean
  it_type_id: number | null
  sm_code: string | null
  submitted_by: string | null
  submitted_at: string | null
  send_status: string | null
  sent_at: string | null
  sent_message_id: number | null
  send_error: string | null
}

export interface ReshipStock {
  order_id: string
  warehouse_code?: string | null
  all_in_stock?: boolean
  summary: string
  items: { sku: string; quantity: number; warehouse_code: string | null; sellable: number; enough: boolean; other_warehouses: string[] }[]
}

export interface LogisticsTicketRow extends LogisticsTicketItem {
  store_id: string | null
  store_name: string | null
  buyer_key: string | null
  conversation_id: number | null
  open_same_order: number
}

/** 会话页右侧（GET /internal/conversations/{id}/customer）：订单、客户资料、历史会话 */
export interface Money {
  value: string
  currency: string
}
export interface CustomerOrder {
  order_id: string
  legacy_order_id: string | null
  record_no: string | null
  marketplace_id: string | null
  created_at: string | null
  paid_at: string | null
  payment_status: string | null
  order_status: string | null
  cancel_state: string | null
  total: Money | null
  refunds: { amount: Money | null; date: string | null; status: string | null }[]
  items: { item_id: string | null; sku: string | null; title: string | null; quantity: number | null; status: string | null }[]
  quantity: number
  shipping_service: string | null
  buyer_note: string | null
  ship_to: { name: string | null; company: string | null; address: string | null; country: string | null; phone: string | null; email: string | null }
}
export interface CustomerPackage {
  order_id: string
  package_id: string | null
  carrier: string | null
  tracking_number: string | null
  tracking_status: string | null
  latest_event: string | null
  event_time: string | null
}
export interface CustomerHistoryItem {
  id: number
  store_id: string
  store_name: string
  marketplace_id: string
  item_id: string | null
  item_url: string | null
  title: string | null
  last_message_at: string | null
  preview: string
  order_id: string | null
}
export interface CustomerPanelData {
  buyer_key: string
  related_order: CustomerOrder | null
  packages: CustomerPackage[]
  packages_error: string | null
  orders: CustomerOrder[]
  profile: {
    buyer_key: string
    name: string | null
    email: string | null
    phone: string | null
    stores: { store_id: string; store_name: string; orders: number }[]
    total_quantity: number
    order_count: number
    amounts: { currency: string; total: string; count: number; avg: string }[]
    first_order_at: string | null
    last_order_at: string | null
    refund_count: number
    refund_amounts: { currency: string; total: string; count: number; avg: string }[]
  }
  history: CustomerHistoryItem[]
  errors: { store_id: string; store_name: string; error: string }[]
}
