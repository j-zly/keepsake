/**
 * 定时任务面板（看板 StatsView）接线护栏
 * （keepsake_cron_panel，纯静态扫描）：
 *   1) 任务卡必须显示**下次执行时间**（面板原先只给 name/schedule/last_status，
 *      用户看不到「什么时候跑」），且走 utils/format 的 toLocal（后端给 UTC，
 *      前端转本地）——不许裸 new Date()；
 *   2) last_run 保留，不许被 next_run 顶掉；
 *   3) 面板是**只读**的：本单只加字段，不许在 StatsView 里对 jobs.json 发写请求。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const STATS = read('views/StatsView.vue')

test('任务卡时间位显示 next_run', () => {
  assert.match(STATS, /row\.next_run/, '必须读 next_run 字段')
  assert.match(STATS, /下次 \{\{ toLocal\(row\.next_run\) \}\}/, '必须本地化显示下次执行时间')
})

test('时间位走 toLocal，不许裸 new Date', () => {
  assert.match(STATS, /import \{ toLocal \} from '\.\.\/utils\/format'/, '复用现成 toLocal')
  const body = STATS.slice(STATS.indexOf('<template #time>'))
  assert.doesNotMatch(body.slice(0, 200), /new Date\(/, '时间格式化交给 toLocal，别自己造时区')
})

test('last_run 保留', () => {
  assert.match(STATS, /上次 \{\{ toLocal\(row\.last_run_at\) \}\}/, '上次执行时间不许被顶掉')
})

test('面板只读：不许对 crons 发写方法', () => {
  const calls = STATS.match(/api\.(post|put|delete|patch)\(\s*`?'?crons/g) || []
  assert.equal(calls.length, 0, '面板只读，启停管理是后话')
})
