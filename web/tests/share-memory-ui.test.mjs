/**
 * 记忆共享开关（share_memory）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **走 share 端点**（带 confirm）—— 后端要显式二次确认，漏了就 400；
 *   2) **失败回滚** + 红条：界面显示「已共享」而服务端根本没改 = 假的「以为改成了」；
 *   3) **缺省是关**：老数据没有这个字段时按关（共享是放宽权限的开关，缺省必须最严）；
 *   4) **开关在 primary 的分流链外面**：它只改读可见性，不是治理动作。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')
const API = read('api/index.js')

test('接口层：走 share 端点（带 confirm）', () => {
  assert.match(API, /setAgentShare: \(agentId, enabled\) =>\s*\n?\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/share`, \{ enabled, confirm: true \}\)/,
    '接口层带 confirm（后端要显式二次确认）')
})

test('页面：行内开关读 share_memory，切换走 setAgentShare', () => {
  assert.match(VIEW, /function shareOn\(a\)/, '开关状态读 share_memory（乐观值优先）')
  assert.match(VIEW, /return a\.share_memory === true/, '缺字段按**关**（老数据兼容，缺省最严）')
  assert.match(VIEW, /api\.setAgentShare\(id, !!val\)/, '切换走 setAgentShare')
  assert.match(VIEW, /:model-value="shareOn\(a\)"/, '开关绑的是乐观值')
  assert.match(VIEW, /@change="toggleShare\(a, \$event\)"/, '开关事件接线')
})

test('失败回滚：丢掉这次的乐观值 + 报红条', () => {
  assert.match(VIEW, /delete next\[id\]/, '失败回滚：丢掉这次的乐观值')
  assert.match(VIEW, /error\.value = `记忆共享开关切换失败/, '失败要报红条')
})

test('开关在 primary 的分流链之外（只改读可见性，不是治理动作）', () => {
  const actions = VIEW.slice(VIEW.indexOf('<template #actions>'), VIEW.indexOf('<!-- 四个动作共用'))
  const elseAt = actions.indexOf('<template v-else>')
  assert.ok(elseAt !== -1, '缺少 v-else 分支')
  assert.ok(actions.indexOf('toggleShare(a, $event)') > elseAt, '共享开关在 primary 分流链之外（primary 行也给）')
  assert.doesNotMatch(actions.slice(0, elseAt), /toggleShare/, '分流点之前不许有共享开关')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
})
