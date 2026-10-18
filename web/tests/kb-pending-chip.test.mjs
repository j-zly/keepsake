/**
 * 知识库「审核入口」的护栏（原 kb_pending_chip_zero 演进而来）。
 *
 * 设计盲区回归点：旧实现把 chip 用 `v-if="pendCount > 0"` 整行藏起来，导致队列空时
 * 面板打不开、已驳回历史也无处查看 —— 用户找审核功能被「扣了入口」。
 * 后来审核队列又从 KbView 整段搬去了独立页 /kb-review（KbReviewView），chip 本身
 * 随之没了。现在这条不变式换了个说法继续钉死：**入口永远在**。
 *
 *   1) 侧栏「知识库 › 审核」是 GROUPS 里写死的一项（不按计数隐藏 ⇒ 空队列也点得进）；
 *   2) KbView 只留一行指路「待审 N 条 › 去审核」，router-link 指向 /kb-review，
 *      且 v-if 只看 pending_count（没东西要判就不占地方，但入口没被删）；
 *   3) 那个 N 全来自后端（/review/counts 的 kb_pending），前端零写死数字。
 *
 * 静态扫描，纯字符串断言；不进浏览器、不起 fixture。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const KBR = read('views/KbReviewView.vue')
const APP = read('App.vue')

test('入口不藏：侧栏「知识库 › 审核」是无条件项，不按待审计数隐藏', () => {
  // GROUPS 里的项写死 ⇒ 空队列也点得进（历史在审核页里照看）
  assert.match(APP, /\{ path: '\/kb-review', label: '审核', icon: ICON\.\w+ \}/,
    '侧栏知识库组必须有「审核」项，且 icon 复用 ICON 集')
  // 徽标只是提示：v-if 挂在 badgeFor 上，不挂在项本身 ⇒ 数 0 时项照样在
  assert.match(APP, /v-if="badgeFor\(m\.path\)"/,
    '只有徽标按计数隐藏，导航项本身不许按计数隐藏')
})

test('KbView：只剩一行指路，且 router-link 指向 /kb-review', () => {
  assert.match(KB, /<router-link class="tab" to="\/kb-review">待审 \{\{ kbPendingCount \}\} 条 › 去审核<\/router-link>/,
    '指路行要写清「待审 N 条 › 去审核」并跳到审核页')
  // 只在真有未审项时占地方；隐藏的是这一行，不是入口本身
  assert.match(KB, /<template v-if="kbPendingCount > 0">/,
    '指路行的 v-if 挂在 pending_count 上')
})

test('KbView：chip 与待审面板已搬走（不留残骸，也不留双份队列）', () => {
  for (const s of ['chipText', 'pendingOpen', 'pendRows', 'loadPending', 'togglePending', 'pendLoading']) {
    assert.ok(!KB.includes(s), `KbView 里还留着「${s}」—— 审核队列已搬到 KbReviewView`)
  }
  assert.doesNotMatch(KB, /api\.kbPending/, 'KbView 不该再拉待审队列')
  // 但审核页确实把整套搬过去了（否则是真删功能，不是搬家）
  assert.match(KBR, /api\.kbPending\(/)
  assert.match(KBR, /chip-tone="accent"/)
})

test('计数全来自后端，指路行的数不许写死在模板里', () => {
  assert.match(KB, /kbPendingCount\.value = Number\(d\?\.kb_pending\) \|\| 0/,
    '待审数必须来自后端 /review/counts 的 kb_pending（与侧栏徽标同源）')
  assert.match(APP, /kb_pending/, '侧栏徽标读的是同一个字段')
  // 接口问不到就不渲染那行，而不是拿 0 冒充「已审完」
  assert.match(KB, /catch \{[\s\S]{0,120}?kbPendingCount\.value = 0/)
  // 模板里的 N 是变量，不是字面量
  assert.doesNotMatch(KB, /待审 \d+ 条/, '模板不许出现写死的待审数字')
})
