<template>
  <div class="stat panel">
    <div class="label">{{ label }}</div>
    <div class="value num" :class="tone ? `tone-${tone}` : ''">
      {{ value }}<span v-if="unit" class="unit">{{ unit }}</span>
    </div>
    <div v-if="percent !== null" class="bar" role="img" :aria-label="`${label} ${percent}%`">
      <i :style="{ width: clamped + '%' }" :class="tone || 'ok'"></i>
    </div>
    <div v-if="hint" class="hint">{{ hint }}</div>
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
  /** ok / warn / danger：只影响数字与进度条颜色 */
  tone: { type: String, default: '' },
})

/** 进度条永远画在 0~100，超预算的部分由 tone=danger 表达，不让条溢出 */
const clamped = computed(() => Math.min(100, Math.max(0, props.percent ?? 0)))
</script>

<style scoped>
.stat {
  padding: 14px 16px;
}
.label {
  color: var(--text-faint);
  font-size: 11.5px;
  letter-spacing: 0.3px;
}
.value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.15;
  letter-spacing: -0.4px;
}
.tone-ok {
  color: var(--ok);
}
.tone-warn {
  color: var(--warn);
}
.tone-danger {
  color: var(--danger);
}
.unit {
  margin-left: 4px;
  color: var(--text-faint);
  font-size: 12px;
  font-weight: 400;
}
.bar {
  height: 4px;
  margin-top: 10px;
  border-radius: 3px;
  background: var(--bg-hover);
  overflow: hidden;
}
.bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
}
.bar i.ok {
  background: var(--ok);
}
.bar i.warn {
  background: var(--warn);
}
.bar i.danger {
  background: var(--danger);
}
.hint {
  margin-top: 6px;
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.5;
}
</style>