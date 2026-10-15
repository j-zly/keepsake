/**
 * 勾选批量（升/降常驻、归档、删除、勾选导出）前端接线护栏
 * （keepsake_bulk_ops，纯静态扫描）：
 *   1) 两个列表页用**同一个** SelectionBar，选中集合跨页保留，翻页不清空；
 *   2) 批量写只走 `POST /api/v1/ops/bulk`，确认串 = 选中数且原样传给后端；
 *   3) 勾选导出走 `POST /api/v1/ops/bulk-export` + fetch + Authorization
 *      （keys 拼进 GET query 会在 500 条时撞 8K 请求行上限）；
 *   4) 常驻层页有单条删除，且与记忆条目页同一口径（手输 key 末 8 位）；
 *   5) 新代码不出现原生 alert/confirm —— 全走 utils/dialog 与 ConfirmDialog。
 * 这些都是「编译能过、点下去才发现」的接线断链，所以钉成静态断言。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const FRAG = read('views/FragmentsView.vue')
const CORE = read('views/CoreView.vue')
const BAR = read('components/SelectionBar.vue')
const API = read('api/index.js')
const EXPORT = read('utils/export.js')

const PAGES = [['FragmentsView', FRAG], ['CoreView', CORE]]

test('两个列表页共用同一个 SelectionBar，接线齐全', () => {
  assert.match(BAR, /全选本页/, '工具条必须有「全选本页」')
  assert.match(BAR, /已选 \{\{ count \}\} 条/, '工具条必须显示已选条数')
  assert.match(BAR, /清空选择/, '工具条必须有「清空选择」')
  assert.match(BAR, /defineEmits\(\['select-all', 'clear'\]\)/, '全选/清空要靠事件回抛')
  // 全选只动当前页（跨页累积靠逐页勾选）
  assert.match(BAR, /pageSelected === props\.pageCount/, '全选判定 = 本页已选 = 本页条数')
  for (const [name, src] of PAGES) {
    assert.ok(src.includes('<SelectionBar'), `${name} 没接 SelectionBar`)
    assert.ok(
      /import SelectionBar from '\.\.\/components\/SelectionBar\.vue'/.test(src),
      `${name} 没引入 SelectionBar`,
    )
    for (const attr of [':count="selCount"', ':page-count="items.length"',
      ':page-selected="pageSelected"', '@select-all="selectAll"', '@clear="clearSel"']) {
      assert.ok(src.includes(attr), `${name} 的 SelectionBar 缺 ${attr}`)
    }
    assert.match(src, /function toggle\(key\)/, `${name} 缺单条勾选切换`)
    assert.match(src, /:model-value="isOn\(row\.key\)"/, `${name} 卡片勾选状态没从选中集合回显`)
  }
})

test('选中集合跨页保留：翻页/刷新列表都不清空', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /const sel = ref\(new Set\(\)\)/, `${name} 选中集合必须是 key 的 Set`)
    assert.match(src, /function clearSel\(\)/, `${name} 缺 clearSel`)
    // 只有 clearSel 换空集合；load/onPage 一律不许顺手清
    const clears = src.match(/sel\.value = new Set\(\)/g) || []
    assert.equal(clears.length, 1, `${name} 只许在 clearSel 里清空选中集合（实际 ${clears.length} 处）`)
  }
})

test('批量写走 ops/bulk，确认串 = 选中条数且原样传给后端', () => {
  assert.match(API, /bulkOps: \(payload\) => http\.post\('\/api\/v1\/ops\/bulk', payload\)/,
    'api/index.js 缺 bulk 透传')
  for (const [name, src] of PAGES) {
    assert.match(src, /async function doBulk\(typed\)/, `${name} 缺 doBulk`)
    assert.match(src, /api\.bulkOps\(\{ action, keys, confirm: typed \}\)/,
      `${name} 必须把手输确认串原样传给后端（后端才算权威）`)
    assert.match(src, /const bulkExpect = computed\(\(\) => String\(selKeys\.value\.length\)\)/,
      `${name} 的 confirm 必须是选中条数`)
    assert.match(src, /:expect="bulkExpect"/, `${name} 的确认弹框没接 expect`)
    assert.match(src, /slice\(0, 10\)/, `${name} 后果文案必须只列前 10 个 key`)
    assert.match(src, /其余略/, `${name} 后果文案要点明其余略（共 N 条）`)
  }
})

test('批量动作按页面分派：升常驻只在记忆条目页、降常驻只在常驻层页', () => {
  assert.ok(FRAG.includes("askBulk('promote')"), '记忆条目页缺「升常驻」')
  assert.ok(!FRAG.includes("askBulk('demote')"), '记忆条目页不该有「降常驻」')
  assert.ok(CORE.includes("askBulk('demote')"), '常驻层页缺「降常驻」')
  assert.ok(!CORE.includes("askBulk('promote')"), '常驻层页不该有「升常驻」')
  for (const [name, src] of PAGES) {
    for (const act of ['archive', 'delete']) {
      assert.ok(src.includes(`askBulk('${act}')`), `${name} 缺批量「${act}」入口`)
    }
    assert.match(src, /成功 \$\{r\.succeeded\} · 失败 \$\{r\.failed\.length\}/,
      `${name} 批量失败必须报「成功 X · 失败 Y」`)
  }
})

test('删除后果必须点名逐条备份到服务器审计目录', () => {
  for (const [name, src] of PAGES) {
    assert.match(src, /逐条全文已备份到服务器审计目录/, `${name} 删除后果缺备份说明`)
  }
})

test('勾选导出走 POST bulk-export + fetch + Authorization', () => {
  assert.match(EXPORT, /export async function exportKeys\(/)
  assert.match(EXPORT, /method: 'POST'/, '必须走 POST（keys 拼 query 会撞 8K 请求行上限）')
  assert.match(EXPORT, /\/api\/v1\/ops\/bulk-export/, '端点路径必须由这个函数拼')
  assert.match(EXPORT, /body: JSON\.stringify\(\{ keys, format \}\)/, 'keys 走 body')
  assert.match(EXPORT, /Authorization: `Bearer \$\{token\}`/, '必须带 Authorization 头（token 不进 URL）')
  assert.match(EXPORT, /createObjectURL/, '响应体要转 blob 再下载')
  assert.match(EXPORT, /URL\.revokeObjectURL/, '临时 URL 用完要 revoke')
  for (const [name, src] of PAGES) {
    assert.match(src, /async function doExportSelected\(\)/, `${name} 缺「导出选中」`)
    assert.match(src, /exportKeys\(selKeys\.value/, `${name} 没调 exportKeys`)
    // 未勾选时禁用，并提示改用按筛选导出
    assert.match(src, /:disabled="!selCount"/, `${name} 未勾选时「导出选中」必须禁用`)
    assert.match(src, /title="未勾选时/, `${name} 缺「未勾选=按筛选导出」提示`)
  }
})

test('常驻层页单条删除与记忆条目页同一口径（手输 key 末 8 位）', () => {
  assert.match(CORE, /@click="askDelete\(row\)"/, '常驻层卡片缺「删除」按钮')
  assert.match(CORE, /const deleteExpect = computed\(\(\) => \(deleteRow\.value \? deleteRow\.value\.key\.slice\(-8\) : ''\)\)/,
    '删除确认串必须是 key 末 8 位')
  assert.match(CORE, /api\.deleteFragment\(row\.key, typed\)/, '确认串必须原样传给后端')
  assert.match(CORE, /v-model="deleting"/, '缺删除确认弹框')
})

test('新代码不出现原生弹框', () => {
  assert.ok(!/\balert\(|\bconfirm\(|\bprompt\(/.test(FRAG + CORE + BAR),
    '禁原生弹框，一律 utils/dialog / ConfirmDialog')
  // 批量确认必须走 ConfirmDialog（三层防手滑），不许 showConfirm 一步到位
  for (const [name, src] of PAGES) {
    assert.match(src, /v-model="bulkAsk"/, `${name} 缺批量 ConfirmDialog`)
    assert.match(src, /:consequences="bulkConsequences"/, `${name} 批量后果未逐条列出`)
  }
})
