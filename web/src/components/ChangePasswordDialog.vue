<template>
  <el-dialog
    v-model="open"
    title="修改密码"
    width="440px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @closed="forget"
  >
    <p class="tip">改完之后，除本设备外的其它会话都会失效，需要重新登录。</p>

    <div v-if="error" class="banner banner--danger" role="alert">{{ error }}</div>

    <el-form label-position="top" @submit.prevent>
      <el-form-item label="旧密码">
        <el-input
          v-model="oldPassword"
          type="password"
          show-password
          autocomplete="current-password"
          placeholder="当前正在用的密码"
          @keyup.enter="submit"
        />
      </el-form-item>
      <el-form-item label="新密码">
        <el-input
          v-model="newPassword"
          type="password"
          show-password
          autocomplete="new-password"
          :placeholder="`至少 ${MIN_LEN} 位`"
          @keyup.enter="submit"
        />
      </el-form-item>
      <el-form-item label="确认新密码">
        <el-input
          v-model="confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="再输一遍新密码"
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button :disabled="busy" @click="open = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">确认修改</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 改密码弹框（侧栏底部「修改密码」入口）。
 *
 * 🔴 口令只在组件内存里，**绝不进 localStorage / URL / console**：
 * 三个 ref 一关弹框就清零（`forget`），输入框一律 type=password，
 * 口令走 POST body（见 api/index.js 的 changePassword）。
 *
 * 前端先校「两次新密码一致」，后端仍是权威 —— 前端校一遍只是为了少发一次
 * 请求，不是为了替代后端的旧口令校验与强度校验。
 */
import { computed, ref } from 'vue'
import { api } from '../api'
import { showToast } from '../utils/dialog'

/** 与后端 `auth.MIN_PASSWORD_LEN` 同口径（后端仍是权威，这里只是提前拦一手） */
const MIN_LEN = 8

const props = defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue'])

const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const error = ref('')
const busy = ref(false)

/** 关掉弹框就把三个口令从内存里抹掉（下次打开是空白，不留残值）。 */
function forget() {
  oldPassword.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
  error.value = ''
}

async function submit() {
  if (busy.value) return
  if (!oldPassword.value || !newPassword.value || !confirmPassword.value) {
    error.value = '请填写三个密码框'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致'
    return
  }
  if (newPassword.value.length < MIN_LEN) {
    error.value = `新密码至少 ${MIN_LEN} 位`
    return
  }
  busy.value = true
  error.value = ''
  try {
    await api.changePassword(oldPassword.value, newPassword.value)
    open.value = false
    // forget 走 @closed（关闭动画后）；这里再抹一次，确保发请求用的那两份也清掉
    forget()
    showToast('密码已修改，其他设备需重新登录')
  } catch (e) {
    // 403 = 旧密码不对；400 = 新密码太弱；文案都用后端给的，不在前端编一套
    error.value = e.message || '修改失败'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.tip {
  margin: 0 0 12px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.banner {
  text-align: left;
}
</style>
