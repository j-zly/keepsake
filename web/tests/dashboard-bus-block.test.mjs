/**
 * 看板（Dashboard / StatsView）上的调度展示面已摘除（dashboard_bus_block_remove）。
 *
 * 背景：「定时任务」独立成页之后，看板上的那一块调度展示是**重复展示面**，
 * 而且它的文案还停在壳时代（脚本位于 /root/.hermes/scripts/）—— 两处各写一份
 * 真相必漂。主脑拍板：摘除，指向单一事实源（「定时任务」页）。
 *
 * 这类改动的静默失败模式不是「报错」，而是「悄悄又长回来」：看板重新调一次
 * /crons、或那行指路被改回旧文案，页面看着正常、真相却开始漂。所以逐条钉死：
 *   D1 看板模板不再出现那一块的标题字样与壳时代的脚本路径文案；
 *   D2 该块的**专属取数与操作逻辑**一并清干净（不是只删模板留半截死代码）；
 *   D3 原位留一行轻提示：写明调度归「定时任务」页，且带跳 /scheduler 的链接，
 *      样式沿用既有的次要文本类（不许新造配色）；
 *   D4 后端 API 面零改动：/crons 端点不动，只是看板不再调它；
 *   D5 别把「分级班车」那张水位卡跟着误删 —— 它是水位，不是调度展示面。
 *
 * 范围：只盯 web 端（看板组件 + 路由 + api 客户端），后端一行没动。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const STATS = read('views/StatsView.vue')
const ROUTER = read('router/index.js')
const API = read('api/index.js')

/** 剥掉注释再查红线 —— 否则本文件自己的注释就能把护栏顶红。 */
const noComment = (s) => s.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '')

test('D1 看板不再渲染那一块调度展示面（标题字样与壳时代文案都没了）', () => {
  assert.doesNotMatch(STATS, /记忆类定时任务/, '看板不许再出现这一块的标题')
  assert.doesNotMatch(STATS, /\/root\/\.hermes\/scripts/, '壳时代的脚本路径文案不许留在看板上')
  assert.doesNotMatch(STATS, /jobs\.json/, '调度文件路径只属于「定时任务」页')
  // 提示行本身要在（下一条钉），但**不许**又长出一整块调度卡片
  assert.doesNotMatch(noComment(STATS), /class="card-grid/, '看板不许再摆调度卡网格')
})

test('D2 该块的专属取数与操作逻辑一并清干净（不留半截死代码）', () => {
  // 取数：看板不再调 /crons，也不再持有那批状态变量
  assert.doesNotMatch(STATS, /api\.crons\(/, '看板不再读调度列表')
  for (const dead of ['crons', 'schedulerStatus', 'cronError', 'schedLabel', 'busyCron',
    'toggleAsk', 'editAsk', 'newSchedule', 'askToggle', 'doToggle', 'openEdit', 'doEdit']) {
    assert.doesNotMatch(STATS, new RegExp(`\\b${dead}\\b`), `死变量 ${dead} 要连模板带逻辑一起删`)
  }
  // 那个块专用的组件与工具也一并收掉（别留没用的 import）
  for (const gone of ['ItemCard', 'ConfirmDialog', 'copyText', 'utils/dialog']) {
    assert.doesNotMatch(STATS, new RegExp(gone), `看板不再有 ${gone} 的用处`)
  }
  // 看板是只读面：写操作一个都不许留
  assert.doesNotMatch(STATS, /api\.(post|put|delete|patch)\(/, '看板不许直连写接口')
})

test('D3 原位一行轻提示：写明归「定时任务」页，并带跳 /scheduler 的链接', () => {
  assert.match(STATS, /班车调度与执行统一在「定时任务」页管理/, '指路文案要在')
  assert.match(STATS, /<router-link class="card-link" to="\/scheduler">/, '要带跳 /scheduler 的链接')
  // 链接目标真的存在（点了不许落到 404）
  assert.match(ROUTER, /\{ path: '\/scheduler', name: 'scheduler', component: SchedulerView/)
  // 样式沿用既有类：不新造配色 ⇒ 提示行不许出现硬编码色值
  assert.match(STATS, /<p class="src cron-hint">/, '沿用既有的次要文本类 .src')
  const hint = STATS.slice(STATS.indexOf('class="src cron-hint"'), STATS.indexOf('class="src cron-hint"') + 300)
  assert.doesNotMatch(hint, /#[0-9a-f]{3,8}|rgb\(/i, '提示行不许硬编码颜色')
})

test('D4 后端 API 面零改动：/crons 端点不动，只是看板不再调', () => {
  // api 客户端的方法一个都不许摘（定时任务页还在用）
  for (const m of ['crons:', 'cronPause:', 'cronResume:', 'cronEdit:', 'cronCreate:', 'cronUpdate:',
    'cronTrigger:', 'cronDelete:', 'cronRuns:']) {
    assert.match(API, new RegExp(m.replace(':', '\\s*:')), `api 客户端要保留 ${m}`)
  }
})

test('D5 「分级班车」那张水位卡不许跟着被误删（它是水位，不是调度展示面）', () => {
  assert.match(STATS, /<h3 class="section-title">分级班车<\/h3>/, '水位卡留着')
  assert.match(STATS, /api\.tieringStatus\(\)/, '水位数据照旧读')
})
