/**
 * 「多代理维度」的前端护栏（看板 / 常驻层 / 记忆维护三页同一份口径）。
 *
 * 钉住三件事：
 *   1) **看板代理卡**：在册 / 已批准 / 在线三个数都在，「在线」的口径**写在卡上**
 *      （注册表 last_seen_at 5 分钟内）——「在线」是个没人知道怎么算的词，
 *      口径不写出来就等于编；读写标「累计」（注册表没有按日打点，别造「今日」）。
 *   2) **常驻层分列两态**：无筛选 = 全部分列摘要；正好选中一个代理 = 那个代理的
 *      占用条**单独放大**（顶部那张卡换成它的口径）。两态写反 = 用户看着这一屏
 *      的东西，得到的却是别人的预算。
 *   3) **维护页占比行**：常驻水位总量下面要有每代理字符占比（分母是水位总量，
 *      不是各代理自己的预算 —— 两个口径不许混）。
 *
 * 前两条里的纯函数（utils/agents.js）**跑真函数**（不抄一份到测试里），
 * 第三条与接线是静态扫描。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  agentUsageRows,
  isOnline,
  ONLINE_HINT,
  sharePercent,
  usageRowsForFilter,
} from '../src/utils/agents.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const STATS = read('views/StatsView.vue')
const CORE = read('views/CoreView.vue')
const MAINT = read('views/MaintenanceView.vue')
const API = read('api/index.js')

// ---------------------------------------------------------------- 接口层

test('接口层：走 /api/v1/stats/core-per-agent', () => {
  assert.match(
    API,
    /corePerAgent: \(\) => http\.get\('\/api\/v1\/stats\/core-per-agent'\)/,
    '分列数据必须走后端那一个只读端点（别在前端自己按 tags 聚合一遍）',
  )
})

// ---------------------------------------------------------------- ① 看板代理卡

test('看板代理卡：在册 / 已批准 / 在线三个数都在', () => {
  assert.match(STATS, /label="在册代理"/)
  assert.match(STATS, /label="已批准"/)
  assert.match(STATS, /label="在线"/)
  // 三个数都来自后端（注册表），不是前端写死的常量
  assert.match(STATS, /api\.listAgents|loadAgentRows\(api\.listAgents\)/)
  assert.match(STATS, /status === 'approved'/)
})

test('看板代理卡：在线口径写在卡上（注册表 last_seen_at 5 分钟内）', () => {
  assert.match(STATS, /:hint="ONLINE_HINT"/, '在线那张卡必须把口径写在 hint 里')
  assert.match(ONLINE_HINT, /last_seen_at/, '在线口径要写明用的是哪一列')
  assert.match(ONLINE_HINT, /5 分钟/, '在线口径要写明时间窗')
})

test('看板代理卡：读写标「累计」，不伪造「今日」', () => {
  assert.match(STATS, /累计读/, '读写必须标累计')
  assert.match(STATS, /累计写/)
  assert.doesNotMatch(STATS, /今日读|今日写|今日读写/, '注册表没有按日打点，不许编「今日」增量')
})

test('看板常驻层：按代理分列（各自生效预算 = core_budget_chars ?? 全局）', () => {
  assert.match(STATS, /按代理分列/, '看板要有按代理分列这一节')
  assert.match(STATS, /agentUsageRows\(perAgent\.value, agentNames\.value\)/)
  // 🔴 2026-10 看板图表化：分列从「一堆文字卡」改成**横向条形图**（代理一多
  // 文字卡堆不动）。每根柱子要显示它自己生效的预算 —— tooltip 里给全。
  assert.match(STATS, /<AgentUsageBars[\s\S]{0,240}:rows="coreAgentCards"/,
    '分列要用 AgentUsageBars 画')
  const KIT = read('utils/chart-kit.js')
  assert.match(KIT, /row\.budget/, '每根柱子要显示它自己生效的预算')
  assert.match(KIT, /row\.chars/, '每根柱子要显示字符数')
})

// ---------------------------------------------------------------- ② 常驻层两态

test('分列摘要两态：无筛选 = 全部；选中 = 只留选中的', () => {
  const rows = agentUsageRows(
    {
      'main-brain': { count: 2, chars: 100, budget_effective: 60000 },
      'zly-hermes': { count: 0, chars: 0, budget_effective: 60000 },
    },
    { 'main-brain': '主脑' },
  )
  assert.equal(rows.length, 2)
  assert.equal(rows[0].label, '主脑')
  assert.equal(rows[0].percent, 0.17)          // 100 / 60000，两位小数
  assert.equal(rows[1].label, 'zly-hermes')    // 没有备注名 ⇒ 裸 id
  // 无筛选 ⇒ 全部分列（现状不变）
  assert.equal(usageRowsForFilter(rows, []).length, 2)
  // 选中一个 ⇒ 只剩那一个
  const one = usageRowsForFilter(rows, ['zly-hermes'])
  assert.equal(one.length, 1)
  assert.equal(one[0].agent_id, 'zly-hermes')
  // 选中不存在的代理 ⇒ 空（不许悄悄显示别人的行）
  assert.deepEqual(usageRowsForFilter(rows, ['nobody']), [])
})

test('常驻层页：顶部预算条按「本视图口径」两态切换', () => {
  assert.match(CORE, /const oneAgentRow = computed/, '要能判断「正好选中一个代理」')
  assert.match(CORE, /agents\.value\.length === 1/, '单代理判定 = 只选了一个')
  assert.match(CORE, /const shownAgentRows = computed/, '无筛选时要显示全部分列摘要')
  assert.match(CORE, /budgetCard/, '顶部那张卡要按本视图口径切')
  assert.match(CORE, /api\.corePerAgent\(\)/, '分列数据走后端端点')
  // 复用现有那根条（tokens.css 的 .stat-bar），不许自己再造一根
  assert.match(CORE, /class="stat-bar"/)
})

test('在线判据：last_seen_at 5 分钟内 = 在线，解析不出来 = 离线', () => {
  const now = Date.parse('2026-10-22T10:00:00Z')
  assert.equal(isOnline({ last_seen_at: '2026-10-22T09:58:00Z' }, now), true)
  assert.equal(isOnline({ last_seen_at: '2026-10-22T09:30:00Z' }, now), false)
  assert.equal(isOnline({ last_seen_at: '' }, now), false)
  assert.equal(isOnline({}, now), false)
})

// ---------------------------------------------------------------- ③ 维护页占比行

test('维护页：常驻水位总量下面有每代理字符占比行', () => {
  assert.match(MAINT, /常驻层水位/, '维护页要有常驻水位这一节')
  assert.match(MAINT, /agentShares/, '每代理占比行')
  assert.match(MAINT, /占 \{\{ row\.percent \}\}%/, '占比要显示出来')
  assert.match(MAINT, /api\.corePerAgent\(\)/)
})

test('占比的分母是水位总量（不是各代理自己的预算）', () => {
  // 200 / 1000 = 20%（拿水位总量当分母）；若误用各代理自己的预算 60000 会得到 0.33%
  assert.equal(sharePercent(200, 1000), 20)
  assert.equal(sharePercent(0, 1000), 0)
  assert.equal(sharePercent(200, 0), 0)
  assert.match(MAINT, /water\.value\?\.used/, '分母取水位总量')
})

test('维护页：退居提名条目显示归属代理 chip（主脑名下的不挂）', () => {
  assert.match(MAINT, /DEMOTE_CANDIDATE_TAG/, '退居提名列表要用班车那一个标')
  assert.match(MAINT, /ownerChip/, '归属 chip 的判定')
  assert.match(MAINT, /tag--owner/, '归属 chip 走既有 .tag--owner（零新色）')
  assert.match(MAINT, /primaryAgentId/, '主脑名下的条目不挂 chip（默认就是它的）')
})
