import { reactive } from 'vue'

/**
 * 左侧菜单（ERP 风格）。
 * view：要打开的页面；box：工作台里的消息箱（未处理 / 已处理 …）；没有对应功能的菜单先打开“建设中”页面（view = 'todo'）。
 * 菜单项按参考 ERP 全部列出，不需要的直接从这里删掉即可。
 */
export type ViewKey =
  | 'workbench' | 'knowledge' | 'aftersales' | 'tickets' | 'shipnotices' | 'cases' | 'invoices' | 'ebaycases' | 'customers' | 'sync'
  | 'followups'
  | 'performance' | 'templates' | 'offers' | 'bestoffers'
  | 'usage' | 'status' | 'accounts' | 'settings' | 'todo'

/** 工作台消息箱：决定列表查询条件 */
export type BoxKey = 'pending' | 'resolved' | 'handoff' | 'failed' | 'unassigned' | 'mine' | 'auto' | 'all'
  | 'system' | 'system_unread' | 'system_read'

export type CountKey = 'buyerUnread' | 'pending' | 'resolved' | 'system' | 'unassigned' | 'mine' | 'failed' | 'handoff'

export interface NavLeaf {
  id: string // 菜单唯一键，也用于“建设中”页面的标题
  label: string
  view: ViewKey
  box?: BoxKey
  count?: CountKey
  adminOnly?: boolean
  feature?: 'aftersales_files'
  /** 动态文字（如 “小雷 (我)”） */
  dynamicLabel?: 'me'
  /** 右侧的小加号（团队成员） */
  addable?: boolean
  /** 只是分组小标题（不可点） */
  caption?: boolean
  /** 消息分组的父级（如 买家消息、平台消息）：橙色、可点，下面跟着带图标的子级消息箱 */
  group?: boolean
  icon?: IconName
}

export interface NavGroup {
  id: string
  label: string
  icon: IconName
  children?: NavLeaf[]
  /** 二级分组（eBay 下面的 消息管理 / 售后管理 / 智能客服） */
  sections?: { id: string; label: string; icon: IconName; parent?: NavLeaf; children: NavLeaf[] }[]
  /** 没有子菜单的一级菜单 */
  leaf?: NavLeaf
  adminOnly?: boolean
}

export type IconName =
  | 'rocket' | 'users' | 'store' | 'mail' | 'inbox' | 'check' | 'up' | 'warn' | 'team' | 'ban' | 'user'
  | 'service' | 'robot' | 'gear' | 'book' | 'chart'

