/**
 * 代理登记页：凭据一键复制（ks_credential_recoverable）
 *
 * 只用字符串包含判断，不写正则 —— 上一版被自己的转义绊倒三次，浪费的是用户的时间。
 * 钉的就是他要的那五件事：点一下就复制整条、不要打任何字、不给会失败的钮、
 * 提示里不泄露串、破坏性动作的手输闸别被顺手拆掉。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.join(HERE, '..', 'src', rel), 'utf-8')
const VIEW = read('views/AgentsView.vue')
const API = read('api/index.js')
const CRED = read('components/CredentialDialog.vue')

const has = (s, t, m) => assert.ok(s.includes(t), m)
const no = (s, t, m) => assert.ok(!s.includes(t), m)

test('点一下就把整条凭据复制到剪贴板', () => {
  has(VIEW, '@click="copyCredNow(a)"', '卡面钮是一键复制')
  has(VIEW, 'copyText(r.credential)', '复制的是整条 credential')
  has(VIEW, 'api.revealCredential(row.agent_id)', '调用不传任何手输值')
  no(VIEW, "ask('copycred'", '不再弹确认框要人打字')
  no(VIEW, 'doCopy(a.credential_hint)', '不许存在只复制前缀的钮')
})

test('api 层自动带意图位，用户不输入任何东西', () => {
  has(API, 'revealCredential: (agentId) =>', 'revealCredential 不收手输参数')
  has(API, '/reveal_credential', '端点路径在 api 层单点')
  has(API, '{ confirm: true }', '意图位由代码带（后端缺位 400）')
  has(API, 'backfillCredential: (agentId, credential) =>', '回填同样不收手输值')
})

test('不可还原的行不给会失败的钮，只说下一步', () => {
  has(VIEW, 'v-if="a.recoverable"', '复制钮被 recoverable 门控')
  has(VIEW, '回填已有凭据', '不可还原时指出去回填')
})

test('取出后走共用弹框，提示里不出现凭据', () => {
  has(VIEW, "showCred(r, 'reveal')", '显示走两页共用的 CredentialDialog')
  has(CRED, "showToast('已复制到剪贴板')", '成功提示不含凭据内容')
  has(CRED, 'readonly', '剪贴板不可用时有一条能手全选的活路')
})

test('破坏性动作的手输闸没被这次改动顺手拆掉', () => {
  has(VIEW, "if (dlgKind.value === 'rotate') return revokePrefix.value", '换发仍要手输前 8 位')
  has(VIEW, "if (dlgKind.value === 'delete') return dlgRow.value?.agent_id", '删除仍要手输完整 id')
})
