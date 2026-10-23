/**
 * 知识库**勾选批量**（审核页批量批准/驳回、列表页批量删除）+ **标签区重做**
 * 的前端接线护栏（keepsake_kb_bulk_ui，纯静态扫描）：
 *   1) 两页用**同一个** SelectionBar，选中集合是 Set、**翻页不清空**；
 *   2) 批量写只走 bulk-* 端点，确认串 = 选中条数且原样传给后端；
 *   3) 失败明细报「成功 X · 失败 Y」并列前 10 条 id+原因；
 *   4) 标签区：Top-N 折叠 + 每 chip 带文档数（×N）+「+N 更多」（N 来自接口的
 *      total_distinct，前端不自己数）；机器标签的剔除**在后端**（前端不许自己
 *      按前缀过滤 —— 两份口径必然漂）。
 *
 * 这些都是「编译能过、点下去才发现」的接线断链，所以钉成静态断言。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const KBR = read('views/KbReviewView.vue')
const BAR = read('components/SelectionBar.vue')
const API = read('api/index.js')

/** 两个页面各一份：列表页（批量删除）/ 审核页（批量批准 + 批量驳回） */
const PAGES = [['KbView', KB], ['KbReviewView', KBR]]

test('两页共用同一个 SelectionBar，接线齐全', () => {
  assert.match(BAR, /全选本页/, '工具条必须有「全选本页」')
  assert.match(BAR, /已选 \{\{ count \}\} 条/, '工具条必须显示已选条数')
  assert.match(BAR, /清空选择/, '工具条必须有「清空选择」')
  for (const [name, src] of PAGES) {
    assert.ok(src.includes('<SelectionBar'), `${name} 没接 SelectionBar`)
    assert.ok(
      /import SelectionBar from '\.\.\/components\/SelectionBar\.vue'/.test(src),
      `${name} 没引入 SelectionBar`,
    )
    for (const attr of [':count="selCount"', ':page-count=', ':page-selected="pageSelected"',
      '@select-all="selectAll"', '@clear="clearSel"']) {
      assert.ok(src.includes(attr), `${name} 的 SelectionBar 缺 ${attr}`)
    }
    assert.match(src, /function toggle\(/, `${name} 缺单条勾选切换`)
    assert.match(src, /function selectAll\(on\)/, `${name} 缺「全选本页」`)
    assert.match(src, /function clearSel\(\)/, `${name} 缺 clearSel`)
  }
})

test('选中集合是 Set 且翻页不清空：只有 clearSel 换空集合', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /const sel = ref\(new Set\(\)\)/, `${name} 选中集合必须是 id 的 Set`)
    // 只有 clearSel 清空；load/onPage 一律不许顺手清（翻页清空 = 以为只动了本页）
    const clears = src.match(/sel\.value = new Set\(\)/g) || []
    assert.equal(clears.length, 1, `${name} 只许在 clearSel 里清空选中集合（实际 ${clears.length} 处）`)
  }
})

test('批量写走 bulk-* 端点，确认串 = 选中条数且原样传给后端', () => {
  for (const name of ['kbBulkApprove', 'kbBulkReject', 'kbBulkDelete']) {
    assert.match(API, new RegExp(`${name}: \\(payload\\) =>`), `api/index.js 缺 ${name} 透传`)
  }
  assert.match(API, /http\.post\('\/api\/v1\/kb\/documents\/bulk-approve', payload\)/)
  assert.match(API, /http\.post\('\/api\/v1\/kb\/documents\/bulk-reject', payload\)/)
  assert.match(API, /http\.post\('\/api\/v1\/kb\/documents\/bulk-delete', payload\)/)
  // 审核页：批量批准 + 批量驳回（理由必填，整批一个）
  assert.match(KBR, /api\.kbBulkApprove\(\{ ids, confirm: typed \}\)/, '批量批准没把确认串原样传给后端')
  assert.match(KBR, /api\.kbBulkReject\(\{ ids, confirm: typed, reason: rejectReason\.value\.trim\(\) \}\)/,
    '批量驳回必须带理由（后端 400 reason_required）')
  assert.match(KBR, /const bulkExpect = computed\(\(\) => String\(selCount\.value\)\)/,
    '确认串必须是选中条数')
  // 列表页：批量删除
  assert.match(KB, /api\.kbBulkDelete\(\{ ids, confirm: typed \}\)/, '批量删除没把确认串原样传给后端')
  assert.match(KB, /const bulkDeleteExpect = computed\(\(\) => String\(selCount\.value\)\)/,
    '确认串必须是选中条数')
})

