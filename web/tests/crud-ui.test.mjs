/**
 * 记忆 CRUD 前端接线护栏（keepsake_memory_crud_fe，纯静态扫描）：
 *   1) 四条操作都真接到了 api/index.js 里存在的方法（名字对不上 ⇒ 前端一跑就白屏）；
 *   2) api/index.js 的新增透传方法不许硬编码 URL —— 全走 http 实例；
 *   3) 危险操作走 ConfirmDialog 的 expect 手输口径，不许出现原生 alert/confirm；
 *   4) CoreView 的搜索接线在（输入框 + 查询 + 清空回全量 + 防串序号）。
 * 这些是「编译能过、上线才发现」的接线断链，所以钉成静态断言。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const FRAG = read('views/FragmentsView.vue')
const CORE = read('views/CoreView.vue')
const API = read('api/index.js')

/** 抽出 api/index.js 里 `name: (...) => ...` 的定义块（到下一个同级键为止） */
function apiMethod(name) {
  const m = API.match(new RegExp(`\\b${name}:\\s*\\([^\\n]*\\n?[\\s\\S]*?(?=\\n\\s{2}\\w|\\n\\})`))
  assert.ok(m, `api/index.js 里没有 ${name} 方法`)
  return m[0]
}

test('四个操作都接到 api 的真实方法上', () => {
  for (const [name, fn] of [
    ['createFragment', 'doCreate'],
    ['deleteFragment', 'doDelete'],
    ['importFragments', 'doImportRun'],
  ]) {
    assert.match(API, new RegExp(`\\b${name}:`), `api/index.js 缺 ${name}`)
    assert.ok(FRAG.includes(`api.${name}(`), `FragmentsView 没调 api.${name}()`)
    assert.ok(FRAG.includes(`function ${fn}(`), `FragmentsView 缺 ${fn} 接线`)
  }
  // 导出：走 fetch 拉 blob（端点要 Authorization 头，裸 <a href> 带不了）
  assert.match(FRAG, /async function doExport\(/, '缺 doExport')
  assert.match(FRAG, /fetch\(/, '导出必须用 fetch 带 Authorization 头')
  assert.match(FRAG, /Authorization: `Bearer \$\{token\}`/, '导出必须带 Authorization 头')
  assert.match(FRAG, /createObjectURL/, '导出要把 blob 交给浏览器下载')
  assert.match(FRAG, /import\.meta\.env|defaults\.baseURL/, 'baseURL 不许另写一份')
})

test('api/index.js 新增透传方法：URL 不硬编码、confirm 显式带上', () => {
  for (const name of ['createFragment', 'deleteFragment', 'importFragments']) {
    const body = apiMethod(name)
    assert.match(body, /http\.(post|get)\(/, `${name} 必须走 http 实例`)
    assert.ok(
      !/https?:\/\//.test(body),
      `${name} 里出现了硬编码域名，baseURL 只归 http 实例管`,
    )
  }
  // 新增/真删/导入的防手滑参数由前端显式带，不许靠后端默认
  assert.match(apiMethod('createFragment'), /confirm: true/)
  assert.match(apiMethod('deleteFragment'), /\{ confirm \}/)
  assert.match(apiMethod('importFragments'), /\(payload\) => http\.post\('\/api\/v1\/import', payload\)/)
})

test('危险操作走 ConfirmDialog：无原生 alert/confirm，删除要手输确认串', () => {
  assert.ok(!/\balert\(|\bconfirm\(|\bprompt\(/.test(FRAG + CORE), '禁原生弹框，一律 utils/dialog')
  assert.ok(!/window\.open/.test(FRAG), '导出别用 window.open（带不了 Authorization 头）')
  // 删除：ConfirmDialog + expect=key 末 8 位 + 后果三条
  assert.match(FRAG, /v-model="deleting"/)
  assert.match(FRAG, /deleteExpect[\s\S]{0,120}slice\(-8\)/, '删除必须要求手输 key 末 8 位')
  assert.match(FRAG, /api\.deleteFragment\(row\.key, typed\)/, '确认串必须原样传给后端')
  // 导入实跑：expect = 预演出来的 would_add（与 merge/forget 同口径）
  assert.match(FRAG, /String\(importPlan\.value\.would_add\)/)
  assert.match(FRAG, /dry_run: false, confirm: typed/)
  // 预演先于实跑：dry_run:true 的那一路必须在
  assert.match(FRAG, /dry_run: true/)
})

test('CoreView：搜索接线齐（q 透传 + 清空回全量 + 防串 + 搜索态空态）', () => {
  assert.match(CORE, /v-model="q"/)
  assert.match(CORE, /@keyup\.enter="search"/)
  assert.match(CORE, /@clear="onClear"/)
  assert.match(CORE, /api\.coreList\(\{\s*q: /, 'q 必须透传给后端（匹配语义在服务端）')
  assert.match(CORE, /function onClear\(\)\s*\{\s*q\.value = ''/, '清空要回全量，不是空态')
  assert.match(CORE, /isStale\(seq, reqSeq\)/, '缺防串序号，旧响应会覆盖新结果')
  assert.match(CORE, /v-if="dirty"/, '搜索态要有自己的空态，不许共用全量空态')
})
