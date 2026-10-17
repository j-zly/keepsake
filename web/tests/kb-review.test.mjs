/**
 * 知识库「审核队列」（批2：非可信来源写入的知识文档先审后用）的前端接线护栏。
 *
 * 四条，全是「不接线就静默出错」的：
 *   1) **chip 接线**：计数来自 GET /kb/pending 的 `pending_count`（前端零写死数字），
 *      为 0 时整行隐藏（不编一个「待审 0」），点 chip 才开面板；
 *   2) **二次确认**：批准与驳回都走 ConfirmDialog（没有裸 confirm/alert），
 *      驳回走的是「先填理由 → 再确认」两步；
 *   3) **驳回理由必填**：不给理由就点不下去（后端同样 400 reason_required）；
 *   4) **站内手建不受影响**：审核入口只挂在待审面板里，文档卡的编辑/删除按钮
 *      仍只由 isManual 决定 —— 给手建加审核是明令禁止的回归。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const API = read('api/index.js')

test('chip 接线：计数来自 /kb/pending，前端一个数字都不写死', () => {
  assert.match(API, /kbPending: \(params\) => http\.get\('\/api\/v1\/kb\/pending', \{ params \}\)/,
    'api 层要有 kbPending（前端不自己拼队列）')
  assert.match(KB, /api\.kbPending\(\{ page: pendPage\.value, page_size: pendSize\.value \}\)/,
    '页面要真的调这个接口')
  assert.match(KB, /pendCount\.value = Number\(r\?\.pending_count\) \|\| 0/,
    'chip 上的数必须是后端给的 pending_count')
  assert.match(KB, /待审 \{\{ pendCount \}\}/, 'chip 文案要渲染后端给的数')
  // 计数为 0 ⇒ 整行隐藏（不编一个假 chip）
  assert.match(KB, /<div v-if="pendCount > 0" class="filter-title">审核<\/div>/,
    '没有待审文档时「审核」那行标题要隐藏')
  assert.match(KB, /<div v-if="pendCount > 0" class="tabs">/,
    'chip 行本身也要隐藏')
  // 🔴 写死判据：队列不许来自任何本地常量/假数据
  assert.ok(!/const PEND_SAMPLE\s*=/.test(KB), '待审队列只能来自后端')
  assert.match(KB, /catch \(e\) \{[\s\S]{0,200}?pendCount\.value = 0/,
    '接口失败要清零（不拿上一个库的旧队列冒充现状）')
  // 打开页面就拉一次计数：chip 不能等点开才知道
  assert.match(KB, /onMounted\([\s\S]{0,400}loadPending\(\)/)
})

test('批准/驳回都走 ConfirmDialog 二次确认，没有裸 confirm', () => {
  assert.match(KB, /<ConfirmDialog[\s\S]{0,320}?v-model="reviewOpen"/,
    '审核确认框没接上')
  assert.match(KB, /@confirm="doReview"/, '确认回调没接线')
  assert.match(KB, /:tone="reviewKind === 'reject' \? 'danger' : 'primary'"/,
    '驳回是危险动作，按钮色要与批准分开')
  assert.match(KB, /@click="askApprove\(row\)"/, '批准按钮没接线')
  assert.match(KB, /@click="openReject\(row\)"/, '驳回按钮没接线')
  // 🔴 没有第三种交互：原生 confirm/alert 一律不许
  assert.doesNotMatch(KB, /[^.\w]confirm\s*\(/, '不许用原生 confirm')
  assert.doesNotMatch(KB, /[^.\w]alert\s*\(/, '不许用原生 alert')
  // 批准/驳回打的是既有端点，不是自造第四个
  assert.match(API, /kbApproveDocument: \(id\) =>/)
  assert.match(API, /kbRejectDocument: \(id, reason\) =>/)
  assert.match(KB, /await api\.kbApproveDocument\(row\.doc_id\)/)
  assert.match(KB, /await api\.kbRejectDocument\(row\.doc_id, rejectReason\.value\.trim\(\)\)/)
})

test('驳回理由必填：填完才进二次确认，后端也要有那道闸', () => {
  assert.match(KB, /:disabled="!rejectReason\.trim\(\)"/,
    '没填理由不许点「下一步：确认驳回」')
  assert.match(KB, /function askRejectConfirm\(\) \{\s*\n\s*if \(!rejectReason\.value\.trim\(\)\) return/,
    '二次确认那一步也要再挡一次空理由')
  assert.match(KB, /v-model="rejectReason"/, '理由输入框没接 v-model')
  // 理由要出现在确认框的后果文案里（审核的人看得见自己批的是什么）
  assert.match(KB, /驳回理由会记进审核台账：\$\{rejectReason\.value\.trim\(\)\}/)
})

test('待审文档：状态、正文预览、同名重复警示都在卡上', () => {
  // pending 文档此刻一块切片都没有 —— 正文只能看 review_meta 预览（要点破这件事）
  assert.match(KB, /\{\{ row\.preview \|\| '（没有提交正文）' \}\}/)
  assert.match(KB, /row\.status === 'rejected' \? '已驳回' : '待审'/, '状态徽标缺失')
  assert.match(KB, /来源 \{\{ row\.submitted_by \|\| '未知' \}\}/, '来源代理要显出来')
  assert.match(KB, /v-if="row\.dup_of"/, '同名/近重复警示没渲染')
  assert.match(KB, /row\.dup_exact \? '同名重复' : '近重复'/, '重复类型要说清是同名还是近重复')
  // 已驳回的可再审：驳回按钮不给，批准按钮改成「重新批准」
  assert.match(KB, /v-if="row\.status !== 'rejected'"/, '已驳回的不该再给「驳回」')
  assert.match(KB, /row\.status === 'rejected' \? '重新批准' : '批准入库'/)
})

test('站内手建路径零变化：审核入口只在待审面板里', () => {
  // 文档卡的写按钮判据仍然只有 isManual（没有多一个「要不要审」分支）
  assert.match(KB, /<template v-if="isManual\(doc\)" #actions>[\s\S]{0,220}openEdit\(doc\)[\s\S]{0,120}askDelete\(doc\)/,
    '文档卡的编辑/删除按钮判据被改了')
  assert.doesNotMatch(KB, /function isManual\(doc\) \{[\s\S]{0,200}pending/, 'isManual 里混进了审核判据')
})
