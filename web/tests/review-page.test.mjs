/**
 * 「审核独立成页」的前端护栏（用户原话：「分两个界面，一个页面太挤了」「审核那边也是需要分页的」）。
 *
 * 这批改动是**搬走不是复制**，所以最危险的不是编译不过，而是两处都留着：
 * 维护页里剩一段残骸、或审核逻辑被复制成两份各自漂移。钉死四件事：
 *   1) 路由与侧栏：/review 存在，记忆组第一位，徽标挂在「审核」上（记忆维护不再有徽标）；
 *   2) 维护页零残留：`cand*` / 候选文案 / 证据样式全都不许在那儿（搬干净了才算分页）；
 *   3) 审核页有分页：PagerBar 接线 + 每页 10/20/50 三档、默认 10（这就是用户要的「审核要分页」）；
 *   4) 面包屑两处说同一句话：侧栏 label 与 router meta.title 一致，不许一个叫「审核」
 *      另一个叫别的（crumb 从 GROUPS 取，所以 label 改了这里就红）。
 *
 * 静态扫描，node --test 直接跑，不起浏览器。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const REVIEW = read('views/ReviewView.vue')
const MAINT = read('views/MaintenanceView.vue')
const APP = read('App.vue')
const ROUTER = read('router/index.js')
const STATS = read('views/StatsView.vue')

/** 维护页搬走之后不该再出现的任何痕迹（状态 / 函数 / 组件 / 区块标题 / 样式）。
 *  注意「退居建议」四个字本身还留在班车设置弹框的说明里（讲的是自动降级规则，
 *  不是审核队列），所以只钉代码标识符与「常驻候选」这个区块标题。
 *  ⚠️ `PagerBar` **不再钉**（2026-10-24 uipolish_r2）：维护页那张「退居提名」
 *     只读表自己加了分页（每页 10 条）—— 那是**只读列表**的分页，不是审核队列
 *     搬回来；审核动作（批准 / 驳回）仍然只在 /review 页。 */
const LEFT_BEHIND = [
  'candDir', 'cands', 'candPage', 'candCount', 'candPagerTotal', 'candOpen',
  'loadCandidates', 'doCandidate', 'evidKeysOf', 'dropTag', 'splitTags',
  'ItemCard', '常驻候选', '待审',
]

test('路由：/review 有页、有标题，且排在维护之前', () => {
  assert.match(ROUTER, /import ReviewView from '\.\.\/views\/ReviewView\.vue'/,
    '路由没引 ReviewView —— 直接访问 /review 会白屏')
  assert.match(
    ROUTER,
    /\{ path: '\/review', name: 'review', component: ReviewView, meta: \{ title: '记忆审核' \} \}/,
  )
  // 不是 public 页：要 token 才进得去（守卫按 meta.public 判，这里不许出现）
  assert.doesNotMatch(ROUTER, /path: '\/review'[^}]*public: true/)
})

test('侧栏：记忆组审核项存在且徽标跟着它走，记忆维护不再有徽标（顺序以 nav-order.test.mjs 为唯一锁）', () => {
  assert.match(APP, /\{ path: '\/review', label: '审核', icon: ICON\.\w+ \}/,
    '记忆组里要有「审核」项，且 icon 复用 ICON 集（不新造图标依赖）')
  // 徽标：promote + demote 挂到 /review，维护项彻底不挂了
  assert.match(APP, /function reviewBadge\(\)/)
  assert.match(APP, /if \(path === '\/review'\) return reviewBadge\(\)/)
  assert.doesNotMatch(APP, /maintenanceBadge/, '记忆维护项不再有待审徽标（徽标已迁到审核项）')
  assert.doesNotMatch(APP, /if \(path === '\/maintenance'\)/,
    '记忆维护不该再有徽标分支')
})

test('维护页：候选审核区搬干净了（不留残骸，也不留双份逻辑）', () => {
  for (const s of LEFT_BEHIND) {
    assert.ok(!MAINT.includes(s), `MaintenanceView 里还留着「${s}」—— 审核区已搬到 ReviewView`)
  }
  // 但维护页自己的三块活儿必须还在（合并 / 遗忘 / 分级班车设置）
  for (const s of ['合并 · dry-run', '遗忘 · dry-run', 'runMerge', 'runForget', '分级班车设置']) {
    assert.ok(MAINT.includes(s), `MaintenanceView 少了「${s}」`)
  }
  // 维护页不拉候选列表了
  assert.doesNotMatch(MAINT, /loadCandidates\(\)/)
})

