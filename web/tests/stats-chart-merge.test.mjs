/**
 * 看板「近 7 天记忆增量」**合成一张图** + **悬停浮层** 的护栏
 * （keepsake stats_chart_merge，纯静态扫描 StatsView + 真函数跑纯计算）。
 *
 * 钉死四件最容易悄悄回退的事：
 *   1) **一张图**：旧的「总量折线小图」（SparkLine）删掉了，页面上只剩
 *      按代理的堆叠柱（标题就叫「近 7 天记忆增量」）；
 *   2) **柱顶有当日总量数字**（分项之外还要看得到那天一共多少）；
 *   3) **悬停浮层**：整列命中（不必精准点到某一段）+ 绝对定位浮层，
 *      内容 = 日期 / 当日总量 / 各代理分项（降序）；
 *   4) **零新依赖**：本页手写内联 SVG，不许把 ECharts 或任何新包请进来。
 *
 * 另：两段说明文字（代理区的「读写是注册表的累计计数…」、cron 区的
 * 「来源：{{ cronSource }}」）已按需求删掉 —— 不许又长回来。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = `${ROOT}src`
const STATS = readFileSync(`${SRC}/views/StatsView.vue`, 'utf8')
const PKG = JSON.parse(readFileSync(`${ROOT}/package.json`, 'utf8'))

/* ---------------------------------------------------------------- ① 一张图 */

test('① 旧的总量折线小图删了：页面只剩一张「近 7 天记忆增量」', () => {
  assert.doesNotMatch(STATS, /SparkLine/, '旧的总量折线小图不许还在')
  assert.doesNotMatch(STATS, /dailyPoints|dailyHint/, '折线那一套数据加工也该一起收掉')
  const titles = [...STATS.matchAll(/<div class="chart-title">([^<]+)<\/div>/g)].map((m) => m[1])
  assert.deepEqual(
    titles.filter((t) => t.includes('近 7 天记忆增量')),
    ['近 7 天记忆增量'],
    '增量图只此一张，标题就叫「近 7 天记忆增量」',
  )
})

test('① 主体仍是按代理的堆叠柱（横轴 = daily 段，色带 = daily_by_agent 段）', () => {
  assert.match(STATS, /class="stack-chart"/, '主体是内联 SVG 堆叠柱')
  assert.match(STATS, /dailyDays\.value, series/, '横轴用后端补齐的 daily 段')
  assert.match(STATS, /bucketByAgent\(data\.value\?\.daily_by_agent \|\| \[\]\)/,
    '色带用 daily_by_agent 段')
})

test('① 柱顶写当日总量数字（数字来自接口，零写死）', () => {
  assert.match(STATS, /class="totals"/, '柱顶要有总量那一组文字')
  assert.match(STATS, /\{\{ c\.total \}\}/, '柱顶写的是接口给的逐日总量')
})

/* ---------------------------------------------------------------- ② 悬停浮层 */

test('② 整列命中：透明 rect 逐日捕获 mouseenter', () => {
  assert.match(STATS, /class="hit"/, '要有整列命中层')
  assert.match(STATS, /fill="transparent"/, '命中层是透明 rect（不画东西，只吃事件）')
  assert.match(STATS, /@mouseenter="hoverDay = c\.day"/, '逐日捕获 mouseenter')
  assert.match(STATS, /@mouseleave="hoverDay = ''"/, '移出图区要收掉浮层')
})

test('② 浮层是绝对定位的浮层，内容 = 日期 + 当日总量 + 各代理分项', () => {
  assert.match(STATS, /class="chart-tip"/, '要有浮层')
  assert.match(STATS, /role="tooltip"/, '浮层要带上 tooltip 角色')
  assert.match(STATS, /\.chart-tip \{[^}]*position: absolute/, '浮层必须绝对定位')
  assert.match(STATS, /\{\{ hoverTip\.date \}\}/, '浮层显示日期')
  assert.match(STATS, /\{\{ hoverTip\.total \}\}/, '浮层显示当日总量')
  assert.match(STATS, /v-for="p in hoverTip\.parts"/, '浮层列出各代理分项')
  assert.match(STATS, /\{\{ p\.label \}\}/, '分项显示名字')
  assert.match(STATS, /\{\{ p\.count \}\}/, '分项显示条数')
})

test('② 浮层内容出自纯函数 dayBreakdown（可单测，数字不自己算）', () => {
  assert.match(STATS, /import \{[^}]*dayBreakdown[^}]*\} from '\.\.\/utils\/daily-by-agent'/,
    '浮层内容要抽成纯函数')
  assert.match(STATS, /dayBreakdown\(agentBuckets\.value, day, agentLabel\)/,
    '浮层按「那天 + 分桶 + 显示名」算')
})

/* ---------------------------------------------------------------- ③ 零新依赖 */

test('③ 零新依赖：不引 ECharts、依赖表不许加包', () => {
  assert.doesNotMatch(STATS, /echarts/, '本页是手写内联 SVG，不许把 ECharts 请进来')
  assert.deepEqual(
    Object.keys(PKG.dependencies).sort(),
    ['axios', 'echarts', 'element-plus', 'vue', 'vue-router'],
    '依赖表不许新增（echarts 只留给别的图表组件）',
  )
})

/* ---------------------------------------------------------------- ④ 删掉的两段说明 */

test('④ 代理区的「读写是注册表的累计计数…」整段删了', () => {
  assert.doesNotMatch(STATS, /读写是注册表的累计计数/, '那段说明文字不许又长回来')
  assert.doesNotMatch(STATS, /「在线」= 最近活动落在 5 分钟内/, '同一段里的第二句也删')
})

test('④ cron 区的「来源：{{ cronSource }}」删了（连变量一起收掉）', () => {
  assert.doesNotMatch(STATS, /来源：/, '来源那一行删了')
  assert.doesNotMatch(STATS, /cronSource/, 'cronSource 没人用了 ⇒ 变量也删掉')
})
