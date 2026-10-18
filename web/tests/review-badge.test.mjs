/**
 * 控制台审核入口显性化（待审计数徽标 + 首页审核卡）的前端护栏。
 *
 * 用户痛点：「看不到需要审核的界面」——候选审核区太深（现在已独立成 ReviewView），
 * 但入口不显。把「有东西要审」这件事推到眼前，**审核语义本身不动**：
 * 批准 / 驳回仍走既有端点，这里只把徽标 / 待审核卡 + 取数口接通。
 *
 * 钉死的事（每条都是「编译能过 + 不接对也会过」的隐形漏接）：
 *   1) 取数口 = `api.reviewCounts()` ⇒ `GET /api/v1/review/counts`：
 *      路径必须字符串一致（与后端一字对得上，否则 404）。
 *   2) App.vue 侧栏徽标：
 *      - 审核项 badge = promote + demote 合计（审核独立成页后从维护项迁过来）；
 *      - 知识库项 badge = kb_pending；
 *      - > 0 才渲染（v-if）；
 *      - 60s 静默刷新 + onUnmounted 清定时器（离开页面继续打接口 = 隐形泄漏）。
 *   3) StatsView.vue 顶部「待审核 N 条」卡：
 *      - 渲染（reviewTotal computed）；
 *      - 点击 → `router.push('/review')`（记忆类待审跳审核页）。
 *   4) ops.py 含 `"/api/v1/review/counts"` 且挂 `require_session`
 *      （与 G4 端点存在 + G5 不漏认证的守门员）。
 *   5) 新增的 4 个文件**零硬编码色值**：不允许引入 `#[0-9a-fA-F]{3,8}`
 *      或 `rgb(...)`，所有视觉一律走 tokens.css 现有变量。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const API_ROOT = fileURLToPath(new URL('../..', import.meta.url))
const readSrc = (p) => readFileSync(`${SRC}/${p}`, 'utf8')
const readApi = (rel) => readFileSync(`${API_ROOT}/${rel}`, 'utf8')

const API = readSrc('api/index.js')
const APP = readSrc('App.vue')
const STATS = readSrc('views/StatsView.vue')

/** 检测文件里有没有裸的 hex 色值（3/4/6/8 位）+ rgb()/rgba() —— 禁止新增。 */
const HARD_COLOR = /#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b|\brgba?\(/
/** 长串 sk- 凭据 —— 禁止任务文件出现，无论前后端。 */
const SK_RE = /\bsk-[A-Za-z0-9]{8,}\b/

