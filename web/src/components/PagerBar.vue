<!--
  分页条：左「共 N 条 + 每页条数」，右「页码窗口 + 跳至 __ 页」，紧凑一排。

  不用 el-pagination —— 它把 total/sizes/pager 摊成三块、留白很大，
  这里自己排一行。样式是 tokens.css 里的全局 .pager 原子类，本组件只出结构。
  页码窗口由 utils/format.js 的 pageWindow 给，最多 5 个数字 + 2 个省略号。
  每页 20/50/100 与「共 N 条」的口径都来自接口，这里不另算。
-->
<template>
  <div class="pager">
    <div class="left">
      <span class="num">{{ totalText }}</span>
      <span class="dot">·</span>
      <label class="size">
        每页
        <select class="num" :value="pageSize" @change="onSize($event.target.value)">
          <option v-for="s in SIZES" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
    </div>

    <div class="right">
      <button class="pg" :disabled="page <= 1" @click="go(page - 1)">上一页</button>
      <template v-for="(p, i) in windows" :key="`${p}-${i}`">
        <span v-if="p === '…'" class="pg gap">…</span>
        <button v-else class="pg num" :class="{ on: p === page }" @click="go(p)">{{ p }}</button>
      </template>
      <button class="pg" :disabled="page >= pageCount" @click="go(page + 1)">下一页</button>
      <label class="jump">
        跳至
        <input
          v-model="jump"
          class="num"
          type="number"
          min="1"
          :max="pageCount"
          placeholder="1"
          @keyup.enter="doJump"
        />
        页
      </label>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { pageWindow } from '../utils/format'

const SIZES = [20, 50, 100]

const props = defineProps({
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  /** 全库浏览：存储层没有 count 原语，总条数未知，措辞改成「至少 N 条」 */
  unknown: { type: Boolean, default: false },
})

const emit = defineEmits(['update:page', 'update:pageSize'])

const pageCount = computed(() => Math.max(1, Math.ceil((props.total || 0) / props.pageSize)))
const windows = computed(() => pageWindow(props.page, pageCount.value))
const totalText = computed(() =>
  props.unknown ? `至少 ${props.total} 条（总数未知）` : `共 ${props.total} 条`,
)

const jump = ref('')
// 翻页后清空跳页输入框，否则会一直停在一个已经没意义的页码上
watch(() => props.page, () => (jump.value = ''))

function go(p) {
  const n = Math.min(Math.max(1, p), pageCount.value)
  if (n !== props.page) emit('update:page', n)
}
function doJump() {
  const n = Number.parseInt(jump.value, 10)
  if (Number.isFinite(n)) go(n)
}
function onSize(v) {
  emit('update:pageSize', Number(v))
}
</script>
