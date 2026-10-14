/**
 * format.js 的最小自检：纯函数、断言式，`node --test tests/` 直接跑。
 * 重点盯两件容易静默出错的事：UTC→本地 是否差时区、页码窗口会不会漏首末页。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { toLocal, since, pageWindow } from '../src/utils/format.js'

test('toLocal：带时区的 UTC 转本地', () => {
  const s = toLocal('2026-01-02T03:04:00+00:00')
  const d = new Date('2026-01-02T03:04:00+00:00')
  assert.equal(s.length, 16) // 'YYYY-MM-DD HH:mm'
  assert.equal(s.slice(-5), `${String(d.getHours()).padStart(2, '0')}:04`)
})

test('toLocal：裸 UTC（无 Z）也按 UTC 解释，而不是当本地', () => {
  assert.equal(toLocal('2026-01-02 03:04:00'), toLocal('2026-01-02T03:04:00Z'))
})

test('toLocal：空值 / 解析不了 → 原样或破折号，绝不静默变空', () => {
  assert.equal(toLocal(''), '—')
  assert.equal(toLocal(null), '—')
  assert.equal(toLocal('上周某天'), '上周某天')
})

test('since：未来时间戳返回空串，不显示负数', () => {
  const future = new Date(Date.now() + 60000).toISOString()
  assert.equal(since(future), '')
  assert.equal(since('2026-01-02T03:04:00Z'), '')
})

test('since：秒/分/小时/天四档', () => {
  const iso = (ms) => new Date(Date.now() - ms).toISOString()
  assert.equal(since(iso(5000)), '刚刚')
  assert.equal(since(iso(12 * 60000)), '12 分钟前')
  assert.equal(since(iso(3 * 3600 * 1000)), '3 小时前')
  assert.equal(since(iso(5 * 86400 * 1000)), '5 天前')
})

test('pageWindow：小总数不出现省略号，且首页末页都在', () => {
  assert.deepEqual(pageWindow(1, 3), [1, 2, 3])
  assert.deepEqual(pageWindow(1, 1), [1])
  assert.deepEqual(pageWindow(1, 0), [])
})

test('pageWindow：当前页居中，两端折叠成省略号', () => {
  assert.deepEqual(pageWindow(10, 100), [1, '…', 9, 10, 11, '…', 100])
  assert.deepEqual(pageWindow(1, 100), [1, 2, '…', 100])
  assert.deepEqual(pageWindow(100, 100), [1, '…', 99, 100])
  // 首末页与当前页相邻时不重复插省略号
  assert.deepEqual(pageWindow(2, 100), [1, 2, 3, '…', 100])
})

test('pageWindow：越界页码收敛到合法区间，不越界访问', () => {
  assert.deepEqual(pageWindow(999, 5), [1, '…', 4, 5])
  assert.deepEqual(pageWindow(-3, 5), [1, 2, '…', 5])
})