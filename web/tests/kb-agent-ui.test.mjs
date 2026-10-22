/**
 * 知识库「来源代理」这一层的前端接线护栏（kb_store/kb_search/kb_get/kb_remove
 * 开放给代理之后，控制台要能看出归属并按代理筛）：
 *   1) 文档卡渲染**来源代理 chip**（复用记忆卡片的 `.tag--owner` 配方，零新色）；
 *   2) **来源代理下拉**：选项来自 /api/v1/agents（前端不自己拼代理集合）；
 *   3) 筛选**必须作为 `agents` 参数透传给后端**（分页列表只在前端切当前页 =
 *      「看着筛了、其实漏了」）；
 *   4) 归属 chip 的文字：显示名 → 裸 id → 「无归属」三态（纯函数，直接跑真函数）。
 *
 * 接线断链（「编译能过、上线跑起来才发现」）都钉在这四条上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { NO_OWNER_LABEL, agentOwnerLabel } from '../src/utils/agents.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')

// ---------------------------------------------------------------------------
// ① 来源代理 chip（文档卡）
// ---------------------------------------------------------------------------

test('文档卡渲染来源代理 chip：复用 .tag--owner 配方', () => {
  assert.match(KB, /:class="doc\.agent_id \? 'tag--owner' : ''"/,
    '有归属的走强调色 chip，无归属的走普通灰 chip（与记忆卡片同款）')
  assert.match(KB, /\{\{ ownerText\(doc\) \}\}/, 'chip 文字要接 ownerText')
  assert.match(KB, /function ownerText\(doc\)/, '缺 ownerText 接线')
  assert.match(KB, /function ownerTitle\(doc\)/, '归属 chip 要有悬浮说明')
})

// ---------------------------------------------------------------------------
// ② 下拉选项来自注册表（前端零写死）
// ---------------------------------------------------------------------------

test('来源代理下拉：选项来自 /api/v1/agents，不在前端定义代理集合', () => {
  assert.match(KB, /loadAgentOptions\(api\.listAgents\)/, '选项要经注册表拉（翻页取完）')
  assert.match(KB, /v-for="a in agentOptions"/, '下拉要遍历后端给的选项')
  assert.ok(!/const AGENT_OPTIONS\s*=/.test(KB), '代理集合只能来自后端，不许在前端定义')
  // 打开页面就拉一次（筛选项不该等用户点别的才知道）
  assert.match(KB, /onMounted\([\s\S]{0,320}loadAgents\(\)/)
})

// ---------------------------------------------------------------------------
// ③ 筛选必须透传给后端（服务端过滤）
// ---------------------------------------------------------------------------

test('筛选走服务端：两路列表请求都带 agents=', () => {
  assert.match(KB, /const agentParam = computed\(\(\) => \(agents\.value\.length \? agents\.value\.join\(','\) : undefined\)\)/,
    '筛选值要拼成逗号串（不选 = 不传这个键 = 不过滤）')
  assert.equal(
    (KB.match(/agents: agentParam\.value/g) || []).length,
    2,
    'loadDocs 与 loadBoth 都要透传 agents',
  )
  assert.match(KB, /function onAgents\(\)/, '缺 onAgents 接线')
  assert.match(KB, /function onAgents\(\)[\s\S]{0,200}?return search\(\)/,
    '换筛选要重查（页码各回第 1 页）')
})

// ---------------------------------------------------------------------------
// ④ 归属 chip 的文字三态（纯函数）
// ---------------------------------------------------------------------------

test('归属 chip 文字三态：显示名 → 裸 id → 无归属', () => {
  assert.equal(agentOwnerLabel('worker-a', { 'worker-a': '子代理' }), '子代理')
  assert.equal(agentOwnerLabel('worker-a', {}), 'worker-a', '拿不到显示名就显示裸 id')
  assert.equal(agentOwnerLabel('', { 'worker-a': '子代理' }), NO_OWNER_LABEL)
  assert.equal(agentOwnerLabel(null, {}), NO_OWNER_LABEL)
  assert.equal(agentOwnerLabel('  worker-a  ', {}), 'worker-a', '两侧空白要 strip')
})
