// 售后单“纠纷处理”弹窗（咨询 / 退货 / 纠纷）共用：取详情（/full，eBay 实时）、备注、提交处理（二次确认）、上一个 / 下一个
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from './api'
import { errorText } from './format'
import type { CaseRow } from './caseCommon'

export interface Amt { value: number; currency: string | null }
export interface Note { id: number; text: string; kind: string; created_by: string | null; created_at: string | null }
export interface HistoryItem { actor: string | null; action: string | null; description: string | null; at: string | null }
export interface OrderInfo {
  order_id?: string; buyer?: string | null; name?: string | null; address?: string | null; country?: string | null
  created_at?: string | null; paid_at?: string | null; status?: string | null; total?: string | null; currency?: string | null
  lines?: { sku: string | null; title: string | null; quantity: number | null; item_id: string | null; price: string | null; currency: string | null }[]
  packages?: { carrier: string | null; tracking_number: string | null; shipped_at: string | null }[]
  payment_status?: string | null; payment_status_label?: string | null
  refunds?: { amount: Amt | null; at: string | null; status: string | null; line: string | null }[]
  error?: string
}
export interface Choice { code: string; label: string }
export interface FullBase {
  notes: Note[]; extras: Record<string, any>; order: OrderInfo; live: any; live_error: string | null
  conversation_id: number | null
  actions: { blocked: string | null; labels: Record<string, string>; can: Record<string, boolean>
    senior: Record<string, boolean>; why: Record<string, string | null> }
  [k: string]: any
}
export interface ActionResult { done: string; partial_error?: string; queued?: { outbound_id: number; send_after: string }
  status: string | null; is_open: boolean; work_state: string | null; outcome?: string | null }

