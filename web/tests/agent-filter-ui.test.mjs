/**
 * 代理筛选 + 卡片代理标签的前端护栏（常驻层 / 记忆条目两页同一份口径）。
 *
 * 钉住四件事：
 *   1) **筛选**：选中代理 A ⇒ 列表只剩 `agent:A` 的条目（不选 = 全集，回归线）；
 *      归属标签按**逗号边界的完整标签相等**判（`agent:win` 不命中 `agent:win-desktop`）；
 *   2) **卡片 chip 两态**：有归属 ⇒ 显示代理显示名（拿不到就裸 id）；无归属 ⇒ 「无归属」；
 *   3) **服务端过滤**：两页的列表接口都是服务端分页的，代理筛选**必须**作为
 *      `agents` 参数透传给后端（只在前端切当前页 = 看着筛了、其实漏了）；
 *   4) 两页共用 `utils/agents.js` 那一份口径（不许各写一份）。
 *
 * 前两条跑真函数（utils/agents.js 是纯函数，node --test 直接跑）；
 * 后两条是静态扫描（接线不许断）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  NO_OWNER_LABEL,
  filterByOwners,
  loadAgentOptions,
  ownerLabel,
  ownerOf,
} from '../src/utils/agents.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const FRAG = read('views/FragmentsView.vue')
const CORE = read('views/CoreView.vue')
const PAGES = [['FragmentsView', FRAG], ['CoreView', CORE]]

/** 三条样例：一个有归属、一个相似 id、一个无归属。 */
const ROWS = [
  { key: 'k1', tags: 'core,agent:worker' },
  { key: 'k2', tags: 'core,agent:worker-2' },
  { key: 'k3', tags: 'core' },
]

// ---------------------------------------------------------------------------
// ① 选中代理 A 后列表只剩 agent:A 的条目
// ---------------------------------------------------------------------------

test('选中代理 A ⇒ 只剩 agent:A 的条目（相似 id 与无归属都不许混进来）', () => {
  const got = filterByOwners(ROWS, ['worker'])
  assert.deepEqual(got.map((r) => r.key), ['k1'])
})

test('多选代理 ⇒ 取并集', () => {
  const got = filterByOwners(ROWS, ['worker', 'worker-2'])
  assert.deepEqual(got.map((r) => r.key), ['k1', 'k2'])
})

test('归属标签按逗号边界判：agent:worker 不命中 agent:worker-2', () => {
  assert.equal(ownerOf('core,agent:worker'), 'worker')
  assert.equal(ownerOf('core,agent:worker-2'), 'worker-2')
  assert.equal(ownerOf('core'), '')
  assert.equal(ownerOf(''), '')
  assert.equal(ownerOf(undefined), '')
})

// ---------------------------------------------------------------------------
// ② 不选 = 全集（回归线：现状不变）
// ---------------------------------------------------------------------------

test('不选代理 = 全集（一条都不许少）', () => {
  assert.deepEqual(filterByOwners(ROWS, []), ROWS)
  assert.deepEqual(filterByOwners(ROWS, undefined), ROWS)
})

// ---------------------------------------------------------------------------
// ③ 卡片代理 chip：两态
// ---------------------------------------------------------------------------

test('卡片代理 chip：有归属显示显示名，无归属显示「无归属」', () => {
  const names = { worker: '工人' }
  assert.equal(ownerLabel('core,agent:worker', names), '工人')
  assert.equal(ownerLabel('core', names), NO_OWNER_LABEL)
  assert.equal(NO_OWNER_LABEL, '无归属')
})

test('卡片代理 chip：拿不到显示名就显示裸 id（不许显示空）', () => {
  assert.equal(ownerLabel('core,agent:worker', {}), 'worker')
  assert.equal(ownerLabel('core,agent:worker', undefined), 'worker')
  assert.equal(ownerLabel('core,agent:worker', { worker: '' }), 'worker')
})

// ---------------------------------------------------------------------------
// 代理下拉的选项：注册表是分页的 ⇒ 必须翻页取完
// ---------------------------------------------------------------------------

test('代理下拉：翻页取完（不能只取第一页）', async () => {
  const calls = []
  const pages = {
    1: { items: Array.from({ length: 100 }, (_, i) => ({ agent_id: `a${i}`, name: '' })), count: 150 },
    2: { items: Array.from({ length: 50 }, (_, i) => ({ agent_id: `b${i}`, name: `名${i}` })), count: 150 },
  }
  const listAgents = async (params) => {
    calls.push(params.page)
    return pages[params.page]
  }
  const opts = await loadAgentOptions(listAgents)
  assert.deepEqual(calls, [1, 2])
  assert.equal(opts.length, 150)
  assert.equal(opts[0].id, 'a0')
  assert.equal(opts[149].name, '名49')
})

// ---------------------------------------------------------------------------
// 接线：两页同一份口径 + 服务端过滤
// ---------------------------------------------------------------------------

test('两页都渲染「按代理筛选」的多选下拉（选项来自注册表）', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /v-model="agents"/, `${name} 缺代理多选下拉`)
    assert.match(src, /multiple/, `${name} 的代理下拉必须能多选`)
    assert.match(src, /v-for="a in agentOptions"/, `${name} 的代理下拉缺选项`)
    assert.match(src, /:value="a\.id"/, `${name} 的代理下拉取值必须是 agent_id`)
    assert.match(src, /import \{[^}]*loadAgentOptions[^}]*\} from '\.\.\/utils\/agents'/, `${name} 缺代理选项加载接线`)
  }
})

test('两页都把 agents 透传给后端（服务端分页的列表不许只在前端切当前页）', () => {
  for (const [name, src] of PAGES) {
    const body = src.slice(src.indexOf('async function load'))
    assert.match(
      body.slice(0, 900),
      /agents: agents\.value\.length \? agents\.value\.join\(','\) : undefined/,
      `${name} 的 agents 参数没透传给后端`,
    )
  }
  // 记忆条目页与常驻层页各打各的列表端点
  assert.match(FRAG, /api\.fragments\(\{[\s\S]{0,400}agents:/, 'FragmentsView 没把 agents 传给 /api/v1/fragments')
  assert.match(CORE, /api\.coreList\(\{[\s\S]{0,400}agents:/, 'CoreView 没把 agents 传给 /api/v1/core')
})

test('两页的卡片都渲染归属代理 chip（有归属 / 无归属两态走同一个 chip）', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /class="tag"/, `${name} 的卡片缺归属代理 chip`)
    // 有归属 ⇒ 强调色 chip；无归属 ⇒ 走普通 .tag（灰）
    assert.match(
      src,
      /:class="ownerOf\(row\.tags\) \? 'tag--owner' : ''"/,
      `${name} 的归属 chip 没区分有归属 / 无归属两态`,
    )
    assert.match(src, /\{\{ ownerText\(row\) \}\}/, `${name} 的归属 chip 没显示归属文字`)
    assert.match(src, /import \{[^}]*ownerLabel[^}]*ownerOf[^}]*\} from '\.\.\/utils\/agents'/, `${name} 缺归属解析接线`)
  }
})

test('两页的代理筛选用同一把尺子（utils/agents.js，不许各写一份）', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /filterByOwners\(/, `${name} 没走共用的 filterByOwners`)
    assert.doesNotMatch(src, /function ownerOf\(|function filterByOwners\(/, `${name} 不许自己再写一份归属/筛选逻辑`)
  }
})
