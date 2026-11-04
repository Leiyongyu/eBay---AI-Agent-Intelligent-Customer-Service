import { session } from './api'

export const STATE_LABELS: Record<string, string> = {
  RECEIVED: '等待合并窗口',
  RUNNING: '生成中',
  REVIEW_REQUIRED: '待审核',
  AUTO_ELIGIBLE: '自动候选（待审）',
  SENDING: '发送中',
  SENT: '已发送',
  SEND_UNCERTAIN: '发送结果不确定',
  RETRYABLE_ERROR: '待重试',
  FAILED_HANDOFF: '失败转人工',
  CANCELLED_STALE: '已合并作废',
  RESOLVED_NO_REPLY: '无需回复',
}

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/** 会话情况阶段（AI 起草时判断） */
export const STAGE_LABELS: Record<string, string> = {
  presale: '售前', shipping: '物流', aftersales: '售后', invoice: '发票', other: '其他',
}

/** 需要人工审核的任务状态：会话卡片和头部用深红实底高亮 */
export const REVIEW_STATES = ['REVIEW_REQUIRED', 'AUTO_ELIGIBLE', 'FAILED_HANDOFF']

export function stateTag(state?: string | null): TagType {
  if (!state) return 'info'
  if (['SENT', 'RESOLVED_NO_REPLY'].includes(state)) return 'success'
  if (['SEND_UNCERTAIN', 'FAILED_HANDOFF'].includes(state)) return 'danger'
  if (['REVIEW_REQUIRED', 'AUTO_ELIGIBLE', 'RETRYABLE_ERROR'].includes(state)) return 'warning'
  return 'primary'
}

export function riskTag(risk?: string | null): TagType {
  return ({ L0: 'success', L1: 'primary', L2: 'warning', L3: 'danger' } as Record<string, TagType>)[risk || ''] || 'info'
}

export function toolTag(status: string): TagType {
  return ({ FOUND: 'success', NOT_FOUND: 'info', AMBIGUOUS: 'warning', STALE: 'warning', ERROR: 'danger' } as Record<string, TagType>)[status] || 'info'
}

export function reasonLabel(code: string): string {
  return session.meta?.reason_labels[code] || code
}

export function isGateInfo(code: string): boolean {
  return session.meta?.gate_info_reasons.includes(code) ?? false
}

export function intentLabel(code?: string | null): string {
  if (!code) return '—'
  const name = session.meta?.intents[code]?.name
  return name ? `${name}（${code}）` : code
}

export function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function fmtWaiting(sec?: number | null): string {
  if (sec == null) return ''
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`
  return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
