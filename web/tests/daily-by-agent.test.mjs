/**
 * 「近 7 天记忆增量」图的纯计算（`utils/daily-by-agent.js`）。
 *
 * 钉死三件最容易悄悄错的事：
 *   1) **分组**：同一天的多个代理各归各（不合并成一行「总」），无归属归 `-`；
 *   2) **归并**：7 天总量降序前 N + 「其他」桶（其余代理合并，条数不丢）；
 *   3) **摆位置**：堆叠柱每一格的高度加起来 = 那天的总量（画歪了就看出来）。
 *
 * 纯函数模块（不 import Vue）⇒ node --test 直接跑。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import {
  bucketByAgent,
  dayBreakdown,
  OTHER_KEY,
  stackLayout,
  topSeries,
} from '../src/utils/daily-by-agent.js'

const label = (a) => (a === '-' ? '未标注' : a)

test('分组：同一天的多个代理各归各', () => {
  const b = bucketByAgent([
    { d: '2026-10-23', agent: 'win', n: 2 },
    { d: '2026-10-23', agent: 'mac', n: 1 },
    { d: '2026-10-22', agent: 'win', n: 3 },
  ])
  const win = b.find((x) => x.agent === 'win')
  const mac = b.find((x) => x.agent === 'mac')
  assert.equal(win.total, 5)
  assert.equal(win.byDay.get('2026-10-23'), 2)
  assert.equal(win.byDay.get('2026-10-22'), 3)
  assert.equal(mac.total, 1)
})

test('分组：没有归属的老条目归 `-` 桶（不假装属于某个代理）', () => {
  const b = bucketByAgent([{ d: '2026-10-23', agent: '-', n: 4 }])
  assert.equal(b.length, 1)
  assert.equal(b[0].agent, '-')
  assert.equal(b[0].total, 4)
})

test('分组：空库 ⇒ 空数组（不编一条 0 出来）', () => {
  assert.deepEqual(bucketByAgent([]), [])
  assert.deepEqual(bucketByAgent(undefined), [])
})

test('序列：总量降序前 6 + 「其他」桶（其余合并，条数不丢）', () => {
  // 8 个代理：a1..a7 各 1 条，a8 有 10 条 ⇒ 排序后 a8 第一，其余进「其他」
  const rows = []
  for (let i = 1; i <= 7; i += 1) rows.push({ d: '2026-10-23', agent: `a${i}`, n: 1 })
  rows.push({ d: '2026-10-23', agent: 'a8', n: 10 })
  const series = topSeries(bucketByAgent(rows), 6, label)
  assert.equal(series.length, 7, '前 6 + 「其他」= 7 个序列')
  assert.equal(series[0].key, 'a8', '总量最大的排第一')
  assert.equal(series[0].total, 10)
  const other = series[series.length - 1]
  assert.equal(other.key, OTHER_KEY)
  assert.equal(other.restCount, 2, '前 6 名之外的两个（a6/a7）都进「其他」')
  assert.equal(other.total, 2, '「其他」的总量 = 落选代理的条数')
})

test('序列：代理不足 6 个时**不**造「其他」桶', () => {
  const series = topSeries(bucketByAgent([{ d: '2026-10-23', agent: 'win', n: 1 }]), 6, label)
  assert.equal(series.length, 1)
  assert.equal(series[0].key, 'win')
})

test('堆叠柱：每格高度加起来 = 那天的总量', () => {
  const days = ['2026-10-21', '2026-10-22', '2026-10-23']
  const rows = [
    { d: '2026-10-22', agent: 'win', n: 3 },
    { d: '2026-10-22', agent: 'mac', n: 1 },
    { d: '2026-10-23', agent: 'win', n: 5 },
  ]
  const opts = { colorOf: () => 'red', padT: 14 }
  const series = topSeries(bucketByAgent(rows), 6, label)
  const L = stackLayout(days, series, opts)
  assert.equal(L.total, 9, '总量 = 全部格子的条数')
  const plotH = L.baseY - opts.padT
  for (const day of days) {
    const segs = L.segs.filter((s) => s.key.endsWith(`|${day}`))
    const h = segs.reduce((a, s) => a + s.h, 0)
    const n = series.reduce((a, s) => a + (s.byDay.get(day) || 0), 0)
    if (!n) {
      assert.equal(segs.length, 0, '没有写入的那天不许画格子')
      continue
    }
    assert.ok(Math.abs(h - (n / L.max) * plotH) < 1e-6, `${day} 的堆叠高度对不上`)
  }
})

test('堆叠柱：同一格里的段自下而上（图例顺序 = 堆叠顺序）', () => {
  const days = ['2026-10-23']
  const series = topSeries(bucketByAgent([
    { d: '2026-10-23', agent: 'win', n: 2 },
    { d: '2026-10-23', agent: 'mac', n: 3 },
  ]), 6, label)
  const L = stackLayout(days, series, {})
  const segs = L.segs.filter((s) => s.key.endsWith('|2026-10-23'))
  assert.equal(segs.length, 2)
  // 第一个序列（win）在下面：它的下沿贴着基线
  assert.ok(Math.abs(segs[0].y + segs[0].h - L.baseY) < 1e-6)
  // 第二个（mac）压在它上面：下沿 = 上一段的顶
  assert.ok(Math.abs(segs[1].y + segs[1].h - segs[0].y) < 1e-6)
})

test('堆叠柱：横轴标签是 MM-DD，网格线是 0 / 中值 / 满值', () => {
  const days = ['2026-10-21', '2026-10-23']
  const series = topSeries(bucketByAgent([{ d: '2026-10-23', agent: 'win', n: 4 }]), 6, label)
  const L = stackLayout(days, series, {})
  assert.deepEqual(L.xLabels.map((x) => x.text), ['10-21', '10-23'])
  assert.deepEqual(L.grid.map((g) => g.v), [0, 2, 4])
  assert.equal(L.max, 4)
})

test('堆叠柱：段沿用序列的颜色（图例色点与柱子里是同一份）', () => {
  const series = [{
    key: 'a', label: 'a', total: 1, color: 'red', byDay: new Map([['2026-10-23', 1]]),
  }]
  const L = stackLayout(['2026-10-23'], series, { colorOf: (s) => s.color })
  assert.equal(L.segs.length, 1)
  assert.equal(L.segs[0].color, 'red')
})

test('堆叠柱：空数据不炸（max 兜底为 1，不除零）', () => {
  const L = stackLayout([], [], {})
  assert.equal(L.segs.length, 0)
  assert.equal(L.max, 1)
  assert.equal(L.total, 0)
})

/* ---------------------------------------------------------------- 悬停浮层明细
 * 浮层显示「某日全量 + 各代理分项」—— 数字必须自洽（总量 = 分项之和），
 * 否则用户看到的总量和分项对不上，整张图就不敢信了。 */

