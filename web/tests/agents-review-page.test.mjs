/**
 * 「代理治理拆两页」的前端护栏（代理登记 /agents + 代理审核 /agents-review）。
 *
 * 用户口径：「代理治理拆成代理登记 + 代理审核两页」—— 待审队列与已批准代理的
 * 日常维护混在一页时，批准这个动作要滚到很下面才看得见，而待审的代理此刻
 * **读写全拒**，恰恰是最该被一眼看见的那摞。
 *
 * 这批改动是**搬走不是复制**，最危险的不是编译不过，而是两处都留着：登记页里
 * 剩一段待审残骸、或凭据显示逻辑被复制成两份各自漂移。钉死这七件事：
 *   1) 路由与侧栏：/agents-review 有页、有标题；侧栏「代理治理」拆成
 *      「代理登记 / 代理审核」两项（顺序锁在 nav-order.test.mjs，这里只钉存在）；
 *   2) ①**审核页只出现 pending**：已批准 / 已吊销的行一律不在审核页渲染；
 *   3) ②**登记页不出现 pending**：只渲染已批准 + 已吊销两摞（已吊销保留展示、可重新批准）；
 *   4) ③**徽标计数取 pending_count**：侧栏「代理审核」那枚红徽标的数来自后端
 *      pending_count（前端零写死），60s 静默刷新；
 *   5) 凭据显示**只有一份实现**：两页共用 components/CredentialDialog.vue；
 *   6) 交互零漂移：批准 / 吊销仍走 ConfirmDialog，吊销仍要手输 agent_id 前 8 位；
 *   7) 红线：零后端改动 + 无原生弹框 + 零硬编码色 + 零凭据串。
 *
 * 静态扫描（node --test 直接跑，不起浏览器、不引新依赖）；关键三条还把页面里
 * 真正的筛选/取数表达式抠出来跑一遍（造假数据喂进去，看它到底留下谁）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const REVIEW = read('views/AgentsReviewView.vue')
const VIEW = read('views/AgentsView.vue')
const APP = read('App.vue')
const ROUTER = read('router/index.js')
const API = read('api/index.js')

/** 造一份三种状态混在一起的注册表（真实后端就是这三种 status）。 */
const MIXED = [
  { agent_id: 'a-ok', status: 'approved' },
  { agent_id: 'b-wait', status: 'pending' },
  { agent_id: 'c-off', status: 'revoked' },
]

/** 把源码里的筛选谓词抠出来真跑一遍（无框架、无依赖）：改坏了这里就红。 */
function predicateOf(src) {
  const m = src.match(/\.filter\(\(a\) => (a\.status === '[a-z]+')\)/)
  assert.ok(m, '页面里找不到 status 筛选谓词')
  // eslint-disable-next-line no-new-func
  return new Function('a', `return (${m[1]})`)
}

test('路由与侧栏：/agents-review 有页、有标题；侧栏那一项拆成两项', () => {
  assert.match(ROUTER, /import AgentsReviewView from '\.\.\/views\/AgentsReviewView\.vue'/,
    '路由没引 AgentsReviewView —— 直接访问 /agents-review 会白屏')
  assert.match(
    ROUTER,
    /\{ path: '\/agents-review', name: 'agents-review', component: AgentsReviewView, meta: \{ title: '代理审核' \} \}/,
  )
  assert.doesNotMatch(ROUTER, /path: '\/agents-review'[^}]*public: true/, '不是公开页：要 token 才进得去')
  // 侧栏：记忆组里两项都在（顺序锁在 nav-order.test.mjs）
  assert.match(APP, /\{ path: '\/agents', label: '代理登记', icon: ICON\.\w+ \}/)
  assert.match(APP, /\{ path: '\/agents-review', label: '代理审核', icon: ICON\.\w+ \}/)
  assert.doesNotMatch(APP, /代理治理（登记\/审核）/, '旧的那一项已经拆掉了')
})

