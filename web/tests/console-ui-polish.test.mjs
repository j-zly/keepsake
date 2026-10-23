/**
 * 记忆维护页美化 + 看板水位按代理（console_ui_polish）的结构护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）：`.vue` 跑不起来，
 * 这一页能跑的是「模板里有没有那个结构」与「样式层有没有那把尺子」。
 *
 * 钉死的六件事：
 *   A1 维护页代理占用**是卡片**（名字 + 大号占比 + 细条 + 字符数），不是裸文字行；
 *   A2 ≥90% 的卡走警示色（--warn），不是蓝也不是红；
 *   A3 退居提名**按列对齐**（条目 / 归属 / 提名时间三列）；
 *   A4 空状态是**矮卡片 + 一句灰字**，不留大片空区块；
 *   A5 页脚说明是 secondary 小字，不跟正文抢视觉；
 *   A6 取数逻辑零改动（同一个接口、同一个口径、同一份 computed）。
 *   B1 看板水位区是**按代理的条组**（前 8 个 + 「其余 N 个」折叠，≥90% 警示色）；
 *   B2 **待办完成率的环还在**，**总预算环没了**；
 *   B3 水位条组是**内联 CSS**（本页不引新依赖），数字全部来自接口。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const MAINT = read('views/MaintenanceView.vue')
const STATS = read('views/StatsView.vue')

/** 取两个视图里的 <style scoped> 段（样式断言只看这一段）。 */
function styleOf(src) {
  const i = src.indexOf('<style scoped>')
  return i < 0 ? '' : src.slice(i)
}
const MAINT_STYLE = styleOf(MAINT)
const STATS_STYLE = styleOf(STATS)

/* ============================================================ A 记忆维护页 */

