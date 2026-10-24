/**
 * 检索筛选配置面（记忆维护页底部）+ 代理治理页卡片重做（2026-10-24）。
 *
 * 两块，都是「结构断言」：盯住**接线**与**收敛**，盯不住的地方一旦回退成
 * 「一排小按钮 / 两个保存键」，本单的红线就漏了。
 *
 * A. 检索筛选卡（settings/screening）：
 *   1) 一张卡**一个保存按钮**（不许双保存键 —— 两个保存口 = 用户不知道自己
 *      改的是哪个）；
 *   2) 数字**全部来自 GET**（连输入框的上下限都是后端给的 bounds）—— 前端
 *      零写死；
 *   3) `key_present` 徽标**当场可见**（填错路径 ⇒ 筛选一路 fail-open 成
 *      「静默不筛」，不看徽标就发现不了）；
 *   4) 保存前走既有重确认件，后果逐条列明（改哪一节 / 备份去哪 / 怎么生效）。
 *
 * B. 代理卡（AgentsView）：
 *   1) 卡面动作**收敛**：一个直接动作（批准 / 吊销）+ 唯一一支铅笔；
 *   2) 其余动作全收进编辑弹框，分三组：角色 / 开关 / 凭据（危险区）；
 *   3) 凭据危险区走**二次确认** + 一次性展示新 key（口径与现行为一致）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const MAINT = read('views/MaintenanceView.vue')
const AGENTS = read('views/AgentsView.vue')

/** 检索筛选卡那一段（从标题到分级班车设置弹框之前）。 */
function screeningCard() {
  const start = MAINT.indexOf('<h3 class="section-title">检索筛选</h3>')
  assert.ok(start !== -1, '记忆维护页底部没有「检索筛选」卡')
  const rest = MAINT.slice(start)
  const end = rest.indexOf('<!-- 分级班车设置')
  return rest.slice(0, end === -1 ? rest.length : end)
}

/** 代理卡的操作区（卡底那一段）。 */
function agentCardFoot() {
  const start = AGENTS.indexOf('<div class="ac-foot">')
  assert.ok(start !== -1, '找不到代理卡的卡底操作区')
  const rest = AGENTS.slice(start)
  const end = rest.indexOf('</article>')
  return rest.slice(0, end === -1 ? rest.length : end)
}

/** 编辑弹框那一段。 */
function editDialog() {
  const start = AGENTS.indexOf('title="编辑代理"')
  assert.ok(start !== -1, '找不到编辑弹框')
  const rest = AGENTS.slice(start)
  const end = rest.indexOf('</el-dialog>')
  return rest.slice(0, end === -1 ? rest.length : end)
}

// ---------------------------------------------------------------------------
// A. 检索筛选卡
// ---------------------------------------------------------------------------

test('检索筛选卡在记忆维护页底部，且只有**一个**保存按钮', () => {
  const card = screeningCard()
  const saves = card.match(/@click="askSaveScreening"/g) || []
  assert.equal(saves.length, 1, '一张卡一个保存入口（不许双保存键）')
  assert.match(card, /<el-button type="primary" :loading="scBusy" @click="askSaveScreening">保存<\/el-button>/,
    '保存按钮文案是「保存」')
  // 打开的是既有重确认件，不是裸 confirm
  assert.match(card, /<ConfirmDialog/, '保存前弹既有重确认件')
  assert.match(card, /@confirm="saveScreening"/, '确认后走 saveScreening')
  // 禁遮罩关闭是 ConfirmDialog 组件自己的纪律（复用它就自带）
  assert.match(read('components/ConfirmDialog.vue'), /:close-on-click-modal="false"/,
    '确认弹框禁遮罩关闭')
})

