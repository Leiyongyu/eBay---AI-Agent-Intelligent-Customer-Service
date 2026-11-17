// 退款申请单（后端 refund_requests.py）：类型、状态、原因的中文和颜色，几个页面共用
export type RefundStatus = 'PENDING' | 'REJECTED' | 'REFUNDING' | 'REFUNDED' | 'ERROR'
export type RefundSource = 'MANUAL' | 'AFTERSALES' | 'CANCEL'

export interface RefundRequestRow {
  id: number
  store_id: string
  store_name: string
  order_id: string
  buyer: string | null
  conversation_id: number | null
  source: RefundSource
  source_label: string
  case_id: number | null
  case_kind: string | null
  case_external_id: string | null
  full: boolean
  full_label: string
  amount: number
  currency: string
  lines: { line_item_id: string; amount: number; sku: string | null; title: string | null }[]
  reason: string
  reason_label: string
  execution: 'order_issue_refund' | 'post_order_return'
  execution_label: string
  return_id: string | null
  return_payload: Record<string, unknown> | null
  comment: string | null
  note: string | null
  order_total: number | null
  order_currency: string | null
  review_mode: 'review' | 'direct'
  status: RefundStatus
  status_label: string
  submitted_by: string
  submitted_at: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  review_note: string | null
  executed_by: string | null
  executed_at: string | null
  ebay_refund_id: string | null
  ebay_refund_status: string | null
  error: string | null
  refund_case_id: number | null
  updated_at: string | null
  can_review?: boolean
  blocked?: string | null
}

export interface RefundPrefill {
  execution: 'order_issue_refund' | 'post_order_return'
  execution_label: string
  return_id: string | null
  store_id: string
  store_name: string
  order_id: string | null
  orders: string[]
  conversation_id: number | null
  buyer: string | null
  case: { id: number; kind: string; external_id: string; reason: string | null; status: string | null; amount: number | null; currency: string | null } | null
  source: RefundSource
  source_label: string
  reason: string
  order: { total: number | null; currency: string | null; created_at: string | null; refunded_flag: boolean; cancelled: boolean } | null
  lines: { line_item_id: string; sku: string | null; title: string | null; quantity: number | null; amount: number | null; currency: string | null }[]
  requested: number
  remaining: number | null
  open_request: RefundRequestRow | null
  mode: 'review' | 'direct'
  rule: RefundRule | null
  blocked: string | null
  /** 通过退货退款时，同订单别的进行中的退货 / 纠纷（只提示不拦） */
  open_case_notice?: string | null
  can_submit: boolean
  can_refund_directly: boolean
  reasons: { code: string; label: string }[]
}

export interface RefundRule {
  id?: number
  store_id: string | null
  currency: string | null
  min_amount: number | null
  max_amount: number | null
  mode: 'direct' | 'review'
  enabled: boolean
  sort?: number
  note: string | null
}

export const STATUS_TYPE: Record<string, 'warning' | 'info' | 'primary' | 'success' | 'danger'> = {
  PENDING: 'warning', REJECTED: 'info', REFUNDING: 'primary', REFUNDED: 'success', ERROR: 'danger',
}
export const fmtMoney = (v: number | null | undefined, cur?: string | null) => (v == null ? '—' : `${cur || ''} ${Number(v).toFixed(2)}`.trim())
