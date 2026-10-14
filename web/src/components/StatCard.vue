<!--
  统计大卡：数字 32px/700 + 标签 13px 灰 + 可选 4px 占比条。
  样式在 tokens.css 的 .stat* 原子类里；这里只管取值与配色语义。
-->
<template>
  <div class="stat">
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value num" :class="tone ? `tone-${tone}` : ''">
      {{ value }}<span v-if="unit" class="unit">{{ unit }}</span>
    </div>
    <div v-if="percent !== null" class="stat-bar" role="img" :aria-label="`${label} ${percent}%`">
      <i :style="{ width: clamped + '%' }" :class="tone || 'ok'"></i>
    </div>
    <div v-if="hint" class="stat-hint">{{ hint }}</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '-' },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
  /** 传了就画一条细进度条（预算占用这类）；不传就不画 */
  percent: { type: Number, default: null },
  /** ok / accent / danger：只影响数字与进度条颜色 */
  tone: { type: String, default: '' },
})

/** 进度条永远画在 0~100，超预算的部分由 tone=danger 表达，不让条溢出 */
const clamped = computed(() => Math.min(100, Math.max(0, props.percent ?? 0)))
</script>
