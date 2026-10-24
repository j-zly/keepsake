/**
 * 记忆共享开关（share_memory）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **走 share 端点**（带 confirm）—— 后端要显式二次确认，漏了就 400；
 *   2) **失败要报红条**：保存失败不许留下「以为改成了」的界面状态；
 *   3) **缺省是关**：老数据没有这个字段时按关（共享是放宽权限的开关，缺省必须最严）；
 *   4) **开关在编辑弹框里**（2026-10-24 UI 重做）：卡面只做**只读** chip，
 *      拨开关的地方在编辑弹框 —— primary 照样能给（它只改读可见性，不是治理动作）。
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

test('页面：开关读 share_memory，卡面只读 chip、拨开关在编辑弹框', () => {
  assert.match(VIEW, /function shareOn\(a\)/, '开关状态读 share_memory')
  assert.match(VIEW, /return a\.share_memory === true/, '缺字段按**关**（老数据兼容，缺省最严）')
  // 卡面：**只读** chip（不拨开关）
  assert.match(VIEW, /共享\{\{ shareOn\(a\) \? '开' : '关' \}\}/, '卡面上是只读 chip')
  // 编辑弹框里才有开关件
  assert.match(VIEW, /v-model="editShare"/, '编辑弹框里有共享开关')
  assert.match(VIEW, /editShare\.value = row\?\.share_memory === true/, '打开弹框时从服务器口径初始化')
  // 保存走 setAgentShare
  assert.match(VIEW, /api\.setAgentShare\(targetId, wantShare\)/, '保存走 setAgentShare')
})

test('保存失败：报红条（不许留下「以为改成了」的界面状态）', () => {
  assert.match(VIEW, /'记忆共享开关'/, '失败红条要说得清是哪一项没改成')
  assert.match(VIEW, /showError\(`\$\{label\}保存失败：\$\{e\.message\}`\)/, '后端 message 原样透出')
})

test('卡面上不许挂开关件（只读展示，拨开关在弹框里）', () => {
  const actions = VIEW.slice(VIEW.indexOf('<div class="ac-foot">'), VIEW.indexOf('</article>'))
  assert.ok(actions.length > 0, '找不到卡片操作区')
  assert.doesNotMatch(actions, /el-switch/, '卡面不许挂开关件')
  // 编辑弹框里 primary 也能拨（只改读可见性，不是治理动作）
  assert.match(VIEW, /<el-switch v-model="editShare" \/>/, '共享开关不按角色锁死')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
})
