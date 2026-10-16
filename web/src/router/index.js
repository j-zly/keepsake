import { createRouter, createWebHashHistory } from 'vue-router'

import { api, getToken } from '../api'
import AgentsView from '../views/AgentsView.vue'
import CoreView from '../views/CoreView.vue'
import FragmentsView from '../views/FragmentsView.vue'
import KbView from '../views/KbView.vue'
import LoginView from '../views/LoginView.vue'
import MaintenanceView from '../views/MaintenanceView.vue'
import SetupView from '../views/SetupView.vue'
import StatsView from '../views/StatsView.vue'
import TokenView from '../views/TokenView.vue'

const routes = [
  // setup / login 是 P3 的正常入口；token 页保留给「用静态 token 的老脚本/临时排查」
  { path: '/setup', name: 'setup', component: SetupView, meta: { public: true } },
  { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
  { path: '/token', name: 'token', component: TokenView, meta: { public: true } },
  { path: '/', redirect: '/core' },
  { path: '/core', name: 'core', component: CoreView, meta: { title: '常驻层' } },
  { path: '/fragments', name: 'fragments', component: FragmentsView, meta: { title: '检索层' } },
  { path: '/maintenance', name: 'maintenance', component: MaintenanceView, meta: { title: '记忆维护' } },
  { path: '/kb', name: 'kb', component: KbView, meta: { title: '知识库' } },
  { path: '/agents', name: 'agents', component: AgentsView, meta: { title: '代理治理' } },
  { path: '/stats', name: 'stats', component: StatsView, meta: { title: '看板' } },
]

// hash 模式：静态目录直挂根路径也能深链，不需要后端做 SPA 回退路由
const router = createRouter({ history: createWebHashHistory(), routes })

/**
 * 首屏守卫：没有会话 token 就问后端「初始化过没有」——
 * 没初始化 → /setup（第一次用，自己设账号密码）；初始化过 → /login。
 *
 * 已初始化的人再点 /setup 会被弹回 /login（抢注窗口后端也关了 409，这里只是少一次报错）。
 */
router.beforeEach(async (to) => {
  if (to.meta.public) {
    if (getToken() && to.name === 'setup') return { name: 'login' }
    return true
  }
  if (getToken()) return true
  let initialized = false
  try {
    initialized = (await api.authStatus()).initialized
  } catch (e) {
    // 问不到就当没初始化过：/setup 页会把后端的错误显示出来，不静默卡死
    initialized = false
  }
  return { name: initialized ? 'login' : 'setup', query: { redirect: to.fullPath } }
})

export default router
