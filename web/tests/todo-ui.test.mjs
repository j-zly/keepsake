/**
 * 待办页（2026-10 ks_todo 一等公民）的前端护栏。
 *
 * 钉住任务书判据里的两条：
 *   1) **到期高亮两态**：3 天内到期 = 橙（warn）、已过期 = 红（danger）——
 *      两态必须一眼分开（同一个红表示「还剩两天」和「欠了十天」是分不清的）。
 *   2) **nav-order 钉新页**：「待办」在侧栏的位置（在「审核」之前）。
 *
 * 纯函数（utils/todo.js）**跑真函数**；`.vue` 跑不起来 ⇒ 接线走静态扫描。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  STATUS_LABEL,
  TODO_STATUSES,
  completionPercent,
  dueChipText,
  dueTone,
  groupByStatus,
} from '../src/utils/todo.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/TodosView.vue')
const APP = read('App.vue')
const API = read('api/index.js')
const ROUTER = read('router/index.js')

// ---------------------------------------------------------------- 到期两态

test('到期高亮两态：soon → 橙（warn）、overdue → 红（danger）', () => {
  assert.equal(dueTone('soon'), 'warn', '临期（3 天内）= 橙')
  assert.equal(dueTone('overdue'), 'danger', '过期 = 红')
  assert.equal(dueTone('none'), '', '没到期日不上色')
  assert.equal(dueTone(''), '')
  assert.equal(dueTone(undefined), '')
})

test('到期 chip 的文字分三档', () => {
  assert.equal(dueChipText({ due_state: 'overdue', due: '2026-10-01' }), '已过期')
  assert.equal(dueChipText({ due_state: 'soon', due: '2026-10-25' }), '快到期')
  assert.equal(dueChipText({ due_state: 'none', due: '2026-12-01' }), '到期 2026-12-01')
  assert.equal(dueChipText({ due_state: 'none', due: '' }), '')
})

test('到期态**由后端给**：前端不自己算日期', () => {
  // 🔴 前端只翻译 due_state —— 自己算日期会与后端口径漂（时区一差就错）。
  assert.ok(!/new Date\(/.test(read('utils/todo.js')), '前端不许自己算日期')
  assert.ok(!/Date\.now\(/.test(read('utils/todo.js')))
  assert.match(VIEW, /due_state/, '页面用的是后端给的 due_state')
})

test('页面用的是 warn / danger 两个既有档（不新增色值）', () => {
  assert.match(VIEW, /chip--\$\{tone\}|dueChipClass/)
  assert.match(read('styles/tokens.css'), /\.chip--warn\s*\{/)
  assert.match(read('styles/tokens.css'), /\.chip--danger\s*\{/)
})

// ---------------------------------------------------------------- 状态分组

test('三态分组：开放 / 已完成 / 已废弃分开摆', () => {
  assert.deepEqual(TODO_STATUSES, ['open', 'done', 'obsolete'])
  assert.equal(STATUS_LABEL.open, '开放')
  assert.equal(STATUS_LABEL.done, '已完成')
  assert.equal(STATUS_LABEL.obsolete, '已废弃')
  const rows = [
    { status: 'open' }, { status: 'open' },
    { status: 'done' },
    { status: 'obsolete' },
  ]
  const g = groupByStatus(rows)
  assert.equal(g.open.length, 2)
  assert.equal(g.done.length, 1)
  assert.equal(g.obsolete.length, 1)
})

test('完成率口径：已销掉（done + obsolete）÷ 全部', () => {
  assert.equal(completionPercent({ done: 3, obsolete: 1, total: 8 }), 50)
  assert.equal(completionPercent({ done: 0, obsolete: 0, total: 0 }), 0)
  assert.equal(completionPercent({}), 0)
})

// ---------------------------------------------------------------- 接线

test('页面走 /api/v1/todos（列表 / 计数 / 建议废弃）', () => {
  assert.match(API, /listTodos:[\s\S]{0,120}\/api\/v1\/todos/)
  assert.match(API, /todoSummary:[\s\S]{0,120}\/api\/v1\/todos\/summary/)
  assert.match(API, /todoObsoleteSuggestions:[\s\S]{0,160}\/api\/v1\/todos\/obsolete-suggestions/)
  assert.match(VIEW, /api\.listTodos/)
  assert.match(VIEW, /api\.todoSummary/)
})

test('新增表单的 owner 是下拉（注册表代理），不是自由文本', () => {
  assert.match(VIEW, /el-select[\s\S]{0,600}form\.owner/)
  assert.match(VIEW, /agentOptions/)
})

test('下拉用的字段名是 agent_id（注册表那行的真名就是 agent_id）', () => {
  // 🔴 用 a.id 会让下拉**全是空值**（注册表那行没有 id 字段，只有 agent_id）。
  assert.match(VIEW, /:value="a\.agent_id"/)
  assert.match(VIEW, /\[a\.agent_id, a\.name \|\| a\.agent_id\]/,
    '显示名映射也要用 agent_id')
  assert.doesNotMatch(VIEW, /a\.id\b/, '注册表行里没有 id 字段（只有 agent_id）')
})

test('下拉只给已批准的代理（未批准的 owner 后端直接 404）', () => {
  assert.match(VIEW, /status === 'approved'/)
})

test('删除要手输标题（防手滑）', () => {
  assert.match(VIEW, /showPrompt/)
  assert.match(VIEW, /expect: row\.title/)
})

test('批量标完成用 SelectionBar（与记忆条目页同一份组件）', () => {
  assert.match(VIEW, /<SelectionBar/)
  assert.match(VIEW, /批量标完成/)
})

test('看板有待办小节（开放 / 逾期，点击进页）', () => {
  const stats = read('views/StatsView.vue')
  assert.match(stats, /api\.todoSummary/)
  assert.match(stats, /gotoTodos/)
  assert.match(stats, /router\.push\('\/todos'\)/)
})

test('维护页只「提名」不自动改状态', () => {
  const maint = read('views/MaintenanceView.vue')
  assert.match(maint, /api\.todoObsoleteSuggestions/)
  assert.match(maint, /只提名，不改状态/)
})

// ---------------------------------------------------------------- nav-order

test('nav-order：「待办」在侧栏「审核」之前（业务页在上、审核收尾）', () => {
  const group = APP.match(/title: '记忆',\s*\n\s*items: \[([\s\S]*?)\]\s*,?\n\s*\}/)
  assert.ok(group, '没找到记忆组')
  const labels = [...group[1].matchAll(/label: '([^']+)'/g)].map((m) => m[1])
  assert.ok(labels.includes('待办'), '记忆组里没有「待办」')
  const todoAt = labels.indexOf('待办')
  const reviewAt = labels.indexOf('审核')
  assert.ok(todoAt < reviewAt, '「待办」必须排在「审核」之前')
})

test('路由表里有 /todos', () => {
  assert.match(ROUTER, /path: '\/todos'/)
  assert.match(ROUTER, /TodosView/)
})
