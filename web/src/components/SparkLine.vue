<!--
  折线：近 7 天的**记忆条目增量**（2026-10 ks_todo 看板图表化）。

  数据源：`GET /api/v1/stats?days=7` 的 `daily` 段（PG 侧一条
  `GROUP BY 日期` 的 SQL，见 `api/routers/stats.py`）。

  🔴 按需引入 ECharts（core + LineChart + Grid + Tooltip + CanvasRenderer）。
  🔴 色值一律走 `utils/chart-theme.js`（本文件不写任何色值）。
  🔴 实例在 onUnmounted 里 dispose —— 切页不泄漏 canvas。
-->
<template>
  <div class="chart" :style="{ height: height + 'px' }" ref="host"></div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { createChartController, sparkOption } from '../utils/chart-kit.js'
import { chartPalette, onThemeChange } from '../utils/chart-theme.js'

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  /** `[{date, count}]` */
  points: { type: Array, default: () => [] },
  /** 图高（px） */
  height: { type: Number, default: 200 },
})

const host = ref(null)
let ctl = null
let stopTheme = null

function draw() {
  if (!ctl) return
  ctl.setOption(sparkOption(props.points, chartPalette()))
}

function onResize() {
  if (ctl) ctl.resize()
}

onMounted(() => {
  ctl = createChartController(host.value, echarts)
  draw()
  window.addEventListener('resize', onResize)
  stopTheme = onThemeChange(draw)
})

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  if (stopTheme) {
    stopTheme()
    stopTheme = null
  }
  if (ctl) {
    ctl.dispose()          // 🔴 图表实例不释放 = 切几次页就泄漏一份 canvas
    ctl = null
  }
})

watch(() => props.points, draw, { deep: true })
</script>

<style scoped>
.chart {
  width: 100%;
  min-width: 0;
}
</style>
