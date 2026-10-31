/**
 * 待办页的**纯函数**（node --test 直接跑，不碰 DOM / api）。
 *
 * 🔴 **到期状态由后端给**（`due_state`：none / soon / overdue）——
 *   前端**不自己算日期**：浏览器时区与后端基准日一旦不一致，
 *   就会出现「页面上红了、后端口径里没红」。这里只把状态**翻译成上色**。
 */

/** 三态（与后端 `govern/todo.py` 的 STATUSES 同款）。 */
export const TODO_STATUSES = ['open', 'done', 'obsolete']

/** 状态 → 人话。 */
export const STATUS_LABEL = {
  open: '开放',
  done: '已完成',
  obsolete: '已废弃',
}

/**
 * 到期状态 → 上色档。
 *
 * 🔴 **两态**：soon（3 天内到期）= 橙（warn）、overdue（已过期）= 红（danger）。
 *   判据在**后端**（`govern.todo.due_state`），这里只做翻译。
 *
 * @param {string} state `none` / `soon` / `overdue`
 * @returns {string} `''` / `'warn'` / `'danger'`（对应 .chip--warn / .chip--danger）
 */
export function dueTone(state) {
  if (state === 'overdue') return 'danger'
  if (state === 'soon') return 'warn'
  return ''
}

/**
 * 到期 chip 的文字（`due_state` → 人话）。
 * @param {object} row 一条待办（`{due, due_state}`）
 */
export function dueChipText(row) {
  const state = row?.due_state
  if (state === 'overdue') return '已过期'
  if (state === 'soon') return '快到期'
  const due = String(row?.due || '')
  return due ? `到期 ${due}` : ''
}

/**
 * 按状态分组（页面上的三段卡片：开放 / 已完成 / 已废弃）。
 * @param {object[]} rows 待办行
 */
export function groupByStatus(rows) {
  const list = rows || []
  return {
    open: list.filter((r) => r?.status === 'open'),
    done: list.filter((r) => r?.status === 'done'),
    obsolete: list.filter((r) => r?.status === 'obsolete'),
  }
}

/**
 * 完成率（0~100）：已销掉的（done + obsolete）占全部的比例。
 *
 * 🔴 口径写在页面上（看板的环形仪表同款）—— 「完成率」含不含「废弃」
 *   是个口径问题，不写清楚两张卡就会对不上。
 *
 * @param {object} summary 后端计数 `{open, done, obsolete, total}`
 */
export function completionPercent(summary) {
  const total = Number(summary?.total || 0)
  if (!(total > 0)) return 0
  const closed = Number(summary?.done || 0) + Number(summary?.obsolete || 0)
  return Math.round((closed * 10000) / total) / 100
}

/**
 * 按代理的待办完成率行（看板中卡那组小条）。
 *
 * 🔴 **数字全是后端给的**（`GET /api/v1/todos/summary` 的 `by_agent` 段 ——
 *    一条 GROUP BY 聚合出来的）：这里只做「翻译 + 排序 + 条长封顶」，
 *    **不自己数、不自己算比例**（两处各算一次必然漂）。
 *
 * * `agent` 是注册表里的显示名；`-` 桶 = 「未标注」（owner 查不到注册表），
 *   页面上显示成「未标注」—— 不假装它属于某个代理；
 * * 排序按完成率降序（谁完成得好排前面）；
 * * `width` 是细条的长度，封顶 100%（条不许画出卡片）；
 * * `tone` 是小条的**配色档**（见 `todoAgentTone`）：100% ⇒ 绿（`is-ok`），
 *   其余 ⇒ 空串（默认档 = 蓝）。
 *
 * @param {object[]} byAgent 后端 `by_agent` 段（`[{agent, agent_id, ratio, ...}]`）
 * @returns {object[]} `{key, agent, agent_id, ratio, width, tone}`
 */
export function todoAgentRows(byAgent) {
  return (byAgent || [])
    .map((r) => {
      const ratio = Number(r?.ratio || 0)
      return {
        key: `${r?.agent_id || '-'}-${r?.agent || '-'}`,
        agent: r?.agent === '-' ? '未标注' : (r?.agent || r?.agent_id || '未标注'),
        agent_id: r?.agent_id || '-',
        ratio,
        width: Math.min(100, Math.max(0, ratio)),
        tone: todoAgentTone(ratio),
      }
    })
    .sort((a, b) => b.ratio - a.ratio)
}

/**
 * 按代理小条的配色档。
 *
 * 🔴 **口径**：`ratio >= 100` ⇒ `'is-ok'`（绿，全销掉了）；其余 ⇒ `''`
 *   （默认档 = 蓝 `var(--accent)` = 维持现状）。**0 / 缺失**也算蓝
 *   （没有待办 ≠ 没完成，不该满屏红）。
 *
 * 🔴 **为什么中间档与看板的环不一样**（`completionColor` 里中间档是红）：
 *   环是**单点**指标 —— 红 = 「还有没销掉的」，一眼要看出来；小条组是**满屏多行**
 *   —— 每一行都标红就变成「满屏红」，反而看不出谁是谁。绿/蓝两档足够区分
 *   「全销掉 / 没全销掉」。空串 = 默认档，是本页水位条组沿用的同一套写法。
 *
 * @param {number} ratio 该代理的完成率（%）
 * @returns {string} `'is-ok'` / `''`（默认档）
 */
export function todoAgentTone(ratio) {
  const v = Number(ratio)
  if (!Number.isFinite(v) || v <= 0) return ''
  if (v >= 100) return 'is-ok'
  return ''
}
