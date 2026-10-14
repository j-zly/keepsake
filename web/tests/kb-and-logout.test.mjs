/**
 * P9 的两条护栏（纯静态扫描，node --test 直接跑）：
 *   1) 知识库页默认就得显示文档列表 —— 只挂 summary 的话打开又是空的；
 *   2) 登出必须过 ConfirmDialog —— 直接 api.logout() 就是误点即退。
 * 都是「编译能过、跑起来才发现」的事，所以钉在源码上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const KB = readFileSync(fileURLToPath(new URL('../src/views/KbView.vue', import.meta.url)), 'utf8')
const APP = readFileSync(fileURLToPath(new URL('../src/App.vue', import.meta.url)), 'utf8')
const API = readFileSync(fileURLToPath(new URL('../src/api/index.js', import.meta.url)), 'utf8')

test('知识库页：打开就加载摘要 + 文档列表（不靠输入检索词才有数据）', () => {
  assert.match(KB, /api\.kbSummary\(\)/)
  assert.match(KB, /api\.kbDocuments\(/)
  // onMounted 里两个都要挂；否则又回到「只有计数、列表空着」
  assert.match(KB, /onMounted\([\s\S]{0,200}loadSummary\(\)[\s\S]{0,120}loadDocs\(\)/)
})

test('知识库页：检索框可选 —— q 为空时不发检索，清空回到文档列表', () => {
  assert.match(KB, /const searching = computed\(\(\) => !!q\.value\.trim\(\)\)/)
  // P10：空输入直接回车 ⇒ 不发请求（清空 ✕ 走 onClear，回默认列表）
  assert.match(KB, /if \(!term\) return/)
  assert.match(KB, /@clear="onClear"/)
  assert.match(KB, /function onClear\(\)/)
})

test('知识库页：点文档看切片走新接口，且带分页', () => {
  assert.match(KB, /api\.kbDocumentChunks\(curDoc\.value\.doc_id, \{[\s\S]{0,120}page_size/)
  assert.match(KB, /openDoc/)
})

test('api 客户端：两个新方法在，且既有 kb 方法没被改动', () => {
  assert.match(API, /kbDocuments: \(params\) => http\.get\('\/api\/v1\/kb\/documents', \{ params \}\)/)
  assert.match(API, /kbDocumentChunks: \(docId, params\) =>/)
  assert.match(API, /kbSummary: \(\) => http\.get\('\/api\/v1\/kb\/summary'\)/)
  assert.match(API, /kbSearch: \(params\) => http\.get\('\/api\/v1\/kb\/search', \{ params \}\)/)
})

test('登出：先确认再退出，确认框沿用 ConfirmDialog（红实心）', () => {
  assert.match(APP, /import ConfirmDialog from '\.\/components\/ConfirmDialog\.vue'/)
  assert.match(APP, /<ConfirmDialog[\s\S]{0,400}?@confirm="onSignOutConfirm"/)
  assert.match(APP, /tone="danger"/)
  // 登出按钮本身只开确认框，真正调 logout 的在确认回调里
  assert.match(APP, /async function signOut\(\) \{\s*\n\s*confirmSignOut\.value = true/)
  assert.match(APP, /async function onSignOutConfirm\(\)[\s\S]{0,200}?await api\.logout\(\)/)
})