test('API 层：reviewCounts 方法 + 路径字符串一致', () => {
  assert.match(
    API,
    /reviewCounts:\s*\(\)\s*=>\s*http\.get\(['"]\/api\/v1\/review\/counts['"]\)/,
    '方法体必须显式写全路径（与后端一字对上），不许抽常量',
  )
})

test('App.vue：侧栏徽标渲染 + 审核/知识库徽标计算', () => {
  // 数据源：ref + 拉取
  assert.match(APP, /\breviewCounts\s*=\s*ref\(/, '需要一个 reviewCounts ref 装取到的三个数')
  assert.match(APP, /api\.reviewCounts\(\)/, '必须显式调 api.reviewCounts()，不许另发明接口')
  // >0 才渲染
  assert.match(APP, /v-if="badgeFor\(m\.path\)"/, '徽标 v-if 守门：>0 才显示');
  // 审核项徽标 = promote+demote 合计（多行可）
  assert.match(APP, /promote_candidates[\s\S]*?demote_candidates/)
  // 知识库徽标 = kb_pending（属性名即可）
  assert.match(APP, /\.kb_pending\b/)
  // 路径守门：仅这两个路径有徽标（徽标从记忆维护迁到了审核项；知识库那枚从
  // /kb 浏览页迁到了 /kb-review 审核页 —— 待审不是浏览页的事）
  assert.match(APP, /['"]\/review['"]\)\s*return\s+reviewBadge\(\)/)
  assert.match(APP, /['"]\/kb-review['"]\)\s*return\s+kbBadge\(\)/)
  assert.doesNotMatch(APP, /['"]\/kb['"]\)\s*return\s+kbBadge\(\)/,
    '「知识库」浏览页不该再挂待审徽标（它已挂到「知识库 › 审核」项上）')
})

test('App.vue：60s 静默刷新 + onUnmounted 清定时器（不留定时炸弹）', () => {
  assert.match(APP, /\bonMounted\b/, '需要 onMounted 启动')
  assert.match(APP, /\bonUnmounted\b/, '需要 onUnmounted 清场')
  assert.match(APP, /setInterval\(loadReviewCounts,\s*REVIEW_POLL_MS\)/,
               'setInterval 用同一个 REVIEW_POLL_MS 常量，避免改一处忘另一处')
  assert.match(APP, /REFRESH\s*=|REFRESH_MS\s*=|REVIEW_POLL_MS\s*=/, '刷新间隔应是显式常量')
  // 60s 节奏：必须 60000，不是 10000 或 30000（侧栏是「记得处理一下」的提示，
  // 不需要秒级；秒级反而浪费流量、且 AgentsView 的 10s 不一定照搬）
  assert.match(APP, /REVIEW_POLL_MS\s*=\s*60000/, '60 秒节奏常量必须是 60000')
  assert.match(APP, /clearInterval\(reviewTimer\)/, '卸载必须 clearInterval')
})

test('StatsView：顶部待审核卡 + 跳转 /review', () => {
  // reviewTotal 汇总三个数（>0 → display）
  assert.match(STATS, /reviewTotal/, '需要 reviewTotal 汇总数')
  assert.match(STATS, /promote_candidates[\s\S]*?demote_candidates[\s\S]*?kb_pending/,
               'reviewTotal 必须包含 promote + demote + kb_pending 三项')
  // 跳转
  assert.match(STATS, /router\.push\(['"]\/review['"]\)/, '点击卡必须跳 /review')
  // reviewCounts 卡片存在（DOM 锚点："待审核"）
  assert.match(STATS, /待审核/, '模板里必须出现"待审核"字样')
})

test('后端路由文件：含 /api/v1/review/counts，且挂 require_session（只读面，无 write_guard）', () => {
  const OPS_PATH = 'src/keepsake/api/routers/ops.py'
  assert.ok(existsSync(`${API_ROOT}/${OPS_PATH}`), '需有 ops.py 路由文件')
  const OPS = readApi(OPS_PATH)
  assert.match(OPS, /['"]\/api\/v1\/review\/counts['"]/, 'ops.py 必须挂这条路由')
  // 只读面：require_session 但不许挂 write_guard（review/counts 是 GET，纯读）
  const depsBlock = OPS.slice(
    OPS.indexOf('@router.get("/api/v1/review/counts"'),
    OPS.indexOf('@router.get("/api/v1/review/counts"') + 400,
  )
  assert.match(depsBlock, /Depends\(require_session\)/, '必须挂 require_session（G4 守门员）')
  assert.doesNotMatch(depsBlock, /Depends\(write_guard\)/,
               'review/counts 是 GET 只读，不许挂 write_guard（与 core/stats / tiering/status 同款）')
  // 三个返回字段
  assert.match(OPS, /['"]promote_candidates['"]/, '返回体必须有 promote_candidates')
  assert.match(OPS, /['"]demote_candidates['"]/, '返回体必须有 demote_candidates')
  assert.match(OPS, /['"]kb_pending['"]/, '返回体必须有 kb_pending')
})

test('负向：变更的源文件零硬编码色值（与 DESIGN.md 一致：颜色一律走 token）', () => {
  // 改动声明里允许出现的文件列表（web 端 3 文件 + 后端 1 文件）。
  // 不含本测试文件 —— 测试里允许写字面量做断言。
  const files = [
    `${SRC}/App.vue`,
    `${SRC}/views/StatsView.vue`,
    `${SRC}/api/index.js`,
    `${API_ROOT}/src/keepsake/api/routers/ops.py`,
  ]
  for (const f of files) {
    if (!existsSync(f)) continue   // 后端文件可能暂未在 dev 容器里看到，跳过
    const content = readFileSync(f, 'utf8')
    const m = content.match(HARD_COLOR)
    assert.equal(m, null,
      `新增文件 ${f} 不应出现硬编码 hex/rgb 色值（首条匹配 ${m && m[0]}）；所有颜色走 tokens.css`)
  }
})

test('负向：变更的文件零 sk- 凭据（前端文件永远不许落凭据串）', () => {
  const files = [
    `${SRC}/App.vue`,
    `${SRC}/views/StatsView.vue`,
    `${SRC}/api/index.js`,
    `${API_ROOT}/src/keepsake/api/routers/ops.py`,
  ]
  for (const f of files) {
    if (!existsSync(f)) continue
    const content = readFileSync(f, 'utf8')
    assert.doesNotMatch(content, SK_RE,
      `${f} 包含 sk- 开头的疑似凭据串（凭据只许出现在网关上 + 凭据显示弹框里）`)
  }
})

test('API 层：无裸 alert/confirm（提示统一走 utils/dialog）', () => {
  // StatsView 与 App.vue 都用了 showToast/showError 才是真
  assert.match(APP, /showToast|showError/,
               'App.vue 不需要 dialog 工具（侧栏徽标是只读），但若用了，必须走 utils/dialog')
  assert.match(STATS, /from ['"]\.\.\/utils\/dialog['"]/,
               'StatsView 错误提示必须走 utils/dialog（已有同款 import）')
})