test('浮层：同一天多个代理按条数降序', () => {
  const b = bucketByAgent([
    { d: '2026-10-23', agent: 'win', n: 2 },
    { d: '2026-10-23', agent: 'mac', n: 7 },
    { d: '2026-10-22', agent: 'mac', n: 100 },
  ])
  const info = dayBreakdown(b, '2026-10-23', label)
  assert.deepEqual(info.parts.map((p) => p.key), ['mac', 'win'], '按条数降序（不是按 agent id）')
  assert.deepEqual(info.parts.map((p) => p.count), [7, 2])
  assert.equal(info.total, 9, '只看那一天 —— 别的天不许混进来')
})

test('浮层：「未标注」桶参与合计（不摊到别人头上）', () => {
  const b = bucketByAgent([
    { d: '2026-10-23', agent: 'win', n: 3 },
    { d: '2026-10-23', agent: '-', n: 4 },
  ])
  const info = dayBreakdown(b, '2026-10-23', label)
  const un = info.parts.find((p) => p.key === '-')
  assert.ok(un, '未标注自己占一行')
  assert.equal(un.label, '未标注')
  assert.equal(un.count, 4)
  assert.equal(info.total, 7, '总量含未标注那 4 条')
})

test('浮层：总量 = 分项之和（数字自洽）', () => {
  const rows = []
  for (let i = 1; i <= 5; i += 1) rows.push({ d: '2026-10-23', agent: `a${i}`, n: i })
  rows.push({ d: '2026-10-23', agent: '-', n: 3 })
  const info = dayBreakdown(bucketByAgent(rows), '2026-10-23', label)
  assert.equal(info.total, info.parts.reduce((a, p) => a + p.count, 0))
  assert.equal(info.total, 18)
})

test('浮层：没有写入的那天 ⇒ 0 条、无分项（不编一条代理出来）', () => {
  const b = bucketByAgent([{ d: '2026-10-23', agent: 'win', n: 2 }])
  const info = dayBreakdown(b, '2026-10-22', label)
  assert.equal(info.total, 0)
  assert.deepEqual(info.parts, [])
})

test('堆叠柱：逐日一列的锚点（柱顶数字 + 整列命中区）', () => {
  const days = ['2026-10-22', '2026-10-23']
  const series = topSeries(bucketByAgent([
    { d: '2026-10-22', agent: 'win', n: 3 },
    { d: '2026-10-23', agent: 'win', n: 5 },
  ]), 6, label)
  const L = stackLayout(days, series, {})
  assert.equal(L.cols.length, 2)
  assert.deepEqual(L.cols.map((c) => c.day), days)
  assert.deepEqual(L.cols.map((c) => c.total), [3, 5], '逐日总量')
  for (const c of L.cols) {
    assert.ok(c.cx > 0 && c.cx < 720, '列中心在画布内')
    assert.ok(c.topY < L.baseY, '柱顶在基线之上')
    assert.ok(c.hitW > 0 && c.hitX >= 0, '整列命中区在画布内')
  }
})
