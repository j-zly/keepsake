<template>
  <el-container v-if="showShell" class="layout">
    <el-aside width="180px" class="aside">
      <div class="brand">Keepsake 控制台</div>
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/core">常驻层</el-menu-item>
        <el-menu-item index="/fragments">检索层</el-menu-item>
        <el-menu-item index="/kb">知识库</el-menu-item>
        <el-menu-item index="/stats">看板</el-menu-item>
      </el-menu>
      <div class="foot">
        <el-button link size="small" @click="signOut">退出登录</el-button>
      </div>
    </el-aside>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
  <router-view v-else />
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, getToken, setToken } from './api'
import { showToast } from './utils/dialog'

const route = useRoute()
const router = useRouter()

// 登录 / 初始化 / token 三页不套外壳，否则套一个空侧边栏没意义
const BARE_PAGES = ['token', 'login', 'setup']
const showShell = computed(() => !BARE_PAGES.includes(route.name) && !!getToken())

/** 先让服务端把这个会话作废，再清本地 token —— 只清本地的话 token 还留在服务端文件里。 */
async function signOut() {
  try {
    await api.logout()
  } catch (e) {
    // 会话本来就过期/不存在时也要能退出去，失败不挡人
  }
  setToken('')
  showToast('已退出登录')
  router.push({ name: 'login' })
}
</script>

<style scoped>
.layout {
  height: 100vh;
}
.aside {
  border-right: 1px solid var(--el-border-color-light);
  display: flex;
  flex-direction: column;
}
.brand {
  font-weight: 600;
  padding: 16px;
  font-size: 15px;
}
.foot {
  margin-top: auto;
  padding: 12px;
}
</style>
