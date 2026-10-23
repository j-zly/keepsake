/**
 * 「审核页删除 / 批量删除」+「看板按代理分列」的前端护栏。
 *
 * 两条需求，两处页面，钉死的是**最容易悄悄漂**的几件事：
 *   1) 审核页行操作有「删除」，走**既有**单条端点（不新造第四个写端点），
 *      确认串 = 文档标题全文（防手滑），删完刷新列表与页头计数；
 *   2) 审核页批量条有「批量删除」，走**既有** bulk-delete，确认串 = 选中条数
 *      （与「知识库」页同一把尺子，不许两套）；
 *   3) 看板用 `daily_by_agent` 段画**按代理**的图：内联 SVG、图例带色点、
 *      7 天总量降序前 6 + 「其他」桶、横轴仍由 `daily` 段铺（只补一次）；
 *   4) 红线：零新依赖、零硬编码色值、数字全部来自接口。
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
const STATS = read('views/StatsView.vue')
const API = read('api/index.js')

// ---------------------------------------------------------------- 审核页：删除

test('审核页行操作有「删除」，走既有单条 delete 端点', () => {
  assert.match(KBR, /@click="askDelete\(row\)"/, '审核页行操作必须有「删除」')
  assert.match(KBR, /class="act-btn act-btn--danger"[\s\S]{0,200}?@click="askDelete\(row\)"/,
    '删除按钮要与「驳回」同一档（act-btn--danger）')
  assert.match(API, /kbDeleteDocument: \(id, confirm\) =>/)
  assert.match(KBR, /await api\.kbDeleteDocument\(row\.doc_id, typed\)/,
    '必须打既有单条端点（页面里不许自己拼接口路径）')
  assert.doesNotMatch(KBR, /\/api\/v1\//, '页面里不许硬编码接口路径（统一走 api 层）')
})

test('删除的确认串 = 文档标题全文（防手滑），且走 ConfirmDialog', () => {
  assert.match(KBR, /const deleteExpect = computed\(\(\) => \(deleteRow\.value \? deleteRow\.value\.title : ''\)\)/,
    '确认串必须是文档标题全文（与「知识库」页的真删同一档）')
  assert.match(KBR, /:expect="deleteExpect"/, '确认框必须把确认串交给 ConfirmDialog')
  assert.match(KBR, /@confirm="doDelete"/)
  assert.match(KBR, /tone="danger"/, '真删是危险方向（红）')
  // 交互红线：走 ConfirmDialog / utils/dialog，不许原生弹框
  const bare = KBR.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
  assert.match(KBR, /from '\.\.\/utils\/dialog'/)
})

test('删除成功后刷新列表与页头计数', () => {
  const body = KBR.match(/async function doDelete\(typed\) \{[\s\S]*?\n\}/)
  assert.ok(body, 'doDelete 不见了')
  assert.match(body[0], /await loadPending\(\)/, '删完必须重拉队列（列表 + 页头计数都靠它）')
  assert.match(body[0], /notifyCounts\(\)/, '删完必须喊一声刷侧栏那枚「待审 N」徽标')
})

test('审核页批量条有「批量删除」，确认串 = 选中条数', () => {
  assert.match(KBR, /@click="askBulkDelete\(\)"/, '批量条必须有「批量删除」')
  assert.match(KBR, /const bulkExpect = computed\(\(\) => String\(selCount\.value\)\)/,
    '批量确认串 = 选中条数（与后端 confirm 同一口径）')
  assert.match(KBR, /await api\.kbBulkDelete\(\{ ids, confirm: typed \}\)/,
    '必须打既有 bulk-delete 端点')
  // 与「知识库」页同一把尺子：同一按钮语义不许两套确认口径
  assert.match(KB, /await api\.kbBulkDelete\(\{ ids, confirm: typed \}\)/)
  assert.match(KB, /const bulkDeleteExpect = computed\(\(\) => String\(selCount\.value\)\)/)
})

test('批量删除的语气是危险色，批准仍是主色（一个实例按 kind 切）', () => {
  assert.match(KBR, /:tone="bulkKind === 'approve' \? 'primary' : 'danger'"/)
  assert.match(KBR, /bulkKind\.value = 'delete'/)
})

// ---------------------------------------------------------------- 看板：按代理分列

test('看板用 daily_by_agent 段画按代理的图（不是自己造数）', () => {
  assert.match(STATS, /data\.value\?\.daily_by_agent/, '必须读接口给的 daily_by_agent 段')
  assert.match(STATS, /const dailyDays = computed\(\(\) => \(data\.value\?\.daily \|\| \[\]\)/,
    '横轴仍由 daily 段铺（补零只补一次：后端补一次、前端再补一次必漂）')
})

test('序列 = 7 天总量降序前 6 + 「其他」桶', () => {
  assert.match(STATS, /const DAILY_TOP_N = 6/, '前 6 个代理 + 「其他」桶')
  assert.match(STATS, /topSeries\(bucketByAgent\(data\.value\?\.daily_by_agent \|\| \[\]\), DAILY_TOP_N/,
    '分组 / 排序 / 归并走纯函数模块（utils/daily-by-agent.js，可单测）')
  assert.match(STATS, /from '\.\.\/utils\/daily-by-agent'/, '看板必须用那份纯函数模块')
  // 排序与归并的实现细节在纯函数模块里（单测见 tests/daily-by-agent.test.mjs）
  const UTIL = read('utils/daily-by-agent.js')
  assert.match(UTIL, /b\.total - a\.total/, '按 7 天总量降序')
  assert.match(UTIL, /sorted\.slice\(0, topN\)/, '前 N 个单列')
  assert.match(UTIL, /const rest = sorted\.slice\(topN\)/, '其余进「其他」桶')
  assert.match(UTIL, /label: `其他 \$\{rest\.length\} 个代理`/, '其余合并成「其他」桶')
})

test('图是内联 SVG + 图例带色点（不引入新依赖）', () => {
  assert.match(STATS, /<svg[\s\S]*?:viewBox="agentChart\.viewBox"/, '必须是内联 SVG')
  assert.match(STATS, /<rect[\s\S]*?:fill="seg\.color"/, '每段一个矩形（天 × 代理）')
  assert.match(STATS, /class="legend"/, '要有图例')
  assert.match(STATS, /color: s\.key === OTHER_KEY\s*\?\s*OTHER_COLOR/,
    '每个序列都要有色（图例色点与柱子里是同一份，不许两处各画一份）')
  assert.match(STATS, /class="dot" :style="\{ background: s\.color \}"/, '图例带色点')
  assert.match(STATS, /<span class="lg-num num">\{\{ s\.total \}\}<\/span>/,
    '图例上的数字来自接口（前端零写死）')
  // 零新依赖：没往 package.json 里加东西
  const pkg = read('../package.json')
  assert.equal(pkg, read('../package.json'))
  assert.doesNotMatch(pkg, /"d3"|"chart\.js"|"apexcharts"|"echarts-for-react"/,
    '不许引入新图表依赖')
})

test('零新色：图表色值全走 tokens（DESIGN.md 的色板）', () => {
  const color = STATS.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(/g)
  assert.equal(color, null, `看板不许出现硬编码色值（首条 ${color && color[0]}），一律走 tokens.css`)
  for (const token of ['--accent', '--ok', '--warn', '--danger', '--text-dim', '--text-faint']) {
    assert.ok(STATS.includes(`var(${token})`), `序列色板该用 ${token}（tokens.css 里的既有令牌）`)
  }
})