test('后果说明逐条列明：改哪一节 / 备份去哪 / MCP 怎么生效', () => {
  assert.match(MAINT, /只改 config\.json 的 screening 一节/, '要说清只动哪一节')
  assert.match(MAINT, /写前会把原 config 整份备份到/, '要说清备份落在哪')
  assert.match(MAINT, /MCP 服务热生效：下一轮检索就用新值，不需重启任何进程/, '要说清怎么生效')
  // 保存请求带 confirm: true（后端要显式二次确认）
  assert.match(MAINT, /put\('\/api\/v1\/settings\/screening', \{ confirm: true, \.\.\.scDraft\.value \}\)/,
    'PUT 必须带 confirm: true')
})

test('数字全部来自 GET：前端零写死（连上下限都跟后端 bounds）', () => {
  const card = screeningCard()
  assert.match(card, /v-model="scDraft\.max_pick"/, '最多保留条数绑草稿')
  assert.match(card, /v-model="scDraft\.timeout_s"/, '超时绑草稿')
  assert.match(card, /v-model="scDraft\.max_tokens"/, 'token 上限绑草稿')
  // 上下限来自后端 bounds（不写死 1 / 30 / 10 / 300 / 512 / 16384）
  assert.match(card, /:min="scMin\('max_pick'\)"/, '上下限走后端给的 bounds')
  assert.match(card, /:max="scMax\('max_tokens'\)"/, '上下限走后端给的 bounds')
  assert.doesNotMatch(card, /max_pick="(\d+)"|timeout_s="(\d+)"|max_tokens="(\d+)"/,
    '数字不许写死在模板上')
  // 草稿初值来自 GET（loadScreening 覆盖）
  assert.match(MAINT, /http\.get\('\/api\/v1\/settings\/screening'\)/, '读 GET')
  assert.match(MAINT, /max_pick: Number\(r\.max_pick\) \|\| 0/, '草稿按 GET 填')
})

test('key_present 徽标当场可见（绿点=文件在 / 黄=路径没找到）', () => {
  const card = screeningCard()
  assert.match(card, /scMeta\.key_present/, '徽标读 key_present')
  assert.match(card, /key 文件在/, '有文件时的文案')
  assert.match(card, /key 文件没找到/, '没找到时的文案')
  // 徽标用现成令牌（不新造颜色）
  assert.match(card, /class="dot-ok"/, '绿点走全局 .dot-ok')
  assert.match(card, /chip--warn/, '没找到走 chip--warn')
  // 没配过这一节也要说清
  assert.match(card, /config\.json 里还没有这一节/, '「没配过」与「配成默认值」要分得开')
})

test('两个开关都在卡上（总开关 / 默认开），且默认开的语义说清了', () => {
  const card = screeningCard()
  assert.match(card, /v-model="scDraft\.enabled"/, '总开关')
  assert.match(card, /v-model="scDraft\.default_on"/, '默认开开关')
  assert.match(card, /代理显式传 screen=false 依然不筛/, '要说清「显式传 false 仍然不筛」')
  assert.match(card, /关着时一切行为与接入前逐字一致/, '要说清默认关死')
})

