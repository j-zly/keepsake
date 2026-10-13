<template>
  <div class="wrap">
    <el-card class="card">
      <template #header>首次使用 · 设置管理员账号</template>
      <p class="hint">
        这是控制台的第一个管理员账号，只有设置这一次。口令由后端做
        <strong>PBKDF2 加盐哈希</strong>后存盘 —— 后端也看不到你的明文口令。
      </p>
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="账号">
          <el-input v-model="username" placeholder="例如 admin" autocomplete="username" />
        </el-form-item>
        <el-form-item label="口令">
          <el-input
            v-model="password"
            type="password"
            show-password
            placeholder="至少 8 位"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="确认口令">
          <el-input
            v-model="password2"
            type="password"
            show-password
            placeholder="再输一遍"
            autocomplete="new-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="submit">创建并进入</el-button>
        <el-button text @click="goLogin">已有账号，去登录</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'
import { showError, showToast } from '../utils/dialog'

const router = useRouter()
const username = ref('')
const password = ref('')
const password2 = ref('')
const loading = ref(false)

/** 口令两次不一致就别打后端了（少一次无谓的 400，也少一次失败计数）。 */
async function submit() {
  const name = username.value.trim()
  if (!name) return showError('请填写账号')
  if (password.value.length < 8) return showError('口令至少 8 位')
  if (password.value !== password2.value) return showError('两次输入的口令不一致')
  loading.value = true
  try {
    // setup 成功即自动登录：后端把会话 token 一并给了，前端直接存
    const body = await api.setup(name, password.value)
    setToken(body.token)
    password.value = password2.value = ''
    showToast('管理员账号已创建')
    router.push('/core')
  } catch (e) {
    showError(e.message || '创建失败')
  } finally {
    loading.value = false
  }
}

function goLogin() {
  router.push('/login')
}
</script>

<style scoped>
.wrap {
  display: flex;
  justify-content: center;
  padding-top: 10vh;
}
.card {
  width: 460px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
</style>
