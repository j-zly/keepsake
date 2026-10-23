/**
 * 知识库「审核队列」（批2：非可信来源写入的知识文档先审后用）的前端接线护栏。
 *
 * 五条，全是「不接线就静默出错」的：
 *   1) **取数接线**：列表与计数都来自 GET /kb/pending 的 `pending_count` / `count`
 *      （前端零写死数字），进页面就拉一次。
 *   2) **二次确认**：批准与驳回都走 ConfirmDialog（没有裸 confirm/alert），
 *      驳回走的是「先填理由 → 再确认」两步；
 *   3) **驳回理由必填**：不给理由就点不下去（后端同样 400 reason_required）；
 *   4) **卡上信息**：状态、正文预览、同名重复警示都在（审的人得看得见才判得了）；
 *   5) **站内手建不受影响**：文档卡的编辑/删除按钮仍只由来源域（canEdit）决定 ——
 *      给手建加审核是明令禁止的回归。
 *
 * 队列本身已从 KbView 搬到独立页 KbReviewView（分页与批准/驳回都在那边），所以这里
 * 的断言对象是审核页；KbView 那边只留一行指路（见 kb-pending-chip.test.mjs）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const KBR = read('views/KbReviewView.vue')
const API = read('api/index.js')

test('取数接线：队列与计数来自 /kb/pending，前端一个数字都不写死', () => {
  assert.match(API, /kbPending: \(params\) => http\.get\('\/api\/v1\/kb\/pending', \{ params \}\)/,
    'api 层要有 kbPending（前端不自己拼队列）')
  assert.match(KBR, /api\.kbPending\(\{ page: pendPage\.value, page_size: pendSize\.value \}\)/,
    '审核页要真的调这个接口')
  assert.match(KBR, /pendCount\.value = Number\(r\?\.pending_count\) \|\| 0/,
    '页头上的数必须是后端给的 pending_count')
  assert.match(KBR, /pendTotal\.value = Number\(r\?\.count\) \|\| 0/,
    '队列全长必须是后端给的 count（待审+已驳回）')
  assert.match(KBR, /待审 \{\{ num\(pendCount\) \}\} · 队列共 \{\{ num\(pendTotal\) \}\}/,
    '页头计数行两个数都摆出来，且未拉到时显示 — 而不是拿 0 冒充「已审完」')
  // 🔴 写死判据：队列不许来自任何本地常量/假数据
  assert.ok(!/const PEND_SAMPLE\s*=/.test(KBR), '待审队列只能来自后端')
  assert.match(KBR, /catch \(e\) \{[\s\S]{0,200}?pendCount\.value = 0/,
    '接口失败要清零（不拿上一个库的旧队列冒充现状）')
  // 打开页面就拉一次：审核页不靠用户点哪儿才决定有没有数据
  assert.match(KBR, /onMounted\([\s\S]{0,400}loadPending\(\)/)
  // 队列搬走了，KbView 不许再自己拉一份（两处各自刷新会漂）
  assert.doesNotMatch(KB, /api\.kbPending/, 'KbView 不该再拉待审队列（队列已独立成页）')
})

test('批准/驳回都走 ConfirmDialog 二次确认，没有裸 confirm', () => {
  assert.match(KBR, /<ConfirmDialog[\s\S]{0,320}?v-model="reviewOpen"/,
    '审核确认框没接上')
  assert.match(KBR, /@confirm="doReview"/, '确认回调没接线')
  assert.match(KBR, /:tone="reviewKind === 'reject' \? 'danger' : 'primary'"/,
    '驳回是危险动作，按钮色要与批准分开')
  assert.match(KBR, /@click="askApprove\(row\)"/, '批准按钮没接线')
  assert.match(KBR, /@click="openReject\(row\)"/, '驳回按钮没接线')
  // 🔴 没有第三种交互：原生 confirm/alert 一律不许
  assert.doesNotMatch(KBR, /[^.\w]confirm\s*\(/, '不许用原生 confirm')
  assert.doesNotMatch(KBR, /[^.\w]alert\s*\(/, '不许用原生 alert')
  // 批准/驳回打的是既有端点，不是自造第四个
  assert.match(API, /kbApproveDocument: \(id\) =>/)
  assert.match(API, /kbRejectDocument: \(id, reason\) =>/)
  assert.match(KBR, /await api\.kbApproveDocument\(row\.doc_id\)/)
  assert.match(KBR, /await api\.kbRejectDocument\(row\.doc_id, rejectReason\.value\.trim\(\)\)/)
})

test('驳回理由必填：填完才进二次确认，后端也要有那道闸', () => {
  assert.match(KBR, /:disabled="!rejectReason\.trim\(\)"/,
    '没填理由不许点「下一步：确认驳回」')
  assert.match(KBR, /function askRejectConfirm\(\) \{\s*\n\s*if \(!rejectReason\.value\.trim\(\)\) return/,
    '二次确认那一步也要再挡一次空理由')
  assert.match(KBR, /v-model="rejectReason"/, '理由输入框没接 v-model')
  // 理由要出现在确认框的后果文案里（审核的人看得见自己批的是什么）
  assert.match(KBR, /驳回理由会记进审核台账：\$\{rejectReason\.value\.trim\(\)\}/)
})

test('待审文档：状态、正文预览、同名重复警示都在卡上', () => {
  // pending 文档此刻一块切片都没有 —— 正文只能看 review_meta 预览（要点破这件事）
  assert.match(KBR, /\{\{ row\.preview \|\| '（没有提交正文）' \}\}/)
  assert.match(KBR, /row\.status === 'rejected' \? '已驳回' : '待审'/, '状态徽标缺失')
  assert.match(KBR, /来源 \{\{ row\.submitted_by \|\| '未知' \}\}/, '来源代理要显出来')
  assert.match(KBR, /v-if="row\.dup_of"/, '同名/近重复警示没渲染')
  assert.match(KBR, /row\.dup_exact \? '同名重复' : '近重复'/, '重复类型要说清是同名还是近重复')
  // 已驳回的可再审：驳回按钮不给，批准按钮改成「重新批准」
  assert.match(KBR, /v-if="row\.status !== 'rejected'"/, '已驳回的不该再给「驳回」')
  assert.match(KBR, /row\.status === 'rejected' \? '重新批准' : '批准入库'/)
})

test('站内手建路径零变化：审核只管待审队列，不许渗进文档卡的写按钮', () => {
  // 文档卡的写按钮判据仍然只有来源域（canEdit：手动域 + 代理域），
  // 没有多一个「要不要审」分支
  assert.match(KB, /<template v-if="canEdit\(doc\)" #actions>[\s\S]{0,220}openEdit\(doc\)[\s\S]{0,120}askDelete\(doc\)/,
    '文档卡的编辑/删除按钮判据被改了')
  assert.doesNotMatch(KB, /function canEdit\(doc\) \{[\s\S]{0,200}pending/, 'canEdit 里混进了审核判据')
})
