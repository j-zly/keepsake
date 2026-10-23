/**
 * uipolish_r2 的结构护栏（用户 2026-10-23 深夜看图反馈的三件事）。
 *
 * 纯静态扫描 + 纯函数断言（node --test 直接跑，不起浏览器）：
 *
 *   A 退居提名表：**归属代理列每行都有 chip**（后端给的 `agent` 字段 = 那把尺子）
 *     + **分页**（每页 10 条，PagerBar 同一个组件）。
 *   B 看板「按代理水位与趋势」三卡**等高**（grid stretch + 卡内 flex 列）：
 *     左卡图例压成一行灰字、中卡环缩小居中 + 计数小字、右卡图表与两卡拉齐。
 *   C 待办完成率**按代理**分列（后端 `by_agent` 段 ⇒ 前端只翻译不自己算）。
 *
 * 每条断言都指着一个**真的会坏**的地方：结构被改回去 / 数字被前端自己算。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { todoAgentRows } from '../src/utils/todo.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const MAINT = read('views/MaintenanceView.vue')
const STATS = read('views/StatsView.vue')
const PAGER = read('components/PagerBar.vue')

/** 取视图里的 <style scoped> 段（样式断言只看这一段）。 */
function styleOf(src) {
  const i = src.indexOf('<style scoped>')
  return i < 0 ? '' : src.slice(i)
}
const STATS_STYLE = styleOf(STATS)

/* ============================================================ A 退居提名表 */

test('A1 归属代理列每行都有 chip（不再是「主脑名下就不挂」那套）', () => {
  assert.match(MAINT, /:class="ownerId\(row\) \? 'tag--owner' : ''"/,
    'chip 按「有没有归属」分两档')
  assert.match(MAINT, /{{ ownerChip\(row\) }}/, '每行都渲染 chip 文字')
  assert.doesNotMatch(MAINT, /v-if="ownerChip\(row\)"/,
    '不许再按「是不是主脑」把整行 chip 藏掉（那一列于是全空）')
  assert.doesNotMatch(MAINT, /primaryAgentId/, '旧的那套判断不许复活')
})

test('A2 归属 id 吃后端给的 agent 字段（前端不再自己拆 tags）', () => {
  assert.match(MAINT, /row\?\.agent/, '优先读后端的 agent 字段')
  assert.match(MAINT, /function ownerId\(row\)/, '有 ownerId 这把尺子')
  assert.match(MAINT, /ownerOf\(row\?\.tags\)/, '老后端没这字段时退回前端同尺子')
  assert.match(MAINT, /NO_OWNER_LABEL/, '无归属显示「无归属」（既有常量）')
})

test('A3 提名表有分页（PagerBar 同一个组件，每页 10 条）', () => {
  assert.match(MAINT, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/,
    '用既有分页组件，不另造轮子')
  assert.match(MAINT, /<PagerBar[\s\S]*?:total="demotePagerTotal"[\s\S]*?:page="demotePage"/,
    'PagerBar 必须接提名那组分页状态')
  assert.match(MAINT, /@update:page="onDemotePage"/)
  assert.match(MAINT, /@update:page-size="onDemoteSize"/)
  assert.match(MAINT, /const demotePageSize = ref\(10\)/, '默认每页 10 条')
  assert.match(MAINT, /const DEMOTE_PAGE_SIZES = \[10, 20, 50\]/,
    '每页档位含 10（PagerBar 的下拉认这个，否则显示空白）')
  // 取数必须是服务端分页（前端切当前页 = count 与翻页都骗人）
  assert.match(MAINT, /page: demotePage\.value,\s*\n\s*page_size: demotePageSize\.value/,
    '列表接口必须带页码与每页条数')
})

test('A4 PagerBar 支持 sizes prop（下拉认得「每页 10 条」这个档）', () => {
  assert.match(PAGER, /sizes: \{ type: Array, default: \(\) => \[20, 50, 100\] \}/,
    '默认档位不变（20/50/100）')
  assert.match(PAGER, /v-for="s in sizes"/, '下拉的选项来自 sizes prop')
})

/* ============================================================ B 看板三卡 */

test('B1 三卡等高（grid stretch + 卡内 flex 列）', () => {
  assert.match(STATS_STYLE, /\.chart-grid\s*\{[^}]*align-items:\s*stretch/,
    '三卡必须等高（原来是 align-items: start ⇒ 一高一低、留白难看）')
  assert.doesNotMatch(STATS_STYLE, /\.chart-grid\s*\{[^}]*align-items:\s*start/,
    '旧的「各自贴合内容」不许还在')
  assert.match(STATS_STYLE, /\.chart-grid > \.panel\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column/,
    '卡内改成 flex 列（内容自己往下排）')
})

