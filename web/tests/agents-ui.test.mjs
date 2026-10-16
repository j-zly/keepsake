/**
 * 代理治理页（keepsake_agent_registry 的控制台面）的前端接线护栏。
 *
 * 这页的静默失败模式特别多，逐条钉死：
 *   1) **凭据纪律**：approve 的明文只许活在一次性弹框的前端内存态 ——
 *      不许出现任何持久面写入（本页连浏览器的持久存储 API 都不许碰），
 *      关掉弹框就抹掉，页面不许把明文拼进 URL。
 *   2) **三道确认闸**：approve / exclude 要 `confirm: true`，revoke 要手输
 *      agent_id 前 8 位，改角色要手输完整 agent_id —— 与后端口径逐字一致。
 *      少一闸就是「点一下真改数据」。
 *   3) **待审醒目**：pending > 0 时顶部必须有 banner，且它的读写正被拒，
 *      静默地把它排在网格里等于没提示。
 *   4) **自动刷新要收干净**：10s 定时器不 clearInterval，离开这页还在打接口。
 *   5) **主代理行不给「设为主代理」**：防的是把自己降了、还没人批。
 *   6) 无裸 alert / confirm，提示一律走 utils/dialog。
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

test('凭据展示：全文 + 复制 + 一次性红色提醒', () => {
  assert.match(VIEW, /<pre class="pre">\{\{ cred \}\}<\/pre>/, '凭据全文要显示出来（用户要抄走）')
  assert.match(VIEW, /只显示这一次，请立即转交给代理进程/, '一次性提醒是这句，且要给红色语义色')
  assert.match(VIEW, /class="warn"/, '提醒走红色语义类')
  assert.match(VIEW, /@click="doCopy\(cred\)"/, '有复制按钮（凭据形态长，只靠手抄容易错）')
  // 确认框不许让凭据被顺手截走：误点遮罩不该等于「看过就作废」
  assert.match(VIEW, /:close-on-click-modal="false"/, '凭据弹框同样禁遮罩关闭')
})

test('五条 API 齐全，且 confirm 口径与后端逐字一致', () => {
  assert.match(API, /listAgents: \(\) => http\.get\('\/api\/v1\/agents'\)/)
  assert.match(API, /approveAgent: \(agentId\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/approve`, \{ confirm: true \}\)/)
  assert.match(API, /revokeAgent: \(agentId, confirm\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/revoke`, \{ confirm \}\)/)
  assert.match(API, /setAgentRole: \(agentId, role, confirm\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/role`, \{ role, confirm \}\)/)
  assert.match(API, /setAgentExclude: \(agentId, exclude\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/exclude`, \{ confirm: true, exclude \}\)/)
})

test('三道确认闸接在 ConfirmDialog 上，串由用户手输', () => {
  assert.match(VIEW, /import ConfirmDialog from '\.\.\/components\/ConfirmDialog\.vue'/)
  assert.match(VIEW, /<ConfirmDialog[\s\S]*?@confirm="doAction"/, '四个动作都要先过确认框')
  // 吊销 = agent_id 前 8 位（与 ops 归档「key 末 8 位」同一道闸）
  assert.match(VIEW, /\(dlgRow\.value\?\.agent_id \|\| ''\)\.slice\(0, 8\)/, '吊销确认串取前 8 位')
  assert.match(VIEW, /if \(dlgKind\.value === 'revoke'\) return revokePrefix\.value/, '吊销走 8 位串')
  // 改角色 = 完整 agent_id
  assert.match(VIEW, /if \(dlgKind\.value === 'role'\) return dlgRow\.value\?\.agent_id \|\| ''/, '改角色走全串')
  // approve / exclude 不手输，后端要的是 confirm: true —— 手输串留空即可
  assert.match(VIEW, /@confirm="doAction"/, '确认回调统一收手输串')
  const body = VIEW.slice(VIEW.indexOf('async function doAction'))
  assert.match(body.slice(0, 900), /api\.approveAgent\(row\.agent_id\)/, '批准走 approveAgent')
  assert.match(body.slice(0, 900), /api\.revokeAgent\(row\.agent_id, typed\)/, '吊销把用户手输的串原样发出去')
  assert.match(body.slice(0, 900), /api\.setAgentRole\(row\.agent_id, 'primary', typed\)/, '设主代理发 role=primary')
  assert.match(body.slice(0, 900), /api\.setAgentExclude\(row\.agent_id, !row\.exclude_from_primary\)/, '上下文开关是 toggle')
})

test('主代理行不给「设为主代理」按钮', () => {
  assert.match(
    VIEW,
    /<button v-if="a\.role !== 'primary'" class="act-btn" :disabled="busy" @click="ask\('role', a\)">\s*\n?\s*设为主代理/,
    '已经是主代理的行必须没有这个按钮（防把自己降了没人批）',
  )
})

test('待审醒目：pending_count>0 走 banner--danger', () => {
  assert.match(VIEW, /v-if="pendingCount > 0" class="banner banner--danger"/, '待审数 > 0 时顶部必须红条')
  assert.match(VIEW, /个代理待审核/, '红条要说清「几个待审核」')
  // 卡片上的状态徽标同样分色：pending 正在被拒，不能跟已批准一样灰着
  assert.match(VIEW, /if \(a\.status === 'pending'\) return 'danger'/, 'pending 徽标走红色语义')
})

test('自动刷新：10s 静默重拉，卸载必须清定时器', () => {
  assert.match(VIEW, /const REFRESH_MS = 10000/, '刷新周期是 10 秒')
  assert.match(VIEW, /timer = setInterval\(\(\) => load\(true\), REFRESH_MS\)/, '定时器走静默重拉')
  assert.match(VIEW, /onUnmounted\(\(\) => \{\s*\n\s*if \(timer\) clearInterval\(timer\)/, '卸载时必须 clearInterval')
  assert.match(VIEW, /onUnmounted\(/)
})

test('刷新失败保留旧数据不清屏', () => {
  const body = VIEW.slice(VIEW.indexOf('async function load'))
  assert.match(body.slice(0, 700), /data\.value = await api\.listAgents\(\)/, '成功才整份替换')
  assert.doesNotMatch(body.slice(0, 700), /agents\.value = \[\]/, '失败路径不许把列表清空')
  assert.match(body.slice(0, 700), /error\.value = `读取代理列表失败/, '失败要有可见提示')
  assert.match(body.slice(0, 700), /if \(lastErr !== error\.value\) showError/, '连着失败只报一次，不刷屏')
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
