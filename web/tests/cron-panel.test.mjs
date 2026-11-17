/**
 * 定时任务读面（面板）的接线护栏。
 *
 * 看板上的那块调度展示面已摘除（dashboard_bus_block_remove），调度读面的**正源**
 * 现在是「定时任务」页（SchedulerView）—— 护栏跟着搬家，盯的还是同一件事：
 *   1) 任务行必须显示**下次执行时间**（用户看不到「什么时候跑」就没法安排），
 *      且走 utils/format 的 toLocal（后端给 UTC，前端转本地）——不许裸 new Date()；
 *   2) 「跑过什么」的账不许丢：执行记录入口在（后端有 /crons/{id}/runs）；
 *   3) 写操作一律走 api.cronXxx（都带 confirm: true），不许裸 api.post(`crons…`)。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const SCHED = read('views/SchedulerView.vue')
const API = read('api/index.js')

test('任务行显示 next_run', () => {
  assert.match(SCHED, /row\.next_run/, '必须读 next_run 字段')
  assert.match(SCHED, /\{\{ row\.next_run \? toLocal\(row\.next_run\) : '—' \}\}/, '必须本地化显示下次执行时间')
})

test('时间位走 toLocal，不许裸 new Date', () => {
  assert.match(SCHED, /import \{ toLocal \} from '\.\.\/utils\/format'/, '复用现成 toLocal')
  const body = SCHED.slice(SCHED.indexOf('col-next'))
  assert.doesNotMatch(body.slice(0, 200), /new Date\(/, '时间格式化交给 toLocal，别自己造时区')
})

test('「跑过什么」的账没丢：执行记录入口在', () => {
  assert.match(SCHED, /@click="openRuns\(row\)"/, '行里要有执行记录入口')
  assert.match(SCHED, /await api\.cronRuns\(row\.job_id\)/, '执行记录走后端 /crons/{id}/runs')
  assert.match(API, /cronRuns: \(jobId\) =>\s*http\.get\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/runs`\)/)
})

test('写操作一律走 api.cronXxx，不许裸 api.post(`crons…`）', () => {
  const calls = SCHED.match(/api\.(post|put|delete|patch)\(\s*`?'?crons/g) || []
  assert.equal(calls.length, 0, '统一走 api 客户端里那批带 confirm 的方法')
})
