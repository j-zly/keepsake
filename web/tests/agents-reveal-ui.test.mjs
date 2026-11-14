/**
 * 代理登记页：凭据「重复复制 / 回填」（ks_credential_recoverable，2026-11-14）。
 *
 * 起因（用户原话）：「我们记忆系统 代理登记页面需要可以复制凭证」→
 * 追问后定档：「肯定是不能换发啊，是直接能复制」。
 * 旧模型里明文只在签发那一刻出现一次，错过只能换发，而**换发会把线上代理打断**
 * （旧串立即失效，它配置里的串变废串）—— 那是不可接受的代价，所以这一版把
 * 「取出」与「回填」两条路补上，同时不能把密钥面做松。
 *
 * 逐条钉死：
 *   1) 「查看 / 复制凭据」钮存在，且**只对 recoverable 的行**渲染（不可还原的行点了
 *      必然 409，那种钮的存在本身会把人逼去乱点换发）；
 *   2) 两条新动作都走**完整 agent_id** 的手输闸，与后端 `_need_text` 同档（前端不许更松）；
 *   3) 动作经 api 层的两个方法打到正确的端点（路径单点，别在页面里手拼 URL）；
 *   4) 取出的明文进**共用的** CredentialDialog（`mode='reveal'`），不许另写一份显示逻辑；
 *   5) 剪贴板失败有一条手抄的活路：只读输入框绑 prop（不另存副本）；
 *   6) 🔴 复制成功的提示**不回显凭据**（旧写法把整把钥匙打进 toast，截图/录屏全收得到）；
 *   7) 回填：粘贴框在开框与提交后都要清空（内存里不留可用钥匙），且失败也清（finally）；
 *   8) 卡面「凭据」那栏有复制钮，复制的是 credential_hint（前 8 位记认），不是明文；
 *   9) 后端不可还原时页面上有**说清下一步**的话（回填或换发），不是只丢一个 409。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.join(HERE, '..', 'src', rel), 'utf-8')
const VIEW = read('views/AgentsView.vue')
const CRED = read('components/CredentialDialog.vue')
const API = read('api/index.js')

test('1) 「查看 / 复制凭据」钮存在，且只对可还原的行渲染', () => {
  assert.match(VIEW, /askFromEdit\('reveal'\)/, '要有一个走既有确认流程的取凭据入口')
  assert.match(VIEW, /v-if="editRow\?\.status === 'approved' && editRow\?\.recoverable"[\s\S]{0,120}askFromEdit\('reveal'\)/,
    '钮必须被 recoverable 门控 —— 不可还原的行给了就是误导')
  assert.match(VIEW, /查看 \/ 复制凭据/, '文案要说"复制"，用户要的就是这个动作')
})

test('2) 取出与回填都要完整 agent_id（与后端同档，前端不许更松）', () => {
  for (const kind of ['reveal', 'backfill']) {
    assert.match(VIEW, new RegExp(`if \\(dlgKind\\.value === '${kind}'\\) return dlgRow\\.value\\?\\.agent_id \\|\\| ''`),
      `${kind} 的确认串必须是完整 agent_id`)
  }
  // 反例：不许退回成 revokePrefix（前 8 位）那种弱档
  assert.doesNotMatch(VIEW, /if \(dlgKind\.value === 'reveal'\) return revokePrefix\.value/,
    '取出明文不许只用前 8 位当闸')
})

test('3) api 层两个方法打到正确端点，页面不手拼 URL', () => {
  assert.match(API, /revealCredential: \(agentId, confirm\) =>/, 'api 层要有 revealCredential')
  assert.match(API, /\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/reveal_credential/, '路径单点在 api 层')
  assert.match(API, /backfillCredential: \(agentId, credential, confirm\) =>/, 'api 层要有 backfillCredential')
  assert.match(API, /\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/backfill_credential/, '回填端点同样单点')
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body, /api\.revealCredential\(row\.agent_id, typed\)/, '页面经 api 层调用')
  assert.match(body, /api\.backfillCredential\(row\.agent_id, text, typed\)/, '回填同样经 api 层')
})

test('4) 取出的明文走共用 CredentialDialog（mode=reveal），不另写一份显示逻辑', () => {
  assert.match(VIEW, /showCred\(r, 'reveal'\)/, '取出后交给共用弹框')
  assert.match(VIEW, /:mode="credMode"/, 'mode 要传给组件（两档文案不同）')
  assert.match(CRED, /mode: \{ type: String, default: 'issue' \}/, '组件收 mode，缺省仍是签发档')
  assert.doesNotMatch(VIEW, /<pre class="pre">\{\{ cred \}\}<\/pre>/, '页面不许自己再渲染一份凭据')
})

test('5) 剪贴板失败有手抄的活路，且不额外复制一份明文', () => {
  assert.match(CRED, /class="cred-plain"[\s\S]{0,120}readonly/, '要一条只读可全选的输出')
  assert.match(CRED, /:value="credential"/, '输入框绑 prop —— 不在组件里另存副本')
  assert.doesNotMatch(CRED, /credCopy|copyBuffer|localStorage/, '组件里不许出现第二份凭据状态')
})

test('6) 🔴 复制成功不许把凭据打到屏幕上', () => {
  assert.doesNotMatch(CRED, /showToast\(`已复制：\$\{props\.credential\}`\)/,
    '旧写法会把整把钥匙显示在 toast 里（截图 / 录屏 / 投屏都收得到）')
  assert.match(CRED, /showToast\('已复制到剪贴板'\)/, '成功提示只说"已复制"，不含内容')
  // 页面侧同理：明文取出后的提示里不许带凭据串
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.doesNotMatch(body, /showToast\(`[^\`]*\$\{row\.agent_id\}[^\`]*\$\{[^\}]*credential[^\}]*\}/,
    '提示里可以带 agent_id，但不能带凭据明文')
})

test('7) 回填的粘贴框：开框清空、提交后必清（失败也清）', () => {
  assert.match(VIEW, /const backfillText = ref\(''\)/, '粘贴框是一个 ref（只活内存）')
  assert.match(VIEW, /function openEdit\(row\) \{[\s\S]{0,200}backfillText\.value = ''/,
    '每次开框都从空串开始，上一回粘的串不留存')
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body, /} finally \{[\s\S]{0,120}backfillText\.value = ''/, '提交后（含失败）都要擦掉')
  assert.doesNotMatch(VIEW, /backfillText[\s\S]{0,40}(localStorage|sessionStorage)/, '不落持久存储')
})

// 用户 11-14 当场打回：「为什么只能复制 ksk_4d3a？？不应该是复制全部的凭证吗」
// —— 卡面上放一个"只复制前 8 位"的钮等于骗人：前缀不是凭据，粘出去连不上，
//    而页面上唯一那个复制钮会让人以为已经拿到钥匙。所以卡面钮必须走整条凭据那条路。
test('8) 卡面复制钮复制的是整条凭据（不是前缀），且只给可还原的行', () => {
  const kv = VIEW.slice(VIEW.indexOf('<span class="k">凭据</span>'))
  const row = kv.slice(0, kv.indexOf('</div>'))
  assert.match(row, /class="copy-btn"/, '凭据那栏要有复制钮')
  assert.match(row, /ask\(\'copycred\', a\)/, '钮走「复制完整凭据」那条带确认闸的路')
  assert.match(row, /v-if="a\.recoverable"/, '不可还原的行不给这个钮（给了必然失败 = 误导）')
  assert.doesNotMatch(row, /doCopy\(a\.credential_hint\)/, '不许再有"只复制前缀"的钮')
  // 复制动作的产物必须是整条串：走 reveal 端点 + copyText
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  const branch = body.slice(body.indexOf("kind === 'copycred'"))
  const block = branch.slice(0, branch.indexOf("} else if"))
  assert.match(block, /api\.revealCredential\(row\.agent_id, typed\)/, '取出整条串')
  assert.match(block, /copyText\(r\.credential\)/, '写进剪贴板的是整条 credential，不是 hint')
  assert.doesNotMatch(block, /credential_hint/, '这条路上不该出现前缀')
  assert.doesNotMatch(block, /showCred\(/, '一键复制不把明文显示到屏幕上（少一处被截走的机会）')
})

test('8b) 不可还原的行：前缀旁说清下一步，而不是留一个会失败的钮', () => {
  const fn = VIEW.slice(VIEW.indexOf('function hintText(a)'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  assert.match(body, /a\.recoverable/, '要分可还原/不可还原两档')
  assert.match(body, /回填已有凭据/, '不可还原时给出去回填的话，而不是让人以为页面坏了')
  assert.match(body, /点旁边的钮复制整条/, '可还原时说明这个钮复制整条')
})

test('8c) 弹框标题分档（兜底是"删除 xxx？"，凭据类漏写就把复制显示成删除）', () => {
  const t = VIEW.slice(VIEW.indexOf('const dlgTitle = computed'))
  const body = t.slice(0, t.indexOf('\n})'))
  for (const kind of ['reveal', 'copycred', 'backfill']) {
    assert.match(body, new RegExp(`dlgKind\\.value === '${kind}'`), `${kind} 必须有自家标题`)
  }
})

test('9) 不可还原的行说清下一步（回填优先，不是默认换发）', () => {
  assert.match(VIEW, /editRow\?\.status === 'approved' && !editRow\?\.recoverable/,
    '不可还原分支要有专门的界面')
  assert.match(VIEW, /不换发、旧串照旧可用、线上代理不断线/, '文案必须说清回填不换发（用户拍板的那点）')
  assert.match(VIEW, /askFromEdit\('backfill'\)/, '回填动作同样走二次确认')
})
