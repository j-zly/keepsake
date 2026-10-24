/**
 * 代理登记页（keepsake_agent_registry 的控制台面 · 业务页 /agents）的前端接线护栏。
 *
 * 2026-10-20 拆页：待审队列整段搬去 /agents-review（代理审核页）。本页只管
 * 已批准代理的日常维护 + 登记新代理。下列第 3、7 条随之搬去
 * **agents-review-page.test.mjs**（红条与 pending_count 全量口径现在钉在那儿）；
 * 一次性凭据的显示实现抽成共用组件 components/CredentialDialog.vue
 * （两页同一份，不许复制第二份 —— 见下面「凭据展示」那条）。
 *
 * 2026-10-24 UI 重做（用户点名「太丑」）：一代理一张卡，**卡面只留一个直接
 * 动作（批准 / 吊销）+ 一支铅笔（唯一编辑入口）**；角色 / 开关 / 凭据 / 删除
 * 全收进编辑弹框（分三组：角色 / 开关 / 凭据）。下列断言随之重指向新结构 ——
 * **判据没变**（primary 行不给治理动作、确认闸一字不弱、凭据只活一次），
 * 变的只是「这些动作现在长在哪儿」。
 *
 * 这页的静默失败模式特别多，逐条钉死：
 *   1) **凭据纪律**：approve 的明文只许活在一次性弹框的前端内存态 ——
 *      不许出现任何持久面写入（本页连浏览器的持久存储 API 都不许碰），
 *      关掉弹框就抹掉，页面不许把明文拼进 URL。
 *   2) **三道确认闸**：approve 要 `confirm: true`，revoke 要手输
 *      agent_id 前 8 位，改角色要手输完整 agent_id —— 与后端口径逐字一致。
 *      少一闸就是「点一下真改数据」。
 *   3) **待审醒目**：pending > 0 时顶部必须有 banner，且它的读写正被拒，
 *      静默地把它排在网格里等于没提示 —— 已随审核页搬走（那边钉）。
 *   4) **自动刷新要收干净**：10s 定时器不 clearInterval，离开这页还在打接口。
 *   5) **主代理行一个动作都不给**：不只是「不给设为主代理」—— 吊销 / 排除 /
 *      批准同样不给。吊销主脑 = 写闸 fail-closed 把它降成 sub，常驻写入全降级、
 *      候选审批链瘫痪；排除对它本就没有意义。四钮必须整块收在 v-else 里，
 *      漏挂一个钮就等于自锁洞还开着。
 *   6) **搜索 + 服务端分页**：filter-panel 三件套（回车查 / 清空回全量 /
 *      seq 防串）与 PagerBar 接线；q/page/page_size 全部透传，前端不切片。
 *   7) **pending_count 全量口径**：红条上的待审数不许被搜索词或分页吞掉
 *      —— 已随审核页搬走（那边钉）。本页改成反钉：待审的痕迹不许留在这页。
 *   8) 无裸 alert / confirm，提示一律走 utils/dialog。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')
const API = read('api/index.js')

test('凭据纪律：本页不碰浏览器持久存储，凭据只活在前端内存态', () => {
  assert.doesNotMatch(VIEW, /localStorage|sessionStorage|setToken/, '凭据明文不许落任何持久面')
  assert.doesNotMatch(VIEW, /indexedDB|document\.cookie/, '持久面只此一处就够，别再开第二个')
  // 明文不进 URL / 不进日志
  assert.doesNotMatch(VIEW, /location\.(href|hash|search)|console\./, '凭据不许进地址栏或控制台')
  // 关掉弹框即抹掉：@closed 是这条纪律的兑现点
  assert.match(VIEW, /@closed="forget"/, '弹框关闭时必须调 forget 清掉明文')
  assert.match(
    VIEW,
    /function forget\(\) \{\s*\n\s*cred\.value = ''\s*\n\s*credNote\.value = ''/,
    'forget 要把明文与提示一起清空',
  )
})

test('凭据展示：全文 + 复制 + 一次性红色提醒（共用组件，两页同一份实现）', () => {
  const CRED = read('components/CredentialDialog.vue')
  assert.match(CRED, /<pre class="pre">\{\{ credential \}\}<\/pre>/, '凭据全文要显示出来（用户要抄走）')
  assert.match(CRED, /只显示这一次，请立即转交给代理进程/, '一次性提醒是这句，且要给红色语义色')
  assert.match(CRED, /class="warn"/, '提醒走红色语义类')
  assert.match(CRED, /@click="doCopy"/, '有复制按钮（凭据形态长，只靠手抄容易错）')
  // 确认框不许让凭据被顺手截走：误点遮罩不该等于「看过就作废」
  assert.match(CRED, /:close-on-click-modal="false"/, '凭据弹框同样禁遮罩关闭')
  // 🔴 两页都调这一个组件 —— 谁再自己留一份显示逻辑，就是两处漂移的坑
  for (const [name, src] of [['登记页', VIEW], ['审核页', read('views/AgentsReviewView.vue')]]) {
    assert.match(src, /import CredentialDialog from '\.\.\/components\/CredentialDialog\.vue'/, `${name}必须引共用组件`)
    assert.match(src, /<CredentialDialog/, `${name}必须真的渲染那个组件`)
    assert.doesNotMatch(src, /<pre class="pre">\{\{ cred \}\}<\/pre>/, `${name}不许自己再留一份凭据显示逻辑`)
  }
})

test('五条 API 齐全，且 confirm 口径与后端逐字一致', () => {
  // 列表端点收 q/page/page_size（服务端过滤+分页），匹配语义在服务端
  assert.match(API, /listAgents: \(params\) => http\.get\('\/api\/v1\/agents', \{ params \}\)/)
  assert.match(API, /approveAgent: \(agentId\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/approve`, \{ confirm: true \}\)/)
  assert.match(API, /revokeAgent: \(agentId, confirm\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/revoke`, \{ confirm \}\)/)
  assert.match(API, /setAgentRole: \(agentId, role, confirm\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/role`, \{ role, confirm \}\)/)
  assert.match(API, /setAgentExclude: \(agentId, exclude\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/exclude`, \{ confirm: true, exclude \}\)/)
})

test('三道确认闸接在 ConfirmDialog 上，串由用户手输', () => {
  assert.match(VIEW, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/)
  assert.match(VIEW, /<ConfirmDialog[\s\S]*?@confirm="doAction"/, '危险动作都要先过确认框')
  // 吊销 = agent_id 前 8 位（与 ops 归档「key 末 8 位」同一道闸）
  assert.match(VIEW, /\(dlgRow\.value\?\.agent_id \|\| ''\)\.slice\(0, 8\)/, '吊销确认串取前 8 位')
  assert.match(VIEW, /if \(dlgKind\.value === 'revoke'\) return revokePrefix\.value/, '吊销走 8 位串')
  // 改角色 = 完整 agent_id
  assert.match(VIEW, /if \(dlgKind\.value === 'role'\) return dlgRow\.value\?\.agent_id \|\| ''/, '改角色走全串')
  // approve 不手输，后端要的是 confirm: true —— 手输串留空即可
  assert.match(VIEW, /@confirm="doAction"/, '确认回调统一收手输串')
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body.slice(0, 1400), /api\.approveAgent\(row\.agent_id\)/, '批准走 approveAgent')
  assert.match(body.slice(0, 1400), /api\.revokeAgent\(row\.agent_id, typed\)/, '吊销把用户手输的串原样发出去')
  assert.match(body.slice(0, 1400), /api\.setAgentRole\(row\.agent_id, 'primary', typed\)/, '设主代理发 role=primary')
  // 🔴 上下文开关 2026-10-24 起是**编辑弹框里的开关件**（不再走确认框）：
  //    它是「只挡它之后写的东西」的投递偏好，不是危险动作 —— 走确认框反而挡路。
  const editBody = VIEW.slice(VIEW.indexOf('async function doEdit'))
  assert.match(editBody, /api\.setAgentExclude\(targetId, !wantContext\)/, '上下文开关走 setAgentExclude')
})

test('主代理行不给「设为主代理」按钮', () => {
  // 2026-10-24 UI 重做：设主代理收进**编辑弹框**（卡面只留一个直接动作 + 一支铅笔）。
  // 判据不变：已经是主代理的那行**不许**出现这个入口（防把自己降了没人批）。
  assert.match(
    VIEW,
    /<el-button v-if="!editPrimary" :disabled="busy" @click="askFromEdit\('role'\)">\s*\n?\s*设为主代理/,
    '已经是主代理的行必须没有这个按钮（防把自己降了没人批）',
  )
  assert.match(VIEW, /<span v-else class="tip">它已经是主代理了。<\/span>/, '要跟用户说清为什么没有这个钮')
})

// ---------------------------------------------------------------------------
// 自锁洞：primary 行必须**一个治理动作都不给**
// ---------------------------------------------------------------------------

/** 取卡片操作区那一段（`<div class="ac-foot">` 到它的闭合 `</div>`）。 */
function actionsBlock() {
  const start = VIEW.indexOf('<div class="ac-foot">')
  assert.ok(start !== -1, '找不到卡片操作区')
  const rest = VIEW.slice(start)
  const end = rest.indexOf('</article>')
  return rest.slice(0, end === -1 ? rest.length : end)
}