test('审核页：分页是真分页（PagerBar 接线 + 每页 10/20/50，默认 10）', () => {
  assert.match(REVIEW, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/,
    '审核页必须用 PagerBar（与其它列表页同一个组件，不另造轮子）')
  assert.match(REVIEW, /<PagerBar[\s\S]*?:total="candPagerTotal"[\s\S]*?:page-size="candPageSize"/,
    'PagerBar 的 total/page-size 必须接候选的分页状态')
  assert.match(REVIEW, /@update:page="onCandPage"/)
  assert.match(REVIEW, /@update:page-size="onCandSize"/)
  // 每页条数三档摆在页面上：这就是「审核要分页」里用户能自己调的那一半
  assert.match(REVIEW, /const PAGE_SIZES = \[10, 20, 50\]/, '每页档位必须是 10/20/50')
  assert.match(REVIEW, /v-for="n in PAGE_SIZES"/, '三档必须真的渲染出来（不是写死的常量摆设）')
  assert.match(REVIEW, /const candPageSize = ref\(10\)/, '默认 10 条/页（一屏看不完 20 张卡）')
  // 服务端分页：page/page_size 跟着 ref 走，不许再写死一把梭
  assert.doesNotMatch(REVIEW, /page:\s*1,\s*page_size:\s*\d+/, '还在写死第一页 N 条')
  assert.match(REVIEW, /page:\s*candPage\.value,\s*page_size:\s*candPageSize\.value/)
  // 翻页/改条数都回到合法位置（末页审空退一页、换条数回第 1 页）
  assert.match(REVIEW, /function onCandSize\(s\) \{\s*candPageSize\.value = s\s*candPage\.value = 1\s*loadCandidates\(\)/)
  assert.match(
    REVIEW,
    /if \(!data\.items\.length && candPage\.value > 1 && !data\.has_more\) \{\s*candPage\.value -= 1\s*return loadCandidates\(\)/,
    '末页审空后必须退一页，否则落进「没有待审候选」空态（谎报，前面还有页）',
  )
})

test('审核页：页面自带标题与方向说明（独立成页后自带上下文）', () => {
  assert.match(REVIEW, /<h1 class="page-title">记忆审核<\/h1>/)
  // 两种方向的语义得写在页面上：批准动作正好相反，看错方向按下去就是反的
  assert.match(REVIEW, /晋升候选（申请进常驻层）与退居建议（班车建议退出常驻层）/)
  assert.match(REVIEW, /批准=退出常驻层（正文不删、不失效）；驳回=摘掉建议标，这条继续留在常驻层。/)
})

test('面包屑：侧栏 label 与路由标题说同一句话（crumb 从 GROUPS 取）', () => {
  // 「记忆 › 审核」：GROUPS 里的 label 与 router meta.title 都叫「审核/记忆审核」，
  // 差一个「记忆」前缀是既有惯例（记忆维护 / 知识库同款），别的不许出现第三种叫法。
  assert.match(APP, /label: '审核'/)
  assert.match(ROUTER, /name: 'review'[^}]*title: '记忆审核'/)
})

test('看板待审卡与徽标都指到审核页', () => {
  assert.match(STATS, /router\.push\(['"]\/review['"]\)/, '待审核卡必须跳 /review')
  assert.doesNotMatch(STATS, /'\/maintenance'/, '看板不该再把人送去记忆维护页')
})

test('交互红线 + 零新色：审核页走 ConfirmDialog / utils/dialog，颜色全走 token', () => {
  const bare = REVIEW.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
  assert.match(REVIEW, /@confirm="doCandidate"/, '批准/驳回必须在确认回调里执行')
  const color = REVIEW.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g)
  assert.equal(color, null, `审核页不许出现硬编码色值（首条 ${color && color[0]}），一律走 tokens.css`)
  const sk = REVIEW.match(/\bsk-[A-Za-z0-9]{8,}\b/)
  assert.equal(sk, null, '审核页不许落 sk- 凭据串')
})
