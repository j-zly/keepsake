import { createRouter, createWebHashHistory } from 'vue-router'

import { getToken } from '../api'
import CoreView from '../views/CoreView.vue'
import FragmentsView from '../views/FragmentsView.vue'
import KbView from '../views/KbView.vue'
import StatsView from '../views/StatsView.vue'
import TokenView from '../views/TokenView.vue'

const routes = [
  { path: '/token', name: 'token', component: TokenView, meta: { public: true } },
  { path: '/', redirect: '/core' },
  { path: '/core', name: 'core', component: CoreView, meta: { title: '常驻层' } },
  { path: '/fragments', name: 'fragments', component: FragmentsView, meta: { title: '检索层' } },
  { path: '/kb', name: 'kb', component: KbView, meta: { title: '知识库' } },
  { path: '/stats', name: 'stats', component: StatsView, meta: { title: '看板' } },
]

// hash 模式：静态目录直挂根路径也能深链，不需要后端做 SPA 回退路由
const router = createRouter({ history: createWebHashHistory(), routes })

// 无 token 一律先过 token 页（401 由 axios 拦截器兜住，这里管首屏）
router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!getToken()) return { name: 'token', query: { redirect: to.fullPath } }
  return true
})

export default router
