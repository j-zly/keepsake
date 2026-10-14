/**
 * 「记忆维护 › 常驻候选」审核区的护栏。
 *
 * 这块区要做的事只有三件，全是「不做就静默出错」的：
 *   1) **接线**：三个动作都打在**既有接口**上（拉候选 / 批准 / 驳回），
 *      没有第四个自造接口 —— 后端零改动的约定就靠这行钉住；
 *   2) **摘标签按逗号边界**：驳回要把 `core_candidate` 整个摘掉，
 *      而 `core_candidate_v2` / `xcore_candidate` 一个都不能动；
 *      写成 `tags.includes(tag)` 就会误伤，所以这条钉在纯函数上。
 *   3) **交互红线**：批准/驳回一律走 ConfirmDialog 二次确认，没有裸 alert/confirm。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const VIEW = readFileSync(fileURLToPath(new URL('../src/views/MaintenanceView.vue', import.meta.url)), 'utf8')

/**
 * 把页面里那段「标签串纯函数」原样抽出来真跑一遍。
 *
 * 为什么不单独放 utils/tags.js：这几个函数只有这一个页面用，为它开一个模块不划算
 * （且 utils/ 里的公共函数都有各自的单测文件，孤零零多一个反而更难找）。
 * 页面里那一段标了「必须零依赖」，所以这里 new Function 直接执行它 ——
 * 测的是**页面真正在跑的那份代码**，不是测试里抄一份。
 * 哪天有人给它加了 import，这行会直接抛错，而不是悄悄测着另一份实现。
 */
const { splitTags, dropTag, agentOf } = new Function(
  VIEW.slice(
    VIEW.indexOf('/** 逗号串 → 标签数组'),
    VIEW.indexOf('const cands = ref('),
  ) + '\nreturn { splitTags, dropTag, agentOf }',
)()

