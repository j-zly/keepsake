<template>
  <div class="wrap">
    <div class="card panel">
      <div class="head">
        <span class="logo" aria-hidden="true"></span>
        <h1>登录 Keepsake 控制台</h1>
        <p>用管理员账号进入。口令和会话 token 只留在本机，不会出现在 URL 里。</p>
      </div>

      <div v-if="error" class="banner banner--danger" role="alert">{{ error }}</div>

      <el-form label-position="top" @submit.prevent>
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
        <el-checkbox v-model="remember">记住我 30 天（不勾则 12 小时过期）</el-checkbox>
      </el-form>

      <el-button type="primary" class="full" :loading="loading" @click="submit">登录</el-button>
      <button class="alt" @click="goToken">用静态 token 进入 →</button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, setToken } from '../api'
import { showToast } from '../utils/dialog'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const remember = ref(true)
const loading = ref(false)
const error = ref('')

/** 登录：会话 token 存 localStorage（记住我 ⇒ 30 天，否则 12 小时）。 */
async function submit() {
  const name = username.value.trim()
  if (!name || !password.value) {
    error.value = '请填写账号与口令'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const body = await api.login(name, password.value, remember.value)
    setToken(body.token)
    password.value = ''
    showToast('登录成功')
    router.push(route.query.redirect || '/core')
  } catch (e) {
    // 401 文案统一（「账号或口令不正确」），这里原样透出即可 —— 行内提示，不弹窗
    error.value = e.message || '登录失败'
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
  padding: 12vh 16px 0;
}
.card {
  width: 400px;
  padding: 26px 24px 22px;
}
.head {
  margin-bottom: 20px;
  text-align: center;
}
.logo {
  display: inline-block;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: linear-gradient(140deg, var(--accent), color-mix(in srgb, var(--accent) 45%, var(--bg)));
}
.head h1 {
  margin: 12px 0 6px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.head p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.banner {
  text-align: left;
}
.full {
  width: 100%;
  margin-top: 4px;
}
.alt {
  display: block;
  width: 100%;
  margin-top: 12px;
  padding: 6px;
  background: transparent;
  border: 0;
  color: var(--text-faint);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.alt:hover {
  color: var(--accent);
}
</style>