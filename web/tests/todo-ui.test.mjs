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
import { blankForm, todoFormError, todoPayload } from '../src/utils/todoForm.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/TodosView.vue')
const DIALOG = read('components/TodoDialog.vue')
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
  // 表单在弹框里（2026-10-22 整改）—— 下拉与候选都在弹框组件内
  assert.match(DIALOG, /el-select[\s\S]{0,600}form\.owner/)
  assert.match(DIALOG, /v-for="a in agents"/, '候选代理由父页传进来')
  assert.match(VIEW, /:agents="agentOptions"/, '父页把已批准的代理传进弹框')
  assert.match(VIEW, /agentOptions/)
})

test('下拉用的字段名是 agent_id（注册表那行的真名就是 agent_id）', () => {
  // 🔴 用 a.id 会让下拉**全是空值**（注册表那行没有 id 字段，只有 agent_id）。
  assert.match(DIALOG, /:value="a\.agent_id"/)
  assert.match(VIEW, /\[a\.agent_id, a\.name \|\| a\.agent_id\]/,
    '显示名映射也要用 agent_id')
  assert.doesNotMatch(DIALOG, /a\.id\b/, '注册表行里没有 id 字段（只有 agent_id）')
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

// ================================================================ 2026-10-22 UI 整改
// 任务书：① 页面默认不渲染裸表单（无「加一条」按钮，有「新待办」入口）；
//         ② 弹框开→提交→关的组件态断言；③ 按钮防窄卡折字。

test('① 页面默认不渲染裸表单：没有「加一条」，只有「新待办」入口', () => {
  assert.doesNotMatch(VIEW, /加一条/, '裸表单的「加一条」按钮不许留在页面上')
  assert.doesNotMatch(VIEW, /v-model="form\./, '新增表单的输入框不许裸铺在页面模板里')
  assert.match(VIEW, /新待办/, '页头要有「新待办」入口')
  assert.match(VIEW, /class="pill-btn"/, '入口是强调蓝胶囊按钮')
  assert.match(VIEW, /type="primary" class="pill-btn"/, '入口是主按钮（强调蓝）')
  assert.match(VIEW, /<TodoDialog/, '页面要挂新增弹框')
  assert.match(VIEW, /v-model="createOpen"/, '弹框开合由 createOpen 控')
  assert.match(VIEW, /const createOpen = ref\(false\)/, '弹框默认关着（页面首屏没有表单）')
  assert.match(VIEW, /@click="openCreate"/, '点「新待办」才开弹框')
  assert.match(VIEW, /function openCreate\(\)\s*\{\s*createOpen\.value = true/, 'openCreate 打开弹框')
})

test('① 筛选面板化：筛选区只管筛，不再混着新增表单', () => {
  assert.match(VIEW, /class="filter-panel"/, '筛选要有面板')
  assert.match(VIEW, /<div class="filter-title">筛选待办<\/div>/, '面板标题说明这块只管筛')
  assert.match(VIEW, /v-model="owner"/, '归属筛选')
  assert.match(VIEW, /v-model="status"/, '状态筛选')
  assert.match(VIEW, /清除筛选/, '有「清除筛选」回全量')
  // 归属/到期日那句提示长句挪进弹框，不占页面主区
  assert.doesNotMatch(VIEW, /在册且已批准/, '那句提示长句不许留在页面主区')
  assert.match(DIALOG, /在册且已批准/, '提示长句在弹框表单下方')
})

test('② 弹框开→提交→关：开时重置、空标题前端先拦、提交成功关框+刷新+toast', () => {
  // 开：每次打开重置（标题/详情/到期日清空；归属保留上一次选的）
  assert.match(
    DIALOG,
    /watch\(\s*\(\) => props\.modelValue,[\s\S]{0,200}blankForm\(form\.owner\)/,
    '打开弹框时要重置表单',
  )
  assert.match(DIALOG, /const form = reactive\(blankForm\(\)\)/, '表单状态在弹框里')

  // 拦：空标题**前端先拦**（真函数口径 + 弹框里那道守卫；不满足就不 emit）
  assert.equal(todoFormError({ title: '' }), '标题不能为空')
  assert.equal(todoFormError({ title: '   ' }), '标题不能为空')
  assert.equal(todoFormError({ title: '写周报' }), '')
  assert.match(DIALOG, /const err = todoFormError\(form\)[\s\S]{0,160}return/, '空标题要先拦')
  assert.match(DIALOG, /emit\('submit', todoPayload\(form\)\)/, '校验过了才 emit')
  assert.match(DIALOG, /:disabled="!form\.title\.trim\(\)"/, '空标题时提交钮禁用')

  // 提交：打后端 → 关框 → toast → 刷新列表（顺序也要对）
  const body = VIEW.match(/async function create\(payload\)[\s\S]*?\n}/)[0]
  const iCreate = body.indexOf('api.createTodo')
  const iClose = body.indexOf('createOpen.value = false')
  const iToast = body.indexOf('showToast')
  const iRefresh = body.indexOf('refresh()')
  assert.ok(iCreate >= 0, '提交要打后端')
  assert.ok(iClose > iCreate, '提交成功后先关框')
  assert.ok(iToast > iClose, '关框后 toast')
  assert.ok(iRefresh > iToast, '最后刷新列表')
})

test('② 载荷口径：标题去掉首尾空白，owner/due 空串 = 交给主代理 / 不限期', () => {
  assert.deepEqual(
    todoPayload({ title: '  写周报  ', detail: '细节', owner: 'w1', due: '2026-10-30' }),
    { title: '写周报', detail: '细节', owner: 'w1', due: '2026-10-30' },
  )
  assert.deepEqual(blankForm('w1'), { title: '', detail: '', owner: 'w1', due: '' })
  assert.deepEqual(blankForm(), { title: '', detail: '', owner: '', due: '' })
})

test('③ 按钮防窄卡折字：操作行 nowrap + 容器 flex-wrap（插槽内容用 :deep）', () => {
  // 卡内操作行：按钮不许折字，容器放不下就整组换行
  assert.match(VIEW, /\.card-link,\s*\.act-btn\s*\{[^}]*white-space: nowrap/, '操作行按钮要 nowrap')
  assert.match(VIEW, /\.card-foot\s*\{[^}]*flex-wrap: wrap/, '操作行容器要 flex-wrap')
  assert.match(VIEW, /\.card-acts\s*\{[^}]*flex-wrap: wrap/, '按钮组容器要 flex-wrap')
  // SelectionBar 的插槽内容在子组件里 —— 父页 scoped 够不着，必须 :deep
  assert.match(VIEW, /:deep\(\.sel-acts \.el-button\)\s*\{[^}]*white-space: nowrap/, '插槽按钮要 :deep + nowrap')
  assert.match(DIALOG, /:deep\(\.el-button\)\s*\{[^}]*white-space: nowrap/, '弹框按钮也要 nowrap')
})

test('danger 操作二次确认：删除走手输标题；批量标完成不算 danger，不弹框', () => {
  assert.match(VIEW, /showPrompt/, '删除要二次确认')
  assert.match(VIEW, /expect: row\.title/, '删除的手输串 = 标题全文')
  const batch = VIEW.match(/async function batchDone\(\)[\s\S]*?\n}/)[0]
  assert.doesNotMatch(batch, /showConfirm/, '批量标完成不是危险动作，不弹确认框')
  assert.match(batch, /setTodoStatus\(id, 'done'\)/, '批量标完成仍逐条改状态')
})