test('审核区接的是三个既有接口，没有自造第四个', () => {
  for (const call of ['api.fragments(', 'api.promote(', 'api.updateFragment(']) {
    assert.ok(VIEW.includes(call), `MaintenanceView 里没有 ${call}`)
  }
  // 候选数据源必须是按标签列（tag=core_candidate），不是全文检索
  assert.match(VIEW, /api\.fragments\(\{\s*tag: CANDIDATE_TAG/)
  assert.ok(VIEW.includes('core_candidate'), '候选标签常量不见了')
  assert.ok(VIEW.includes('常驻候选'), '区块标题文案不见了')
})

test('候选列表走分页：PagerBar 接线 + 写死的那一拉到底已经拆掉', () => {
  assert.match(VIEW, /import PagerBar from '\.\.\/components\/PagerBar\.vue'/, '没引 PagerBar')
  assert.match(VIEW, /<PagerBar[\s\S]*?:total="candPagerTotal"/, 'PagerBar 没接候选的 total')
  assert.match(VIEW, /@update:page="onCandPage"/, '翻页事件没接线')
  assert.match(VIEW, /@update:page-size="onCandSize"/, '每页条数事件没接线')
  // 写死页的判据：page: 1, page_size: 50 曾经是一把梭 50 条，必须拆掉
  assert.doesNotMatch(VIEW, /page:\s*1,\s*page_size:\s*50/, '还在写死第一页 50 条')
  // 页码/每页条数是响应式的，默认与 PagerBar 的 SIZES 选项对齐（20）
  assert.match(VIEW, /const candPage = ref\(1\)/)
  assert.match(VIEW, /const candPageSize = ref\(20\)/)
  assert.match(VIEW, /page:\s*candPage\.value,\s*page_size:\s*candPageSize\.value/)
  // total 口径与 FragmentsView 同源：有 count 用 count，has_more 时撑出下一页
  assert.match(
    VIEW,
    /const candPagerTotal = computed\(\(\) =>\s*candCount\.value \?\? \(candHasMore\.value \? candPage\.value \* candPageSize\.value \+ 1 : cands\.value\.length\)/,
  )
  // 翻页重拉必须走既有的 candSeq 防串，否则慢的旧页响应会盖掉新页
  const load = VIEW.slice(VIEW.indexOf('async function loadCandidates()'), VIEW.indexOf('function askApprove'))
  assert.match(load, /const seq = \+\+candSeq/)
  assert.match(load, /if \(seq !== candSeq\) return/)
  // 改每页条数回到第 1 页（否则会停在一个不存在的页码上）
  assert.match(VIEW, /function onCandSize\(s\) \{\s*candPageSize\.value = s\s*candPage\.value = 1\s*loadCandidates\(\)/)
  // 末页审空后必须退一页，否则落进「没有待审候选」空态（谎报，前面还有页）
  assert.match(
    load,
    /if \(!data\.items\.length && candPage\.value > 1 && !data\.has_more\) \{\s*candPage\.value -= 1\s*return loadCandidates\(\)/,
    '末页审空后没退页，会落进空态谎报「没有待审候选」',
  )
})

test('摘标签只摘完整标签：core_candidate 不误伤 core_candidate_v2 / xcore_candidate', () => {
  assert.equal(splitTags(' a , ,b ').join('|'), 'a|b')
  // 边界剔除：同类前缀的标签必须原样留下
  assert.equal(dropTag('core_candidate,core_candidate_v2', 'core_candidate'), 'core_candidate_v2')
  assert.equal(dropTag('xcore_candidate,core_candidate', 'core_candidate'), 'xcore_candidate')
  // 正常剔除：其余标签与顺序不变
  assert.equal(dropTag('core,core_candidate,agent:worker', 'core_candidate'), 'core,agent:worker')
  assert.equal(dropTag(' core_candidate , shared ', 'core_candidate'), 'shared')
  // 幂等：标签不存在时原样返回（重复驳回同一条不会吃掉别的标签）
  assert.equal(dropTag('core,agent:worker', 'core_candidate'), 'core,agent:worker')
  // 唯一标签被摘完 ⇒ 空串（正文不受影响，条目仍在检索层）
  assert.equal(dropTag('core_candidate', 'core_candidate'), '')

  assert.equal(agentOf('core,agent:worker'), 'worker')
  assert.equal(agentOf('core'), '')
  assert.equal(agentOf('agent:'), '')
})

test('驳回走的是「只摘候选标」这条路径，不是删条目', () => {
  // 摘完的标签串交给 updateFragment；content 一个字都不带过去
  assert.match(VIEW, /api\.updateFragment\(row\.key, \{ tags: dropTag\(row\.tags, CANDIDATE_TAG\) \}\)/)
  assert.ok(!/api\.remove\(|api\.archive\(/.test(VIEW), '驳回不许顺带删/归档条目')
})

test('批准后候选标也要摘掉：promote 只加 core，摘标得自己补一次', () => {
  // 后端 _with_core 只会把 core 放到最前，不认识 core_candidate ⇒ 只调 promote 的话
  // 那条会带着候选标继续留在待审列表里，等于批准没生效。顺序也钉死：先 promote 再摘标，
  // 且摘标以 promote 返回的 tags（后端刚算出的权威值）为准，不用列表行里可能过期的 tags。
  const approve = VIEW.match(/await api\.promote\(row\.key\)([\s\S]*?)\n\s*\}/)
  assert.ok(approve, '找不到 promote 调用')
  assert.match(approve[1], /api\.updateFragment\(row\.key, \{ tags: dropTag\(r\.tags, CANDIDATE_TAG\) \}\)/)
  assert.ok(
    VIEW.indexOf('api.promote(row.key)') <
      VIEW.indexOf('api.updateFragment(row.key', VIEW.indexOf('api.promote(row.key)')),
    '必须先 promote 再摘标：反了会白审一场',
  )
})

test('批准与驳回都要二次确认：都在 ConfirmDialog 的确认回调里执行', () => {
  // 两个入口只负责打开确认框，真正的写操作在 doCandidate（@confirm 回调）里
  assert.match(VIEW, /@confirm="doCandidate"/)
  assert.match(VIEW, /async function doCandidate\(\)/)
  // 框开着的时候按钮禁掉，避免连点重复提交
  assert.match(VIEW, /:busy="busyC"/)
  // 三分支状态不许共用空态：加载骨架 / 真的 0 条 / 失败各走各的
  assert.match(VIEW, /v-if="candLoading && !cands\.length"/)
  assert.match(VIEW, /v-else-if="!candError && !cands\.length"/)
  assert.match(VIEW, /banner--danger">\{\{ candError \}\}/)
})

test('交互红线：没有裸 alert / confirm，也没有裸 window 弹框', () => {
  // 前面不许有标识符字符 ⇒ showConfirm( / ConfirmDialog( 不算
  const bare = VIEW.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
})
