/**
 * 「定时任务」独立页（scheduler_page_v2）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）：这一页能跑的是「结构在不在」
 * 与「文案对不对」—— 定时任务的静默失败模式不是报错，而是「按钮看着能点，
 * 什么也没发生」或「删了的东西重启又回来」。
 *
 * 钉死的六件事：
 *   S1 路由有 /scheduler，侧栏有「定时任务」入口（紧邻记忆维护）；
 *   S2 增 / 改 / 暂停 / 恢复 / 运行一次 / 删除 六个动作都在页面上；
 *   S3 **确认弹框不要求键入任何东西**（ConfirmDialog 不传 expect ⇒ 不渲染 input）；
 *   S4 删除弹框写明「重启后不会复活」与「可重新添加」（后果要写清）；
 *   S5 维护页的两张班车卡**搬干净了**（标记类名不许还在），并留了指路链接；
 *   S6 无裸 alert / confirm（提示一律走 utils/dialog 与 ConfirmDialog）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const ROUTER = read('router/index.js')
const APP = read('App.vue')
const API = read('api/index.js')
const SCHED = read('views/SchedulerView.vue')
const MAINT = read('views/MaintenanceView.vue')

/** 剥掉注释再查红线 —— 否则本文件自己的注释就能把护栏顶红。 */
const noComment = (s) => s.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '')

test('S1 路由有 /scheduler，侧栏有「定时任务」入口', () => {
  assert.match(ROUTER, /import SchedulerView from '\.\.\/views\/SchedulerView\.vue'/)
  assert.match(ROUTER, /\{ path: '\/scheduler', name: 'scheduler', component: SchedulerView/)
  assert.match(APP, /\{ path: '\/scheduler', label: '定时任务', icon: ICON\.\w+ \}/)
  // 位置：紧邻「记忆维护」（班车管理与记忆维护相邻）
  const maint = APP.indexOf("path: '/maintenance'")
  const sched = APP.indexOf("path: '/scheduler'")
  assert.ok(maint >= 0 && sched > maint, '「定时任务」要排在「记忆维护」之后')
})

test('S2 六个动作都在页面上（增 / 改 / 暂停 / 恢复 / 运行一次 / 删除）', () => {
  assert.match(SCHED, /添加任务/, '页头要有「添加任务」')
  assert.match(SCHED, /@click="openEdit\(row\)"/, '行操作要有「编辑」')
  assert.match(SCHED, /askToggle\(row\)/, '行操作要有暂停 / 恢复')
  assert.match(SCHED, /askTrigger\(row\)/, '行操作要有「运行一次」')
  assert.match(SCHED, /askDelete\(row\)/, '行操作要有「删除」')
  assert.match(SCHED, /openRuns\(row\)/, '要能看执行记录')
  // 后端五条写端点都接上了（API 层一个都不许漏）
  assert.match(API, /cronCreate: \(payload\) =>/)
  assert.match(API, /cronUpdate: \(jobId, payload\) =>/)
  assert.match(API, /cronDelete: \(jobId\) =>/)
  assert.match(API, /cronTrigger: \(jobId\) =>/)
  assert.match(API, /cronRuns: \(jobId\) =>/)
})

test('S3 确认弹框不要求键入（ConfirmDialog 不传 expect ⇒ 页面里没有输入框）', () => {
  assert.match(SCHED, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/,
    '复用现成确认组件')
  // 🔴 ConfirmDialog 只有 expect 非空才渲染 <input> —— 不传 expect 就是「不键入」
  assert.doesNotMatch(SCHED, /:expect=/, '确认弹框不许要求键入确认串')
  assert.doesNotMatch(SCHED, /showPrompt\(/, '不许用手输确认串的弹框')
  assert.doesNotMatch(SCHED, /confirmExpect/, '不许留手输确认串的状态')
})

test('S4 删除的后果要写清：重启后不会复活 + 可重新添加', () => {
  assert.match(SCHED, /删除不会保留，可重新添加。/, '删除弹框要写明「可重新添加」')
  assert.match(SCHED, /重启后不会复活/, '删除弹框要写明「重启后不会复活」')
  // 触发一次的后果也要写明（它不动墓碑、只跑一次）
  assert.match(SCHED, /立刻执行一次（不影响原有的按点调度）。/)
})

test('S5 维护页的两张班车卡搬干净了（标记类名不许还在），并留了指路', () => {
  assert.doesNotMatch(MAINT, /ops-card/, '维护页不许还留班车卡的类名')
  assert.doesNotMatch(MAINT, /ops-grid/, '维护页不许还留班车卡的网格容器')
  assert.doesNotMatch(MAINT, /class="ops"/, '维护页不许还留班车卡的按钮行')
  assert.match(MAINT, /to="\/scheduler"/, '维护页要留一句指向「定时任务」页的链接')
  // 迁走的三块活儿在定时任务页上（功能不得丢）
  for (const s of ['合并 · dry-run', '遗忘 · dry-run', 'runMerge', 'runForget', '分级班车设置']) {
    assert.ok(SCHED.includes(s), `SchedulerView 少了「${s}」`)
  }
})

test('S6 无裸 alert / confirm', () => {
  const bare = SCHED.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '禁原生弹框，统一走 utils/dialog 与 ConfirmDialog')
  assert.match(SCHED, /import \{ showError, showToast \} from '\.\.\/utils\/dialog'/)
})
