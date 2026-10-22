import { createRouter, createWebHashHistory } from 'vue-router'

import { api, getToken } from '../api'
import AgentsReviewView from '../views/AgentsReviewView.vue'
import AgentsView from '../views/AgentsView.vue'
import CoreView from '../views/CoreView.vue'
import FragmentsView from '../views/FragmentsView.vue'
import KbReviewView from '../views/KbReviewView.vue'
import KbView from '../views/KbView.vue'
import LoginView from '../views/LoginView.vue'
import MaintenanceView from '../views/MaintenanceView.vue'
import MessagesView from '../views/MessagesView.vue'
import ReviewView from '../views/ReviewView.vue'
import SetupView from '../views/SetupView.vue'
import StatsView from '../views/StatsView.vue'
import TokenView from '../views/TokenView.vue'
import TodosView from '../views/TodosView.vue'

const routes = [
  // setup / login 是 P3 的正常入口；token 页保留给「用静态 token 的老脚本/临时排查」
  { path: '/setup', name: 'setup', component: SetupView, meta: { public: true } },
  { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
  { path: '/token', name: 'token', component: TokenView, meta: { public: true } },
  { path: '/', redirect: '/core' },
  { path: '/core', name: 'core', component: CoreView, meta: { title: '常驻层' } },
  { path: '/fragments', name: 'fragments', component: FragmentsView, meta: { title: '检索层' } },
  // 审核独立成页（原来挤在记忆维护页里）：候选 70+ 条时整页很长，动线被危险操作淹没
  { path: '/review', name: 'review', component: ReviewView, meta: { title: '记忆审核' } },
  { path: '/maintenance', name: 'maintenance', component: MaintenanceView, meta: { title: '记忆维护' } },
  { path: '/kb', name: 'kb', component: KbView, meta: { title: '知识库' } },
  // 知识库审核独立成页（原来挤在 KbView 里、要点顶部那颗 chip 才展开）：待审队列要逐条判，
  // 铺在文档浏览页的抽屉里既看不清也翻不动。配方与 /review 记忆审核同款
  { path: '/kb-review', name: 'kb-review', component: KbReviewView, meta: { title: '知识库审核' } },
  { path: '/agents', name: 'agents', component: AgentsView, meta: { title: '代理登记' } },
  // 消息中心独立成页：代理之间的往来消息全量可查（用户铁律：绝不被背着开小会）
  { path: '/messages', name: 'messages', component: MessagesView, meta: { title: '消息中心' } },
  // 代理审核独立成页（原来挤在 AgentsView 里）：待审队列要逐条判，
  // 与已批准代理的日常维护（登记/改名/设角色）分开摆
  { path: '/agents-review', name: 'agents-review', component: AgentsReviewView, meta: { title: '代理审核' } },
  // 待办（2026-10 ks_todo 一等公民）：独立成页 —— 待办有状态机与到期日，
  // 挤在别页里既看不清也翻不动；侧栏位置在「审核」之前（业务页在前、审核收尾）。
  { path: '/todos', name: 'todos', component: TodosView, meta: { title: '待办' } },
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
