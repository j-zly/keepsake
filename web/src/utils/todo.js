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
