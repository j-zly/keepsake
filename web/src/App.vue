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
        <el-button link size="small" @click="signOut">退出（清 token）</el-button>
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
import { getToken, setToken } from './api'

const route = useRoute()
const router = useRouter()

// token 页（首屏无 token 时）不套外壳，否则套一个空侧边栏没意义
const showShell = computed(() => route.name !== 'token' && !!getToken())

function signOut() {
  setToken('')
  router.push({ name: 'token' })
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
