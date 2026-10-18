/**
 * 「知识库审核独立成页 /kb-review」的前端护栏。
 *
 * 用户口径：「分两个界面」「审核那边需要分页」—— 指的是知识库（上一单的 /review 是记忆
 * 候选审核，保持现状不动）。KbView 一页里挤着文档浏览/检索/新增/导入导出，待审队列只能
 * 靠点顶部那颗 chip 才展开、还固定 20 条一页；审核是逐条判的动作，铺在浏览页的抽屉里
 * 既看不清也翻不动。
 *
 * 这批改动是**搬走不是复制**，所以最危险的不是编译不过，而是两处都留着：KbView 里剩
 * 一段残骸、或审核逻辑被复制成两份各自漂移。钉死五件事：
 *   1) 路由与侧栏：/kb-review 有页、有标题、知识库组第一位，kb_pending 徽标挂在它身上
 *      （「知识库」浏览页不再挂徽标）；
 *   2) 搬干净了：KbView 里 pend* 状态/函数/面板/Chip 一律不许剩，也不许再拉队列；
 *   3) 审核页有分页：PagerBar 接线 + 每页 10/20/50 三档、默认 10（这就是「审核要分页」）；
 *   4) 交互零漂移：批准/驳回仍走 ConfirmDialog、驳回理由仍必填、打的是同一对既有端点；
 *   5) 红线：交互红线（无原生弹框）+ 零新色 + 零凭据。
 *
 * 静态扫描，node --test 直接跑，不起浏览器。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KBR = read('views/KbReviewView.vue')
const KB = read('views/KbView.vue')
const APP = read('App.vue')
const ROUTER = read('router/index.js')

test('路由：/kb-review 有页、有标题，且不是公开页', () => {
  assert.match(ROUTER, /import KbReviewView from '\.\.\/views\/KbReviewView\.vue'/,
    '路由没引 KbReviewView —— 直接访问 /kb-review 会白屏')
  assert.match(
    ROUTER,
    /\{ path: '\/kb-review', name: 'kb-review', component: KbReviewView, meta: \{ title: '知识库审核' \} \}/,
  )
  // 不是 public 页：要 token 才进得去（守卫按 meta.public 判）
  assert.doesNotMatch(ROUTER, /path: '\/kb-review'[^}]*public: true/)
})

test('侧栏：kb_pending 徽标挂审核项、浏览页不挂（顺序以 nav-order.test.mjs 为唯一锁）', () => {
  assert.match(APP, /\{ path: '\/kb-review', label: '审核', icon: ICON\.\w+ \}/)
  assert.match(APP, /if \(path === '\/kb-review'\) return kbBadge\(\)/)
  // 浏览页不再挂徽标：待审不是浏览页的事，而且两处挂同一枚徽标会让人以为重复了
  assert.doesNotMatch(APP, /if \(path === '\/kb'\) return/,
    '「知识库」项不该再有徽标分支（徽标已挂到「知识库 › 审核」项上）')
})

test('面包屑：侧栏 label 与路由标题说同一句话（crumb 从 GROUPS 取）', () => {
  // 「知识库 › 审核」：GROUPS 的 label 与 meta.title 一个叫「审核」一个叫「知识库审核」，
  // 差一个「知识库」前缀是既有惯例（记忆审核 / 知识库同款），别的不许出现第三种叫法。
  assert.match(APP, /path: '\/kb-review', label: '审核'/)
  assert.match(ROUTER, /name: 'kb-review'[^}]*title: '知识库审核'/)
})

test('搬走不是复制：KbView 里不许剩待审队列的任何痕迹', () => {
  for (const s of [
    'pendRows', 'pendTotal', 'pendPage', 'pendSize', 'pendLoading', 'pendError',
    'loadPending', 'togglePending', 'pendingOpen', 'chipText', 'busyReview',
    'reviewOpen', 'reviewKind', 'rejectAsk', 'rejectReason', 'doReview', 'askApprove',
    'openReject', 'askRejectConfirm',
  ]) {
    assert.ok(!KB.includes(s), `KbView 里还留着「${s}」—— 审核队列已搬到 KbReviewView`)
  }
  assert.doesNotMatch(KB, /api\.kbPending/, 'KbView 不该再拉待审队列（两处各自刷新会漂）')
  // 但审核页确实把整套搬过去了（否则是真删功能，不是搬家）
  for (const s of ['api.kbPending', 'busyReview', 'doReview', 'askApprove', 'openReject', 'rejectAsk']) {
    assert.match(KBR, new RegExp(s.replace(/[.()]/g, '\\$&')), `审核页缺「${s}」—— 是搬家不是删除`)
  }
  // 页面里不许留着只有「记忆审核」才认的入口跳转（那一页保持现状不动）
  assert.match(KBR, /<h1 class="page-title">知识库审核<\/h1>/)
})

test('分页是真分页（PagerBar 接线 + 每页 10/20/50，默认 10）', () => {
  assert.match(KBR, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/,
    '审核页必须用 PagerBar（与其它列表页同一个组件，不另造轮子）')
  assert.match(KBR, /<PagerBar[\s\S]*?:total="pendTotal"[\s\S]*?:page-size="pendSize"/,
    'PagerBar 的 total/page-size 必须接队列的分页状态')
  assert.match(KBR, /@update:page="onPendPage"/)
  assert.match(KBR, /@update:page-size="onPendSize"/)
  // 每页条数三档摆在页面上：这就是「审核要分页」里用户能自己调的那一半
  assert.match(KBR, /const PAGE_SIZES = \[10, 20, 50\]/, '每页档位必须是 10/20/50')
  assert.match(KBR, /v-for="n in PAGE_SIZES"/, '三档必须真的渲染出来（不是写死的常量摆设）')
  assert.match(KBR, /const pendSize = ref\(10\)/, '默认 10 条/页（一屏看不完 20 张卡）')
  // 服务端分页：page/page_size 跟着 ref 走，不许再写死一把梭
  assert.doesNotMatch(KBR, /page:\s*1,\s*page_size:\s*\d+/, '还在写死第一页 N 条')
  assert.match(KBR, /page:\s*pendPage\.value,\s*page_size:\s*pendSize\.value/)
  // 改条数回第 1 页（否则会停在一个不存在的页码上）
  assert.match(KBR, /function onPendSize\(s\) \{\s*pendSize\.value = Number\(s\)\s*pendPage\.value = 1\s*loadPending\(\)/)
})

test('交互零漂移：端点、确认框、驳回理由必填都跟搬之前一致', () => {
  const API = read('api/index.js')
  // 批准/驳回打的是既有端点，不是自造第四个
  assert.match(API, /kbApproveDocument: \(id\) =>/)
  assert.match(API, /kbRejectDocument: \(id, reason\) =>/)
  assert.match(KBR, /await api\.kbApproveDocument\(row\.doc_id\)/)
  assert.match(KBR, /await api\.kbRejectDocument\(row\.doc_id, rejectReason\.value\.trim\(\)\)/)
  // 二次确认框一个实例按 kind 切语气
  assert.match(KBR, /@confirm="doReview"/)
  assert.match(KBR, /:tone="reviewKind === 'reject' \? 'danger' : 'primary'"/)
  // 驳回理由必填（后端同样 400 reason_required）
  assert.match(KBR, /:disabled="!rejectReason\.trim\(\)"/)
  assert.match(KBR, /function askRejectConfirm\(\) \{\s*\n\s*if \(!rejectReason\.value\.trim\(\)\) return/)
})

test('交互红线 + 零新色：走 ConfirmDialog / utils/dialog，颜色全走 token', () => {
  const bare = KBR.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
  assert.match(KBR, /from '\.\.\/utils\/dialog'/, '提示必须走 utils/dialog（showToast / showError）')
  const color = KBR.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g)
  assert.equal(color, null, `审核页不许出现硬编码色值（首条 ${color && color[0]}），一律走 tokens.css`)
  const sk = KBR.match(/\bsk-[A-Za-z0-9]{8,}\b/)
  assert.equal(sk, null, '审核页不许落 sk- 凭据串')
})

test('零后端改动：这批只动前端，接口口径一个字没改', () => {
  const API = read('api/index.js')
  // 队列与计数仍是批2 那两个端点，路径只在 api 层写死一次，页面不许自己另拼
  assert.match(API, /kbPending: \(params\) => http\.get\('\/api\/v1\/kb\/pending', \{ params \}\)/)
  assert.doesNotMatch(KBR, /\/api\/v1\//, '页面里不许硬编码接口路径（统一走 api 层）')
  assert.match(KBR, /r\?\.pending_count/, '字段名 pending_count 不许改')
  assert.match(KBR, /r\?\.count/, '字段名 count 不许改')
  assert.match(KBR, /子代理（非主代理）提交的知识文档一律先进这里/,
    '页面自带上下文：谁在写会被拦下来看，站内手建不经过这一关')
})
