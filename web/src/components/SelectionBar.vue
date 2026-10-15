<!--
  勾选批量工具条（记忆条目页 / 常驻层页共用同一份）。

  为什么是组件而不是各页写一遍：勾选口径（已选 N 条 / 全选本页 / 清空）
  必须两个页面**逐字一致** —— 一边「全选」只选可见、一边选全库，
  用户按同一个按钮却拿到不同的集合，批量删就会删错范围。

  选中集合由页面持有（`Set<key>`，跨页保留），本组件只负责呈现与发事件：
  勾选状态要从每张卡片回显，那是卡片所在页面的事，抽进组件反而多绕一层。

  全选本页只动**当前页**的 key：跨页累积由「逐页勾选」完成，
  「全选」语义模糊（是本页还是当前筛选全部？）时宁可字面一些。
-->
<template>
  <div class="selbar">
    <el-checkbox
      :model-value="allOn"
      :indeterminate="someOn"
      @change="onAll"
    >全选本页</el-checkbox>
    <span class="sel-count">已选 {{ count }} 条</span>
    <button class="soft-btn" :disabled="!count" @click="$emit('clear')">清空选择</button>
    <div class="sel-acts"><slot /></div>
    <p class="sel-tip">勾选跨页保留：翻页不清空，只有「清空选择」才清。</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  /** 已选条数（跨页累计，不只是本页） */
  count: { type: Number, default: 0 },
  /** 本页条数（全选的判定基准） */
  pageCount: { type: Number, default: 0 },
  /** 本页已选条数（半选态） */
  pageSelected: { type: Number, default: 0 },
})

const emit = defineEmits(['select-all', 'clear'])

const allOn = computed(() => props.pageCount > 0 && props.pageSelected === props.pageCount)
const someOn = computed(() => props.pageSelected > 0 && !allOn.value)

function onAll(v) {
  emit('select-all', !!v)
}
</script>

<style scoped>
.selbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin: 12px 0 4px;
  padding: 10px 14px;
  background: var(--bg-elev);
  border: 1px solid var(--border-2);
  border-radius: var(--r-sm);
}
/* 已选条数：与卡上的 key 同一份「等宽 + 弱色」配方，扫一眼就知道量级 */
.sel-count {
  font-size: 12.5px;
  color: var(--text-dim);
}
.sel-acts {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.sel-tip {
  flex-basis: 100%;
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}
</style>