test('① 审核页只出现 pending：已批准 / 已吊销的行一律不在这一页渲染', () => {
  assert.match(REVIEW, /const rows = computed\(\(\) => \(data\.value\?\.items \|\| \[\]\)\.filter\(\(a\) => a\.status === 'pending'\)\)/,
    '队列必须只筛 pending')
  assert.match(REVIEW, /<ItemCard v-for="a in rows"/, '卡片循环走的是筛过的 rows')
  assert.doesNotMatch(REVIEW, /status === 'approved'/, '审核页不渲染已批准的（那是登记页的事）')
  assert.doesNotMatch(REVIEW, /status === 'revoked'/, '审核页不渲染已吊销的（那是登记页的事）')
  // 真跑一遍那个谓词：混着三种状态喂进去，只该留下 pending
  const keep = predicateOf(REVIEW)
  assert.deepEqual(MIXED.filter((a) => keep(a)).map((a) => a.agent_id), ['b-wait'])
})

test('② 登记页不出现 pending：只渲染已批准 + 已吊销两摞', () => {
  assert.match(VIEW, /const approvedRows = computed\(\(\) => agents\.value\.filter\(\(a\) => a\.status === 'approved'\)\)/)
  assert.match(VIEW, /const revokedRows = computed\(\(\) => agents\.value\.filter\(\(a\) => a\.status === 'revoked'\)\)/)
  assert.match(VIEW, /<ItemCard v-for="a in grp\.rows"/, '卡片循环走的是两摞筛过的行')
  assert.doesNotMatch(VIEW, /a\.status === 'pending'/, '登记页不渲染 pending 行')
  // 真跑一遍两个谓词：pending 一个都不许留下
  const keepApproved = predicateOf(VIEW.match(/const approvedRows[\s\S]*?\)\)/)[0])
  const keepRevoked = predicateOf(VIEW.match(/const revokedRows[\s\S]*?\)\)/)[0])
  const left = MIXED.filter((a) => keepApproved(a) || keepRevoked(a)).map((a) => a.agent_id)
  assert.deepEqual(left, ['a-ok', 'c-off'])
  assert.ok(!left.includes('b-wait'), 'pending 的行不许出现在登记页')
  // 已吊销的那一摞保留展示，且仍能「重新批准」（既有行为，拆页时别丢）
  assert.match(VIEW, /title: '已吊销'/)
  assert.match(VIEW, /a\.status === 'revoked' \? '重新批准' : '批准'/)
})

test('③ 徽标计数取 pending_count（侧栏「代理审核」那枚红徽标）', () => {
  assert.match(APP, /const agentsPending = ref\(0\)/, '徽标数要有个落脚点')
  // 取数口 = GET /api/v1/agents 的 pending_count（全库口径，不随 q / page 变）
  assert.match(APP, /await api\.listAgents\(\{ page: 1, page_size: 1 \}\)/, '徽标只取那个数，列表不必拉全')
  assert.match(APP, /agentsPending\.value = Number\(data\?\.pending_count\) \|\| 0/,
    '徽标数必须取后端的 pending_count（前端零写死数字）')
  assert.match(APP, /function agentsBadge\(\)/)
  assert.match(APP, /if \(path === '\/agents-review'\) return agentsBadge\(\)/, '徽标挂在代理审核项上')
  // > 0 才显示（模板的 v-if 守门）
  assert.match(APP, /v-if="badgeFor\(m\.path\)"/)
  // 60s 静默刷新 + 卸载清定时器（与记忆审核 / 知识库审核同一路径）
  assert.match(APP, /REVIEW_POLL_MS = 60000/)
  assert.match(APP, /setInterval\(loadReviewCounts,\s*REVIEW_POLL_MS\)/)
  assert.match(APP, /clearInterval\(reviewTimer\)/)
  // 真跑一遍那个取数表达式：后端给几个就是几个，没给就退 0
  const expr = APP.match(/agentsPending\.value = (Number\(data\?\.pending_count\) \|\| 0)/)[1]
  // eslint-disable-next-line no-new-func
  const pick = new Function('data', `return (${expr})`)
  assert.equal(pick({ pending_count: 3 }), 3)
  assert.equal(pick({}), 0)
})

