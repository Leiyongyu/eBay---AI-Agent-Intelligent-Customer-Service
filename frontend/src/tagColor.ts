// 会话标签的颜色：可以是任意颜色 #rrggbb、none（无色），也兼容早期的 primary / success / warning / danger / info
import type { CSSProperties } from 'vue'

const NAMED: Record<string, string> = {
  primary: '#3a7bfd', success: '#2fae5b', warning: '#f59a23', danger: '#e5484d', info: '#8a94a6',
}

/** 调色盘里的预设色（和现在用的标签颜色一致） */
export const TAG_PALETTE = [
  '#a78bcf', '#f5b90a', '#26a69a', '#f59ac0', '#d1008a', '#5b8def', '#5e6b8c', '#f2462e',
  '#5dd6a8', '#ef6a50', '#f5a055', '#13a3ad', '#73c6ec', '#0aa2f2', '#3a7bfd', '#8a94a6',
]

/** 实际颜色；无色返回 null */
export function tagHex(color?: string | null): string | null {
  if (!color || color === 'none') return null
  if (color.startsWith('#')) return color
  return NAMED[color] || NAMED.info
}

/** 圆点 */
export function dotStyle(color?: string | null): CSSProperties {
  const h = tagHex(color)
  return h ? { background: h } : { background: 'transparent', boxShadow: 'inset 0 0 0 1px var(--line)' }
}

function light(hex: string): boolean {
  const n = parseInt(hex.slice(1, 7), 16)
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
  return 0.299 * r + 0.587 * g + 0.114 * b > 170
}

/** el-tag 的样式（覆盖 Element Plus 的标签变量）；dark = 实心，否则浅底 */
export function tagStyle(color?: string | null, dark = false): CSSProperties {
  const h = tagHex(color)
  if (!h) {
    return { '--el-tag-bg-color': 'transparent', '--el-tag-border-color': 'var(--line)', '--el-tag-text-color': 'var(--text-2)',
      '--el-tag-hover-color': 'var(--text-2)' } as CSSProperties
  }
  if (dark) {
    const text = light(h) ? '#1f2329' : '#ffffff'
    return { '--el-tag-bg-color': h, '--el-tag-border-color': h, '--el-tag-text-color': text, '--el-tag-hover-color': text } as CSSProperties
  }
  return { '--el-tag-bg-color': `${h}22`, '--el-tag-border-color': `${h}88`, '--el-tag-text-color': 'var(--text)',
    '--el-tag-hover-color': h } as CSSProperties
}

/** 调色盘清空 = 无色 */
export const pickerValue = (color?: string | null) => tagHex(color) || ''
export const fromPicker = (v: string | null | undefined) => (v ? v.toLowerCase() : 'none')
