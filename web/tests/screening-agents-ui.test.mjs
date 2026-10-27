/**
 * 检索筛选配置面（**唯一编辑入口在「AI 配置」页**）+ 代理治理页卡片重做。
 *
 * 两块，都是「结构断言」：盯住**接线**与**收敛**，盯不住的地方一旦回退成
 * 「一排小按钮 / 两个保存键」，本单的红线就漏了。
 *
 * A. 检索筛选（2026-10 ks_maint_ui_dedup 断言反转）：
 *   1) **记忆维护页的旧筛选卡彻底摘净**（草稿 / 表单 / 保存入口 / 直连端点
 *      一个都不许留 —— 两个编辑入口 = 用户不知道自己改的是哪一份配置）；
 *   2) 维护页只留**一行迁移指引** + 指向 /ai-settings 的链接；
 *   3) 「AI 配置」页是 screening 的**唯一编辑面**：通用 settings/ai 端点，
 *      数字与上下限照抄后端 bounds、凭据只显「已设置 / 未设置」、保存走既有
 *      重确认件（后果逐条列明）。
 *
 * B. 代理卡（AgentsView）：
 *   1) 卡面动作**收敛**：一个直接动作（批准 / 吊销）+ 唯一一支铅笔；
 *   2) 其余动作全收进编辑弹框，分三组：角色 / 开关 / 凭据（危险区）；
 *   3) 凭据危险区走**二次确认** + 一次性展示新 key（口径与现行为一致）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const MAINT = read('views/MaintenanceView.vue')
const AGENTS = read('views/AgentsView.vue')
const AISET = read('views/AiSettingsView.vue')
const API = read('api/index.js')

/** src/views 下所有 .vue —— 用于「全站只有一个 screening 编辑入口」的扫描。 */
const VIEWS = readdirSync(`${SRC}/views`).filter((n) => n.endsWith('.vue'))

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
// A. 检索筛选：唯一编辑入口在「AI 配置」页
// ---------------------------------------------------------------------------

test('记忆维护页的旧筛选卡彻底摘净（零残留）', () => {
  for (const sym of ['scDraft', 'scMeta', 'scBusy', 'scConfirmOpen', 'scMin', 'scMax',
    'sc-card', 'sc-head', 'sc-title', 'sc-grid', 'settings/screening',
    'askSaveScreening', 'saveScreening', 'loadScreening', 'scConsequences']) {
    assert.ok(!MAINT.includes(sym), `记忆维护页里还留着「${sym}」—— 旧筛选卡没摘净`)
  }
  // 旧卡的标题行 / 表单 / 保存按钮一个都不许在
  assert.doesNotMatch(MAINT, /检索结果 LLM 筛选/, '旧卡标题不许还在')
  assert.doesNotMatch(MAINT, /class="section-title">检索筛选/, '旧卡的小节标题不许还在')
  assert.doesNotMatch(MAINT, /v-model="scDraft\./, '旧卡的表单控件不许还在')
  // 「检索筛选」这个词全页只许出现一次（就是那句迁移指引，不许再有第二处配置面）
  assert.equal(MAINT.split('检索筛选').length - 1, 1, '「检索筛选」只许出现在迁移指引里')
})

test('维护页留了迁移指引：灰字 + 指向 /ai-settings 的链接', () => {
  const note = MAINT.match(/<p class="src migrate-note">[\s\S]*?<\/p>/)
  assert.ok(note, '要有迁移指引段落')
  assert.match(note[0], /检索筛选的配置已统一到「AI 配置」页/, '要有迁移指引文案')
  assert.ok((MAINT.match(/to="\/ai-settings"/g) || []).length >= 1, '要有指向 /ai-settings 的链接')
  // 链接沿用既有链接样式类（不新造颜色）
  assert.match(note[0], /<router-link class="card-link" to="\/ai-settings">/, '链接走既有 .card-link')
})

test('「AI 配置」页是 screening 的唯一编辑面（通用 settings/ai 端点）', () => {
  // 端点：GET 快照 + PUT 保存（section_id 传 screening）
  assert.match(API, /aiSettings: \(\) => http\.get\('\/api\/v1\/settings\/ai'\)/)
  assert.match(API, /setAiSettings: \(sectionId, fields\) =>/)
  assert.match(API, /\/api\/v1\/settings\/ai\/\$\{encodeURIComponent\(sectionId\)\}/, '保存按 section_id 写')
  // 页面按后端给的 sections 逐节成卡 —— screening 是其中一节，必然在这一页
  assert.match(AISET, /v-for="card in cards"/)
  assert.match(AISET, /cards\.value = buildCards\(data\.sections\)/)
  assert.match(AISET, /api\.setAiSettings\(card\.id, payload\)/, '保存按卡片节 id 写')
  // 🔴 全站再不许有第二个 screening 编辑入口（直连旧端点的地方一个都不许剩）
  const offenders = VIEWS.filter((n) => read(`views/${n}`).includes('settings/screening'))
  assert.deepEqual(offenders, [], 'screening 的编辑入口必须唯一')
})

test('AI 配置页的筛选纪律没丢：上下限照抄后端 / 凭据只显状态 / 保存走重确认件', () => {
  // 数字零写死：上下限照抄 GET 给的 min/max
  assert.match(AISET, /:min="f\.min"/)
  assert.match(AISET, /:max="f\.max"/)
  assert.doesNotMatch(AISET, /:min="\d|:max="\d/, '前端不许自己写死上下限')
  // 凭据真值永不回显：只显「已设置 / 未设置」
  assert.match(AISET, /'已设置' : '未设置'/)
  assert.doesNotMatch(AISET, /f\.value\.key|f\.value\.api_key/, '凭据真值不许回显')
  // 保存走既有重确认件，后果逐条列明
  assert.match(AISET, /<ConfirmDialog/)
  assert.match(AISET, /只改 config\.json 的 \$\{c\.id\} 一节/, '要说清只动哪一节')
  assert.match(AISET, /写前会把原 config 整份备份到/, '要说清备份落在哪')
  // 禁遮罩关闭是 ConfirmDialog 组件自己的纪律（复用它就自带）
  assert.match(read('components/ConfirmDialog.vue'), /:close-on-click-modal="false"/,
    '确认弹框禁遮罩关闭')
})

test('两个页面都没有裸 alert / confirm（禁原生弹框）', () => {
  for (const [name, src] of [['MaintenanceView', MAINT], ['AiSettingsView', AISET]]) {
    const bare = src.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
    assert.deepEqual(bare, [], `${name} 必须走 ConfirmDialog / utils/dialog`)
  }
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
