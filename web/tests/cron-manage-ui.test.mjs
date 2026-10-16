/**
 * 定时任务「可管理」（keepsake_cron_manage）的前端接线护栏。
 *
 * 看板的任务卡从只读升级为能暂停 / 恢复 / 改调度，这三件事的静默失败模式
 * 都不是「报错」，而是「按钮看着能点，什么也没发生」，所以逐条钉死：
 *   1) 三个 API 方法齐全，且**都显式带 confirm: true**（后端不放行漏确认的写）；
 *   2) 暂停 / 恢复走 ConfirmDialog 且按 enabled 切文案与方向，**成功后重拉列表**
 *      （启停状态由 jobs.json 说了算，本地改一改很快就和真实调度对不上）；
 *   3) 改调度弹框里必须能看见「旧值 → 新值」，后端 400 的校验原文回显在弹框内；
 *   4) 管理按钮**只对有 job_id 的行渲染** —— 认不出的行保持只读，
 *      不去对一个没读懂的 jobs.json 发写请求；
 *   5) 无裸 alert / confirm，提示一律走 utils/dialog。
 *
 * 注：cron-panel.test.mjs 那条「面板只读」护栏针对的是 StatsView 里直接调
 * 写方法的名字（api.post(`crons…`），本单的三条走 api.cronPause/Resume/Edit，
 * 那条断言仍然成立；真正的形状由本文件盯。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const STATS = read('views/StatsView.vue')
const API = read('api/index.js')

/** 剥掉注释再查文案类红线 —— 否则本文件自己的注释就能把护栏顶红。 */
const noComment = (s) => s.replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '')

test('三个 API 方法齐全，且都显式带 confirm', () => {
  assert.match(API, /cronPause: \(jobId\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/pause`, \{ confirm: true \}\)/)
  assert.match(API, /cronResume: \(jobId\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/resume`, \{ confirm: true \}\)/)
  assert.match(API, /cronEdit: \(jobId, schedule\) =>\s*http\.post\(`\/api\/v1\/crons\/\$\{encodeURIComponent\(jobId\)\}\/edit`, \{ confirm: true, schedule \}\)/)
})

test('任务卡按 enabled 给「暂停」或「恢复」，且都走确认框', () => {
  assert.match(STATS, /\{\{ row\.enabled \? '暂停' : '恢复' \}\}/, '运行中显示暂停、已停用显示恢复')
  assert.match(STATS, /function askToggle\(r\)/, '必须先开确认框再发请求')
  assert.match(STATS, /<ConfirmDialog[\s\S]*?@confirm="doToggle"/, '启停要过 ConfirmDialog')
  assert.match(STATS, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/, '复用现成确认组件')
})

test('确认框的后果按方向给，且不是一句「确定吗」', () => {
  assert.match(STATS, /pausing\.value \? '暂停后不再按计划执行/, '暂停的后果要写明「不再按计划执行」')
  assert.match(STATS, /'恢复后按当前调度继续执行'/, '恢复的后果要写明「按当前调度继续」')
  assert.doesNotMatch(noComment(STATS), /确定吗/, '禁止无信息量的确认文案')
})

test('启停成功后重拉列表', () => {
  const body = STATS.slice(STATS.indexOf('async function doToggle'))
  assert.match(body.slice(0, 600), /await load\(\)/, '启停后必须重拉，状态由 jobs.json 说了算')
})

test('改调度：旧值在正文可见，输入新值，错误回显进弹框', () => {
  assert.match(STATS, /class="edit-old mono">\{\{ row\?\.schedule \|\| '（无）' \}\}/, '确认前要看见旧值')
  assert.match(STATS, /v-model="newSchedule"/, '新调度可输入')
  assert.match(STATS, /editError\.value = e\.message/, '后端 400 的原文回显进弹框')
  assert.match(STATS, /v-if="editError"/, '错误要真的显示出来')
  assert.match(STATS, /await api\.cronEdit\(r\.job_id, expr\)/, '改调度走后端透传，前端不自己判 cron')
})

test('管理按钮只对认得出 job_id 的行出现', () => {
  const acts = STATS.slice(STATS.indexOf('<template #actions>'))
  const hits = acts.match(/v-if="row\.job_id"/g) || []
  assert.equal(hits.length, 2, '暂停 / 改调度两个按钮都要挂在 job_id 闸门上')
})

test('读面带调度器状态，无 CLI 时退「未知」而不是崩', () => {
  assert.match(STATS, /schedulerStatus\.value = c\.scheduler_status \|\| ''/, 'null 要退成空串')
  assert.match(STATS, /\{\{ schedulerStatus \|\| '未知' \}\}/, '空状态显示「未知」')
})

test('无裸 alert / confirm', () => {
  const page = STATS
  assert.doesNotMatch(page, /[^.\w](alert|confirm)\s*\(/, '禁原生弹框，统一走 utils/dialog 与 ConfirmDialog')
  assert.match(page, /import \{ showError, showToast \} from '\.\.\/utils\/dialog'/, '提示走现成的 dialog 工具')
})
