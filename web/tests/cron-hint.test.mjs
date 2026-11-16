/**
 * 「调度人话副标」（cron → 中文）的口径护栏（scheduler_page_ui_fix）。
 *
 * 为什么单独立一个文件：这张表上写错一个字，用户就会**按错的频率等班车**
 * —— `15 * * * *` 是**每小时第 15 分**，不是「每天 00:15」（实拍里就是这么错过：
 * 下次执行 23:15 自己把这句中文打脸了）。这类错静态扫不出来，必须**真跑一遍**函数。
 *
 * 钉死的三件事：
 *   H1 小时域是 `*` ⇒ 每小时（N 分），**不许说成每天**；
 *   H2 四条班车种子（BUS_JOBS）的副标与 cron 表达式自洽；
 *   H3 认不出来的形状退「自定义调度」（不许猜语义编一句中文）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { cronHint } from '../src/utils/cron.js'

test('H1 小时域是 * ⇒ 每小时，不许说成每天', () => {
  assert.equal(cronHint('15 * * * *'), '每小时第 15 分')
  assert.doesNotMatch(cronHint('15 * * * *'), /每天/, '每小时第 15 分不是每天 00:15')
  assert.equal(cronHint('0 * * * *'), '每小时整点')
  // 每 N 分钟仍是每 N 分钟（小时域全开 + 分位步进）
  assert.equal(cronHint('*/15 * * * *'), '每 15 分钟')
})

test('H2 四条班车种子的副标与 cron 自洽', () => {
  // 与 core/bus.py 的 BUS_JOBS 逐条对得上（schedule → 中文）
  assert.equal(cronHint('15 * * * *'), '每小时第 15 分')      // ks-distill
  assert.equal(cronHint('0 */2 * * *'), '每 2 小时')            // ks-maintenance
  assert.equal(cronHint('0 */8 * * *'), '每 8 小时')            // ks-synonyms
  assert.equal(cronHint('30 4 * * *'), '每天 04:30')            // ks-tiering
})

test('H3 认不出来的形状退「自定义调度」（不许猜语义）', () => {
  assert.equal(cronHint(''), '自定义调度')
  assert.equal(cronHint('30 4 * *'), '自定义调度')      // 不是五段
  assert.equal(cronHint('30 4 * * * *'), '自定义调度')  // 段数不对
  assert.equal(cronHint('0 9-18 * * 1-5'), '自定义调度') // 区间/列表：说不清就别说
  assert.equal(cronHint('abc'), '自定义调度')
})

test('每周 / 每月：周字段 0 与 7 都是周日', () => {
  assert.equal(cronHint('0 5 * * 1'), '每周一 05:00')
  assert.equal(cronHint('30 4 * * 0'), '每周日 04:30')
  assert.equal(cronHint('30 4 * * 7'), '每周日 04:30')
  assert.equal(cronHint('30 4 * 5 *'), '5 月 04:30')
})
