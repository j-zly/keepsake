/**
 * 消息中心（/messages，代理互聊信箱的控制台面）的前端接线护栏。
 *
 * 纯静态扫描（node --test 直接跑，不起浏览器）。这页的静默失败模式有四条，
 * 逐条钉死：
 *   1) **按线程分组折叠**：同一个 thread_id 的往来收在一组，组头写清**双方**与
 *      **条数**（一屏消息看不出规模就等于没分组），组头可点收起 / 展开；
 *   2) **筛选参数拼接**：代理 / 关键词 / 分页全部透传给后端（服务端筛，前端不切片），
 *      条件为空就**不带这个参数**（`undefined`），不是传空串让后端猜；
 *   3) **空态说人话**：「还没有代理往来消息」+ 下一步指引（禁「暂无数据」）；
 *   4) **只读**：这页只有拉，没有改/删（用户铁律：绝不被背着开小会 ⇒ 消息不许被
 *      治理面改动；列表端点也不许把信标成已读）。
 * 另有两条：正文超 160 字截断（点开看全文）、已读/未读走 .chip 徽标（零新色）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/MessagesView.vue')
const AGENTS = read('views/AgentsView.vue')
const APP = read('App.vue')
const ROUTER = read('router/index.js')
const API = read('api/index.js')

// ---------------------------------------------------------------------------
// ① 线程分组折叠
// ---------------------------------------------------------------------------

test('按 thread_id 分组，组头写清双方与条数，组头可点收起 / 展开', () => {
  assert.match(VIEW, /const groups = computed\(\(\) => \{/, '要有按线程分组的计算')
  assert.match(VIEW, /m\.thread_id\s*\n\s*\? `t:\$\{m\.thread_id\}`/, '同一个 thread_id 归一组')
  assert.match(VIEW, /g\.ids\[0\]\}\s*⇄\s*\$\{g\.ids\[1\]\}/, '组头要写**双方**（谁跟谁在聊）')
  assert.match(VIEW, /\{\{ g\.count \}\} 条/, '组头要写条数')
  assert.match(VIEW, /@click="toggleGroup\(g\.key\)"/, '组头可点（收起 / 展开）')
  assert.match(VIEW, /v-show="!isCollapsed\(g\.key\)"/, '组内容按收起状态显示')
  // 默认全展开：治理面要一眼看得见消息
  assert.match(VIEW, /const collapsed = ref\(new Set\(\)\)/, '默认不收起任何一组')
})

// ---------------------------------------------------------------------------
// ② 筛选参数拼接
// ---------------------------------------------------------------------------

test('筛选条件与分页全部透传给后端（空即不带这个参数）', () => {
  assert.match(VIEW, /api\.listMessages\(\{/, '列表走 listMessages')
  assert.match(VIEW, /agent: \(agent\.value \|\| ''\)\.trim\(\) \|\| undefined/, '代理筛选透传（空即不带）')
  assert.match(VIEW, /q: \(q\.value \|\| ''\)\.trim\(\) \|\| undefined/, '关键词透传（空即不带）')
  assert.match(VIEW, /page: page\.value/, '页码透传')
  assert.match(VIEW, /size: pageSize\.value/, '每页条数透传（参数名是 size，不是 page_size）')
  // 代理下拉的选项来自 /api/v1/agents
  assert.match(VIEW, /api\.listAgents\(\{ page: 1, page_size: 100 \}\)/, '代理下拉来自 /api/v1/agents')
  assert.match(API, /listMessages: \(params\) => http\.get\('\/api\/v1\/messages', \{ params \}\)/, '接口层有 listMessages')
  // 分页条复用全站那一套
  assert.match(VIEW, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/, '复用现成的 PagerBar')
  assert.match(VIEW, /:total="total"/, '总数取后端的 total（过滤后总数）')
})

// ---------------------------------------------------------------------------
// ③ 空态文案
// ---------------------------------------------------------------------------

test('空态说人话：全库空与筛空是两套话', () => {
  assert.match(VIEW, /还没有代理往来消息/, '全库空的文案')
  assert.match(VIEW, /没匹配到消息/, '筛空的文案（另一套话）')
  assert.doesNotMatch(VIEW, /暂无数据/, '禁干巴巴的「暂无数据」')
  assert.match(VIEW, /<b>还没有代理往来消息<\/b>/, '空态要给下一步指引（人话）')
})

// ---------------------------------------------------------------------------
// ④ 只读面 + 正文截断 + 徽标
// ---------------------------------------------------------------------------

test('这页只有拉、没有改：绝不替用户动消息', () => {
  const calls = [...VIEW.matchAll(/api\.(\w+)\(/g)].map((m) => m[1])
  // 🆕 ks_dm_sse：多了一个 nudgeMessages —— 它**不改任何消息**（只推一帧事件
  //   摘要），页面仍然不删、不改、不标已读（用户铁律：绝不替用户动消息）。
  assert.deepEqual([...new Set(calls)].sort(), ['listAgents', 'listMessages', 'nudgeMessages'],
    '本页只许拉 + 推提醒，不许改消息')
  assert.doesNotMatch(VIEW, /\balert\(|window\.confirm/, '禁原生弹框')
  // 消息本体一个字段都不许被这页动（没有删除 / 标已读 / 改正文的接口）
  assert.doesNotMatch(VIEW, /api\.(delete|update|remove|markRead)\w*\(/, '不许动消息本体')
})

test('正文超 160 字截断，点开看全文；已读/未读走 chip 徽标', () => {
  assert.match(VIEW, /const PREVIEW_LEN = 160/, '截断长度 160')
  assert.match(VIEW, /s\.length > PREVIEW_LEN \? `\$\{s\.slice\(0, PREVIEW_LEN\)\}…`/, '超长截断加省略号')
  assert.match(VIEW, /isOpen\(m\.seq\) \? m\.content : preview\(m\.content\)/, '点一下看全文')
  assert.match(VIEW, /m\.read \? '已读' : '未读'/, '已读/未读要有徽标文案')
  assert.match(VIEW, /class="chip" :class="m\.read \? '' : 'chip--accent'"/, '未读用 accent 徽标（零新色）')
})

// ---------------------------------------------------------------------------
// 侧栏与路由接线
// ---------------------------------------------------------------------------

test('侧栏「消息中心」在代理登记之后、代理审核之前；路由有 /messages', () => {
  // 🔴 只管**先后**（消息中心在代理登记之后、代理审核之前）：中间允许插入别的
  // 入口（2026-10 待办就插在这两页之间）。
  assert.match(APP, /\{ path: '\/agents', label: '代理登记', icon: ICON\.users \},[\s\S]*?\{ path: '\/messages', label: '消息中心', icon: ICON\.mail \},[\s\S]*?\{ path: '\/agents-review', label: '代理审核'/,
    '侧栏顺序：代理登记 → 消息中心 → 代理审核')
  assert.match(ROUTER, /\{ path: '\/messages', name: 'messages', component: MessagesView/, '路由挂了 MessagesView')
  assert.match(ROUTER, /import MessagesView from '\.\.\/views\/MessagesView\.vue'/, '路由引了 MessagesView')
})

// ---------------------------------------------------------------------------
// 私信开关（代理登记页那半边）：切换走 dm 端点，失败回滚
// ---------------------------------------------------------------------------

test('私信开关：走 dm 端点（带 confirm），失败报红条', () => {
  assert.match(API, /setAgentDm: \(agentId, enabled\) =>\s*\n?\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/dm`, \{ enabled, confirm: true \}\)/,
    '接口层带 confirm（后端要显式二次确认）')
  // 2026-10-24 UI 重做：私信开关收进**编辑弹框**（卡面只做只读 chip）
  assert.match(AGENTS, /api\.setAgentDm\(targetId, wantDm\)/, '保存走 setAgentDm')
  assert.match(AGENTS, /'私信开关'/, '失败红条要说得清是哪一项没改成')
  assert.match(AGENTS, /showError\(`\$\{label\}保存失败：\$\{e\.message\}`\)/, '失败要报红条')
  assert.match(AGENTS, /function dmOn\(a\)/, '开关状态读 dm_enabled')
  assert.match(AGENTS, /私信\{\{ dmOn\(a\) \? '开' : '关' \}\}/, '卡面上是只读 chip')
  // 🔴 开关在编辑弹框里，**不按角色锁死** —— 关了自己也收不到私信，合法
  assert.match(AGENTS, /<el-switch v-model="editDm" \/>/, '私信开关不按角色锁死')
  const actions = AGENTS.slice(AGENTS.indexOf('<div class="ac-foot">'), AGENTS.indexOf('</article>'))
  assert.ok(actions.length > 0, '找不到卡片操作区')
  assert.doesNotMatch(actions, /el-switch/, '卡面不许挂开关件')
})
