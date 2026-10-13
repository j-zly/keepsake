<template>
  <div class="wrap">
    <el-card class="card">
      <template #header>Keepsake 控制台 · 登录</template>
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="账号">
          <el-input
            v-model="username"
            placeholder="管理员账号"
            autocomplete="username"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="口令">
          <el-input
            v-model="password"
            type="password"
            show-password
            placeholder="登录口令"
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="remember">记住我 30 天</el-checkbox>
        </el-form-item>
      </el-form>
      <div class="actions">
        <el-button type="primary" :loading="loading" @click="submit">登录</el-button>
        <el-button text @click="goToken">用静态 token 进入</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, setToken } from '../api'
import { showError, showToast } from '../utils/dialog'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const remember = ref(true)
const loading = ref(false)

/** 登录：会话 token 存 localStorage（记住我 ⇒ 30 天，否则 12 小时）。 */
async function submit() {
  const name = username.value.trim()
  if (!name || !password.value) return showError('请填写账号与口令')
  loading.value = true
  try {
    const body = await api.login(name, password.value, remember.value)
    setToken(body.token)
    password.value = ''
    showToast('登录成功')
    router.push(route.query.redirect || '/core')
  } catch (e) {
    // 401 文案统一（「账号或口令不正确」），这里原样透出即可
    showError(e.message || '登录失败')
  } finally {
    loading.value = false
  }
}

function goToken() {
  router.push('/token')
}
</script>

<style scoped>
.wrap {
  display: flex;
  justify-content: center;
  padding-top: 12vh;
}
.card {
  width: 420px;
}
.actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
</style>
