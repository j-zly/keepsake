/**
 * 常驻层预算设置 / 三域导入导出 / 导入模板 前端接线护栏
 * （keepsake_budget_io_templates，纯静态扫描）：
 *   1) 模板下载**必须**走 fetch+blob+Authorization（裸 <a href> 会把 token 漏进
 *      浏览器历史），且三个导入弹框各接一条；
 *   2) 常驻层导出固定 `?tag=core`（导出的必须是常驻那批本身，不受搜索词影响）；
 *   3) 导入带 `promote`，且预算设置把 confirm 显式带给后端（防手滑靠后端闸门，
 *      前端不许省）；
 *   4) 新代码不出现原生 alert/confirm —— 全走 utils/dialog。
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
const KB = read('views/KbView.vue')
const API = read('api/index.js')
const EXPORT = read('utils/export.js')

test('下载器走 fetch + blob + Authorization，不许裸 href', () => {
  assert.match(EXPORT, /export async function downloadFile\(/)
  assert.match(EXPORT, /fetch\(/, '必须用 fetch（裸 <a href> 带不了 Authorization 头）')
  assert.match(EXPORT, /Authorization: `Bearer \$\{token\}`/, '必须带 Authorization 头')
  assert.match(EXPORT, /createObjectURL/, '响应体要转 blob 再下载')
  assert.match(EXPORT, /URL\.revokeObjectURL/, '临时 URL 用完要 revoke，否则每个文件泄漏一份内存')
  assert.match(EXPORT, /defaults\.baseURL/, 'baseURL 只归 http 实例管，不许另读一份环境变量')
  assert.match(EXPORT, /import\/template\?kind=/, '模板端点路径必须由这个下载器拼')
})

test('三个导入弹框各接一条「下载模板」', () => {
  for (const [view, name, kind] of [
    [FRAG, 'FragmentsView', 'memory'],
    [KB, 'KbView', 'kb'],
    [CORE, 'CoreView', 'core'],
  ]) {
    assert.match(view, /下载模板/, `${name} 导入弹框没有「下载模板」入口`)
    assert.match(view, /async function getTemplate\(\)/, `${name} 缺 getTemplate 接线`)
    assert.ok(
      view.includes(`downloadTemplate('${kind}')`),
      `${name} 应下载 kind=${kind} 的模板`,
    )
    assert.ok(
      /import \{[^}]*\bdownloadTemplate\b[^}]*\} from '\.\.\/utils\/export'/.test(view),
      `${name} 没从 utils/export 引入 downloadTemplate`,
    )
    // 链接只是触发器，事件必须 preventDefault，否则点了会跳到 # 改掉 hash 路由
    assert.match(view, /@click\.prevent="getTemplate"/, `${name} 的模板链接缺 .prevent`)
  }
})

test('常驻层导出固定 tag=core，不受搜索词影响', () => {
  assert.match(CORE, /async function doExport\(fmt\)/)
  assert.match(CORE, /export\?tag=core&format=\$\{fmt\}/, '导出 URL 必须是 ?tag=core&format=')
  assert.ok(CORE.includes("doExport('jsonl')"), '缺「导出 JSONL」按钮接线')
  assert.ok(CORE.includes("doExport('csv')"), '缺「导出 CSV」按钮接线')
  assert.match(CORE, /downloadFile\(`\/api\/v1\/export/, '导出必须走共用下载器')
  // 导出沿用后端既有的 ?tag= 端点，不许前端自己拼 CSV/JSONL
  assert.ok(!CORE.includes('JSON.stringify'), '导出行不该由前端拼（后端才有导出口径）')
})

test('常驻层导入带 promote，预演与实跑都带', () => {
  assert.match(CORE, /importPromote = ref\(true\)/, '常驻层页默认就该是「导入即升常驻」')
  assert.match(CORE, /promote: importPromote\.value/)
  assert.match(CORE, /would_promote/, '预演回显里要点名「其中升常驻 N 条」')
  assert.match(CORE, /async function planImport\(/)
  assert.match(CORE, /async function doImportRun\(typed\)/)
})

test('预算设置：入口 + confirm 显式带给后端', () => {
  assert.match(CORE, /function openBudget\(\)/)
  assert.match(CORE, /api\.setCoreBudget\(/)
  assert.ok(
    CORE.includes('draftConfirm.value.trim()'),
    'confirm 必须原样带给后端（后端按当前生效值校验）',
  )
  assert.match(CORE, /n >= 1000 && n <= 500000/, '前端要与后端同一区间，否则用户点了才被拒')
  assert.match(API, /setCoreBudget:/, 'api/index.js 缺 setCoreBudget')
  const m = API.match(/setCoreBudget:[\s\S]*?\n\n/) || ['']
  assert.match(m[0], /http\.put\(/, 'setCoreBudget 必须走 http 实例')
  assert.ok(!/https?:\/\//.test(m[0]), 'setCoreBudget 里出现了硬编码域名')
  assert.match(m[0], /confirm/, 'confirm 必须在请求体里')
})

test('新代码不出现原生 alert/confirm', () => {
  for (const [view, name] of [[CORE, 'CoreView'], [EXPORT, 'utils/export.js']]) {
    assert.ok(!/\balert\(/.test(view), `${name} 用了原生 alert`)
    assert.ok(!/\bwindow\.confirm\(/.test(view), `${name} 用了原生 confirm`)
  }
})