test('批量结果报「成功 X · 失败 Y」并列前 10 条失败明细', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /成功 \$\{r\.succeeded\} · 失败 \$\{r\.failed\.length\}/,
      `${name} 批量失败必须报「成功 X · 失败 Y」`)
    assert.match(src, /r\.failed\.slice\(0, 10\)/, `${name} 失败明细只列前 10 条`)
    assert.match(src, /reasonText\(/, `${name} 失败原因要走人话映射（后端给的是码）`)
  }
})

test('批量动作走 ConfirmDialog 三层防手滑（expect = 选中条数）', () => {
  assert.match(KBR, /v-model="bulkOpen"/, '审核页缺批量 ConfirmDialog')
  assert.match(KBR, /:expect="bulkExpect"/, '审核页批量确认框没接 expect')
  assert.match(KBR, /:tone="bulkKind === 'approve' \? 'primary' : 'danger'"/,
    '语气按动作分（批准=primary，驳回/删除=danger）')
  assert.match(KB, /v-model="bulkDeleting"/, '列表页缺批量删除 ConfirmDialog')
  assert.match(KB, /:expect="bulkDeleteExpect"/, '列表页批量删除确认框没接 expect')
  // 删除后果必须点名「物理删除 / 逐篇备份 / 不可撤销」
  assert.match(KB, /物理删除，不可恢复/, '批量删除后果要点名物理删除')
  assert.match(KB, /删前逐篇备份/, '批量删除后果要点名逐篇备份')
})

test('标签区：Top-N 折叠 + ×N 计数 + 「+N 更多」（N 来自接口）', () => {
  // 折叠是纯 CSS（不做 JS 高度测量）
  assert.match(KB, /\.tag-cloud \{[\s\S]{0,200}max-height: 32px;[\s\S]{0,120}overflow: hidden;/,
    '折叠态必须用 CSS 裁一刀（不许 JS 测高度）')
  assert.match(KB, /\.tag-cloud--open \{[\s\S]{0,120}max-height: none;/, '展开态要放开高度')
  // 每 chip 带文档数
  assert.match(KB, /class="tab-n">×\{\{ row\.count \}/, 'chip 必须带文档数（×N）')
  // 「+N 更多」的 N 来自接口的 total_distinct
  assert.match(KB, /tagTotal\.value = Number\(r\?\.total_distinct\)/, '真实去重数必须来自接口')
  assert.match(KB, /\+ \$\{tagMoreCount\} 更多|\`\+\$\{tagMoreCount\} 更多\`/, '「+N 更多」要用接口给的数')
  // 展开时一次取回全部过滤后的标签（分页在前端切）
  assert.match(API, /kbTagsAll: \(\) => http\.get\('\/api\/v1\/kb\/tags', \{ params: \{ all: 1 \} \}\)/,
    'api 层缺「取回全部标签」的透传')
  assert.match(KB, /const r = await api\.kbTagsAll\(\)/, '展开时必须一次取回全部过滤后的标签')
  assert.match(KB, /tagRowsAll\.value\.slice\(0, tagLimit\.value\)/, '分页在前端切')
})

test('机器标签的剔除只在后端：前端不许自己按前缀过滤', () => {
  // 前端再写一份前缀口径 = 两份口径各筛各的，迟早出现「界面上有、后端查不到」
  // （只扫 <script> 段：注释里说明「前缀在后端」不算实现）
  const script = KB.match(/<script setup>([\s\S]*)<\/script>/)[1]
  assert.ok(!/local-kb-id-/.test(script), '前端不许出现机器标签前缀（口径只在后端）')
  assert.ok(!/entry-(fact|note|rule|decision)/.test(script), '前端不许出现机器标签前缀（口径只在后端）')
  // 也不许在前端自己过滤 tags（那是后端的事）
  assert.ok(!/tagsOf\([^)]*\)\.filter/.test(script), '标签过滤不许在前端做')
})

test('交互红线：批量动作不许出现原生弹框', () => {
  for (const [name, src] of PAGES) {
    const bare = src.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
    assert.deepEqual(bare, [], `${name} 必须走 ConfirmDialog / utils/dialog`)
    assert.match(src, /from '\.\.\/utils\/dialog'/, `${name} 提示必须走 utils/dialog`)
  }
})