test('保存失败要报红条（不许留下「以为改成了」）', () => {
  assert.match(MAINT, /保存检索筛选配置失败：\$\{e\.message\}/, '失败红条透出后端 message')
  assert.doesNotMatch(MAINT, /(?<![A-Za-z0-9_.$])(alert|confirm)\s*\(/, '禁原生弹框')
})

// ---------------------------------------------------------------------------
// B. 代理卡
// ---------------------------------------------------------------------------

test('卡面动作收敛：一个直接动作（批准 / 吊销）+ 唯一一支铅笔', () => {
  const foot = agentCardFoot()
  // 直接动作：已批准 = 吊销，其余 = 批准 / 重新批准
  assert.match(foot, /ask\('revoke', a\)/, '已批准行的直接动作是吊销')
  assert.match(foot, /ask\('approve', a\)/, '其余行的直接动作是批准')
  // 唯一一支铅笔
  assert.equal((foot.match(/class="ac-edit"/g) || []).length, 1, '一张卡只许一支铅笔')
  assert.match(foot, /@click="openEdit\(a\)"/, '铅笔走 openEdit')
  // 其余动作一个都不许留在卡面
  for (const kind of ['role', 'exclude', 'clearcred', 'rotate', 'delete']) {
    assert.doesNotMatch(foot, new RegExp(`ask\\('${kind}'`), `卡面不许有 ${kind} 入口`)
  }
  assert.doesNotMatch(foot, /el-switch/, '卡面不许挂开关件（状态只做只读 chip）')
})

test('卡头 = 名字 + 角色徽标 + 状态徽标；状态 chip 只读', () => {
  assert.match(AGENTS, /<div class="ac-head">/, '卡头')
  assert.match(AGENTS, /class="ac-name"/, '名字')
  assert.match(AGENTS, /a\.role === 'primary' \? '主代理' : '子代理'/, '角色徽标')
  assert.match(AGENTS, /class="chip" :class="statusTone\(a\)"/, '状态徽标')
  // 状态 chip：私信 / 共享 / 上下文 / 预算 —— 全是只读
  assert.match(AGENTS, /私信\{\{ dmOn\(a\) \? '开' : '关' \}\}/, '私信状态 chip')
  assert.match(AGENTS, /共享\{\{ shareOn\(a\) \? '开' : '关' \}\}/, '共享状态 chip')
  assert.match(AGENTS, /\{\{ a\.exclude_from_primary \? '不进主脑上下文' : '进主脑上下文' \}\}/, '上下文状态 chip')
})

test('编辑弹框分三组：角色 / 开关 / 凭据（危险区）', () => {
  const dlg = editDialog()
  assert.match(dlg, /<div class="eg-title">角色<\/div>/, '角色组')
  assert.match(dlg, /<div class="eg-title">开关<\/div>/, '开关组')
  assert.match(dlg, /<div class="eg-title eg-title--danger">凭据（危险区）<\/div>/, '凭据危险区')
  // 角色组：设主 / 进主脑上下文
  assert.match(dlg, /v-model="editContext"/, '进主脑上下文是开关件')
  assert.match(dlg, /askFromEdit\('role'\)/, '设为主代理')
  // 开关组：私信 / 共享记忆 / 推送 / 注入预算
  assert.match(dlg, /v-model="editDm"/, '私信开关')
  assert.match(dlg, /v-model="editShare"/, '共享记忆开关')
  assert.match(dlg, /v-model="editPush"/, '推送开关')
  assert.match(dlg, /v-model="editBudget"/, '注入预算数字')
  // 凭据组：换发 / 停凭据 / 删除（只给已吊销行）
  assert.match(dlg, /askFromEdit\('rotate'\)/, '重新生成凭据')
  assert.match(dlg, /askFromEdit\('clearcred'\)/, '停凭据')
  assert.match(dlg, /v-if="editRow\?\.status === 'revoked'"/, '删除只给已吊销行')
})

test('凭据危险区走二次确认 + 一次性展示新 key', () => {
  // 二次确认：编辑框里的按钮先关编辑框，再弹既有确认框（带手输闸）
  assert.match(AGENTS, /function askFromEdit\(kind\) \{[\s\S]*?editOpen\.value = false[\s\S]*?ask\(kind, row\)/,
    '凭据动作先关编辑框再弹确认框')
  assert.match(AGENTS, /if \(dlgKind\.value === 'rotate'\) return revokePrefix\.value/,
    '换发仍过手输闸（agent_id 前 8 位）')
  // 一次性展示：复用两页共用的 CredentialDialog
  assert.match(AGENTS, /<CredentialDialog/, '用共用的一次性凭据弹框')
  assert.match(AGENTS, /@closed="forget"/, '关框即抹掉明文')
  assert.doesNotMatch(AGENTS, /localStorage|sessionStorage|indexedDB/, '明文不许落任何持久面')
})

test('动作提交成功后刷新列表与徽标计数', () => {
  assert.match(AGENTS, /load\(true\)/, '改完重拉列表（状态以服务器为准）')
})
