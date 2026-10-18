/**
 * P11 信息架构护栏（纯静态扫描，node --test 直接跑）：
 *   侧栏三组「总览 / 记忆 / 知识库」，看板是总览入口排第一，
 *   记忆组第一项是「审核」（审核独立成页后排在最前，常驻层之前）；
 *   面包屑从 GROUPS 取，所以分组名一改这里就红。
 * （代理治理入组后记忆组四项、总入口六个；审核独立成页后记忆组五项、总入口七个。）
 * 这类「顺序错了也编译得过」的事，钉在源码上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const APP = readFileSync(fileURLToPath(new URL('../src/App.vue', import.meta.url)), 'utf8')

// 取出 GROUPS 字面量里出现的三段分组（按源码顺序）
const groups = [...APP.matchAll(/title: '([^']+)',\s*\n\s*items: \[([\s\S]*?)\]\s*,?\n\s*\}/g)].map(
  (m) => ({ title: m[1], items: [...m[2].matchAll(/label: '([^']+)'/g)].map((x) => x[1]) }),
)

test('侧栏：看板在第一组「总览」，共三组，八个入口', () => {
  assert.deepEqual(
    groups.map((g) => g.title),
    ['总览', '记忆', '知识库'],
  )
  assert.deepEqual(groups[0].items, ['看板'])
  // 审核排记忆组第一位：它是「有东西要处理」的高频动作，不该埋在分组深处
  assert.deepEqual(groups[1].items, ['审核', '常驻层', '记忆条目', '记忆维护', '代理治理'])
  // 知识库组：审核排第一（待审是高频动作），知识库浏览页在后
  assert.deepEqual(groups[2].items, ['审核', '知识库'])
  assert.equal(groups.flatMap((g) => g.items).length, 8)
})

test('面包屑：随分组走（看板页 总览 › 看板），旧的「知识库与运维」措辞已消失', () => {
  assert.match(APP, /const crumb = computed\(\(\) => \{\s*\n\s*for \(const g of GROUPS\)/)
  assert.doesNotMatch(APP, /知识库与运维/)
})
