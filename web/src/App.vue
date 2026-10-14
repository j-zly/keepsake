<template>
  <div v-if="showShell" class="app">
    <!-- 左侧窄导航：图标 + 文字，选中态靠左侧 2px 高亮条 + 底色，不靠大色块 -->
    <nav class="nav">
      <RouterLink to="/core" class="brand">
        <span class="logo" aria-hidden="true"></span>
        <span class="brand-name">Keepsake</span>
      </RouterLink>

      <ul class="menu">
        <li v-for="m in MENU" :key="m.path">
          <RouterLink :to="m.path" class="item" :class="{ on: route.path === m.path }">
            <span class="ico" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor"
                stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" v-html="m.icon" />
            </span>
            <span class="txt">{{ m.label }}</span>
          </RouterLink>
        </li>
      </ul>

      <div class="nav-foot">
        <!-- 主题切换：纯内联 SVG（不引图标库），选择落在 keepsake.console.theme -->
        <button class="theme-btn" :aria-label="themeLabel" :title="themeLabel" @click="toggle">
          <span class="ico" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"
              :innerHTML="theme === 'dark' ? ICON.moon : ICON.sun" />
          </span>
          <span class="txt">{{ theme === 'dark' ? '浅色' : '深色' }}</span>
        </button>
        <button class="signout" @click="signOut">退出登录</button>
      </div>
    </nav>

    <!-- 顶栏：面包屑 + 右侧退出。整条 sticky，表格粘性表头正好贴在它下面 -->
    <header class="topbar">
      <div class="crumb">
        <span class="faint">控制台</span>
        <span class="slash">/</span>
        <b>{{ route.meta.title || '' }}</b>
      </div>
      <button class="signout" @click="signOut">退出</button>
    </header>

    <main class="main">
      <div class="inner">
        <router-view />
      </div>
    </main>
  </div>
  <router-view v-else />
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, getToken, setToken } from './api'
import { showToast } from './utils/dialog'
import { readTheme, toggleTheme } from './utils/theme'

const route = useRoute()
const router = useRouter()

// 图标内联成 SVG 源码：省掉一个图标依赖，也不用为了 4 个图标背一整个字体。
const ICON = {
  layers:
    '<path d="M8 1.9 1.9 5 8 8.1 14.1 5 8 1.9Z"/><path d="M2.7 8.1 8 10.8l5.3-2.7"/><path d="M2.7 11 8 13.7l5.3-2.7"/>',
  search: '<circle cx="7" cy="7" r="4.3"/><path d="m10.3 10.3 3.4 3.4"/>',
  book: '<path d="M2.6 2.9h3.6a2 2 0 0 1 2 2v8.3a1.7 1.7 0 0 0-1.7-1.7H2.6Z"/><path d="M13.4 2.9H9.8a2 2 0 0 0-2 2v8.3a1.7 1.7 0 0 1 1.7-1.7h3.9Z"/>',
  chart:
    '<path d="M2.4 13.6h11.2"/><path d="M4.1 13.6V8.9"/><path d="M8 13.6V4.6"/><path d="M11.9 13.6V6.9"/>',
  sun: '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.4v1.5M8 13.1v1.5M1.4 8h1.5M13.1 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1"/>',
  moon: '<path d="M13.4 9.5A5.6 5.6 0 0 1 6.5 2.6a5.9 5.9 0 1 0 6.9 6.9Z"/>',
}
const MENU = [
  { path: '/core', label: '常驻层', icon: ICON.layers },
  { path: '/fragments', label: '检索层', icon: ICON.search },
  { path: '/kb', label: '知识库', icon: ICON.book },
  { path: '/stats', label: '看板', icon: ICON.chart },
]

// 登录 / 初始化 / token 三页不套外壳，否则套一个空侧边栏没意义
const BARE_PAGES = ['token', 'login', 'setup']
const showShell = computed(() => !BARE_PAGES.includes(route.name) && !!getToken())

// 主题：首屏已由 main.js 写好 html，这里只需要一份响应式副本给按钮的图标/文案用
const theme = ref(readTheme())
const themeLabel = computed(() => (theme.value === 'dark' ? '切换到浅色' : '切换到深色'))
function toggle() {
  theme.value = toggleTheme()
}

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
.app {
  min-height: 100vh;
}

/* ---------------------------------------------------------------- 导航 --- */
.nav {
  position: fixed;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  width: var(--nav-w);
  padding: 0 10px;
  background: var(--bg);
  border-right: 1px solid var(--border);
}
.brand {
  display: flex;
  gap: 9px;
  align-items: center;
  height: var(--top-h);
  padding: 0 6px;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
  white-space: nowrap;
}
.logo {
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 5px;
  background: linear-gradient(140deg, var(--accent), color-mix(in srgb, var(--accent) 45%, var(--bg)));
}
.menu {
  flex: 1;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  overflow-y: auto;
}
.item {
  display: flex;
  gap: 9px;
  align-items: center;
  height: 32px;
  padding: 0 9px;
  margin-bottom: 2px;
  border-radius: var(--r-ctl);
  color: var(--text-dim);
  font-size: 13px;
  white-space: nowrap;
  transition: background 0.12s, color 0.12s;
}
.item:hover {
  background: var(--bg-hover);
  color: var(--text);
}
/* 选中态：左侧 2px 高亮条 + 底色，靠描边不靠大色块 */
.item.on {
  background: var(--bg-hover);
  color: var(--text);
  font-weight: 500;
  box-shadow: inset 2px 0 0 var(--accent);
}
.ico {
  display: inline-flex;
  flex: none;
}
.signout,
.theme-btn {
  display: flex;
  gap: 7px;
  align-items: center;
  justify-content: center;
  height: 26px;
  padding: 0 9px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-faint);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.signout:hover,
.theme-btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
  color: var(--text);
}
.nav-foot {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 6px 14px;
}

/* ---------------------------------------------------------------- 顶栏 --- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--top-h);
  margin-left: var(--nav-w);
  padding: 0 24px;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border);
}
.crumb {
  display: flex;
  gap: 7px;
  align-items: center;
  font-size: 12.5px;
  color: var(--text-dim);
}
.slash {
  color: var(--border-strong);
}
.crumb b {
  color: var(--text);
  font-weight: 500;
}

/* -------------------------------------------------------------- 内容区 --- */
.main {
  margin-left: var(--nav-w);
}
.inner {
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px;
}

/* < 900px 导航折叠成图标条；表格自身横向滚动，不让整页出现横向滚动条 */
@media (max-width: 900px) {
  .brand-name,
  .item .txt,
  .nav-foot .txt {
    display: none;
  }
  /* 窄条放不下「退出登录」，主题按钮留下图标就够了 */
  .nav-foot .signout {
    display: none;
  }
  .theme-btn {
    padding: 0;
  }
  .item {
    justify-content: center;
    padding: 0;
  }
  .brand {
    justify-content: center;
    padding: 0;
  }
  .topbar {
    padding: 0 14px;
  }
  .inner {
    padding: 16px 14px;
  }
}
</style>