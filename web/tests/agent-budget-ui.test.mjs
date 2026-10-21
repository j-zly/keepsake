/**
 * per-agent 注入预算（`ks_agent_budget`）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **预算栏在「编辑」弹框内** —— 一张卡**一个**编辑入口（沿用合并弹框纪律）；
 *      另开第二个入口 = 两处各改一半，用户不知道自己改的是哪个。
 *   2) **走 budget 端点**（带 confirm）—— 后端要显式二次确认，漏了就 400；
 *   3) **留空 = 沿用全局**（提交 null，不是 0）—— 0 会被后端拒，且语义是
 *      「把常驻层全截掉」，不是「沿用默认」；
 *   4) **placeholder 要写出当前的全局默认** —— 用户不知道留空意味着什么就敢留空。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')
const API = read('api/index.js')
const CORE = read('views/CoreView.vue')

/** 「编辑代理」弹框的模板片段（预算栏必须在它里面）。 */
const EDIT_DIALOG = VIEW.slice(
  VIEW.indexOf('title="编辑代理"'),
  VIEW.indexOf('</el-dialog>', VIEW.indexOf('title="编辑代理"')),
)

test('接口层：走 budget 端点（带 confirm）', () => {
  assert.match(
    API,
    /setAgentBudget: \(agentId, coreBudgetChars\) =>\s*\n?\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/budget`/,
    '接口层要走 /budget 端点',
  )
  const m = API.match(/setAgentBudget:[\s\S]*?\n\n/) || ['']
  assert.match(m[0], /confirm: true/, 'confirm 必须带上（后端要显式二次确认）')
  assert.match(m[0], /core_budget_chars: coreBudgetChars \?\? null/, '空值要转成 null（= 沿用全局）')
})

test('预算栏在「编辑」弹框内（一张卡一个编辑入口）', () => {
  assert.ok(EDIT_DIALOG.length > 0, '找不到编辑弹框')
  assert.match(EDIT_DIALOG, /v-model="editBudget"/, '预算输入框必须在编辑弹框里')
  assert.match(EDIT_DIALOG, /注入预算/, '预算栏要有标签')
  // 编辑入口只有卡片操作区那一个（弹框外的按钮不许再冒一个预算入口）
  const actions = VIEW.slice(VIEW.indexOf('<template #actions>'), VIEW.indexOf('<!-- 四个动作共用'))
  assert.doesNotMatch(actions, /editBudget/, '卡片操作区不许有第二个预算入口')
})

test('placeholder 写清「留空 = 沿用全局（当前 N）」', () => {
  assert.match(VIEW, /留空 = 沿用全局（当前 \$\{globalBudget\}）/,
    'placeholder 必须说出留空的含义与当前全局值')
  assert.match(VIEW, /const globalBudget = ref/, '全局默认预算要有个状态位')
  assert.match(VIEW, /api\.coreStats\(\)/, '全局默认预算从 core/stats 拿（不另存一份）')
})

test('提交：走 setAgentBudget，留空提交 null', () => {
  assert.match(VIEW, /api\.setAgentBudget\(targetId, wantBudget\)/, '保存要走接口层')
  assert.match(VIEW, /if \(!text\) return null/, '留空 ⇒ null（沿用全局，不是 0）')
  assert.match(VIEW, /if \(!Number\.isInteger\(n\) \|\| n <= 0\) return undefined/,
    '0 / 负数 / 非整数前端先拦（后端也会 400）')
})

test('设置页的全局值保留为缺省兜底，文案改成「全局默认注入预算」', () => {
  assert.match(CORE, /label="全局默认注入预算"/, '全局那张卡的文案要改成「全局默认注入预算」')
  assert.match(CORE, /api\.setCoreBudget\(/, '全局设置端点不许被拆掉')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
})
