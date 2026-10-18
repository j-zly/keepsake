/**
 * 知识库「审核 chip」在 pendCount==0 时仍保留入口的护栏（kb_pending_chip_zero）。
 *
 * 设计盲区回归点：旧实现把 chip 用 `v-if="pendCount > 0"` 整行藏起来，导致
 * 队列空时面板打不开、已驳回历史也无处查看 —— 用户找审核功能被「扣了入口」。
 * 这里钉住三件事：
 *   1) chip **始终渲染**（接口可达即显示，不再用 v-if 兜底）；
 *   2) 文案三分支全来自后端（`pending_count` 与 `count`），前端零写死数字；
 *   3) 「审核」二字在 chip 文案里出现，0 状态下用户仍能一眼认出这就是审核入口。
 *
 * 静态扫描，纯字符串断言；不进浏览器、不起 fixture。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')

test('chip 在 pendCount==0 时仍渲染，文案含「审核」（不藏入口）', () => {
  // 1) 不再用 v-if="pendCount > 0" 把整行隐藏 —— 这是回归点
  assert.doesNotMatch(KB, /v-if="pendCount > 0"[^>]*class="filter-title"/,
    '「审核」标题不该再被 pendCount==0 隐藏（设计盲区回归）')
  assert.doesNotMatch(KB, /v-if="pendCount > 0"[^>]*class="tabs"/,
    'chip 行不该再被 pendCount==0 隐藏')

  // 2) chip 文案要走 chipText computed（三分支）
  assert.match(KB, /const chipText = computed\(/,
    '缺 chipText 三分支计算')
  assert.match(KB, /待审 \$\{pendCount\.value\}/,
    '分支 1：pending_count>0 时红字「待审 N」')
  assert.match(KB, /审核 · \$\{pendTotal\.value\} 条历史/,
    '分支 2：count>0 但 pending_count=0 时灰字「审核 · {count} 条历史」')
  assert.match(KB, /'审核 · 空'|审核 · 空/,
    '分支 3：都 0 时灰字「审核 · 空」')

  // 3) chip 模板里仍然把「审核」二字渲染出去 —— 0 状态下用户一眼认出这就是审核入口
  assert.match(KB, /\{\{ chipText \}\}/,
    'chip 模板要走 chipText（不再硬塞「待审 N」）')

  // 4) 数据源仍是后端给的 count / pending_count，前端不写死
  assert.match(KB, /pendCount\.value = Number\(r\?\.pending_count\) \|\| 0/,
    'chip 分支 1 的数必须来自后端 pending_count')
  assert.match(KB, /pendTotal\.value = Number\(r\?\.count\) \|\| 0/,
    'chip 分支 2 的数必须来自后端 count（待审+已驳回）')

  // 5) 视觉走 chip--danger / 默认 tab —— 复用既有 class，零新色
  assert.match(KB, /'chip--danger': pendCount > 0/,
    '红字分支挂 chip--danger（复用既有 token，不写新色）')
  assert.match(KB, /'tab--on': pendingOpen && pendCount > 0/,
    '展开态（tab--on）只在真有未审项时点亮，避免误导')
})
