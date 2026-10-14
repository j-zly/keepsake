<template>
  <div class="wrap">
    <div class="card panel">
      <div class="head">
        <span class="logo" aria-hidden="true"></span>
        <h1>首次使用 · 设置管理员账号</h1>
        <p>
          这是控制台的第一个管理员账号，只有设置这一次。口令由后端做
          <strong>PBKDF2 加盐哈希</strong>后存盘 —— 后端也看不到你的明文口令。
        </p>
      </div>

      <div v-if="error" class="banner banner--danger" role="alert">{{ error }}</div>

      <el-form label-position="top" @submit.prevent>
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

      <el-button type="primary" class="full" :loading="loading" @click="submit">创建并进入</el-button>
      <button class="alt" @click="goLogin">已有账号，去登录 →</button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'
import { showToast } from '../utils/dialog'

const router = useRouter()
const username = ref('')
const password = ref('')
const password2 = ref('')
const loading = ref(false)
const error = ref('')

/** 口令两次不一致就别打后端了（少一次无谓的 400，也少一次失败计数）。 */
async function submit() {
  const name = username.value.trim()
  error.value = ''
  if (!name) {
    error.value = '请填写账号'
    return
  }
  if (password.value.length < 8) {
    error.value = '口令至少 8 位'
    return
  }
  if (password.value !== password2.value) {
    error.value = '两次输入的口令不一致'
    return
  }
  loading.value = true
  try {
    // setup 成功即自动登录：后端把会话 token 一并给了，前端直接存
    const body = await api.setup(name, password.value)
    setToken(body.token)
    password.value = password2.value = ''
    showToast('管理员账号已创建')
    router.push('/core')
  } catch (e) {
    error.value = e.message || '创建失败'
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
  padding: 10vh 16px 0;
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
.full {
  width: 100%;
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