export const NAV: NavGroup[] = [
  { id: 'start', label: '快速入门', icon: 'rocket', leaf: { id: 'start', label: '快速入门', view: 'todo' } },
  {
    id: 'customers', label: '客户管理', icon: 'users',
    children: [
      { id: 'customer-list', label: '客户列表', view: 'customers' },
      { id: 'customer-blacklist', label: '黑名单', view: 'customers' },
    ],
  },
  {
    id: 'ebay', label: 'eBay', icon: 'store',
    sections: [
      {
        id: 'msg', label: '消息管理', icon: 'mail',
        children: [
          { id: 'buyer', label: '买家消息', view: 'workbench', box: 'all', count: 'buyerUnread', group: true },
          { id: 'box-pending', label: '未处理', view: 'workbench', box: 'pending', count: 'pending', icon: 'inbox' },
          { id: 'box-resolved', label: '已处理', view: 'workbench', box: 'resolved', count: 'resolved', icon: 'check' },
          { id: 'box-handoff', label: '升级的消息', view: 'workbench', box: 'handoff', count: 'handoff', icon: 'up' },
          { id: 'box-failed', label: '发送失败', view: 'workbench', box: 'failed', count: 'failed', icon: 'warn' },
          { id: 'sep-1', label: '', view: 'workbench', caption: true },
          { id: 'box-system', label: '平台消息', view: 'workbench', box: 'system', count: 'system', group: true },
          { id: 'box-system-unread', label: '未处理', view: 'workbench', box: 'system_unread', count: 'system', icon: 'inbox' },
          { id: 'box-system-read', label: '已处理', view: 'workbench', box: 'system_read', icon: 'check' },
          { id: 'sep-2', label: '', view: 'workbench', caption: true },
          { id: 'best-offer', label: 'Best offer', view: 'bestoffers' },
        ],
      },
      {
        id: 'after', label: '售后管理', icon: 'service',
        children: [
          // eBay 后台的售后数据（只读，定时拉取）：views/EbayCasesView.vue，菜单 id 对应 CASE_KIND
          { id: 'inquiry', label: '咨询', view: 'ebaycases' },
          { id: 'dispute', label: '纠纷', view: 'ebaycases' },
          { id: 'return', label: '退货', view: 'ebaycases' },
          { id: 'payment-dispute', label: '付款纠纷', view: 'ebaycases' },
          { id: 'feedback', label: '评价管理', view: 'ebaycases' },
          { id: 'cancel', label: '取消交易', view: 'ebaycases' },
          { id: 'refund', label: '退款单', view: 'ebaycases' },
          { id: 'cases', label: '售后工单', view: 'cases' },
          { id: 'tickets', label: '查件单', view: 'tickets' },
          { id: 'followups', label: '异常包裹跟进', view: 'followups' },
          { id: 'shipnotices', label: '发货通知', view: 'shipnotices' },
          { id: 'invoices', label: '发票', view: 'invoices' },
          { id: 'aftersales', label: '售后附件', view: 'aftersales', feature: 'aftersales_files' },
        ],
      },
      {
        id: 'smart', label: '智能客服', icon: 'robot',
        children: [
          { id: 'auto-send', label: '自动发信', view: 'todo' },
          { id: 'auto-feedback', label: '自动留评', view: 'todo' },
          { id: 'templates', label: '消息模板', view: 'templates' },
          { id: 'offers', label: '议价', view: 'offers' },
        ],
      },
      {
        id: 'perf', label: '绩效管理', icon: 'chart',
        children: [{ id: 'performance', label: '绩效统计', view: 'performance' }],
      },
    ],
  },
  {
    // 知识库所有人可看（上传、回滚只有管理员）；其余只有管理员。没有可见菜单项时整组不显示
    id: 'system', label: '系统管理', icon: 'gear',
    children: [
      { id: 'knowledge', label: '知识库', view: 'knowledge' },
      { id: 'usage', label: '用量统计', view: 'usage', adminOnly: true },
      { id: 'status', label: '系统状态', view: 'status', adminOnly: true },
      { id: 'accounts', label: '账号管理', view: 'accounts', adminOnly: true },
      { id: 'settings', label: '系统配置', view: 'settings', adminOnly: true },
      { id: 'sync', label: '数据同步', view: 'sync', adminOnly: true },
    ],
  },
]

/** 售后管理菜单 → 后端类型 */
export const CASE_KIND: Record<string, string> = {
  inquiry: 'INQUIRY', dispute: 'CASE', return: 'RETURN', 'payment-dispute': 'PAYMENT_DISPUTE',
  feedback: 'FEEDBACK', cancel: 'CANCEL', refund: 'REFUND',
}

/** 所有可点的菜单项（平铺），用于按 id 查找 */
export function allLeaves(): NavLeaf[] {
  const out: NavLeaf[] = []
  for (const g of NAV) {
    if (g.leaf) out.push(g.leaf)
    g.children?.forEach((c) => out.push(c))
    g.sections?.forEach((s) => {
      if (s.parent) out.push(s.parent)
      s.children.forEach((c) => out.push(c))
    })
  }
  return out.filter((l) => !l.caption)
}

/** 侧边栏数字：工作台刷新时写入，App 也会定时刷新 */
export const navCounts = reactive<Partial<Record<CountKey, number>>>({})

export function fmtCount(n?: number): string {
  if (n === undefined || n === null) return ''
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return String(n)
}
