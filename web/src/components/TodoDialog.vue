<!--
  新增待办弹框（待办页专用 · 2026-10-22 用户点名整改）。

  为什么从「页面顶部的裸表单」搬进弹框：
  裸表单把「一行输入框 + 下拉 + 按钮 + 提示长句」摊在页面最上面，抢掉整块首屏，
  筛选区与新增区也混成一团（截图里那块「加一条」占了一整屏）。
  现在页头只留**一个**主按钮「＋ 新待办」，表单收进弹框，
  那句归属/到期日的提示长句也跟着进弹框 —— 不再占页面主区。

  复用仓库现成模式（对齐 CredentialDialog / 代理编辑弹框）：
  宽 480px、align-center、close-on-click-modal=false、footer 取消 + 主按钮。
  空标题前端先拦（后端那道 confirm 闸照旧发，两道都不省）。
-->
<template>
  <el-dialog
    :model-value="modelValue"
    title="新待办"
    width="480px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="onDismiss"
  >
    <el-form label-position="top">
      <el-form-item label="标题（必填）">
        <el-input
          v-model="form.title"
          placeholder="要做什么（≤200 字）"
          maxlength="200"
          autocomplete="off"
        />
      </el-form-item>
      <el-form-item label="详情（可空）">
        <el-input
          v-model="form.detail"
          type="textarea"
          :rows="3"
          maxlength="4000"
          placeholder="正文（可空）"
        />
      </el-form-item>
      <el-form-item label="归属代理（留空 = 主代理）">
        <el-select v-model="form.owner" class="sel" placeholder="归属代理" filterable>
          <el-option
            v-for="a in agents"
            :key="a.agent_id"
            :label="a.name ? `${a.agent_id}（${a.name}）` : a.agent_id"
            :value="a.agent_id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="到期日（可空）">
        <el-date-picker
          v-model="form.due"
          class="sel"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="到期日（可空）"
        />
      </el-form-item>
    </el-form>
    <p class="tip">
      归属必须在册且已批准（留空 = 主代理）；到期日填 ISO 日期（YYYY-MM-DD），留空表示不限期。
    </p>
    <template #footer>
      <el-button :disabled="busy" @click="close">取消</el-button>
      <el-button type="primary" :loading="busy" :disabled="!form.title.trim()" @click="submit">
        添加
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, watch } from 'vue'
import { showError } from '../utils/dialog'
import { blankForm, todoFormError, todoPayload } from '../utils/todoForm'

const props = defineProps({
  /** 弹框开合（父页 v-model 绑自己那个 createOpen） */
  modelValue: { type: Boolean, default: false },
  /** 归属下拉的候选：注册表里**已批准**的代理（父页过滤好传进来） */
  agents: { type: Array, default: () => [] },
  /** 父页正在写库：禁掉关闭与重复提交 */
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'submit'])

/** 表单状态（真值只活在这个 ref 里；关框即弃，下次打开重置）。 */
const form = reactive(blankForm())

// 每次打开重置：标题 / 详情 / 到期日清空，**归属保留上一次选的**
// （连着给同一个代理派活时不用条条重选下拉）。
watch(
  () => props.modelValue,
  (open) => {
    if (open) Object.assign(form, blankForm(form.owner))
  },
)

function close() {
  emit('update:modelValue', false)
}

/** ESC / 点 X 传来的 false 也要走同一条路（busy 期间父页那边禁了关窗键）。 */
function onDismiss(v) {
  if (!v) close()
}

/** 提交：空标题**前端先拦**（不发请求），其余交给父页去打后端。 */
function submit() {
  const err = todoFormError(form)
  if (err) {
    showError(err)
    return
  }
  emit('submit', todoPayload(form))
}
</script>

<style scoped>
/* 弹框里的下拉 / 日期：占满弹框宽（筛选区那个 .sel 是 180px 固定宽，这里不一样） */
.sel {
  width: 100%;
}
/* 表单下面那行小字：归属与到期日的口径（随表单一起走，不占页面主区） */
.tip {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 弹框里的按钮不许折行（窄弹框里「添加」竖排成两行就变形了） */
:deep(.el-button) {
  white-space: nowrap;
}
</style>
