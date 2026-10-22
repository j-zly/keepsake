<!--
  横向条形图：**每代理的常驻层占用 vs 各自预算**（2026-10 ks_todo 看板图表化）。

  🔴 **按需引入 ECharts**（`echarts/core` + BarChart + Grid + Tooltip + CanvasRenderer）
    —— 不许整包 import：整包是 1MB 级的 gzip 增量，按需引入才是几十 KB。

  🔴 **色值一律走 `utils/chart-theme.js`**（本文件不写任何色值）。

  🔴 **窄屏不许横向滚动**：容器宽 100%，ECharts 靠 ResizeObserver 自适应。

  本组件只做「壳」：init / setOption / resize / dispose 四步；
  option 的形状在 `utils/chart-kit.js`（纯函数，可测）。
-->
<template>
  <div class="chart" :style="{ height: height + 'px' }" ref="host"></div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { barsOption, createChartController } from '../utils/chart-kit.js'
import { chartPalette, onThemeChange } from '../utils/chart-theme.js'

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  /** `agentUsageRows` 的行：{agent_id, label, chars, budget, percent} */
  rows: { type: Array, default: () => [] },
  /** 图高（px） */
  height: { type: Number, default: 260 },
})

const host = ref(null)
let ctl = null
let stopTheme = null

function draw() {
  if (!ctl) return
  ctl.setOption(barsOption(props.rows, chartPalette()))
}

function onResize() {
  if (ctl) ctl.resize()
}

onMounted(() => {
  ctl = createChartController(host.value, echarts)
  draw()
  window.addEventListener('resize', onResize)
  // 主题切换 ⇒ 重画一次（画布上的颜色是死的，跟不上主题就是一块浅底浅字）
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

watch(() => props.rows, draw, { deep: true })
</script>

<style scoped>
.chart {
  width: 100%;               /* 窄屏铺满，不撑出横向滚动 */
  min-width: 0;
}
</style>
