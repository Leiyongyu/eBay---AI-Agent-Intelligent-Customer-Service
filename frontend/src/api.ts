import { reactive } from 'vue'
import type { Meta, Me } from './types'

// 登录状态：user 为空时显示登录页；reviewer（操作人署名）就是当前账号的显示名
export const session = reactive({
  user: null as Me | null,
  checked: false, // 已向后端确认过登录状态
  reviewer: '',
  meta: null as Meta | null,
})

export function setUser(user: Me | null) {
  session.user = user
  session.reviewer = user?.display_name || ''
  session.checked = true
}

export const isAdmin = () => session.user?.role === 'ADMIN'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  // 登录凭证在 HttpOnly Cookie 里，同源请求浏览器自动带上
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  if (resp.status === 401 && !path.startsWith('/api/auth/login')) setUser(null) // 登录过期：回到登录页
  const text = await resp.text()
  const data = text ? JSON.parse(text) : null
  if (!resp.ok) {
    const detail = data?.detail
    const message =
      typeof detail === 'string' ? detail : detail?.message || (Array.isArray(detail) ? detail[0]?.msg : '') || resp.statusText
    throw new ApiError(resp.status, message, detail)
  }
  return data as T
}

export const get = <T = any>(path: string) => api<T>('GET', path)
export const post = <T = any>(path: string, body: unknown = {}) => api<T>('POST', path, body)

export async function loadMeta() {
  if (!session.meta) session.meta = await get<Meta>('/api/system/meta')
  return session.meta
}

export async function loadMe() {
  try {
    setUser(await get<Me>('/api/auth/me'))
  } catch {
    setUser(null)
  }
  return session.user
}

export function newIdempotencyKey(): string {
  if (crypto?.randomUUID) return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}
