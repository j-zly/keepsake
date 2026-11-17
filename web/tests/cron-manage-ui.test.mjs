/**
 * 定时任务「可管理」的前端接线护栏。
 *
 * 看板上那块调度展示面已摘除（dashboard_bus_block_remove），调度管理的**正源**
 * 现在是「定时任务」页（SchedulerView）—— 这些护栏跟着搬家，盯的还是同一件事：
 * 暂停 / 恢复 / 改调度的静默失败模式都不是「报错」，而是「按钮看着能点，
 * 什么也没发生」，所以逐条钉死：
 *   1) 三个 API 方法齐全，且**都显式带 confirm: true**（后端不放行漏确认的写）；
 *   2) 暂停 / 恢复走 ConfirmDialog 且按 enabled 切文案与方向，**成功后重拉列表**
 *      （启停状态由 jobstore 说了算，本地改一改很快就和真实调度对不上）；
 *   3) 改调度弹框里能改 cron 表达式，后端 400 的校验原文回显在弹框内；
 *   4) 无裸 alert / confirm，提示一律走 utils/dialog；
 *   5) **看板不许再摆一份管理入口** —— 调度归调度，单一事实源。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const SCHED = read('views/SchedulerView.vue')
const STATS = read('views/StatsView.vue')
const API = read('api/index.js')

/** 剥掉注释再查文案类红线 —— 否则本文件自己的注释就能把护栏顶红。 */
const noComment = (s) => s.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '')

test('三个 API 方法齐全，且都显式带 confirm', () => {
  assert.match(API, /cronPause: \(jobId\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/pause`, \{ confirm: true \}\)/)
  assert.match(API, /cronResume: \(jobId\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/resume`, \{ confirm: true \}\)/)
  assert.match(API, /cronEdit: \(jobId, schedule\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/edit`, \{ confirm: true, schedule \}\)/)
})

test('任务行按 enabled 给「暂停」或「恢复」，且都走确认框', () => {
  assert.match(SCHED, /\{\{ row\.enabled \? '暂停' : '恢复' \}\}/, '运行中显示暂停、已停用显示恢复')
  assert.match(SCHED, /function askToggle\(row\)/, '必须先开确认框再发请求')
  assert.match(SCHED, /<ConfirmDialog[\s\S]*?@confirm="doConfirm"/, '启停要过 ConfirmDialog')
  assert.match(SCHED, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/, '复用现成确认组件')
})

test('确认框的后果按方向给，且不是一句「确定吗」', () => {
  assert.match(SCHED, /'暂停后不再按计划执行/, '暂停的后果要写明「不再按计划执行」')
  assert.match(SCHED, /'恢复后按当前调度继续执行/, '恢复的后果要写明「按当前调度继续」')
  assert.doesNotMatch(noComment(SCHED), /确定吗/, '禁止无信息量的确认文案')
})

test('启停成功后重拉列表', () => {
  const body = SCHED.slice(SCHED.indexOf('async function doConfirm'))
  assert.match(body.slice(0, 600), /await load\(\)/, '启停后必须重拉，状态由 jobstore 说了算')
})

test('改调度：表达式可输入，错误回显进弹框，后端透传', () => {
  assert.match(SCHED, /v-model="form\.schedule"/, '新调度可输入')
  assert.match(SCHED, /formError\.value = e\.message/, '后端 400 的原文回显进弹框')
  assert.match(SCHED, /v-if="formError"/, '错误要真的显示出来')
  assert.match(SCHED, /await api\.cronUpdate\(f\.id, payload\)/, '改调度走后端透传，前端不自己判 cron')
})

test('读面带调度器状态，无 CLI 时退空串而不是崩', () => {
  assert.match(SCHED, /schedulerStatus\.value = res\?\.scheduler_status\?\.text \|\| ''/, 'null 要退成空串')
  assert.match(SCHED, /schedulerStatus\.value\.includes\('调度器停止'\)/, '调度器停摆要在页头报警')
})

test('无裸 alert / confirm', () => {
  assert.doesNotMatch(SCHED, /[^.\w](alert|confirm)\s*\(/, '禁原生弹框，统一走 utils/dialog 与 ConfirmDialog')
  assert.match(SCHED, /import \{ showError, showToast \} from '\.\.\/utils\/dialog'/, '提示走现成的 dialog 工具')
})

test('单一事实源：看板不许再摆一份调度管理入口', () => {
  for (const gone of ['askToggle', 'doToggle', 'cronPause', 'cronResume', 'cronEdit', 'cronUpdate']) {
    assert.doesNotMatch(STATS, new RegExp(`\\b${gone}\\b`), `看板不该再有 ${gone}`)
  }
})
