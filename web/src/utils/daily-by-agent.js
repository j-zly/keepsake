/**
 * 「近 7 天记忆增量」图的**纯计算**（看板那张内联 SVG 堆叠柱 + 悬停浮层）。
 *
 * 🔴 本模块**不 import Vue**（纯函数 + 纯数据）⇒ `node --test` 直接跑
 *    （与 `utils/chart-kit.js` 同一套路）。
 *
 * 🔴 **数字全部来自接口**：入参就是 `GET /api/v1/stats?days=7` 的两段 ——
 *    `daily`（横轴：后端补齐了每一天）与 `daily_by_agent`（格子：只给有写入的）。
 *    本模块只做「分组 / 排序 / 归并 / 摆位置」，**不造数**。
 */

/** 「其他」桶的内部键：绝不可能是 agent id 的哨兵（agent id 是人起的名字）。 */
export const OTHER_KEY = 'other'

/**
 * 按代理汇总 `daily_by_agent` 的格子。
 *
 * @param {Array<{d: string, agent: string, n: number}>} rows 接口那一段
 * @returns {Array<{agent: string, total: number, byDay: Map<string, number>}>}
 */
export function bucketByAgent(rows) {
  const out = new Map()
  for (const r of rows || []) {
    const day = String(r?.d || '')
    const agent = String(r?.agent || '-')
    const n = Number(r?.n || 0)
    if (!day) continue
    let b = out.get(agent)
    if (!b) {
      b = { agent, total: 0, byDay: new Map() }
      out.set(agent, b)
    }
    b.total += n
    b.byDay.set(day, (b.byDay.get(day) || 0) + n)
  }
  return [...out.values()]
}

/**
 * 序列 = **7 天总量降序前 `topN`** + 「其他」桶（其余代理合并进去）。
 *
 * 🔴 归并顺序按 agent id 排（同量时次序稳定 ⇒ 每次渲染颜色分配一样，
 *    免得同一批代理天天换颜色）。
 *
 * @param {Array} buckets  `bucketByAgent` 的结果
 * @param {number} topN    前几个单列（其余进「其他」）
 * @param {(agent: string) => string} labelOf 显示名（注册表备注名 → id → 未标注）
 * @returns {Array} 序列（每项：{key, label, title, total, byDay, restCount}）
 */
export function topSeries(buckets, topN, labelOf) {
  const sorted = [...(buckets || [])].sort(
    (a, b) => b.total - a.total || String(a.agent).localeCompare(String(b.agent)))
  const label = typeof labelOf === 'function' ? labelOf : (a) => a
  const series = sorted.slice(0, topN).map((b) => ({
    key: b.agent,
    label: label(b.agent),
    title: b.agent,
    total: b.total,
    byDay: b.byDay,
    restCount: 0,
  }))
  const rest = sorted.slice(topN)
  if (rest.length) {
    const byDay = new Map()
    let total = 0
    for (const b of rest) {
      total += b.total
      for (const [d, n] of b.byDay) byDay.set(d, (byDay.get(d) || 0) + n)
    }
    series.push({
      key: OTHER_KEY,
      label: `其他 ${rest.length} 个代理`,
      title: rest.map((b) => b.agent).join('、'),
      total,
      byDay,
      restCount: rest.length,
    })
  }
  return series
}

/**
 * 某一天的**悬停浮层明细**：当日总量 + 各代理分项（按条数降序）。
 *
 * 🔴 **总量 = 分项之和**（浮层里的数必须自洽 —— 柱高也是这个总和）。
 * 🔴 分项按**真实代理**列（不按「其他」桶归并）：浮层要看得清「谁写了多少」，
 *    「未标注」（无归属的老条目）自己占一行，不摊到别人头上。
 *
 * @param {Array} buckets `bucketByAgent` 的结果
 * @param {string} day     哪一天（`YYYY-MM-DD`）
 * @param {(agent: string) => string} labelOf 显示名（注册表备注名 → id → 未标注）
 * @returns {{day: string, total: number, parts: Array<{key, label, count}>}}
 */
export function dayBreakdown(buckets, day, labelOf) {
  const label = typeof labelOf === 'function' ? labelOf : (a) => a
  const parts = []
  let total = 0
  for (const b of buckets || []) {
    const n = Number(b?.byDay?.get(day) || 0)
    if (!n) continue
    total += n
    parts.push({ key: b.agent, label: label(b.agent), count: n })
  }
  // 降序；同量按 agent id 排（次序稳定 ⇒ 每次渲染顺序一样）
  parts.sort((a, b) => b.count - a.count || String(a.key).localeCompare(String(b.key)))
  return { day, total, parts }
}

/**
 * 堆叠柱的几何（viewBox 坐标系 —— 与实际像素解耦 ⇒ 窄屏不横向滚动）。
 *
 * 每格 = 一天 × 一个代理：一个矩形 `{x, y, w, h, color}`；`y` 越小越靠上
 * （堆叠自下而上，图例顺序 = 自下而上的顺序 ⇒ 图例第一项就是最底下那条）。
 *
 * @param {string[]} days  横轴（后端补齐的每一天）
 * @param {Array} series   `topSeries` 的结果
 * @param {object} opts    `{w, h, padL, padR, padT, padB, colorOf, maxBarW}`
 */
export function stackLayout(days, series, opts = {}) {
  const w = opts.w ?? 720
  const h = opts.h ?? 240
  const padL = opts.padL ?? 38
  const padR = opts.padR ?? 12
  const padT = opts.padT ?? 14
  const padB = opts.padB ?? 30
  const maxBarW = opts.maxBarW ?? 64
  const colorOf = typeof opts.colorOf === 'function' ? opts.colorOf : () => 'currentColor'

  const list = days || []
  const rows = series || []
  const plotW = w - padL - padR
  const plotH = h - padT - padB
  const baseY = padT + plotH
  const totals = list.map((d) => rows.reduce((acc, s) => acc + (s.byDay.get(d) || 0), 0))
  const max = Math.max(1, ...totals)
  const band = list.length ? plotW / list.length : plotW
  const barW = Math.min(maxBarW, band * 0.56)
  const yOf = (v) => padT + plotH * (1 - v / max)

  const segs = []
  const cols = []
  list.forEach((day, i) => {
    const x = padL + i * band + (band - barW) / 2
    let cum = 0
    let topY = baseY
    for (const s of rows) {
      const n = s.byDay.get(day) || 0
      if (n > 0) {
        const y = yOf(cum + n)
        segs.push({
          key: `${s.key}|${day}`,
          x,
          y,
          w: barW,
          h: Math.max(0, yOf(cum) - y),
          color: colorOf(s, rows.indexOf(s)),
        })
        if (y < topY) topY = y
      }
      cum += n
    }
    // 逐日一列的锚点：柱顶数字（total）、悬停浮层的横向位置（cx）
    // 与整列命中区（hitX/hitW）都从这里出。
    cols.push({
      day,
      cx: padL + i * band + band / 2,
      topY,
      total: totals[i],
      hitX: padL + i * band,
      hitW: band,
    })
  })

  return {
    viewBox: `0 0 ${w} ${h}`,
    segs,
    cols,
    max,
    total: totals.reduce((a, b) => a + b, 0),
    padL,
    padR,
    padT,
    plotH,
    right: w - padR,
    baseY,
    grid: [0, max / 2, max].map((v) => ({ v: Math.round(v), y: yOf(v) })),
    xLabels: list.map((d, i) => ({
      text: String(d).slice(5),
      x: padL + i * band + band / 2,
      y: baseY + 16,
    })),
  }
}