test('A1 代理占用区是卡片（名字 + 大号占比 + 细进度条 + 字符数小字）', () => {
  assert.match(MAINT, /class="agent-cards"/, '要有卡片网格容器')
  assert.match(MAINT, /class="agent-card"/, '每代理一张卡')
  assert.match(MAINT, /class="agent-card-name"/, '卡上要有代理名')
  assert.match(MAINT, /class="agent-card-pct num">\{\{ row\.percent \}\}%/,
    '卡上要有大号百分比')
  assert.match(MAINT, /class="agent-card-bar"/, '卡上要有细进度条')
  assert.match(MAINT, /class="agent-card-foot num">\{\{ row\.chars \}\} 字符/,
    '卡上要有字符数小字')
  // 网格自适应宽度（不许写死列数）
  assert.match(MAINT_STYLE, /\.agent-cards\s*\{[^}]*grid-template-columns:\s*repeat\(auto-fill/,
    '卡片网格必须自适应列数')
  // 旧的裸文字行不许还在
  assert.doesNotMatch(MAINT, /<span class="agent-name"/, '裸文字行不许还在')
})

test('A2 ≥90% 的卡进度条转警示色', () => {
  assert.match(MAINT, /'agent-card--warn': row\.percent >= 90/, '阈值 90 写死在模板里')
  assert.match(MAINT_STYLE, /\.agent-card--warn \.agent-card-bar i\s*\{[^}]*var\(--warn\)/,
    '进度条走 --warn（临期告警色），不许新造色值')
  assert.match(MAINT_STYLE, /\.agent-card--warn \.agent-card-pct\s*\{[^}]*var\(--warn\)/,
    '百分比字色也跟着走警示色')
})

test('A3 退居提名按列对齐（条目 / 归属 / 提名时间）', () => {
  assert.match(MAINT, /class="panel demote-list"/, '提名表要有自己的容器')
  assert.match(MAINT, /class="demote-head"/, '要有表头')
  assert.match(MAINT, /class="demote-row"/, '每行一条')
  assert.match(MAINT, /class="cell-key col-key"/, 'frag key 那一列')
  assert.match(MAINT, /class="col-owner"/, '归属代理那一列')
  assert.match(MAINT, /class="num col-time"/, '提名时间那一列')
  assert.match(MAINT_STYLE, /\.demote-head,\s*\n\.demote-row\s*\{[^}]*grid-template-columns/,
    '行与表头必须同一套列宽（不然两列对不上）')
  // 归属 chip 还在（只改样式层，操作入口不动）
  assert.match(MAINT, /tag--owner/, '归属 chip 保留')
})

test('A4 空状态是矮卡片 + 一句灰字', () => {
  assert.match(MAINT, /class="panel empty-mini"/, '空状态走矮卡片')
  assert.doesNotMatch(MAINT, /class="empty"/, '不许再用 44px padding 的大空区块')
  assert.match(MAINT_STYLE, /\.empty-mini\s*\{[^}]*padding:\s*20px/, '矮卡片留白 20px')
  assert.match(MAINT_STYLE, /\.empty-mini\s*\{[^}]*var\(--text-faint\)/, '空状态文字走灰字')
})

test('A5 页脚说明是 secondary 小字', () => {
  assert.match(MAINT, /class="src foot-note/, '页脚说明挂 .foot-note')
  assert.match(MAINT_STYLE, /\.foot-note\s*\{[^}]*font-size:\s*12px/, '脚注 12px')
  assert.match(MAINT_STYLE, /\.foot-note\s*\{[^}]*var\(--text-faint\)/, '脚注走弱文字')
  // 原来那条 banner（蓝竖条，视觉很重）不许再回来抢正文
  assert.doesNotMatch(MAINT, /<div class="banner">\s*dry-run 零写入/, '页脚不许再用 banner')
})

test('A6 取数逻辑零改动（接口 / 口径 / computed 都没动）', () => {
  assert.match(MAINT, /api\.corePerAgent\(\)/, '分列仍走既有只读端点')
  assert.match(MAINT, /const agentShares = computed/, '占比仍是同一份 computed')
  assert.match(MAINT, /sharePercent\(r\.chars, total\)/, '分母仍是水位总量')
  assert.match(MAINT, /water\.value\?\.used/, '水位总量取法没动')
})

/* ============================================================ B 看板水位区 */

test('B1 水位区是按代理的条组（内联 CSS，零新依赖）', () => {
  assert.match(STATS, /class="water-list"/, '要有条组容器')
  assert.match(STATS, /class="water-row"/, '每代理一行')
  assert.match(STATS, /class="water-name"/, '行里有代理名')
  assert.match(STATS, /class="water-pct num"/, '行里有百分比')
  assert.match(STATS, /class="water-track"/, '行里有细条')
  assert.match(STATS, /class="ghost-btn water-more"/, '其余代理用折叠按钮收起来')
  // 数字全部来自既有分列数据（coreAgentCards = agentUsageRows 的行）
  assert.match(STATS, /const coreAgentCards = computed\(\(\) => agentUsageRows\(perAgent\.value, agentNames\.value\)\)/,
    '水位行复用同一份分列数据（不许前端另算一遍）')
  assert.match(STATS, /\{\{ r\.chars \}\} \/ \{\{ r\.budget \}\}/, '条上要给出字符数与预算')
  assert.doesNotMatch(STATS, /echarts/, '本页不许引 ECharts（条组是内联 CSS）')
})

test('B1-续 前 8 个 + 「其余 N 个」折叠，≥90% 警示色', () => {
  assert.match(STATS, /const WATER_TOP_N = 8/, '前 8 个')
  assert.match(STATS, /const waterRest = computed\(\(\) => waterRows\.value\.slice\(WATER_TOP_N\)\)/,
    '其余的切出来折叠')
  assert.match(STATS, /waterRestOpen/, '折叠有开关')
  assert.match(STATS, /其余 \$\{waterRest\.length\} 个代理/, '折叠行写明还有几个')
  assert.match(STATS, /r\.percent >= 100 \? 'is-danger' : \(r\.percent >= 90 \? 'is-warn'/,
    '阈值：≥90% 警示、≥100% 危险')
  assert.match(STATS_STYLE, /\.water-track i\.is-warn\s*\{[^}]*var\(--warn\)/, '警示色走令牌')
})

test('B2 完成率环还在，总预算环没了', () => {
  assert.match(STATS, /<RingGauge :metrics="gaugeMetrics"/, '完成率的环还在')
  assert.match(STATS, /label: '待办完成率'/, '环上只剩完成率')
  assert.doesNotMatch(STATS, /总预算水位/, '总预算环不许还在（总量在「分级班车」那张卡上）')
  assert.doesNotMatch(STATS, /const budgetPercent/, '环用的那个预算比例变量一并收掉')
})

test('B3 取数零改动：还是那两个既有端点', () => {
  assert.match(STATS, /api\.corePerAgent\(\)/, '分列走既有只读端点')
  assert.match(STATS, /api\.todoSummary\(\)/, '待办计数走既有端点')
})
