// 售后原因最终分类：6 种原因（代码和中文名与后端约定一致）、人工写法“原因 + 空格 + 备注”的拆分（仅用于预览，以后端解析为准）
import type { AftersalesReasonCode, AftersalesReasonRow, UnclassifiedKind } from './types'

export const REASONS: { code: AftersalesReasonCode; label: string; hint: string }[] = [
  { code: 'PRODUCT', label: '产品问题', hint: '质量问题、不工作、故障' },
  { code: 'FITMENT', label: '不适配', hint: '装不上、车型不对' },
  { code: 'WAREHOUSE', label: '仓库原因', hint: '发错货、产品漏放、包装问题需改善' },
  { code: 'LISTING', label: 'listing问题', hint: '刊登描述、图片、兼容表有误' },
  { code: 'CUSTOMER', label: '客户原因', hint: '买错、不想要、改主意' },
  { code: 'LOGISTICS', label: '物流问题', hint: '丢件、破损、超时、退回' },
]
export const REASON_LABEL: Record<string, string> = Object.fromEntries(REASONS.map((r) => [r.code, r.label]))
export const STATUS_LABEL: Record<string, string> = { UNCLASSIFIED: '未分类', AI: 'AI 分类', MANUAL: '人工标记' } // 和后端 STATUS_LABELS 一致

/** “产品问题 漏油” → { code: 'PRODUCT', note: '漏油' }；开头不是 6 种之一返回 null（页面提示用） */
export function parseReasonText(text: string): { code: AftersalesReasonCode; note: string } | null {
  const t = text.trim()
  const low = t.toLowerCase()
  const hit = REASONS.find((r) => low === r.label.toLowerCase() || low.startsWith(`${r.label.toLowerCase()} `))
  return hit ? { code: hit.code, note: t.slice(hit.label.length).trim() } : null
}

// AI 依据代码的前缀（后端 ai_basis，如 RETURN:DOES_NOT_FIT、INTENT:fitment、PARCEL:断更）
const BASIS_PREFIX: Record<string, string> = {
  RETURN: '退货原因', INQUIRY: '未收到货咨询', CASE: '纠纷', PAYMENT_DISPUTE: '付款纠纷', CANCEL: '取消原因', REFUND: '退款单',
  INTENT: '买家问题类型', PARCEL: '异常包裹', TICKET: '查件单', FEISHU: '售后工单', CONV: '会话售后类型',
}
export function basisText(b: string | string[] | null | undefined): string {
  if (!b) return ''
  const list = Array.isArray(b) ? b : [b]
  return list.map((x) => {
    const i = x.indexOf(':')
    if (!(i > 0 && BASIS_PREFIX[x.slice(0, i)])) return x
    const kind = x.slice(0, i), code = x.slice(i + 1)
    return `${BASIS_PREFIX[kind]}：${(kind === 'INTENT' && INTENT_LABEL[code]) || code}`
  }).join('；')
}
// ai_basis 里会出现的买家意图（会话里识别出的全部售后类意图）
const INTENT_LABEL: Record<string, string> = {
  quality_issue: '质量问题', damaged_wrong: '破损/错发/缺件', fitment: '适配问题', delivery_delay: '物流延误/没收到', cancel_request: '要求取消',
  shipping_status: '问物流', return_refund: '退货退款', refund_status: '问退款进度', address_change: '改地址',
}

/** 最终原因中文：后端字段叫 reason_label（兼容旧约定 final_reason_label） */
export function finalLabel(r: { reason_label?: string | null; final_reason_label?: string | null; final_reason: string | null }): string {
  return r.reason_label || r.final_reason_label || (r.final_reason ? REASON_LABEL[r.final_reason] : '') || '未分类'
}

/** ai_note 用“；”分段（没分类的原因；会话情况），页面分行显示 */
export const noteParts = (n: string | null | undefined) => (n || '').split('；').map((x) => x.trim()).filter(Boolean)

/** 未分类再分（后端 UNCLASSIFIED_KINDS / options.unclassified_kinds），顺序固定 */
export const UC_KINDS: { key: UnclassifiedKind; label: string }[] = [
  { key: 'missing_return_data', label: '缺退货数据' }, { key: 'direct_refund', label: '直接退款' }, { key: 'other', label: '其他' },
]
export const UC_LABEL: Record<string, string> = Object.fromEntries(UC_KINDS.map((k) => [k.key, k.label]))

type ExplainRow = Pick<AftersalesReasonRow, 'ai_reason' | 'ai_note' | 'ai_basis' | 'events'>
/** AI 分类说明：ai_note（后端写中文，如“退货原因：不适配（DOES_NOT_FIT）”“冲突：……；以飞书工单为准：质量问题”）为主，
 *  ai_basis 里和说明重复的不再列；其余依据优先用售后事件的中文，对不上再按前缀翻译，不是“前缀:代码”的原样显示。 */
export function aiExplain(r: ExplainRow): { notes: string[]; basis: string[] } {
  // 旧数据的 ai_note 是“依据 CANCEL:XXX”，和 ai_basis 重复
  const notes = noteParts(r.ai_note).filter((p) => !(r.ai_reason && /^依据 [A-Z_]+:/.test(p)))
  const all = notes.join('；')
  const raw = r.ai_basis ? (Array.isArray(r.ai_basis) ? r.ai_basis : [r.ai_basis]) : []
  const basis: string[] = []
  for (const b of raw) {
    if (!b || b === r.ai_note || all.includes(b)) continue
    const i = b.indexOf(':')
    const kind = i > 0 ? b.slice(0, i) : ''
    const code = i > 0 ? b.slice(i + 1) : ''
    if (code && all.includes(code)) continue // 说明里已经写了这个代码
    const ev = code ? (r.events || []).find((e) => e.kind === kind && e.reason === code) : undefined
    const text = ev ? `${ev.kind_label || kind}：${ev.reason_label || code}` : basisText(b)
    if (!notes.includes(text) && !basis.includes(text)) basis.push(text)
  }
  return { notes, basis }
}