test('凭据显示只有一份实现：两页共用组件，不许复制粘贴', () => {
  const CRED = read('components/CredentialDialog.vue')
  for (const [name, src] of [['登记页', VIEW], ['审核页', REVIEW]]) {
    assert.match(src, /import CredentialDialog from '\.\.\/components\/CredentialDialog\.vue'/,
      `${name}必须引共用组件（复制一份 = 两处漂移）`)
    assert.match(src, /<CredentialDialog/, `${name}必须真的渲染那个组件`)
    assert.doesNotMatch(src, /<pre class="pre">\{\{ cred \}\}<\/pre>/, `${name}不许自己留一份凭据显示逻辑`)
  }
  // 组件本身：全文 + 复制 + 一次性红色提醒 + 关窗即抹（@closed="forget"）
  assert.match(CRED, /<pre class="pre">\{\{ credential \}\}<\/pre>/)
  assert.match(CRED, /只显示这一次，请立即转交给代理进程/)
  assert.match(CRED, /:close-on-click-modal="false"/, '误点遮罩不该等于「凭据作废」')
  assert.match(REVIEW, /@closed="forget"/, '审核页关窗也要抹掉明文')
  assert.match(REVIEW, /function forget\(\) \{\s*\n\s*cred\.value = ''\s*\n\s*credNote\.value = ''/)
})

test('审核页：10s 静默刷新 + 卸载清定时器；空态说清下一步', () => {
  assert.match(REVIEW, /const REFRESH_MS = 10000/, '刷新周期 10 秒（与登记页同款）')
  assert.match(REVIEW, /timer = setInterval\(\(\) => load\(true\), REFRESH_MS\)/, '定时器走静默重拉')
  assert.match(REVIEW, /onUnmounted\(\(\) => \{\s*\n\s*if \(timer\) clearInterval\(timer\)/, '卸载时必须 clearInterval')
  assert.match(REVIEW, /没有待审核的代理。/, '空态是人话')
  assert.match(REVIEW, /新代理连上来或你手动登记后会出现在这里。/, '空态要指下一步')
  // pending_count 全量口径：红条上的数不许被分页吞掉
  assert.match(REVIEW, /const pendingCount = computed\(\(\) => data\.value\?\.pending_count \|\| 0\)/)
  assert.doesNotMatch(REVIEW, /pendingCount = computed\(\(\) => rows\.value\.filter/, '待审数不许在前端按当前页数（翻页就少一个）')
  assert.match(REVIEW, /v-if="pendingCount > 0" class="banner banner--danger"/, '待审数 > 0 时顶部必须红条')
})

test('交互零漂移：批准 / 吊销仍走 ConfirmDialog；吊销仍要手输 agent_id 前 8 位', () => {
  assert.match(REVIEW, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/)
  assert.match(REVIEW, /<ConfirmDialog[\s\S]*?@confirm="doAction"/, '两个动作都要先过确认框')
  assert.match(REVIEW, /\(dlgRow\.value\?\.agent_id \|\| ''\)\.slice\(0, 8\)/, '吊销确认串取前 8 位')
  assert.match(REVIEW, /dlgKind\.value === 'revoke' \? \(dlgRow\.value\?\.agent_id \|\| ''\)\.slice\(0, 8\) : ''/, '吊销才要手输串')
  assert.match(REVIEW, /await api\.approveAgent\(row\.agent_id\)/, '批准走既有端点')
  assert.match(REVIEW, /await api\.revokeAgent\(row\.agent_id, typed\)/, '吊销把用户手输的串原样发出去')
})

test('红线：零后端改动 + 无原生弹框 + 零硬编码色 + 零凭据串', () => {
  // 端点一个字没改，页面也不许自己拼路径
  assert.match(API, /listAgents: \(params\) => http\.get\('\/api\/v1\/agents', \{ params \}\)/)
  assert.match(API, /approveAgent: \(agentId\) =>/)
  assert.match(API, /revokeAgent: \(agentId, confirm\) =>/)
  assert.doesNotMatch(REVIEW, /\/api\/v1\//, '页面里不许硬编码接口路径（统一走 api 层）')
  // 无原生弹框
  const bare = REVIEW.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
  assert.match(REVIEW, /from '\.\.\/utils\/dialog'/, '提示走现成的 dialog 工具')
  // 零硬编码色值 + 零凭据串
  const color = REVIEW.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g)
  assert.equal(color, null, `审核页不许出现硬编码色值（首条 ${color && color[0]}），一律走 tokens.css`)
  const sk = REVIEW.match(/\bsk-[A-Za-z0-9]{8,}\b/)
  assert.equal(sk, null, '审核页不许落 sk- 凭据串')
})
