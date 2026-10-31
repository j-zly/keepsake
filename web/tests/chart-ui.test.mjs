/**
 * 看板图表化的前端护栏（node --test 直接跑）。
 *
 * 钉住任务书判据里的四条：
 *   ① **chart-theme.js 是唯一色值出处**：三个图表组件与 chart-kit.js 里
 *      **一个裸 hex / rgba 都不许有**；chart-theme.js 里每个 hex 都必须
 *      在 tokens.css 里找得到（改了令牌忘了改这里 ⇒ 这里先红）。
 *   ② **按需引入**：三个组件只 import `echarts/core` + 具体图表/组件/渲染器，
 *      **不许整包 import**（整包是 1MB 级的 gzip 增量）。
 *   ③ **dispose**：图表实例必须在卸载时释放（不释放 = 切几次页就泄漏一份 canvas）。
 *   ④ **option.series 的形状**：图上到底画了什么（柱子 / 仪表 / 折线）。
 *
 * 纯函数（option 构造 + 生命周期）**跑真函数**（不抄一份到测试里）；
 * `.vue` 组件跑不起来 ⇒ 那几条走静态扫描。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  barsOption,
  createChartController,
  gaugeOption,
  sparkOption,
} from '../src/utils/chart-kit.js'
import { CHART_PALETTE, chartPalette, completionColor, usageColor } from '../src/utils/chart-theme.js'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const TOKENS = read('styles/tokens.css')
const THEME = read('utils/chart-theme.js')
const KIT = read('utils/chart-kit.js')

/** 三个图表组件（.vue 跑不起来 ⇒ 静态扫源码）。 */
const CHART_VUES = ['components/AgentUsageBars.vue', 'components/RingGauge.vue',
  'components/SparkLine.vue']

/** 扫一个文件里的裸色值（hex / rgba / hsl），跳过注释行不算 —— 注释里的
 *  「#fff」只是说明文字，不参与渲染。这里保守一点：注释行也扫，
 *  免得有人把注释当借口往组件里塞色值。 */
function colorHits(text) {
  const hits = []
  text.split('\n').forEach((line, i) => {
    if (/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/.test(line)) hits.push(`${i + 1}: ${line.trim()}`)
  })
  return hits
}

// ---------------------------------------------------------------- ① 色值单点

test('① 三个图表组件与 chart-kit.js 里没有裸色值', () => {
  for (const f of CHART_VUES) {
    assert.deepEqual(colorHits(read(f)), [], `${f} 里有裸色值 —— 色值必须回 chart-theme.js`)
  }
  assert.deepEqual(colorHits(KIT), [], 'utils/chart-kit.js 里有裸色值')
})

