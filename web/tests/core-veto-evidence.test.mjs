/**
 * 「退居建议」卡的冲突证据展示（后端 pipeline 常驻否决 ↔ 前端审核区）的护栏。
 *
 * 这块不做就静默出错的三件事：
 *   1) **证据解析**：`tier:evid:<新事实 key>` 从 tags 里取出 key，且**不误伤**
 *      别的标签（`tier:evid:` 本身 / `tier:evidence` / 空 key 都要挡住）；
 *   2) **证据展示**：按需懒加载既有读接口（`api.fragment`），三态（读取中 / 正文 /
 *      证据已不可查）都摆出来 —— 证据条目被删时显示「查不到」，不许崩、不许编；
 *   3) **摘标不残留**：驳回 / 批准都经 `dropTag(…, 'tier:demote-candidate')`，
 *      证据标必须**跟着一起摘**（方向标一摘，证据标就成了列表永远不显示的孤儿）。
 *      两条路径（驳回用列表行的 tags、批准用 demote 返回的 tags）都钉住。
 *
 * 纯函数照旧从 ReviewView（审核独立成页后从 MaintenanceView 搬过来）里
 * **原样抽出来**真跑（不是抄一份）——
 * 抽取窗口（`/** 逗号串 → 标签数组` … `const cands = ref(`）是既有约定。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const VIEW = readFileSync(fileURLToPath(new URL('../src/views/ReviewView.vue', import.meta.url)), 'utf8')

const { splitTags, dropTag, evidKeysOf } = new Function(
  VIEW.slice(
    VIEW.indexOf('/** 逗号串 → 标签数组'),
    VIEW.indexOf('const cands = ref('),
  ) + '\nreturn { splitTags, dropTag, evidKeysOf }',
)()

test('证据解析：只认完整的 tier:evid: 前缀，key 取出干净', () => {
  assert.deepEqual(evidKeysOf('core,tier:evid:memory:frag:abc123'), ['memory:frag:abc123'])
  // 多条证据（管线一天提两次时）都取出来，顺序不乱
  assert.deepEqual(
    evidKeysOf('tier:evid:k1,core,tier:evid:k2'),
    ['k1', 'k2'],
  )
  // 边界：没有证据 / 前缀不完整 / key 为空，都不许误报
  assert.deepEqual(evidKeysOf('core,memory'), [])
  assert.deepEqual(evidKeysOf(''), [])
  assert.deepEqual(evidKeysOf('tier:evidence:x,tier:evid,y'), [], '没有冒号的不算证据标')
  assert.deepEqual(evidKeysOf('tier:evid:   '), [], '空 key 不许进加载列表')
  // 标签边界口径与 splitTags 一致（前缀相接的别的标签不被吃掉）
  assert.deepEqual(splitTags('a,b'), ['a', 'b'])
})

test('驳回 = 摘方向标 + 摘证据标（同批，不留孤儿）', () => {
  // 批准路径同样经过这个函数（demote 返回的 tags 再摘一次），所以函数本身够用
  assert.equal(
    dropTag('core,memory,tier:demote-candidate,tier:evid:memory:frag:abc', 'tier:demote-candidate'),
    'core,memory',
  )
  // 幂等：再来一次原样返回（重复驳回不吃掉别的标签）
  assert.equal(dropTag('core,memory', 'tier:demote-candidate'), 'core,memory')
  // 摘别的标时**不许**连带摘证据标（那是退居方向专属的联动）
  assert.equal(dropTag('core,core_candidate,tier:evid:k1', 'core_candidate'), 'core,tier:evid:k1')
  // 证据标是唯一标签时，摘完留空串（正文不受影响）
  assert.equal(dropTag('tier:demote-candidate,tier:evid:k1', 'tier:demote-candidate'), '')
})

test('证据按需懒加载 + 三态都不崩', () => {
  // 只走既有读接口，没有自造第四个接口
  assert.match(VIEW, /await api\.fragment\(k\)/, '证据正文必须走既有单条读接口')
  assert.match(VIEW, /evidCache\.value\[k\] = null/, '要有「读取中」这一态')
  assert.match(VIEW, /evidCache\.value\[k\] = ''/, '查不到要落成空串而不是抛')
  assert.match(VIEW, /return v \|\| '证据已不可查'/, '查不到必须显式说出来')
  assert.match(VIEW, /function loadEvid\(rows\)/)
  assert.match(VIEW, /loadEvid\(data\.items\)/, '列表拉回来后要触发证据加载')
  // 逐条独立失败：一条 404 不许把整卡 Promise.all 拖成未处理的拒绝
  const load = VIEW.slice(VIEW.indexOf('async function loadEvid('), VIEW.indexOf('function onCandDir('))
  assert.match(load, /catch \{/, '必须有 per-key catch')
  // 只在退居方向加载：晋升候选没有冲突证据这回事
  assert.match(load, /candDir\.value === 'demote' \? evidKeysOf\(row\.tags\) : \[\]/)
})

test('卡片上真的渲染了证据行，且只在退居方向', () => {
  assert.match(
    VIEW,
    /<div v-if="candDir === 'demote' && evidKeysOf\(row\.tags\)\.length" class="evid">/,
    '退居建议卡上要有证据区块',
  )
  assert.match(VIEW, /与这条新事实冲突：/, '必须写清楚这句是在讲冲突证据')
  assert.match(VIEW, /v-for="k in evidKeysOf\(row\.tags\)"/)
  assert.match(VIEW, /:title="evidText\(k\)"/, '可点开看全文（title 给全文）')
  // 两行截断（超长证据不许把卡片撑变形）
  assert.match(VIEW, /-webkit-line-clamp: 2/)
  // 交互红线照旧
  const bare = VIEW.match(/(?<![A-Za-z0-9_.$])(alert|confirm|prompt)\s*\(/g) || []
  assert.deepEqual(bare, [], '必须走 ConfirmDialog / utils/dialog，不许原生弹框')
})
