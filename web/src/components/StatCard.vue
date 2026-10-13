<template>
  <el-card shadow="never" class="stat">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}<span v-if="unit" class="unit">{{ unit }}</span></div>
    <div v-if="hint" class="hint">{{ hint }}</div>
  </el-card>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '-' },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
})
</script>

<style scoped>
.stat {
  text-align: center;
}
.label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.value {
  font-size: 26px;
  font-weight: 600;
  margin-top: 6px;
}
.unit {
  font-size: 13px;
  font-weight: 400;
  margin-left: 4px;
  color: var(--el-text-color-secondary);
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
</style>