test('① chart-theme.js 的每个 hex 都能在 tokens.css 里找到', () => {
  const hexes = [...new Set(THEME.match(/#[0-9a-fA-F]{6}\b/g) || [])]
  assert.ok(hexes.length >= 10, '色板不该只有几个色值')
  const missing = hexes.filter((h) => !TOKENS.includes(h.toLowerCase()))
  assert.deepEqual(missing, [], `这些色值不在 tokens.css 里：${missing.join(' ')}`)
})

test('① chart-theme.js 是 src 下唯一的色值出处（tokens.css 除外）', () => {
  // 只有 chart-theme.js 允许出现 hex（canvas 读不到 CSS 变量 ⇒ 必须有一份副本）
  const offenders = []
  for (const f of CHART_VUES) {
    if (colorHits(read(f)).length) offenders.push(f)
  }
  assert.deepEqual(offenders, [], '裸色值只许出现在 utils/chart-theme.js')
})

test('① 明暗两套色板都在，且每个键都有值', () => {
  for (const theme of ['light', 'dark']) {
    const p = CHART_PALETTE[theme]
    assert.ok(p, `${theme} 色板不见了`)
    for (const key of ['bg', 'panel', 'grid', 'text', 'textDim', 'accent', 'ok', 'warn', 'danger']) {
      assert.match(String(p[key] || ''), /^#[0-9a-fA-F]{6}$/, `${theme}.${key} 不对`)
    }
  }
  // 明暗两套必须**不一样**（同色 = 暗色模式下白底黑字）
  assert.notEqual(CHART_PALETTE.light.grid, CHART_PALETTE.dark.grid)
})

test('① usageColor 的三档阈值（≥100 危险 / ≥90 告警 / 其余强调）', () => {
  const p = chartPalette()
  assert.equal(usageColor(120, p), p.danger)
  assert.equal(usageColor(100, p), p.danger)
  assert.equal(usageColor(95, p), p.warn)
  assert.equal(usageColor(90, p), p.warn)
  assert.equal(usageColor(89.9, p), p.accent)
  assert.equal(usageColor(0, p), p.accent)
})

// 🔴 2026-10 ks_cas_hard 第零部分：完成率的色档与**占用**色档是**两套**。
//   占用（usageColor）：越高越危险（≥100 红）—— 水位条用它，档位是对的。
//   完成率（completionColor）：越高越好 —— 100% 必须是**绿**，不是红。
//   用户实锤过：待办完成率环 100% 显示红色 ⇒ 复用了 usageColor。

test('① completionColor 的三档（100 绿 / 中间红 / 0 或缺失中性蓝）', () => {
  const p = chartPalette()
  assert.equal(completionColor(100, p), p.ok, '全销掉了 ⇒ 绿')
  assert.equal(completionColor(120, p), p.ok)
  assert.equal(completionColor(50, p), p.danger, '还有没销掉的 ⇒ 红')
  assert.equal(completionColor(0.5, p), p.danger)
  assert.equal(completionColor(0, p), p.accent, '0 条待办 ⇒ 中性蓝（不是满屏红）')
  assert.equal(completionColor(undefined, p), p.accent, '数据缺失 ⇒ 中性蓝')
  assert.equal(completionColor(NaN, p), p.accent)
  assert.equal(completionColor('abc', p), p.accent)
})

test('① 完成率环用 completionColor（不许再复用占用档 usageColor）', () => {
  const stats = read('views/StatsView.vue')
  assert.match(stats, /color: completionColor\(todoPercent\.value, p\)/,
    '完成率环的颜色必须走 completionColor')
  assert.doesNotMatch(stats, /usageColor\(todoPercent/,
    '完成率不许再走占用档 usageColor（100% 会被画成红色）')
})

// ---------------------------------------------------------------- ② 按需引入

test('② 三个组件按需引入（echarts/core + 具体图表），不整包 import', () => {
  for (const f of CHART_VUES) {
    const src = read(f)
    assert.match(src, /from 'echarts\/core'/, `${f} 该按需引入 echarts/core`)
    assert.ok(!/from 'echarts'$/.test(src), `${f} 不许整包 import echarts`)
    assert.match(src, /echarts\.use\(\[/, `${f} 该显式注册用到的图表`)
  }
  // 三个图表各注册自己那一块（Bar / Gauge / Line）
  assert.match(read(CHART_VUES[0]), /BarChart/)
  assert.match(read(CHART_VUES[1]), /GaugeChart/)
  assert.match(read(CHART_VUES[2]), /LineChart/)
})

// ---------------------------------------------------------------- ③ dispose

test('③ dispose 真的把实例释放了（且释放后不再画）', () => {
  const calls = []
  const fakeEcharts = {
    init(el) {
      calls.push('init')
      return {
        setOption: () => calls.push('setOption'),
        resize: () => calls.push('resize'),
        dispose: () => calls.push('dispose'),
      }
    },
  }
  const observed = []
  class FakeRO {
    constructor(cb) { this.cb = cb }
    observe(el) { observed.push(el) }
    disconnect() { observed.push('disconnect') }
  }
  const ctl = createChartController({ tag: 'div' }, fakeEcharts, { ResizeObserver: FakeRO })
  ctl.setOption({ series: [] })
  ctl.resize()
  ctl.dispose()
  ctl.dispose()          // 幂等：第二次不许再调 dispose
  const afterDispose = calls.length
  ctl.setOption({ series: [] })   // 释放后不许再画
  ctl.resize()
  assert.equal(calls.filter((c) => c === 'dispose').length, 1, 'dispose 只能调一次')
  assert.ok(observed.includes('disconnect'), 'ResizeObserver 必须断开')
  assert.equal(calls.length, afterDispose, '释放后的调用不许再落到实例上')
})

test('③ 三个组件都在卸载时 dispose', () => {
  for (const f of CHART_VUES) {
    const src = read(f)
    assert.match(src, /onUnmounted\(/, `${f} 没有 onUnmounted`)
    assert.match(src, /ctl\.dispose\(\)/, `${f} 没有 dispose 图表实例`)
    assert.match(src, /onUnmounted\(\(\) => \{[\s\S]{0,600}dispose\(\)/,
      `${f} 的 dispose 不在 onUnmounted 里`)
  }
})

// ---------------------------------------------------------------- ④ option.series

test('④ barsOption：两根柱子（预算轨道 + 占用），y 轴是代理名', () => {
  const rows = [
    { agent_id: 'a', label: '主代理', chars: 800, budget: 1000, percent: 80 },
    { agent_id: 'b', label: '子代理', chars: 1200, budget: 1000, percent: 120 },
  ]
  const opt = barsOption(rows, chartPalette())
  assert.equal(opt.series.length, 2)
  assert.equal(opt.series[0].type, 'bar')
  assert.deepEqual(opt.series[0].data, [1000, 1000], '轨道 = 各自预算')
  assert.deepEqual(opt.series[1].data.map((d) => d.value), [800, 1200])
  assert.deepEqual(opt.yAxis.data, ['主代理', '子代理'])
  // 超 90% 的那根用告警/危险色（颜色取自 chart-theme，不是硬编码）
  assert.notEqual(opt.series[1].data[0].itemStyle.color, opt.series[1].data[1].itemStyle.color)
})

test('④ gaugeOption：每块指标一块仪表，值与标题都对得上', () => {
  const metrics = [
    { label: '总预算水位', percent: 92, color: '#ff0000' },
    { label: '待办完成率', percent: 40, color: '#00ff00' },
  ]
  const opt = gaugeOption(metrics, chartPalette())
  assert.equal(opt.series.length, 2)
  for (const [i, s] of opt.series.entries()) {
    assert.equal(s.type, 'gauge')
    assert.equal(s.data[0].value, metrics[i].percent)
    assert.equal(s.data[0].name, metrics[i].label)
  }
})

test('④ sparkOption：一条折线，数据是每日增量', () => {
  const points = [
    { date: '2026-10-20', count: 3 },
    { date: '2026-10-21', count: 0 },
    { date: '2026-10-22', count: 5 },
  ]
  const opt = sparkOption(points, chartPalette())
  assert.equal(opt.series.length, 1)
  assert.equal(opt.series[0].type, 'line')
  assert.deepEqual(opt.series[0].data, [3, 0, 5])
  assert.deepEqual(opt.xAxis.data, ['10-20', '10-21', '10-22'])
})

test('④ 空数据也不炸（看板刚起时没有数据）', () => {
  const p = chartPalette()
  assert.equal(barsOption([], p).series[0].data.length, 0)
  assert.equal(gaugeOption([], p).series.length, 0)
  assert.equal(sparkOption([], p).series[0].data.length, 0)
})
