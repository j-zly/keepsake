<!--
  危险操作的确认弹框（归档 / 合并实跑 / 遗忘实跑）。

  三层防手滑，缺一不可：
  1) 标题左侧 3px --danger 竖条 —— 一眼看出这是不可逆方向的操作；
  2) 正文逐条列出后果，而不是一句「确定吗」；
  3) expect 非空时必须照抄确认串才能点亮主按钮（归档=key 末 8 位，实跑=当前计数）。

  closeOnClickModal: false —— 误点遮罩不该等于误改数据。
-->
<template>
  <el-dialog
    :model-value="modelValue"
    width="440px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="onDismiss"
  >
    <template #header>
      <div class="hd" :class="tone">
        <span>{{ title }}</span>
      </div>
    </template>

    <ul class="conseq">
      <li v-for="(c, i) in consequences" :key="i">{{ c }}</li>
    </ul>

    <div v-if="expect" class="ask">
      <!-- label 包住 el-input：它渲染的是真 <input>，包住即可正确关联（el-input 没有 inputId prop） -->
      <label class="ask-label">
        <span>键入 <code>{{ expect }}</code> 以确认</span>
        <el-input
          v-model="typed"
          :placeholder="expect"
          autocomplete="off"
          spellcheck="false"
          @keyup.enter="submit"
        />
      </label>
    </div>

    <template #footer>
      <el-button :disabled="busy" @click="close">取消</el-button>
      <el-button
        :type="tone === 'danger' ? 'danger' : 'primary'"
        :loading="busy"
        :disabled="!armed"
        @click="submit"
      >
        {{ confirmText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, required: true },
  /** 后果逐条列出，别合成一句话 */
  consequences: { type: Array, default: () => [] },
  /** 非空则要求照抄此串才能确认（key 末 8 位 / 当前计数） */
  expect: { type: String, default: '' },
  confirmText: { type: String, default: '确认执行' },
  /** danger：红色竖条 + 红色实心主按钮；primary：普通主按钮 */
  tone: { type: String, default: 'danger' },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const typed = ref('')
// 每次打开都清空上一次的输入，避免「上次打过就永远能点确认」
watch(
  () => props.modelValue,
  (open) => {
    if (open) typed.value = ''
  },
)

const armed = computed(() => !props.expect || typed.value.trim() === props.expect)

function submit() {
  if (!armed.value || props.busy) return
  emit('confirm', typed.value.trim())
}
function close() {
  emit('update:modelValue', false)
}
/** ESC / 点 X 传来的 false 也要拦住 busy 期间的状态变化 */
function onDismiss(v) {
  if (!v) close()
}
</script>

<style scoped>
.hd {
  margin-left: -14px;
  padding-left: 11px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  border-left: 3px solid var(--accent);
}
.hd.danger {
  border-left-color: var(--danger);
}
.conseq {
  margin: 0;
  padding-left: 18px;
  color: var(--text-dim);
  font-size: 12.5px;
  line-height: 1.75;
}
.conseq li + li {
  margin-top: 4px;
}
.ask {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.ask-label {
  display: block;
  cursor: text;
}
/* label 包住输入框，点文字也能聚焦 */
.ask-label > span {
  display: block;
  margin-bottom: 7px;
  color: var(--text-faint);
  font-size: 12px;
}
</style>