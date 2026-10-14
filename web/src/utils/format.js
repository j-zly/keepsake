/**
 * 纯展示层的格式化函数（无 DOM / 无 Vue 依赖，`node --test` 可直接跑）。
 *
 * 时间一律「后端给 UTC 源，前端显示本地时间」，原始 UTC 串仍留在 title 里可查。
 */

/** 补零。 */
const p2 = (n) => String(n).padStart(2, '0')

/**
 * UTC 源 → 本地时间 'YYYY-MM-DD HH:mm'。
 *
 * 后端给的多是裸 UTC（`2026-10-14T02:00:00+00:00` 或 `2026-10-14 02:00:00`）。
 * 不带时区的串直接 new Date() 会被当成本地时间显示（差一个时区），所以先补 Z。
 * 解析不了就原样返回 —— 宁可丑也不要静默吞掉一个时间戳。
 */
export function toLocal(utc) {
  if (!utc) return '—'
  const s = String(utc).trim()
  const hasTz = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(s)
  const iso = hasTz ? s : s.replace(' ', 'T') + 'Z'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return s
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(
    d.getMinutes(),
  )}`
}

/**
 * 距今多久：'刚刚' / '12 分钟前' / '3 小时前' / '5 天前'；解析不了或未来返回空串。
 * 后端时间戳偶尔比本机时钟快（时区/漂移），负数一律当空，不显示「-3 天前」。
 */
export function since(utc) {
  if (!utc) return ''
  const s = String(utc).trim()
  const hasTz = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(s)
  const t = new Date(hasTz ? s : s.replace(' ', 'T') + 'Z').getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  if (diff < 0) return ''
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return ''
}

/**
 * 页码窗口：当前页左右各留 1 页，溢出用 '…' 占位；首页与末页永远出现。
 * 返回 (number | '…')[]，直接喂给 PagerBar 渲染按钮。
 */
export function pageWindow(current, total) {
  if (!(total > 0)) return []
  const c = Math.min(Math.max(1, Math.floor(current) || 1), total)
  const lo = Math.max(1, c - 1)
  const hi = Math.min(total, c + 1)
  const out = []
  if (lo > 1) {
    out.push(1)
    if (lo > 2) out.push('…')
  }
  for (let i = lo; i <= hi; i++) out.push(i)
  if (hi < total) {
    if (hi < total - 1) out.push('…')
    out.push(total)
  }
  return out
}