/** path：后端处理接口的前缀（inquiry / return / case） */
export function useCaseDialog(props: { rows: CaseRow[]; index: number },
                              emit: { (e: 'changed', r: CaseRow): void; (e: 'move', i: number): void },
                              path: string) {
  const row = computed(() => props.rows[props.index])
  const full = ref<FullBase | null>(null)
  const loading = ref(false)
  const busy = ref(false)
  let onLoaded: ((f: FullBase) => void) | null = null

  async function load() {
    if (!row.value) return
    loading.value = true
    full.value = null
    try {
      full.value = await get<FullBase>(`/internal/ebay-cases/${row.value.id}/full`)
      onLoaded?.(full.value)
      if (row.value.is_new) post(`/internal/ebay-cases/${row.value.id}/seen`).then(() => (row.value.is_new = false)).catch(() => null)
    } catch (e) {
      ElMessage.error(errorText(e))
    } finally {
      loading.value = false
    }
  }
  watch(() => props.index, load, { immediate: true })

  /** 这个处理现在为什么不能做（null = 可以）：店铺没对接 → 角色 → eBay 现在不允许 */
  function blocked(a: string): string | null {
    const ac = full.value?.actions
    if (!ac) return '加载中'
    if (ac.blocked) return ac.blocked
    if (ac.senior?.[a]) return `${ac.labels[a] || ''}只有管理员和售后可以做`
    if (ac.why?.[a]) return ac.why[a]
    return ac.can[a] ? null : '现在不能做'
  }

  /** 只是角色不够（不用去 eBay 后台）；加载完之前不算 */
  function roleBlocked(a: string): boolean {
    const ac = full.value?.actions
    return !!ac && !ac.blocked && !!ac.senior?.[a]
  }
  /** 页签顶部要显示的“为什么做不了”（加载中不显示） */
  function note(a: string): string | null {
    return full.value ? blocked(a) : null
  }

  async function addNote(text: string) {
    if (!row.value || !text.trim()) return false
    try {
      const n = await post<Note>(`/internal/ebay-cases/${row.value.id}/notes`, { text })
      full.value?.notes.unshift(n)
      Object.assign(row.value, { note: n.text, note_by: n.created_by, note_at: n.created_at })
      emit('changed', row.value)
      return true
    } catch (e) {
      ElMessage.error(errorText(e))
      return false
    }
  }

  /** 提交一个处理：先二次确认，成功后刷新这条记录 */
  async function submit(action: string, payload: Record<string, any>, tip: string, danger = false) {
    if (!row.value || busy.value) return null
    const what = full.value?.actions.labels[action] || action
    try {
      await ElMessageBox.confirm(tip, what, { type: danger ? 'warning' : 'info', confirmButtonText: '确认提交', cancelButtonText: '取消' })
    } catch {
      return null
    }
    busy.value = true
    try {
      const r = await post<ActionResult>(`/internal/ebay-cases/${row.value.id}/${path}/${action}`, payload)
      if (r.partial_error) ElMessageBox.alert(r.partial_error, '只完成了一部分', { type: 'warning' })
      else if (r.queued) ElMessage.success('已放进发件箱，到时间自动发出，之前可在会话里撤回')
      else ElMessage.success(r.done.startsWith('已') ? r.done : `已${what}：${r.done}`)
      Object.assign(row.value, { status: r.status, is_open: r.is_open, work_state: r.work_state })
      emit('changed', row.value)
      await load()
      return r
    } catch (e) {
      ElMessage.error(errorText(e))
      return null
    } finally {
      busy.value = false
    }
  }

  /** 退货 / 纠纷的退款：提交退款申请单（2 号的 /internal/refund-requests，按退款审核规则先审核或直接执行），
   *  执行方式是“通过退货退款”（Post-Order），执行时再按 eBay 的范围核对金额 */
  async function submitRefund(p: Record<string, any>, returnId: string | null, orderId: string | null, tip: string) {
    if (!row.value || busy.value) return null
    try {
      await ElMessageBox.confirm(tip, '提交退款申请', { type: 'warning', confirmButtonText: '确认提交', cancelButtonText: '取消' })
    } catch {
      return null
    }
    busy.value = true
    try {
      const r = await post<{ id: number; status: string; status_label: string; error: string | null }>('/internal/refund-requests', {
        order_id: orderId, case_id: row.value.id, full: p.mode === 'full', amount: p.amount, currency: p.currency,
        comment: p.comment || null, confirm_amount: p.amount, confirm_currency: p.currency,
        execution: 'post_order_return', return_id: returnId, return_payload: { mode: p.mode, keep_item: p.keep_item },
      })
      const state = r.status === 'PENDING' ? (r.error ? `待审核（${r.error}）` : '待审核') : `已执行：${r.status_label}${r.error ? `（${r.error}）` : ''}`
      ElMessageBox.alert(
        `<p>已提交退款申请 #${r.id}，${state}。</p><p><a href="#/ebaycases?m=refund&panel=requests&rid=${r.id}">查看退款申请 #${r.id}</a></p>`,
        '退款申请', { dangerouslyUseHTMLString: true, type: r.status === 'ERROR' ? 'warning' : 'success' })
      emit('changed', row.value)
      await load()
      return r
    } catch (e) {
      ElMessage.error(errorText(e))
      return null
    } finally {
      busy.value = false
    }
  }

  const prev = () => props.index > 0 && emit('move', props.index - 1)
  const next = () => props.index < props.rows.length - 1 && emit('move', props.index + 1)
  return { row, full, loading, busy, load, blocked, roleBlocked, note, addNote, submit, submitRefund, prev, next, setOnLoaded: (fn: (f: FullBase) => void) => (onLoaded = fn) }
}

/** 选好的文件读成 data URL（上传给 eBay 用 base64）；超过 8 MB 或不是图片 / PDF 返回 null 并提示 */
export async function readFile(file: File | undefined | null): Promise<{ data: string; name: string } | null> {
  if (!file) return null
  if (file.size > 8 * 1024 * 1024) {
    ElMessage.warning('文件超过 8 MB')
    return null
  }
  if (!/\.(jpe?g|png|gif|bmp|pdf)$/i.test(file.name)) {
    ElMessage.warning('eBay 只收图片（JPG、PNG、GIF、BMP）或 PDF')
    return null
  }
  const data = await new Promise<string>((res, rej) => {
    const r = new FileReader()
    r.onload = () => res(String(r.result))
    r.onerror = rej
    r.readAsDataURL(file)
  })
  return { data, name: file.name }
}

export const ACTOR: Record<string, string> = { BUYER: '买家', SELLER: '卖家', SYSTEM: '系统', CSR: 'eBay 客服', EBAY: 'eBay' }
