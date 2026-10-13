<template>
  <div class="wrap">
    <el-card class="card">
      <template #header>输入控制台 token</template>
      <p class="hint">
        token 来自后端环境变量 <code>CONSOLE_TOKEN</code> 或
        <code>/root/.hermes/secrets/keepsake_console.token</code>，只保存在本机浏览器
        localStorage，不会随请求 URL 传出。
      </p>
      <el-input
        v-model="token"
        type="password"
        show-password
        placeholder="Bearer token"
        @keyup.enter="save"
      />
      <div class="actions">
        <el-button type="primary" :loading="checking" @click="save">进入控制台</el-button>
        <el-button text @click="back">返回</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, setToken } from '../api'

const route = useRoute()
const router = useRouter()
const token = ref('')
const checking = ref(false)

/** 先试一把 /health + /api/v1/core/stats：401 说明 token 不对，别急着存。 */
async function save() {
  if (!token.value.trim()) {
    ElMessage.warning('请输入 token')
    return
  }
  checking.value = true
  setToken(token.value.trim())
  try {
    await api.coreStats()
    router.push(route.query.redirect || '/core')
  } catch (e) {
    setToken('')
    ElMessage.error(e.message || 'token 校验失败')
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
  padding-top: 12vh;
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
  margin-top: 16px;
  display: flex;
  gap: 8px;
}
</style>
