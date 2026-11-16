/**
 * 「定时任务」独立页（scheduler_page_v2 / scheduler_page_ui_fix）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）：这一页能跑的是「结构在不在」
 * 与「文案对不对」—— 定时任务的静默失败模式不是报错，而是「按钮看着能点，
 * 什么也没发生」或「删了的东西重启又回来」。
 *
 * 钉死的八件事：
 *   S1 路由有 /scheduler，侧栏有「定时任务」入口（紧邻记忆维护）；
 *   S2 增 / 改 / 暂停 / 恢复 / 运行一次 / 删除 六个动作都在页面上；
 *   S3 **确认弹框不要求键入任何东西**（ConfirmDialog 不传 expect ⇒ 不渲染 input）；
 *   S4 删除弹框写明「重启后不会复活」与「可重新添加」（后果要写清）；
 *   S5 两页职责边界：**定时页只管调度**（合并 / 遗忘那套记忆业务操作零残留），
 *      合并 / 遗忘的实跑卡在维护页（scheduler_page_ui_fix 迁回）；
 *   S6 无裸 alert / confirm（提示一律走 utils/dialog 与 ConfirmDialog）；
 *   S7 操作列**五枚按钮一行排齐**（不许 3+2 换行），分级那行的「设置」跟着那一行走；
 *   S8 状态列**只有徽标**（没有按钮），色彩语义：破坏性=红 / 触发执行=主色 / 查看=中性描边。
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

/** 任务行模板段（从 v-for 到列表结束）——表头在它上面，不混进来。 */
const JOB_ROWS = SCHED.slice(SCHED.indexOf('v-for="row in jobs"'), SCHED.indexOf('<!-- 新建'))

/** 行里的某一段（按 8 空格缩进的闭合标签切，chip 那种嵌套不会切错）。 */
const cell = (tag) => {
  const start = JOB_ROWS.indexOf(`<span class="${tag}">`)
  const end = JOB_ROWS.indexOf('\n        </span>', start)
  return JOB_ROWS.slice(start, end)
}

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

test('S5 两页职责边界：定时页零残留，维护页有实跑卡', () => {
  // 定时任务页 = 班车调度管理：合并 / 遗忘那套**记忆业务操作**不许留在这儿
  for (const s of ['runMerge', 'runForget', '合并 · 预演', '遗忘 · 预演', '合并 · 实跑']) {
    assert.ok(!SCHED.includes(s), `SchedulerView 还留着「${s}」—— 合并 / 遗忘已迁回维护页`)
  }
  assert.doesNotMatch(SCHED, /ops-card/, '定时任务页不许还留操作卡的类名')
  assert.doesNotMatch(SCHED, /api\.merge\(|api\.forget\(/, '定时任务页不许还调合并 / 遗忘端点')
  // 维护页 = 记忆业务操作：实跑卡在这儿，且实跑走简单确认弹框
  for (const s of ['runMerge', 'runForget', '合并 · 预演', '遗忘 · 预演', '合并 · 实跑', '遗忘 · 实跑']) {
    assert.ok(MAINT.includes(s), `MaintenanceView 少了「${s}」`)
  }
  assert.match(MAINT, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/,
    '实跑确认框复用现成组件')
  assert.doesNotMatch(MAINT, /:expect=/, '实跑确认不许键入任何字符（confirm:true 即过）')
  assert.match(MAINT, /confirm: true/, '实跑只带 confirm=true')
  // 分级班车的设置入口留在定时页（跟着它自己那一行走）
  assert.match(SCHED, /分级班车设置/)
  assert.ok(!MAINT.includes('tieringOpen'), '分级班车设置弹框不在本页')
  // 维护页要留一句指向「定时任务」页的链接（班车调度在那儿）
  assert.match(MAINT, /to="\/scheduler"/, '维护页要留一句指向「定时任务」页的链接')
})

test('S6 无裸 alert / confirm', () => {
  const bare = SCHED.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '禁原生弹框，统一走 utils/dialog 与 ConfirmDialog')
  assert.match(SCHED, /import \{ showError, showToast \} from '\.\.\/utils\/dialog'/)
})

test('S7 操作列：五枚常驻按钮一行排齐（不许 3+2 换行）', () => {
  // 行里的操作列：编辑 / 暂停 / 运行一次 / 执行记录 / 删除 五枚常驻，
  // 外加分级那行才出现的第六枚「设置」（v-if 收着）。
  const buttons = [...cell('col-acts').matchAll(/<el-button\b[^>]*>/g)].map((m) => m[0])
  const always = buttons.filter((b) => !b.includes('v-if'))
  assert.equal(always.length, 5, `常驻按钮应是 5 枚（现在是 ${always.length} 枚）`)
  assert.equal(buttons.length, 6, `连分级那行的「设置」一共 6 枚（现在是 ${buttons.length} 枚）`)
  // 一行排齐：nowrap（不许换行）+ 列宽够放六枚
  assert.match(SCHED, /\.col-acts\s*\{[^}]*flex-wrap:\s*nowrap/, '操作列不许换行')
  const cols = SCHED.match(/grid-template-columns:\s*([^;]+);/)
  const actsWidth = Number(String(cols[1]).trim().split(/\s+/).pop().replace('px', ''))
  assert.ok(actsWidth >= 280, `操作列宽度 ${actsWidth}px < 280px（放不下五枚按钮）`)
  // 收纳策略：分级那行的「设置」是**第六枚平铺按钮**，不许另开一列
  assert.match(cell('col-acts'), /v-if="isTiering\(row\)"[^>]*@click="openTiering"/, '「设置」跟着分级那行走')
})

test('S8 状态列只有徽标；色彩语义：破坏性=红 / 触发执行=主色 / 查看=中性描边', () => {
  const state = cell('col-state')
  assert.doesNotMatch(state, /el-button/, '状态列里不许有按钮')
  assert.match(state, /运行中/, '状态列要有运行中徽标')
  assert.match(state, /已暂停/, '状态列要有已暂停徽标')
  assert.match(state, /分级已开/, '分级那行要有分级徽标')
  // 色彩：删除=红、运行一次=主色、编辑 / 执行记录=中性描边（没有 type）
  assert.match(SCHED, /type="danger"[^>]*@click="askDelete\(row\)"/, '删除必须是红色（破坏性）')
  assert.match(SCHED, /type="primary"[^>]*@click="askTrigger\(row\)"/, '运行一次必须是主色（触发执行）')
  assert.match(SCHED, /<el-button size="small" @click="openEdit\(row\)">编辑/, '编辑是中性描边')
  assert.match(SCHED, /<el-button size="small" @click="openRuns\(row\)">执行记录/, '执行记录是中性描边')
})
