// 售后管理（咨询 / 纠纷 / 退货 / 付款纠纷 / 评价）两个页面共用：类型、标签、批量操作、每行补充信息、导出
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, get, post } from './api'
import { errorText } from './format'

export interface CaseRow {
  id: number
  kind: string
  store_id: string
  store_name: string
  external_id: string
  order_id: string | null
  item_id: string | null
  buyer: string | null
  status: string | null
  is_open: boolean
  opened_at: string | null
  due_at: string | null
  due_soon: boolean
  overdue: boolean
  remaining_seconds: number | null
  amount: number | null
  currency: string | null
  reason: string | null
  title: string | null
  comment: string | null
  rating: string | null
  detail: Record<string, any>
  // 中文名（后端 case_labels 映射，不认识的值原样）
  status_label?: string | null
  reason_label?: string | null
  rating_label?: string | null
  detail_labels?: { key: string; key_label: string; value: any; value_label: any }[]
  is_new: boolean
  status_changed_at: string | null
  ebay_updated_at: string | null
  view: 'pending' | 'processing' | 'done'
  work_state: string | null
  hold_reason: string | null
  starred: boolean
  tag_ids: number[]
  note: string | null
  note_by: string | null
  note_at: string | null
  operator: string | null
  worked_by: string | null
  worked_at: string | null
  conversation_id: number | null
  blacklisted?: boolean
  extras?: CaseExtras | null // 列表接口从库里带上的商品信息
}
export interface CaseTag {
  id: number
  kind: string
  name: string
  color: string // #rrggbb，或早期的 primary / success / warning / danger / info
  sort: number
  pinned: boolean // 固定在列表上方的快捷标签栏（只有管理员能改）
  created_by?: string | null
}
export interface CaseExtras {
  sku: string | null
  stock_sku: string | null
  quantity: number | null
  price: { value: string; currency: string } | null
  title: string | null
  picture_url: string | null
  order_found: boolean
  order_id?: string | null
  error: string | null
}
export interface ViewCounts {
  views: Record<string, number>
  ratings: Record<string, number>
  starred: number
  tags: Record<string, number>
}
export interface CaseOptions {
  statuses: string[]
  reasons: string[]
  operators: string[]
  hold_reasons: string[]
  stores: { id: string; name: string }[]
  status_labels?: Record<string, string>
  reason_labels?: Record<string, string>
}

export const TAG_DOT: Record<string, string> = {
  primary: '#3a7bfd', success: '#2fae5b', warning: '#f59a23', danger: '#e5484d', info: '#8a94a6',
}
export const TAG_COLORS = [
  { value: 'primary', label: '蓝' }, { value: 'success', label: '绿' }, { value: 'warning', label: '橙' },
  { value: 'danger', label: '红' }, { value: 'info', label: '灰' },
]

