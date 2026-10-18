/**
 * 「常驻层分级班车」的前端护栏。
 *
 * 三块接线，缺一块都是「静默做错事」：
 *   1) **方向徽标**：晋升候选（core_candidate）与退居建议（tier:demote-candidate）
 *      各查各的标签、批准的动正好相反（promote / demote）、确认文案按方向切 ——
 *      混起来的后果是「审核人看着像升常驻，按下去是把常驻降了」。
 *      （审核独立成页后这两条在 ReviewView；班车设置弹框留在 MaintenanceView。）
 *   2) **看板卡片**：读 `GET /api/v1/tiering/status`，水位 ≥90% accent / ≥100% danger
 *      （复用 StatCard 的 tone），且「默认关」必须**显示出来**，不能藏在代码里。
 *   3) **交互红线**：设置走 ConfirmDialog / utils/dialog，没有裸 alert/confirm；
 *      confirm 串是当前生效的 'true'/'false'（改的是全局开关，防手滑）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const REVIEW = readFileSync(fileURLToPath(new URL('../src/views/ReviewView.vue', import.meta.url)), 'utf8')
const MAINT = readFileSync(fileURLToPath(new URL('../src/views/MaintenanceView.vue', import.meta.url)), 'utf8')
const STATS = readFileSync(fileURLToPath(new URL('../src/views/StatsView.vue', import.meta.url)), 'utf8')
const API = readFileSync(fileURLToPath(new URL('../src/api/index.js', import.meta.url)), 'utf8')

test('两个方向各查各的标签：晋升=core_candidate，退居=tier:demote-candidate', () => {
  assert.match(
    REVIEW,
    /const DEMOTE_CANDIDATE_TAG = 'tier:demote-candidate'/,
    '退居建议标签常量不见了',
  )
  assert.match(
    REVIEW,
    /const dirTag = computed\(\(\) => \(candDir\.value === 'demote' \? DEMOTE_CANDIDATE_TAG : CANDIDATE_TAG\)\)/,
  )
  assert.match(REVIEW, /api\.fragments\(\{\s*tag: dirTag\.value/, '列表必须按方向标签查')
  // 方向切换要回到第 1 页并清空计数，否则从第 5 页切过去是空列表
  assert.match(
    REVIEW,
    /function onCandDir\(\) \{\s*candPage\.value = 1\s*candCount\.value = null\s*cands\.value = \[\]\s*loadCandidates\(\)/,
  )
})

test('退居方向的「批准」= demote，「驳回」= 只摘建议标', () => {
  assert.match(REVIEW, /const r = await api\.demote\(row\.key\)/, '退居方向批准必须走 demote')
  assert.match(
    REVIEW,
    /api\.demote\(row\.key\)\s*await api\.updateFragment\(row\.key, \{ tags: dropTag\(r\.tags, DEMOTE_CANDIDATE_TAG\) \}\)/,
    'demote 之后必须摘掉建议标，否则卡片赖在待审列表里',
  )
  // 晋升方向的老纪律不许被改掉：先 promote 再摘标
  assert.match(
    REVIEW,
    /const r = await api\.promote\(row\.key\)\s*await api\.updateFragment\(row\.key, \{ tags: dropTag\(r\.tags, CANDIDATE_TAG\) \}\)/,
  )
  assert.ok(
    !/api\.archive\(|api\.deleteFragment\(/.test(REVIEW),
    '退常驻不许顺带归档/真删：那是降常驻端点自己的语义',
  )
})

test('确认框文案按方向切，不许把降常驻写成升常驻', () => {
  assert.match(REVIEW, /candDir\.value === 'demote' \? '批准这条退出常驻层？' : '批准这条进常驻层？'/)
  assert.match(REVIEW, /return candDir\.value === 'demote' \? '批准退常驻' : '批准入常驻'/)
  // 后果说明里必须有「正文不删、条目不失效」这句（退常驻最怕被误读成删除）
  assert.match(REVIEW, /正文不删、条目不失效 —— 它回到检索层/)
})

test('看板：读 status 端点 + 水位配色 + 默认关要显示', () => {
  assert.match(API, /tieringStatus: \(\) => http\.get\('\/api\/v1\/tiering\/status'\)/)
  assert.match(STATS, /api\.tieringStatus\(\)/)
  assert.match(STATS, /if \(r >= 100\) return 'danger'/)
  assert.match(STATS, /if \(r >= 90\) return 'accent'/)
  // 「默认关」不是注释里的悄悄话，页面上要看得见
  assert.match(STATS, /tiering\.enabled \? '已开' : '默认关'/)
  assert.match(STATS, /班车还没跑过/)
})

test('设置弹框：只动 tiering 一节 + confirm 是当前生效 enabled', () => {
  assert.match(API, /setTiering: \(tiering, confirm\) =>\s*http\.put\('\/api\/v1\/settings\/tiering', \{ tiering, confirm \}\)/)
  assert.match(MAINT, /api\.setTiering\(tDraft\.value, String\(!!tStatus\.value\?\.enabled\)\)/)
  assert.match(
    MAINT,
    /String\(tConfirm\.value\)\.trim\(\) === String\(!!tStatus\.value\?\.enabled\)/,
    'confirm 必须逐字等于当前生效 enabled（后端同一口径）',
  )
  // 弹框不许点遮罩关闭（半路改坏阈值）
  assert.match(MAINT, /v-model="tieringOpen"[\s\S]*?:close-on-click-modal="false"/)
})

test('交互红线：三个页面都没有裸 alert / confirm', () => {
  for (const [name, src] of [['ReviewView', REVIEW], ['MaintenanceView', MAINT], ['StatsView', STATS]]) {
    const bare = src.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
    assert.deepEqual(bare, [], `${name} 必须走 ConfirmDialog / utils/dialog`)
  }
})
