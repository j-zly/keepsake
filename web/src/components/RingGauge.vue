<!--
  环形仪表：**总预算水位**与**待办完成率**两块（2026-10 ks_todo 看板图表化）。

  🔴 按需引入 ECharts（core + GaugeChart + Tooltip + CanvasRenderer）。
  🔴 色值一律走 `utils/chart-theme.js`（本文件不写任何色值）。
  🔴 实例在 onUnmounted 里 dispose —— 切页不泄漏 canvas。
-->
<template>
  <div class="chart" :style="{ height: height + 'px' }" ref="host"></div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { GaugeChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { createChartController, gaugeOption } from '../utils/chart-kit.js'
import { chartPalette, onThemeChange } from '../utils/chart-theme.js'

echarts.use([GaugeChart, TooltipComponent, CanvasRenderer])

const props = defineProps({
  /** `[{label, percent, color}]`（percent 0~100） */
  metrics: { type: Array, default: () => [] },
  /** 图高（px） */
  height: { type: Number, default: 220 },
})

const host = ref(null)
let ctl = null
let stopTheme = null

function draw() {
  if (!ctl) return
  ctl.setOption(gaugeOption(props.metrics, chartPalette()))
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

watch(() => props.metrics, draw, { deep: true })
</script>

<style scoped>
.chart {
  width: 100%;
  min-width: 0;
}
</style>