test('B2 左卡：图例压成条组下方一行灰字', () => {
  assert.match(STATS, /class="water-legend"/, '图例要有一行独立小字')
  assert.match(STATS, /≥90% 走警示色 · ≥100% 走危险色/, '图例写明两档阈值')
  assert.match(STATS_STYLE, /\.water-legend\s*\{[^}]*font-size:\s*12px/,
    '图例走小字档（12px）')
  assert.match(STATS_STYLE, /\.water-legend\s*\{[^}]*var\(--text-faint\)/,
    '图例走灰字（零新色）')
})

test('B3 中卡：环缩小居中 + 环下计数小字', () => {
  assert.match(STATS, /const gaugeHeight = computed\(\(\) => \(chartWidth\.value < 900 \? 140 : 160\)\)/,
    '环缩小了（140/160）—— 环下面还要摆按代理的条组')
  assert.match(STATS, /class="gauge-wrap"/, '环与计数小字有一个居中的容器')
  assert.match(STATS_STYLE, /\.gauge-wrap\s*\{[^}]*align-items:\s*center/,
    '环居中')
  assert.match(STATS, /已销掉 \{\{ todoClosed \}\} \/ 全部 \{\{ todoSummary\.total \}\} 条/,
    '环下那行计数小字（数字来自 todoSummary，零写死）')
  assert.match(STATS, /const todoClosed = computed\(\(\) =>\s*\n?\s*Number\(todoSummary\.value\.done \|\| 0\) \+ Number\(todoSummary\.value\.obsolete \|\| 0\)/,
    '已销掉 = done + obsolete（与完成率同一把尺子）')
})

test('B4 右卡：图表与左中两卡拉齐（卡内占满剩余高度）', () => {
  assert.match(STATS_STYLE, /\.chart-wrap\s*\{[^}]*flex:\s*1 1 auto/,
    '图表那块占满卡里剩下的高度 ⇒ 三卡等高时右卡不留白')
  assert.match(STATS_STYLE, /\.chart-wrap\s*\{[^}]*align-items:\s*center/,
    '柱子垂直居中（viewBox 保比例 ⇒ 不会被拉变形）')
})

/* ============================================================ C 待办按代理分列 */

test('C1 看板中卡有「按代理」的小条组', () => {
  assert.match(STATS, /class="todo-agents"/, '要有按代理的条组容器')
  assert.match(STATS, /class="todo-agent-row"/, '每代理一行')
  assert.match(STATS, /class="todo-agent-name"/, '行里有代理名')
  assert.match(STATS, /class="todo-agent-pct num"/, '行里有完成率百分比')
  assert.match(STATS, /class="todo-agent-track"/, '行里有细条')
  assert.match(STATS, /const TODO_AGENT_TOP_N = 6/, '超 6 个折叠')
  assert.match(STATS, /const todoAgentRest = computed\(\(\) => todoAgentRows\.value\.slice\(TODO_AGENT_TOP_N\)\)/,
    '其余的切出来折叠')
  assert.match(STATS, /todoAgentRestOpen/, '折叠有开关')
  assert.match(STATS, /其余 \$\{todoAgentRest\.length\} 个代理/, '折叠行写明还有几个')
})

test('C2 按代理的数是后端给的（前端不自己数、不自己算比例）', () => {
  assert.match(STATS, /api\.todoSummary\(\)/, '还是走既有那个端点')
  assert.match(STATS, /by_agent: Array\.isArray\(s\?\.by_agent\) \? s\.by_agent : \[\]/,
    'by_agent 段原样收下')
  assert.match(STATS, /todoAgentRowsOf\(todoSummary\.value\.by_agent\)/,
    '条组的数据来自 by_agent 段')
  assert.doesNotMatch(STATS, /todoSummary\.value\.by_agent\]\s*\.?\s*filter\(/,
    '不许在前端自己筛/算')
})

test('C3 纯函数：按代理的行（排序 / 未标注桶 / 条长封顶）', () => {
  const rows = todoAgentRows([
    { agent: '工人', agent_id: 'w1', ratio: 40 },
    { agent: '主脑', agent_id: 'm1', ratio: 90 },
    { agent: '-', agent_id: '-', ratio: 0 },
  ])
  assert.deepEqual(rows.map((r) => r.agent), ['主脑', '工人', '未标注'],
    '按完成率降序；「-」桶显示成「未标注」')
  assert.equal(rows[0].width, 90, '条长 = 百分比')
  assert.equal(rows[2].width, 0)
  // 条长封顶 100（不许画出卡片）
  const over = todoAgentRows([{ agent: 'x', agent_id: 'x', ratio: 130 }])
  assert.equal(over[0].width, 100, '超 100% 的条封顶')
  // 空输入不炸
  assert.deepEqual(todoAgentRows(undefined), [])
})
