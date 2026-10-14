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
