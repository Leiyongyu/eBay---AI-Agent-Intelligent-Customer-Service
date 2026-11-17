// 异常包裹跟进表：类型与选项（后端 api/parcel_followups.py）
export interface Followup {
  id: number
  owner_id: number
  owner_name: string
  occurred_on: string | null
  store_id: string | null
  store_name: string | null
  conversation_id: number | null
  buyer_key: string | null
  order_id: string | null
  sku: string | null
  reason: string
  reason_note: string | null
  carrier: string | null
  tracking_no: string | null
  action: string | null
  result: string | null
  reship_tracking_no: string | null
  amount: number | null
  currency: string | null
  ticket_no: string | null
  next_follow_on: string | null
  status: 'OPEN' | 'DONE'
  note: string | null
  created_at: string | null
  updated_at: string | null
  updated_by: string | null
  closed_at: string | null
  origin: 'AUTO' | 'MANUAL'
  next_step: string | null
  can_close: boolean
}

export type FollowupDraft = Partial<Omit<Followup, 'id' | 'owner_id' | 'owner_name' | 'origin' | 'can_close'>> & { id?: number }

export const REASONS = ['断更', '无轨迹', '妥投未收到', '未收到货纠纷', '物流异常退回', '到货损坏', '谷仓发错', '供应商发错', '产品问题', '地址问题', '其他']
export const ACTIONS = ['重发', '退款', '部分退款', '补偿', '索赔', '等待查件', '等待买家', '其他']
export const CARRIERS = ['DHL', 'Royal Mail', 'EVRI', 'DPD', 'GLS', 'Hermes', 'UPS', 'FedEx', 'Yodel', 'YunExpress', 'Deutsche Post']
export const CURRENCIES = ['EUR', 'GBP', 'USD', 'AUD', 'CAD']

export const REASON_TYPE: Record<string, 'danger' | 'warning' | 'info' | 'primary'> = {
  断更: 'warning', 无轨迹: 'warning', 妥投未收到: 'danger', 未收到货纠纷: 'danger', 物流异常退回: 'danger', 到货损坏: 'danger',
  谷仓发错: 'primary', 供应商发错: 'primary', 产品问题: 'primary', 地址问题: 'warning', 其他: 'info',
}

export const todayStr = () => {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
