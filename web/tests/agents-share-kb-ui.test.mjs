/**
 * 知识库共享开关（share_kb）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **开关在编辑弹框里**：卡面只做**只读** chip（拨开关的地方在编辑弹框）；
 *   2) **走独立的 share-kb 端点**（带 confirm）—— 后端要显式二次确认，漏了就 400；
 *      与记忆那个 `share` 端点**分开**（两个旗解耦，不许共用一个端点）。
 *   3) **缺省是关**：老数据没有这个字段时按关（共享是放宽权限的开关，缺省必须最严）；
 *   4) **失败要报红条**：保存失败不许留下「以为改成了」的界面状态。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')

test('编辑弹框：有「共享知识库」开关行（与共享记忆同构）', () => {
  assert.match(VIEW, /label="共享知识库（打开 = 别的代理能检索到它名下的知识库文档）"/,
    '编辑弹框里有共享知识库开关行')
  assert.match(VIEW, /<el-switch v-model="editShareKb" \/>/, '共享知识库开关件')
  assert.match(VIEW, /editShareKb\.value = row\?\.share_kb === true/,
    '打开弹框时从服务器口径初始化（缺字段按关）')
})

test('卡面：只读 chip（缺字段按关，不拨开关）', () => {
  assert.match(VIEW, /function shareKbOn\(a\)/, '共享知识库状态读 share_kb')
  assert.match(VIEW, /return a\.share_kb === true/, '缺字段按**关**（老数据兼容，缺省最严）')
  assert.match(VIEW, /共享知识库\{\{ shareKbOn\(a\) \? '开' : '关' \}\}/, '卡面上是只读 chip')
  const actions = VIEW.slice(VIEW.indexOf('<div class="ac-foot">'), VIEW.indexOf('</article>'))
  assert.ok(actions.length > 0, '找不到卡片操作区')
  assert.doesNotMatch(actions, /el-switch/, '卡面不许挂开关件')
})

test('提交：走独立的 share-kb 端点（带 confirm）', () => {
  assert.match(VIEW, /const shareKbChanged = wantShareKb !== \(row\.share_kb === true\)/,
    '保存前算出「有没有变」')
  assert.match(VIEW, /\/share-kb`/, '打的是 share-kb 端点（不是记忆那个 share）')
  assert.match(VIEW, /enabled: wantShareKb,/, '提交目标状态')
  assert.match(VIEW, /confirm: true,/, '后端要显式二次确认（漏了就 400）')
})

test('两个旗解耦：知识库开关不与记忆开关绑在一起', () => {
  assert.match(VIEW, /const wantShare = !!editShare\.value/, '记忆旗照旧独立算')
  assert.match(VIEW, /const wantShareKb = !!editShareKb\.value/, '知识库旗独立算')
  assert.doesNotMatch(VIEW, /wantShareKb = .*wantShare\b/, '知识库旗不许由记忆旗派生')
  assert.doesNotMatch(VIEW, /wantShare = .*wantShareKb\b/, '记忆旗不许由知识库旗派生')
})

test('保存失败：报红条（不许留下「以为改成了」的界面状态）', () => {
  assert.match(VIEW, /'知识库共享开关'/, '失败红条要说得清是哪一项没改成')
  assert.match(VIEW, /showError\(`\$\{label\}保存失败：\$\{e\.message\}`\)/, '后端 message 原样透出')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
})
