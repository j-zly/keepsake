/**
 * 图表 option 构造 + 实例生命周期 —— **纯函数**（不 import Vue / echarts）。
 *
 * ## 为什么要单独一个文件
 *
 * `.vue` 组件在 `node --test` 里**跑不起来**（它不是 JS 模块），而两件事必须
 * 能钉在测试里：
 *
 * 1. **option.series 的形状**（图上到底画了什么 —— 柱子、仪表、折线）；
 * 2. **dispose 有没有做**（图表实例不释放 = 切页几次就泄漏一份 canvas）。
 *
 * 于是：构造与生命周期放这里（纯函数 + 可注入的假 echarts，可测），
 * 组件只剩「init / setOption / resize / dispose」四步薄壳。
 *
 * 🔴 **色值一律走 `utils/chart-theme.js`**（本文件不写任何色值）。
 */

import { baseOption, usageColor } from './chart-theme.js'

/**
 * 图表实例控制器：init / setOption / resize / dispose 四件事收在一处。
 *
 * @param {object} el      挂载容器（DOM 元素）
 * @param {object} factory echarts 模块（要能 `init(el)`）
 * @param {object} opt     `{ResizeObserver}`（测试可注入假实现）
 */
export function createChartController(el, factory, opt = {}) {
  const chart = factory.init(el)
  let disposed = false
  let observer = null
  const ROCtor = opt.ResizeObserver ?? (typeof ResizeObserver !== 'undefined' ? ResizeObserver : null)
  if (el && ROCtor) {
    // 🔴 **容器变了就重画**：窄屏 / 侧栏折叠时容器宽度会变，
    //   只监听 window.resize 是不够的（ECharts 自己不会量容器）。
    observer = new ROCtor(() => {
      if (!disposed) chart.resize()
    })
    observer.observe(el)
  }
  return {
    chart,
    /** 整份替换（notMerge=true ⇒ 换数据不会留下上一次的残影）。 */
    setOption(option) {
      if (!disposed) chart.setOption(option, true)
    },
    resize() {
      if (!disposed) chart.resize()
    },
    dispose() {
      if (disposed) return
      disposed = true
      if (observer) {
        observer.disconnect()
        observer = null
      }
      chart.dispose()
    },
  }
}

/** HTML 转义（tooltip 是 HTML —— 代理名是用户填的，不能直接拼进去）。 */
export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function pct(chars, budget) {
  const b = Number(budget || 0)
  if (!(b > 0)) return '0.0'
  return ((Number(chars || 0) * 100) / b).toFixed(1)
}

/**
 * 横向条形图：**每代理 core 占用 vs 各自预算**。
 *
 * 两根 series：预算（浅色轨道，静默）+ 占用（按占比上色），
 * `barGap: '-100%'` 让占用叠在轨道上 —— 一眼看出「离预算还有多远」。
 *
 * @param {object[]} rows  `agentUsageRows` 的行
 * @param {object}   palette 色板（`chartPalette()`）
 */
export function barsOption(rows, palette) {
  const p = palette
  const list = rows || []
  const labels = list.map((r) => r?.label || r?.agent_id || '')
  return {
    ...baseOption(p, {
      grid: { left: 8, right: 56, top: 12, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const row = list[idx] || {}
          const name = escapeHtml(row.label || row.agent_id || '')
          return `${name}<br/>占用 ${Number(row.chars || 0)} 字符 / 预算 ${Number(row.budget || 0)}<br/>占比 ${pct(row.chars, row.budget)}%`
        },
      },
      xAxis: {
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: p.textFaint, fontSize: 11 },
        splitLine: { lineStyle: { color: p.grid } },
      },
      yAxis: {
        type: 'category',
        data: labels,
        inverse: true,
        axisLine: { lineStyle: { color: p.grid } },
        axisTick: { show: false },
        axisLabel: { color: p.textDim, fontSize: 12 },
      },
      series: [
        {
          name: '预算',
          type: 'bar',
          data: list.map((r) => Number(r?.budget || 0)),
          barGap: '-100%',
          silent: true,
          itemStyle: { color: p.track, borderRadius: [0, 4, 4, 0] },
          barWidth: 14,
        },
        {
          name: '占用',
          type: 'bar',
          data: list.map((r) => ({
            value: Number(r?.chars || 0),
            itemStyle: { color: usageColor(r?.percent, p), borderRadius: [0, 4, 4, 0] },
          })),
          barWidth: 14,
        },
      ],
    }),
  }
}

/**
 * 环形仪表：每块指标一块（总预算水位 / 待办完成率）。
 *
 * @param {object[]} metrics `[{label, percent, color, value, max}]`
 * @param {object}   palette 色板
 */
export function gaugeOption(metrics, palette) {
  const p = palette
  const list = metrics || []
  const step = list.length > 1 ? 100 / list.length : 100
  const series = list.map((m, i) => {
    const center = list.length > 1 ? `${step * (i + 0.5)}%` : '50%'
    const color = m?.color || p.accent
    return {
      type: 'gauge',
      center: [center, '58%'],
      radius: '72%',
      min: 0,
      max: 100,
      startAngle: 200,
      endAngle: -20,
      progress: { show: true, width: 12, itemStyle: { color } },
      axisLine: { lineStyle: { width: 12, color: [[1, p.track]] } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: false },
      anchor: { show: false },
      title: { show: true, offsetCenter: [0, '38%'], color: p.textFaint, fontSize: 12 },
      detail: {
        valueAnimation: true,
        offsetCenter: [0, '4%'],
        color,
        fontSize: 22,
        fontWeight: 700,
        formatter: '{value}%',
      },
      data: [{ value: Number(m?.percent || 0), name: m?.label || '' }],
    }
  })
  return {
    ...baseOption(p, {
      grid: { left: 8, right: 8, top: 8, bottom: 8, containLabel: false },
      tooltip: {
        trigger: 'item',
        formatter: (params) => {
          const m = list[params?.dataIndex] || {}
          return `${escapeHtml(m.label || '')}<br/>${Number(m?.percent || 0).toFixed(1)}%`
        },
      },
      series,
    }),
  }
}

/**
 * 折线：近 N 天的**每日新增**（sparkline）。
 *
 * @param {object[]} points `[{date, count}]`
 * @param {object}   palette 色板
 */
export function sparkOption(points, palette) {
  const p = palette
  const list = points || []
  return {
    ...baseOption(p, {
      grid: { left: 8, right: 12, top: 16, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        formatter: (params) => {
          const arr = Array.isArray(params) ? params : [params]
          const idx = arr[0]?.dataIndex ?? 0
          const row = list[idx] || {}
          return `${escapeHtml(row.date || '')}<br/>新增 ${Number(row.count || 0)} 条`
        },
      },
      xAxis: {
        type: 'category',
        data: list.map((x) => String(x?.date || '').slice(5)),
        boundaryGap: false,
        axisLine: { lineStyle: { color: p.grid } },
        axisTick: { show: false },
        axisLabel: { color: p.textFaint, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        axisLabel: { color: p.textFaint, fontSize: 11 },
        splitLine: { lineStyle: { color: p.grid } },
      },
      series: [
        {
          name: '每日新增',
          type: 'line',
          data: list.map((x) => Number(x?.count || 0)),
          smooth: true,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 2, color: p.accent },
          itemStyle: { color: p.accent },
          areaStyle: { color: p.accentSoft },
        },
      ],
    }),
  }
}