test('primary 行只留一句话：吊销 / 批准一个都不给', () => {
  const actions = actionsBlock()
  // 外层按 role 分流：primary 走说明，其余走「批准 / 吊销」
  assert.match(actions, /<span v-if="a\.role === 'primary'" class="act-note">/, 'primary 行要有那句说明')
  assert.match(actions, /主代理 · 不可吊销 \/ 不可排除/, '要说清为什么没有按钮（人话，不是空白）')
  // 🔴 逐个点名 primary 分支（分流点之前）：一个治理动作都不许有
  const primaryBranch = actions.slice(0, actions.indexOf('v-else-if="a.status === \'approved\'"'))
  assert.doesNotMatch(primaryBranch, /ask\('revoke'/, 'primary 行不许出现吊销入口')
  assert.doesNotMatch(primaryBranch, /ask\('approve'/, 'primary 行不许出现批准入口')
  assert.doesNotMatch(primaryBranch, /ask\('role'/, 'primary 行不许出现改角色入口')
  assert.doesNotMatch(primaryBranch, /ask\('exclude'/, 'primary 行不许出现上下文排除入口')
  assert.doesNotMatch(primaryBranch, /act-btn/, 'primary 行不许有任何动作按钮样式残留')
})

test('卡面只留「一个直接动作 + 一支铅笔」（2026-10-24 UI 重做）', () => {
  const actions = actionsBlock()
  // 直接动作：已批准 = 吊销（危险红字），其余 = 批准 / 重新批准
  assert.match(actions, /v-else-if="a\.status === 'approved'"[\s\S]*?ask\('revoke', a\)/, '已批准行的直接动作是吊销')
  assert.match(actions, /v-else class="act-btn"[\s\S]*?ask\('approve', a\)/, '其余行的直接动作是批准')
  // 编辑入口：每卡**唯一**一支铅笔
  const editBtns = actions.match(/class="ac-edit"/g) || []
  assert.equal(editBtns.length, 1, '一张卡只许有一支铅笔（一个编辑入口）')
  assert.match(actions, /@click="openEdit\(a\)"/, '铅笔走 openEdit')
  // 🔴 反钉：卡面上不许再并排挂治理动作（设主 / 排除 / 停凭据 / 换发 / 删除）
  for (const kind of ['role', 'exclude', 'clearcred', 'rotate', 'delete']) {
    assert.doesNotMatch(actions, new RegExp(`ask\\('${kind}'`), `卡面不许出现 ${kind} 入口（收进编辑弹框）`)
  }
  // 开关类在卡面上只做**只读** chip（拨开关的地方在编辑弹框里）
  assert.doesNotMatch(actions, /el-switch/, '卡面不许挂开关件（只读 chip 即可）')
})

// ---------------------------------------------------------------------------
// 重新生成凭据（换发）：approve 本就是换发语义，前端只加一个入口
// ---------------------------------------------------------------------------
//
// 已批准的代理要能自己换一把凭据（泄露场景）：后端 POST .../approve **本就是换发**
// （重新调用即换发、旧的那条随即失效），所以零后端改动，这页只加一个入口：
//   1) 入口只开在「已批准」行上（待审的去了审核页、已吊销的走「重新批准」）；
//   2) 走同一套 kind 机制的手输确认框：照抄 agent_id 前 8 位才让提交；
//   3) 确认后打 approve 端点，新凭据走**两页共用**的那一个 CredentialDialog；
//   4) 🔴 失败不许留下「以为换发了」的误导：红条 + 不弹凭据框。

/**
 * 从 `openMarker`（如 `<template v-else>`）起做 <template> 嵌套配对，
 * 返回它所属分支闭合 `</template>` 的下标；找不到返回 -1。
 * 用途：断言某元素必须落在该分支**外面**——`indexOf(x) > indexOf('<template v-else>')`
 * 是恒真断言（分支内部的元素同样在开标记之后），必须比到闭合位置。
 */
function templateBranchEnd(block, openMarker) {
  const open = block.indexOf(openMarker)
  if (open === -1) return -1
  let depth = 0
  const re = /<\/?template\b/g
  let m
  re.lastIndex = open
  while ((m = re.exec(block)) !== null) {
    depth += m[0].startsWith('</') ? -1 : 1
    if (depth === 0) return m.index
  }
  return -1
}

/** 「重新生成凭据」按钮那一段（2026-10-24 起在**编辑弹框**的危险区里）。 */
function rotateButton() {
  const at = VIEW.indexOf("askFromEdit('rotate')")
  assert.ok(at !== -1, '没有「重新生成凭据」入口')
  const start = VIEW.lastIndexOf('<el-button', at)
  assert.ok(start !== -1, '入口不是按钮')
  return VIEW.slice(start, at + "askFromEdit('rotate')".length)
}

test('「重新生成凭据」在编辑弹框的危险区里；只对已批准行开', () => {
  const btn = rotateButton()
  // 门只开在已批准行上：待审行（去审核页）与已吊销行（走「重新批准」）都拿不到这个钮
  assert.match(btn, /v-if="editRow\?\.status === 'approved'"/, '只有已批准行才给换发钮')
  assert.doesNotMatch(VIEW, /a\.status === 'pending'/, '本页压根不渲染待审行，遑论给待审行换发')
  // 危险区里它**不是** danger 那一档（吊销 / 删除那种红底留给真会毁掉东西的动作）
  assert.doesNotMatch(btn, /type="danger"/, '换发不占危险色那一档')
  // 🔴 换发对主代理无副作用：主代理行也要能换（主脑走本机 bootstrap，不吃这把凭据）
  assert.match(VIEW, /<el-button\s+v-if="editRow\?\.status === 'approved'"/, '主代理行照样给换发入口')
})

test('换发走手输闸：照抄 agent_id 前 8 位才让提交', () => {
  // 闸 = 吊销那一道（同为作废旧凭据）：agent_id 前 8 位
  assert.match(VIEW, /if \(dlgKind\.value === 'rotate'\) return revokePrefix\.value/, '换发要手输前 8 位')
  assert.match(VIEW, /:expect="dlgExpect"/, '确认框吃的就是这个 expect')
  // ConfirmDialog 本体那把闸：串不对就不许点确认（不给正确前缀 = 提交不了）
  const CD = read('components/ConfirmDialog.vue')
  assert.match(
    CD,
    /const armed = computed\(\(\) => !props\.expect \|\| typed\.value\.trim\(\) === props\.expect\)/,
    '串不对不许提交',
  )
  // 后果文案要说清三件事
  assert.match(VIEW, /旧凭据立即作废/, '要说清旧凭据立即作废')
  assert.match(VIEW, /使用该凭据的客户端最迟 60 秒内被拒/, '要说清客户端最迟 60 秒内被拒')
  assert.match(VIEW, /代理状态与记忆归属不变/, '要说清状态与记忆归属不变')
  // 确认后打 approve 端点（confirm: true 由 api 层带）
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body.slice(0, 900), /api\.approveAgent\(row\.agent_id\)/, '换发走 approve 端点（换发语义在后端）')
  // 新凭据走**两页共用**的那一个弹框（不许自己再留一份显示逻辑）
  assert.match(VIEW, /<CredentialDialog/, '用共用弹框')
  assert.doesNotMatch(VIEW, /<pre class="pre">\{\{ cred \}\}<\/pre>/, '不许自己再留一份显示逻辑')
})

test('换发失败：不弹凭据框，红条透出后端 message（不留「以为换发了」的误导）', () => {
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  const catchBlock = body.slice(body.indexOf('} catch (e) {'))
  const errBlock = catchBlock.slice(0, catchBlock.indexOf('} finally'))
  assert.match(errBlock, /showError\(/, '失败走红条')
  assert.match(errBlock, /e\.message/, '后端 message 原样透出')
  assert.doesNotMatch(errBlock, /credOpen\.value = true/, '失败路径不许弹凭据框')
  // 就算后端 200 却没回明文，也不许弹一个空框（空框 = 让人以为换成了）
  assert.match(VIEW, /function showCred\(r\) \{/, '凭据在本页只有一个入口')
  assert.match(VIEW, /if \(!r\?\.credential\) \{/, '没有明文不开框')
})

test('搜索 + 服务端分页：filter-panel 三件套 + PagerBar 接线', () => {
  // 搜索框（回车查 / 清空回全量 / 状态三分支）
  assert.match(VIEW, /class="filter-panel"/, '搜索面板走全站统一的 filter-panel')
  assert.match(VIEW, /v-model="q"/, '输入框绑 q')
  assert.match(VIEW, /@keyup\.enter="search"/, '回车即查')
  assert.match(VIEW, /@clear="onClear"/, '清空 ✕ 回全量')
  assert.match(VIEW, /placeholder="搜索 agent_id 或名称（清空回全量）"/, '占位符说清搜什么')
  assert.match(VIEW, /<button class="soft-btn" :disabled="!dirty" @click="onClear">清除筛选<\/button>/, '有「清除筛选」')
  // seq 防串：连着改词/翻页时旧响应必须丢弃
  assert.match(VIEW, /let reqSeq = 0/, '有请求序号')
  assert.match(VIEW, /const seq = \+\+reqSeq/, '每次请求领号')
  assert.match(VIEW, /if \(isStale\(seq, reqSeq\)\) return/, '过期响应只丢弃不覆盖')
  assert.match(VIEW, /import \{ isStale \} from '\.\.\/utils\/kb'/, '复用现成的 isStale，不重写')
  // 空态三分支：筛选无结果 vs 全库为空
  assert.match(VIEW, /<p v-if="dirty">\s*\n\s*<b>没有匹配到这个词<\/b>/, '筛选无结果说人话')
  assert.match(VIEW, /<p v-else>\s*\n\s*<b>还没有代理接入过<\/b>/, '全库为空是另一套话')
})

test('分页：PagerBar 复用，q/page/page_size 全部透传给后端', () => {
  assert.match(VIEW, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/, '复用现成的 PagerBar')
  assert.match(VIEW, /<PagerBar[\s\S]*?:total="count"/, '总数取后端的 count（过滤后总数）')
  assert.match(VIEW, /:page="page"/, '当前页')
  assert.match(VIEW, /:page-size="pageSize"/, '每页条数')
  assert.match(VIEW, /@update:page="onPage"/, '翻页接线')
  assert.match(VIEW, /@update:page-size="onSize"/, '改每页条数接线')
  // 服务端分页：三个参数都透传（不在前端切片）
  const body = VIEW.slice(VIEW.indexOf('async function load'))
  assert.match(body.slice(0, 700), /q: \(q\.value \|\| ''\)\.trim\(\) \|\| undefined/, 'q 透传（空即 undefined）')
  assert.match(body.slice(0, 700), /page: page\.value/, 'page 透传')
  assert.match(body.slice(0, 700), /page_size: pageSize\.value/, 'page_size 透传')
  // 改词/改条数一律回第 1 页（在第 5 页搜词却停在第 5 页 = 空列表）
  assert.match(VIEW, /function search\(\) \{\s*\n\s*page\.value = 1/, '改词回第 1 页')
  assert.match(VIEW, /function onSize\(s\) \{\s*\n\s*pageSize\.value = s\s*\n\s*page\.value = 1/, '改条数回第 1 页')
})

test('待审已搬走：登记页不留待审痕迹（那一摞在「代理审核」页）', () => {
  // 反钉：pending 的行、待审计数、待审红条都不许留在这页（搬走不是复制）
  assert.doesNotMatch(VIEW, /pendingCount/, '登记页不该再有待审计数（它在审核页与侧栏徽标上）')
  assert.doesNotMatch(VIEW, /v-if="pendingCount > 0" class="banner banner--danger"/, '待审红条搬去审核页')
  assert.doesNotMatch(VIEW, /个代理待审核/, '待审提示搬去审核页')
  assert.doesNotMatch(VIEW, /a\.status === 'pending'/, '登记页不渲染 pending 行')
  // 搬走不是删除：审核页接手了这些（正向断言在 agents-review-page.test.mjs）
  assert.match(read('views/AgentsReviewView.vue'), /pendingCount/, '审核页才有待审计数')
})

test('两组分组：已批准在上、已吊销在下（待审不在本页）', () => {
  assert.match(VIEW, /const approvedRows = computed\(\(\) => agents\.value\.filter\(\(a\) => a\.status === 'approved'\)\)/, '已批准那一摞')
  assert.match(VIEW, /const revokedRows = computed\(\(\) => agents\.value\.filter\(\(a\) => a\.status === 'revoked'\)\)/, '已吊销那一摞（保留展示，可重新批准）')
  assert.match(VIEW, /title: '已批准'/, '分组标题')
  assert.match(VIEW, /title: '已吊销'/, '分组标题')
  assert.match(VIEW, /<article v-for="a in grp\.rows"/, '卡片循环走的是筛过的两摞')
  // 已吊销的行仍能「重新批准」（这是既有行为，别在拆页时丢了）
  assert.match(VIEW, /a\.status === 'revoked' \? '重新批准' : '批准'/, '已吊销的行走重新批准')
})

test('自动刷新：10s 静默重拉，卸载必须清定时器', () => {
  assert.match(VIEW, /const REFRESH_MS = 10000/, '刷新周期是 10 秒')
  assert.match(VIEW, /timer = setInterval\(\(\) => load\(true\), REFRESH_MS\)/, '定时器走静默重拉')
  assert.match(VIEW, /onUnmounted\(\(\) => \{\s*\n\s*if \(timer\) clearInterval\(timer\)/, '卸载时必须 clearInterval')
  assert.match(VIEW, /onUnmounted\(/)
})

test('刷新失败保留旧数据不清屏', () => {
  // 窗口 800：load() 里多了一行「共享开关的乐观值也丢掉」（ks_share_memory），
  // 断言看的是**失败路径那一段**，窗口跟着长一点，判据本身没变。
  const body = VIEW.slice(VIEW.indexOf('async function load'))
  assert.match(body.slice(0, 800), /const list = await api\.listAgents\(\{/, '列表按 q/page 透传')
  assert.match(body.slice(0, 800), /data\.value = list/, '成功才整份替换')
  assert.doesNotMatch(body.slice(0, 800), /agents\.value = \[\]/, '失败路径不许把列表清空')
  assert.match(body.slice(0, 800), /error\.value = `读取代理列表失败/, '失败要有可见提示')
  assert.match(body.slice(0, 800), /if \(lastErr !== error\.value\) showError/, '连着失败只报一次，不刷屏')
})

test('空态说的是「为什么空 + 接下来点哪里」', () => {
  assert.match(VIEW, /class="empty"/, '空态走 .empty 规范')
  assert.match(VIEW, /还没有代理接入过/, '空态标题是人话')
  assert.match(VIEW, /让子代理的 MCP 连上来/, '空态要指下一步')
  assert.doesNotMatch(VIEW, /暂无数据/, '禁干巴巴的措辞')
})

test('无裸 alert / confirm', () => {
  assert.doesNotMatch(VIEW, /[^.\w](alert|confirm)\s*\(/, '禁原生弹框，统一走 utils/dialog 与 ConfirmDialog')
  assert.match(VIEW, /import \{ showError, showToast \} from '\.\.\/utils\/dialog'/, '提示走现成的 dialog 工具')
  assert.doesNotMatch(VIEW, /window\.confirm|window\.alert/, '全局弹框也不许')
})

// ---------------------------------------------------------------------------
// 编辑入口（代理 ID + 备注名**同框**，一个弹框）
// ---------------------------------------------------------------------------
//
// 两个铅笔（id 旁 + 备注名旁）合并成卡片操作区的一个「编辑」按钮：
//   1) 一张卡**一个**编辑入口 —— 行内不再有铅笔钮（复制钮还在，它不是编辑）；
//   2) 弹框里 ID 与备注名同框，回车提交（不提交表单）；🔴 提交顺序是先
//      set_id 再 rename —— 改完 id 旧 key 就没了，rename 必须打新 id；
//   3) 🔴 primary 行的 ID 输入框 disabled：主脑的引导配置绑在 id 上（后端同口径
//      400），但备注名照改 —— primary 只锁 id，不锁名字。

/** doEdit 的函数体（截到下一个顶层声明为止）：别把 doAction 混进来。 */
function doEditBody() {
  const start = VIEW.indexOf('async function doEdit')
  const end = VIEW.indexOf('const dlgOpen', start)
  assert.ok(start !== -1 && end > start, 'doEdit 函数不在预期位置')
  return VIEW.slice(start, end)
}

test('卡片操作区有「编辑」入口（唯一一支铅笔），行内不再有第二支', () => {
  const actions = actionsBlock()
  assert.match(actions, /class="ac-edit"[\s\S]*?@click="openEdit\(a\)"/, '操作区要有「编辑」入口')
  assert.match(actions, /title="编辑：角色 \/ 开关 \/ 凭据"/, '铅笔钮要说清编辑的是什么')
  // 反钉：旧的两个行内铅笔（改 id / 改名）都不许留 —— 一张卡一个编辑入口
  assert.doesNotMatch(VIEW, /id-edit/, 'id 旁的铅笔钮已并入「编辑」')
  assert.doesNotMatch(VIEW, /name-edit/, '备注名旁的铅笔钮已并入「编辑」')
  assert.doesNotMatch(VIEW, /openSetId|openRename/, '旧的两个编辑入口函数不许留')
  // 复制钮保留（复制 id 仍是最常用操作 —— 它不是编辑）
  assert.match(VIEW, /class="copy-btn" title="复制 agent_id"/, '复制钮保留')
})

test('编辑弹框：ID + 备注名同框，两字段预填当前值 + 回车提交', () => {
  assert.match(VIEW, /<el-dialog[\s\S]*?v-model="editOpen"[\s\S]*?title="编辑代理"/,
    '合并弹框要渲染')
  assert.match(VIEW, /v-model="editId"/, '字段一：代理 ID')
  assert.match(VIEW, /v-model="editName"/, '字段二：备注名')
  assert.match(VIEW, /editId\.value = row\?\.agent_id \|\| ''/, 'ID 预填当前值')
  assert.match(VIEW, /editName\.value = row\?\.name \|\| ''/, '备注名预填当前值')
  assert.match(VIEW, /@keyup\.enter\.prevent="doEdit"/, '回车提交（且不提交表单）')
  assert.match(VIEW, /:close-on-click-modal="false"/, '弹框禁遮罩关闭')
  // 后果说明两段都在（凭据不变 + 记忆归属跟着搬）
  assert.match(VIEW, /凭据与权限不变/, '要说清凭据与权限不变（不用重配客户端）')
  assert.match(VIEW, /历史记忆的归属会一起搬/, '要说清历史记忆归属会一起搬')
})

test('提交：先 set_id 再 rename；set_id 失败即中止；toast 合并说一条', () => {
  const body = doEditBody()
  const idAt = body.indexOf('/set_id`')
  const renameAt = body.indexOf('/rename`')
  assert.ok(idAt !== -1 && renameAt !== -1, '两个端点都要打')
  assert.ok(idAt < renameAt, '🔴 必须先 set_id 再 rename（改完 id 旧 key 就没了）')
  assert.match(body, /new_id: newId/, '带上新 id')
  assert.match(body, /confirm: true/, '带 confirm 闸')
  assert.match(body, /encodeURIComponent\(targetId\)\}\/rename/, 'rename 打的是**新** id')
  // toast 合并说一条（按实际发生的部分拼）
  assert.match(body, /已更新：/, '成功 toast 合并说一条')
  assert.match(body, /ID 已改为/, 'toast 里说得清 id 改成什么')
  assert.match(body, /moved_memories/, 'toast 里带搬动条数')
  assert.match(body, /备注名已保存/, 'toast 里说得清备注名改了')
  assert.match(body, /load\(true\)/, '改完重拉列表')
  // set_id 失败（409/400）：红条透出后端 message 并**中止**（不许接着 rename）
  const idBlock = body.slice(body.indexOf('if (idChanged)'), body.indexOf('if (nameChanged)'))
  assert.match(idBlock, /showError\(`改 ID 失败：\$\{e\.message\}`\)/,
    '后端的人话文案原样透出（别吞成「请求失败」）')
  assert.match(idBlock, /return/, 'set_id 失败必须中止（对旧 id 做 rename 是错操作）')
  // 备注名非空护栏（原改名弹框有，合并后不许丢）：前端先拦，别等后端 400
  assert.match(body, /if \(nameChanged && !newName\)/, '空备注名在前端就被拦')
})

test('primary 行：ID 输入框 disabled（能改备注名，不能改 ID）', () => {
  assert.match(VIEW, /:disabled="editPrimary"/, '主代理的 ID 输入框要锁死')
  assert.match(VIEW, /const editPrimary = computed\(\(\) => editRow\.value\?\.role === 'primary'\)/,
    'primary 由角色判定（不能只看当前页有没有 primary）')
  assert.match(VIEW, /主代理 ID 不可改/, '要说清为什么锁死')
})

// ---------------------------------------------------------------------------
// 停凭据 / 删除（ks_agent_delete）：三级语义的后两档
// ---------------------------------------------------------------------------
//
//   吊销   = 停权限、留一条吊销记录
//   停凭据 = 只废那把钥匙（代理照旧在册、状态不动）
//   删除   = 整行抹掉（删前备份，可人工复原）

/** 「停凭据」按钮那一段（2026-10-24 起在**编辑弹框**的危险区里）。 */
function clearCredButton() {
  const at = VIEW.indexOf("askFromEdit('clearcred')")
  assert.ok(at !== -1, '没有「停凭据」入口')
  const start = VIEW.lastIndexOf('<el-button', at)
  assert.ok(start !== -1, '入口不是按钮')
  return VIEW.slice(start, at + "askFromEdit('clearcred')".length)
}

/** 「删除」按钮那一段。 */
function deleteButton() {
  const at = VIEW.indexOf("askFromEdit('delete')")
  assert.ok(at !== -1, '没有「删除」入口')
  const start = VIEW.lastIndexOf('<el-button', at)
  assert.ok(start !== -1, '入口不是按钮')
  return VIEW.slice(start, at + "askFromEdit('delete')".length)
}

test('停凭据：只有「已批准且有凭据提示」的行才渲染这个钮', () => {
  const btn = clearCredButton()
  assert.match(btn, /v-if="editRow\?\.status === 'approved' && editRow\?\.credential_hint"/,
    '门开在「已批准 + 有凭据提示」上（没凭据没得停）')
  // 它在**编辑弹框**里（不在卡面）：卡面只留一个直接动作 + 一支铅笔
  assert.ok(VIEW.indexOf("askFromEdit('clearcred')") > VIEW.indexOf('title="编辑代理"'),
    '停凭据钮在编辑弹框里')
})

test('删除钮只给已吊销的行：approved 的行绝不给删除钮', () => {
  const btn = deleteButton()
  assert.match(btn, /v-if="editRow\?\.status === 'revoked'"/, '删除钮只挂在已吊销行上')
  assert.match(btn, /type="danger"/, '删除是危险色那一档')
  // 🔴 反钉：approved 的行（在跑的活代理）绝不能出现删除入口 —— 先吊销再删，两步隔离
  assert.doesNotMatch(VIEW.slice(VIEW.indexOf('title="编辑代理"')), /v-if="editRow\?\.status === 'approved'[\s\S]{0,200}删除/,
    'approved 的行不许有删除入口')
})

test('两道新手输闸：停凭据 = 前 8 位，删除 = 完整 agent_id', () => {
  assert.match(VIEW, /if \(dlgKind\.value === 'clearcred'\) return revokePrefix\.value/,
    '停凭据复用吊销那道闸（agent_id 前 8 位）')
  assert.match(VIEW, /if \(dlgKind\.value === 'delete'\) return dlgRow\.value\?\.agent_id \|\| ''/,
    '删除要手输完整 agent_id（比吊销高一档）')
  assert.match(VIEW, /dlgKind\.value === 'delete'[\s\S]{0,80}\? 'danger'/, '删除是 danger 那一档')
})

test('两条新端点各自打各自的：confirm 口径与后端逐字一致', () => {
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body, /revoke_credential`, \{ confirm: true \}/, '停凭据发 confirm=true')
  assert.match(
    body,
    /\.delete\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(row\.agent_id\)\}`, \{ data: \{ confirm: typed \} \}\)/,
    '删除发的是用户手输的完整 agent_id',
  )
  assert.match(body, /load\(true\)/, '改完重拉列表（状态以服务器为准）')
})

test('页面说明写清三级语义（吊销 / 停凭据 / 删除）', () => {
  assert.match(VIEW, /吊销 = 停权限、留一条吊销记录/, '说清吊销是什么')
  assert.match(VIEW, /停凭据 = 只废那把钥匙/, '说清停凭据是什么')
  assert.match(VIEW, /删除 = 整行抹掉/, '说清删除是什么')
  assert.match(VIEW, /可人工复原/, '说清删了还能救回来')
})