const pad = (n: number) => String(n).padStart(2, '0')
export function fmtFull(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
/** 剩余处理时间：4天13时58分（过期显示“已超时 …”） */
export function fmtRemain(sec: number | null): string {
  if (sec == null) return ''
  const late = sec < 0
  let s = Math.abs(sec)
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const txt = `${d ? `${d}天` : ''}${h}时${pad(m)}分`
  return late ? `已超时 ${txt}` : txt
}
export const money = (v: number | null | undefined, cur?: string | null) =>
  v == null ? '—' : `${Number(v).toFixed(2)} ${cur || ''}`.trim()

export async function copyText(text?: string | null) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

/** 一个类型页面的共用状态与操作 */
export function useCases(kind: () => string, reload: () => Promise<void> | void) {
  const tags = ref<CaseTag[]>([])
  const extras = reactive<Record<number, CaseExtras | null>>({})
  const busy = ref(false)

  async function loadTags() {
    try {
      // 旧数据 / 旧后端没有 pinned：当作固定（和以前一样都显示在快捷标签栏）
      tags.value = (await get<CaseTag[]>(`/internal/ebay-cases/tags?kind=${kind()}`)).map((t) => ({ ...t, pinned: t.pinned !== false }))
    } catch {
      tags.value = []
    }
  }
  const tagOf = (id: number) => tags.value.find((t) => t.id === id)

  // 每行补充信息（SKU、单价、商品图）：当前页按需加载，最多同时 4 个
  const queue: number[] = []
  let running = 0
  function pump() {
    while (running < 4 && queue.length) {
      const id = queue.shift()!
      running++
      get<CaseExtras>(`/internal/ebay-cases/${id}/extras`)
        .then((r) => (extras[id] = r))
        .catch(() => null)
        .finally(() => {
          running--
          pump()
        })
    }
  }
  function want(rows: CaseRow[]) {
    for (const r of rows) {
      if (r.extras !== undefined) { // 列表接口已经从库里带上了商品信息，不用再单独请求
        extras[r.id] = r.extras
        continue
      }
      if (r.id in extras || queue.includes(r.id)) continue
      extras[r.id] = null
      queue.push(r.id)
    }
    pump()
  }

  async function batch(ids: number[], body: Record<string, unknown>, okText: string) {
    if (!ids.length) return ElMessage.warning('请先勾选记录')
    busy.value = true
    try {
      const r = await post<{ updated: number }>('/internal/ebay-cases/batch', { ids, ...body })
      ElMessage.success(`${okText}：${r.updated} 条`)
      await reload()
    } catch (e) {
      ElMessage.error(errorText(e))
    } finally {
      busy.value = false
    }
  }
  async function star(r: CaseRow) {
    try {
      await api('PUT', `/internal/ebay-cases/${r.id}/star`, { starred: !r.starred })
      r.starred = !r.starred
      reload()
    } catch (e) {
      ElMessage.error(errorText(e))
    }
  }
  async function saveNote(r: CaseRow, note: string) {
    try {
      const res = await api<{ note: string | null; note_by: string; note_at: string }>('PUT', `/internal/ebay-cases/${r.id}/note`, { note })
      Object.assign(r, res)
      ElMessage.success('备注已保存')
      return true
    } catch (e) {
      ElMessage.error(errorText(e))
      return false
    }
  }
  /** 新建标签（所有人都可以）；成功返回新标签 */
  async function createTag(name: string, color: string): Promise<CaseTag | null> {
    try {
      const t = await post<CaseTag>('/internal/ebay-cases/tags', { kind: kind(), name, color })
      await loadTags()
      ElMessage.success(`已新建标签「${t.name}」`)
      return t
    } catch (e) {
      ElMessage.error(errorText(e))
      return null
    }
  }
  /** 改名、改颜色、固定（只有管理员，后端也会拦） */
  async function updateTag(t: CaseTag, body: Record<string, unknown>) {
    try {
      await api('PATCH', `/internal/ebay-cases/tags/${t.id}`, body)
      await loadTags()
    } catch (e) {
      ElMessage.error(errorText(e))
    }
  }
  /** 删除（只有管理员）：从所有记录上去掉 */
  async function deleteTag(t: CaseTag) {
    try {
      const r = await api<{ removed_from?: number }>('DELETE', `/internal/ebay-cases/tags/${t.id}`)
      await loadTags()
      await reload()
      ElMessage.success(`已删除标签「${t.name}」${r?.removed_from ? `，从 ${r.removed_from} 条记录上去掉` : ''}`)
    } catch (e) {
      ElMessage.error(errorText(e))
    }
  }

  return { tags, extras, busy, loadTags, tagOf, want, batch, star, saveNote, createTag, updateTag, deleteTag }
}

/** 导出 CSV（Excel 直接打开不乱码） */
export function downloadCsv(name: string, head: string[], rows: unknown[][]) {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""').replace(/\r?\n/g, ' ')}"`
  const csv = [head, ...rows].map((r) => r.map(esc).join(',')).join('\r\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }))
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}
