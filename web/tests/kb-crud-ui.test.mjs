/**
 * 知识库 CRUD 前端接线护栏（keepsake_kb_crud_fe，纯静态扫描）：
 *   1) 四条写操作都接到 api/index.js 里真实存在的方法（名字对不上 ⇒ 前端一跑就白屏）；
 *   2) manual 域判定必须落在真实字段 source_type 上（skill 域归灌入器，UI 先挡一道）；
 *   3) 危险操作走 ConfirmDialog 的 expect 手输口径，不许出现原生 alert/confirm；
 *   4) 导入先预演后实跑，坏行必须计数返回而不是静默丢；
 *   5) 导出走 fetch + Authorization 头拿 blob（裸链子带不了 token）。
 * 这些都是「编译能过、上线跑起来才发现」的接线断链，所以钉成静态断言。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const API = read('api/index.js')

/** 抽出 api/index.js 里 `name: (...) => ...` 的定义块（到下一个同级键为止） */
function apiMethod(name) {
  const m = API.match(new RegExp(`\\b${name}:\\s*\\([^\\n]*\\n?[\\s\\S]*?(?=\\n\\s{2}\\w|\\n\\})`))
  assert.ok(m, `api/index.js 里没有 ${name} 方法`)
  return m[0]
}

test('四个操作都接到 api 的真实方法上', () => {
  for (const [name, fn] of [
    ['kbCreateDocument', 'doCreate'],
    ['kbUpdateDocument', 'doEdit'],
    ['kbDeleteDocument', 'doDelete'],
    ['kbImportDocuments', 'doImportRun'],
  ]) {
    assert.match(API, new RegExp(`\\b${name}:`), `api/index.js 缺 ${name}`)
    assert.ok(KB.includes(`api.${name}(`), `KbView 没调 api.${name}()`)
    assert.ok(KB.includes(`async function ${fn}(`), `KbView 缺 ${fn} 接线`)
  }
})

test('api/index.js 新增透传方法：URL 不硬编码、confirm 语义显式带上', () => {
  for (const name of ['kbCreateDocument', 'kbUpdateDocument', 'kbDeleteDocument', 'kbImportDocuments']) {
    const body = apiMethod(name)
    assert.match(body, /http\.post\(/, `${name} 必须走 http 实例`)
    assert.ok(!/https?:\/\//.test(body), `${name} 里出现了硬编码域名，baseURL 只归 http 实例管`)
  }
  assert.match(apiMethod('kbCreateDocument'), /confirm: true/)
  assert.match(apiMethod('kbUpdateDocument'), /confirm: true/)
  assert.match(apiMethod('kbDeleteDocument'), /\{ confirm \}/)
  assert.match(apiMethod('kbImportDocuments'), /\(payload\) => http\.post\('\/api\/v1\/kb\/import', payload\)/)
})

test('manual 域判定落在真实字段 source_type 上：skill 域不渲染写按钮', () => {
  assert.match(KB, /function isManual\(doc\)/, '缺 isManual 判定')
  assert.match(KB, /doc\.source_type/, '判定必须读 source_type（后端 _manual_only 同名口径）')
  assert.match(KB, /v-if="isManual\(doc\)" #actions/, '写操作按钮只给手动域渲染')
  assert.match(KB, /v-if="!isManual\(doc\)"/, '非手动域要给只读徽标')
})

test('真删走 ConfirmDialog：手输标题全文，无原生弹框', () => {
  assert.ok(!/\balert\(|\bconfirm\(|\bprompt\(/.test(KB), '禁原生弹框，一律 utils/dialog')
  assert.match(KB, /<ConfirmDialog[\s\S]{0,400}?@confirm="doDelete"/)
  // expect = 文档标题全文（后端 _need_text 比对的就是标题，不是什么末 8 位）
  assert.match(KB, /deleteDoc\.value\.title/)
  assert.match(KB, /api\.kbDeleteDocument\(doc\.doc_id, typed\)/, '手输的确认串必须原样传给后端')
  assert.match(KB, /备份/, '删除后果要点名「已备份」')
})

test('导入：先预演后实跑，手输将新增篇数；坏行计数不静默丢', () => {
  assert.match(KB, /dry_run: true/)
  assert.match(KB, /dry_run: false, confirm: typed/)
  assert.match(KB, /String\(importPlan\.value\.would_add\)/)
  // KB 条目必须有 title：缺标题/缺正文的行计入坏行
  assert.match(KB, /typeof o\.title === 'string'/)
  assert.match(KB, /broken \+= 1/)
  assert.match(KB, /importParse\.broken/, '坏行数要显示出来')
})

test('导出：fetch 带 Authorization 头拉 blob（后端只有 jsonl 一档）', () => {
  assert.match(KB, /async function doExport\(/)
  assert.match(KB, /\/api\/v1\/kb\/export/)
  assert.match(KB, /fetch\(/)
  assert.match(KB, /Authorization: `Bearer \$\{token\}`/, '导出必须带 Authorization 头')
  assert.match(KB, /createObjectURL/, '导出要把 blob 交给浏览器下载')
  assert.match(KB, /\.jsonl/, '后端导出只有 jsonl，后缀照它来')
  assert.ok(!/window\.open/.test(KB), '导出别用 window.open（带不了 Authorization 头）')
})

test('新增/编辑：标题与正文都必填，错误文案原样透出', () => {
  assert.match(KB, /:disabled="!draftTitle\.trim\(\) \|\| !draftContent\.trim\(\)"/, '新增两栏都必填')
  assert.match(KB, /api\.kbCreateDocument\(\{ title, content \}\)/)
  // 改正文才带 content；留空 = 只改标题（后端 title/content 至少给一个）
  assert.match(KB, /api\.kbUpdateDocument\(doc\.doc_id, content \? \{ title, content \} : \{ title \}\)/)
  assert.match(KB, /showError\(`保存失败：\$\{e\.message\}`\)/, '后端文案原样透出，不吞')
})
