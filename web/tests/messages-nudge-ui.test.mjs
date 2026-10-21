/**
 * 消息中心「按收件代理分组 + 手动/批量触发提醒」的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有六条，
 * 逐条钉死：
 *   1) **按收件人分组**（不是按线程）：提醒是发给收件人的；
 *   2) **未读数只由后端给**（`unread` 映射）—— 前端自己数必然与后端两个口径；
 *   3) **送达与否由后端如实回报**（`delivered`）—— 页面不许自己乐观成「已送达」；
 *   4) **勾选口径与另两页一致**：复用 SelectionBar（全选本页 / 清空 / 跨页保留）；
 *   5) **单条提醒要弹框确认**并显示目标当前未读数；
 *   6) **禁原生弹框**（统一走 utils/dialog 的 showConfirm / showToast）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/MessagesView.vue')
const API = read('api/index.js')
const BAR = read('components/SelectionBar.vue')

test('接口层：nudge 走 /api/v1/messages/nudge', () => {
  assert.match(API, /nudgeMessages: \(agents\) =>/, '接口层有 nudgeMessages')
  assert.match(API, /http\.post\('\/api\/v1\/messages\/nudge', \{ agents \}\)/, '端点与 body 形状')
})

test('页面：有一栏「按收件代理」（按收件人分组，不是按线程）', () => {
  assert.match(VIEW, /按收件代理/, '有一栏按收件代理分组')
  assert.match(VIEW, /const recipients = computed\(\(\) => \{/, '收件代理清单是算出来的')
  assert.match(VIEW, /map\.set\(m\.to_id, 0\)/, '清单来自收件人（to_id）')
  assert.match(VIEW, /v-for="r in recipients"/, '逐代理渲染一行')
  assert.match(VIEW, /@click="askNudge\(r\)"/, '每行一个「触发提醒」')
})

test('未读数只由后端给（前端不自己数）', () => {
  assert.match(VIEW, /data\.value\?\.unread \|\| \{\}/, '未读映射取自后端回包')
  assert.match(VIEW, /\{\{ r\.unread \}\} 条未读/, '行内显示后端给的未读数')
  assert.doesNotMatch(VIEW, /items\.value\.filter\([^)]*read/, '不许在前端按 read 自己数未读')
})

test('送达与否由后端如实回报（不许前端乐观）', () => {
  assert.match(VIEW, /for \(const r of body\?\.results \|\| \[\]\) results\[r\.agent_id\] = r/, '逐代理结果原样收下')
  assert.match(VIEW, /function nudgeChip\(result\)/, '结果文案由一个函数统一出')
  assert.match(VIEW, /if \(result\.delivered\) return '✓ 已送达'/, '送达用 ✓ chip')
  assert.match(VIEW, /return '⚠ 未开推送'/, '推送没开要说清（不笼统说失败）')
  assert.match(VIEW, /return '⚠ 离线'/, '没挂着流显示离线')
  assert.match(VIEW, /\{\{ nudgeChip\(nudgeResults\[r\.agent_id\]\) \}\}/, '行内用 chip 显示结果')
  assert.doesNotMatch(VIEW, /delivered:\s*true/, '前端不许自己写死 delivered')
})

test('勾选口径与另两页一致（复用 SelectionBar）', () => {
  assert.match(VIEW, /import SelectionBar from '\.\.\/components\/SelectionBar\.vue'/, '复用现成的 SelectionBar')
  assert.match(VIEW, /:count="selCount"/, '已选数接 SelectionBar')
  assert.match(VIEW, /:page-count="recipients\.length"/, '全选只动本页')
  assert.match(VIEW, /@select-all="selectAll"/, '全选事件接线')
  assert.match(VIEW, /@clear="clearSel"/, '清空事件接线')
  assert.match(VIEW, /const sel = ref\(new Set\(\)\)/, '选中集合是 Set（跨页保留）')
  assert.match(BAR, /全选本页/, '工具条组件是全站共用的那一份')
})

test('批量提醒：工具条上有「批量提醒已选 N 个」', () => {
  assert.match(VIEW, /批量提醒已选 \{\{ selCount \}\} 个/, '批量按钮带已选数')
  assert.match(VIEW, /@click="askBatchNudge"/, '批量按钮接线')
  assert.match(VIEW, /const ids = \[\.\.\.sel\.value\]/, '批量取已选集合')
})

test('单条提醒：弹框确认并显示目标当前未读数', () => {
  assert.match(VIEW, /async function askNudge\(row\)/, '单条提醒走一个函数')
  assert.match(VIEW, /await showConfirm\(/, '用统一弹框确认')
  assert.match(VIEW, /当前未读 \$\{row\.unread\} 条/, '弹框里显示目标当前未读数')
  assert.match(VIEW, /if \(!ok\) return/, '取消就不发请求')
})

test('禁原生弹框', () => {
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
  assert.match(VIEW, /import \{ showConfirm, showError, showToast \} from '\.\.\/utils\/dialog'/, '统一走 utils/dialog')
})
