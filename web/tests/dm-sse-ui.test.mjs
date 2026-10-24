/**
 * 私信实时推送（SSE 快车道）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **走 push 端点**（带 confirm）—— 后端要显式二次确认，漏了就 400；
 *   2) **开关在唯一的编辑弹框里**（一张卡一个编辑入口）—— 不许另开第二个入口；
 *   3) **缺省是关**：老数据没有这个字段时按关（往外放行的开关，缺省必须最严）；
 *   4) **失败要说人话**：保存失败不许留「以为改成了」的界面状态。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')
const API = read('api/index.js')

test('接口层：走 push 端点（带 confirm）', () => {
  assert.match(
    API,
    /setAgentPush: \(agentId, enabled\) =>\s*\n?\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/push`, \{ enabled, confirm: true \}\)/,
    '接口层带 confirm（后端要显式二次确认）',
  )
})

test('接口层：nudge 走 /api/v1/messages/nudge', () => {
  assert.match(API, /nudgeMessages: \(agents\) =>/, '接口层有 nudgeMessages')
  assert.match(API, /http\.post\('\/api\/v1\/messages\/nudge', \{ agents \}\)/, 'nudge 端点与 body 形状')
})

test('页面：编辑弹框里有「私信实时推送」开关', () => {
  assert.match(VIEW, /<el-form-item label="私信实时推送（SSE 长连接）">/, '编辑弹框里有一栏推送开关')
  assert.match(VIEW, /v-model="editPush"/, '开关绑的是弹框里的本地态')
  assert.match(VIEW, /editPush\.value = row\?\.push_enabled === true/, '打开弹框时从服务器口径初始化')
})

test('开关只在一个编辑入口里（一张卡一个编辑入口）', () => {
  // 弹框外的卡片操作区不许再出现推送开关
  const actions = VIEW.slice(VIEW.indexOf('<div class="ac-foot">'), VIEW.indexOf('</article>'))
  assert.ok(actions.length > 0, '找不到卡片操作区')
  assert.doesNotMatch(actions, /editPush|setAgentPush/, '推送开关只在编辑弹框里')
  // 编辑弹框只有一个
  assert.match(VIEW, /title="编辑代理"/, '编辑弹框还在')
})

test('保存：走 setAgentPush，失败要说人话', () => {
  assert.match(VIEW, /api\.setAgentPush\(targetId, wantPush\)/, '保存时打 push 端点')
  assert.match(VIEW, /私信实时推送开关保存失败/, '失败要报红条')
  assert.match(VIEW, /pushChanged = wantPush !== \(row\.push_enabled === true\)/, '只在真变了时才发请求')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
})
