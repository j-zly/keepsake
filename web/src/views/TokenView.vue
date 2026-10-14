<template>
  <div class="wrap">
    <div class="card panel">
      <div class="head">
        <span class="logo" aria-hidden="true"></span>
        <h1>用静态 token 进入</h1>
        <p>
          token 来自后端环境变量 <code>CONSOLE_TOKEN</code> 或
          <code>/root/.hermes/secrets/keepsake_console.token</code>，只保存在本机浏览器
          localStorage，不会随请求 URL 传出。
        </p>
      </div>

      <div v-if="error" class="banner banner--danger" role="alert">{{ error }}</div>

      <el-input
        v-model="token"
        type="password"
        show-password
        placeholder="Bearer token"
        @keyup.enter="save"
      />

      <el-button type="primary" class="full" :loading="checking" @click="save">进入控制台</el-button>
      <button class="alt" @click="back">← 返回</button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, setToken } from '../api'

const route = useRoute()
const router = useRouter()
const token = ref('')
const checking = ref(false)
const error = ref('')

/** 先试一把 /health + /api/v1/core/stats：401 说明 token 不对，别急着存。 */
async function save() {
  error.value = ''
  if (!token.value.trim()) {
    error.value = '请输入 token'
    return
  }
  checking.value = true
  setToken(token.value.trim())
  try {
    await api.coreStats()
    router.push(route.query.redirect || '/core')
  } catch (e) {
    setToken('')
    error.value = e.message || 'token 校验失败'
  } finally {
    checking.value = false
  }
}

function back() {
  router.push('/core')
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
.full {
  width: 100%;
  margin-top: 14px;
}
.alt {
  display: block;
  width: 100%;
  margin-top: 10px;
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