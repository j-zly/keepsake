<!--
  列表卡片（常驻层 / 记忆条目 / 知识库命中 / 定时任务共用）。

  卡内固定顺序：顶行(左 tag chip + 右时间) → 标题/正文摘要 → 标签组 → 操作行。
  样式全在 tokens.css 的 .card* 原子类里，这里只负责结构与插槽顺序。
-->
<template>
  <article class="card">
    <div class="card-top">
      <span class="chip" :class="chipTone ? `chip--${chipTone}` : ''">
        <slot name="tag" />
      </span>
      <span v-if="$slots.time" class="card-time"><slot name="time" /></span>
    </div>

    <slot />

    <div v-if="$slots.tags" class="card-tags"><slot name="tags" /></div>

    <div class="card-foot">
      <button v-if="$slots.lead" class="card-link" @click="$emit('lead')">
        <slot name="lead" />
      </button>
      <span v-else></span>
      <div v-if="$slots.actions" class="card-acts"><slot name="actions" /></div>
    </div>
  </article>
</template>

<script setup>
defineProps({
  /** chip 底色：accent=蓝底（强调）、danger=红底（危险），不传就是中性灰 */
  chipTone: { type: String, default: '' },
})
defineEmits(['lead'])
</script>

<style scoped>
/* 窄卡（网格 3~5 列）下的操作行：按钮要么单行放得下，要么**整组换到下一行**，
   绝不逐字折成「升常/驻」那种竖排（tokens.css 的 .act-btn 没有 nowrap 兜底）。
   槽内容是父页编译的（带父页 scope id），scoped 选择器够不着，得 :deep。 */
.card-foot {
  /* 极端窄卡下连「查看全文」+按钮组都塞不进一行：整块换行，别把链接压没 */
  flex-wrap: wrap;
  row-gap: 6px;
}
.card-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  white-space: nowrap;
}
.card-acts {
  flex-wrap: wrap;
  justify-content: flex-end;
  row-gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
}
:deep(.act-btn) {
  white-space: nowrap;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
