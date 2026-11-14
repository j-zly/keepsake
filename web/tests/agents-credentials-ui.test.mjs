/**
 * 多凭据的前端形状（ks_multi_credential，2026-11-14 用户拍板
 * 「一个代理可以生成多个凭证让多个代理连接，到后面我们可以透过凭证做权限」）。
 *
 * 只用字符串包含判断，**不写正则**（上一版被自己的转义绊倒三次，白烧两轮）。
 * 钉的是用户要的几件事：
 *   1) 卡面不再假设"一个代理一把钥匙"，点进去是清单；
 *   2) 逐把动作全仓只有一份实现（抽屉组件），页面里不留第二份；
 *   3) 复制必须指名到某一把（宁拒不猜）；
 *   4) 明文不长期驻留内存、失败要说人话；
 *   5) 破坏性动作的确认闸一条没少。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.join(HERE, '..', 'src', rel), 'utf-8')
const VIEW = read('views/AgentsView.vue')
const CLD = read('components/CredentialListDrawer.vue')
const API = read('api/index.js')

const has = (s, t, m) => assert.ok(s.includes(t), m || `缺少：${t}`)
const no = (s, t, m) => assert.ok(!s.includes(t), m || `不该出现：${t}`)

test('卡面给的是"几把在用"的入口，不再假设只有一把', () => {
  has(VIEW, '@click="openCreds(a)"', '卡面点进去是凭据清单')
  has(VIEW, '{{ credLine(a) }}', '卡面文案由 credLine 生成')
  has(VIEW, '把在用', '要说"N 把在用"，不是"前 8 位…"')
  has(VIEW, '最近 ${since(a.last_used_at)}', '要带最近使用时间（监管的核心）')
  // 上一代形状全部退场：单把的 hint 展示与页面上的复制钮
  no(VIEW, 'function hintText(', '单把年代的 hintText 不许留着')
  no(VIEW, 'copyCredNow', '页面上不许再自己实现一遍复制')
  no(VIEW, "凭据{{ a.recoverable", '卡面 chip 的"可复制/不可还原"归清单里逐把标')
})

test('逐把动作只有一份实现（抽屉组件），页面里不留第二份', () => {
  for (const m of ['agentCredentials', 'addCredential', 'renameCredential',
    'revokeCredential', 'revealCredential', 'backfillCredential']) {
    has(CLD, `api.${m}(`, `抽屉要会用 ${m}`)
    no(VIEW, `api.${m}(`, `${m} 不许在页面里再实现一遍（两处实现迟早漂）`)
  }
  has(CLD, "import CredentialDialog from '../components/CredentialDialog.vue'",
    '显示明文复用两页共用的 CredentialDialog，不另造一个')
  has(CLD, 'import ConfirmDialog from \'../components/ConfirmDialog.vue\'',
    '作废走全站统一的二次确认弹框')
})

test('复制必须指名到某一把（宁拒不猜）', () => {
  has(API, "revealCredential: (agentId, credId = '')", 'api 层收 cred_id')
  has(API, 'cred_id: credId', 'cred_id 要真的发出去')
  has(CLD, 'api.revealCredential(props.agent.agent_id, r.cred_id)', '点某一把的复制就取那一把')
  // 后端多把在用又不指名 ⇒ 409 ambiguous；前端不许有"不指名"的调用兜底
  no(API, 'revealCredential(agentId)\n', '不许存在不指名的调用形状')
})

test('明文不长期驻留：关掉就抹、粘错就擦', () => {
  has(CLD, "if (!open) cred.value = ''", '显示明文的弹框一关就把这份串抹掉')
  has(CLD, "} finally {\n    // 成功与失败都要擦", '回填框失败也要擦')
  has(CLD, "backfillText.value = ''", '回填串不留内存')
  // 源码级：不落浏览器持久存储（与全站同一条红线）
  no(CLD, 'localStorage', '凭据不许进浏览器持久存储')
  no(CLD, 'sessionStorage', '同上')
  no(CLD, 'indexedDB', '同上')
})

test('新建与换发是两件事，文案不许混', () => {
  has(CLD, '旧钥匙一把都没动', '新建要说清不顶掉任何旧钥匙')
  has(VIEW, '重新生成凭据', '换发（止血用）仍然是独立动作')
  has(VIEW, '作废全部凭据', '代理级的"一把不留"另有动作，且名字要说全')
  no(CLD, 'reissue', '抽屉里不许出现换发（那是"顶掉旧钥匙"，语义不同）')
})

test('作废有二次确认，且不谎报', () => {
  has(CLD, '作废这把凭据？', '确认框标题要指名是"这一把"')
  has(CLD, '其余凭据与状态都不受影响', '后果要说全（防止以为在删代理）')
  has(CLD, 'v-model="revokeOpen"', '确认后才执行（doRevoke 挂在 @confirm）')
  has(CLD, '@confirm="doRevoke"', '执行点在确认回调里')
})

test('清单只公开该有的字段', () => {
  for (const f of ['r.credential_hint', 'r.label', 'r.status', 'r.used_at', 'r.reads', 'r.writes']) {
    has(CLD, f, `清单要显示 ${f}`)
  }
  no(CLD, 'credential_hash', '哈希不出现在界面上')
  no(CLD, 'credential_enc', '密文列更不行')
})

test('破坏性动作的手输闸一条没被这次改动带走', () => {
  has(VIEW, "if (dlgKind.value === 'rotate') return revokePrefix.value", '换发仍手输前 8 位')
  has(VIEW, "if (dlgKind.value === 'clearcred') return revokePrefix.value", '作废全部仍手输前 8 位')
  has(VIEW, "if (dlgKind.value === 'delete') return dlgRow.value?.agent_id", '删除仍手输完整 agent_id')
})

test('编辑弹框的凭据区四个动作齐全（管理 / 换发 / 作废全部 / 删除）', () => {
  const at = VIEW.indexOf('组三：凭据（危险区）')
  assert.ok(at > 0, '危险区分组注释要在')
  const seg = VIEW.slice(at, at + 1400)
  for (const t of ['openCreds(editRow)', "askFromEdit('rotate')",
    "askFromEdit('clearcred')", "askFromEdit('delete')"]) {
    has(seg, t, `凭据区要有 ${t}`)
  }
  // 回填输入框已从页面搬进抽屉（页面里再留一份就是两个入口）
  no(seg, 'backfillText', '回填只在抽屉里做')
})
