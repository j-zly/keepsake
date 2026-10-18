<template>
  <div v-if="showShell" class="app">
    <!-- 左侧导航：独立圆角浅灰面板，不贴边；选中态是整行蓝胶囊 -->
    <nav class="nav">
      <div class="nav-top">
        <span class="logo" aria-hidden="true"></span>
        <span class="nav-top-txt">
          <span class="ws">Keepsake</span>
          <span class="ws-sub"><i class="dot-ok" aria-hidden="true"></i>本地存储</span>
        </span>
      </div>

      <div class="nav-scroll">
        <!-- 分组标题：纯分组说明，不可点击 -->
        <div v-for="g in GROUPS" :key="g.title" class="nav-group">
          <p class="nav-group-title">{{ g.title }}</p>
          <RouterLink
            v-for="m in g.items"
            :key="m.path"
            :to="m.path"
            class="item"
            :class="{ on: route.path === m.path }"
          >
            <span class="ico" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="m.icon" />
            </span>
            <span class="txt">{{ m.label }}</span>
            <!-- 待审徽标：>0 才显示；数字复用 reviewCounts 全库口径。
                 审核项=晋升候选+退居建议之和；知识库项=kb_pending。
                 样式零新色：底色/文字全走既有 token（与 .chip--danger 同款）。 -->
            <span v-if="badgeFor(m.path)" class="item-badge"
              :aria-label="`待审 ${badgeFor(m.path)}`">{{ badgeFor(m.path) }}</span>
          </RouterLink>
        </div>
      </div>

      <div class="nav-foot">
        <!-- 明暗切换：分段控件，选中项白底 -->
        <div class="seg" role="group" aria-label="主题">
          <button :class="{ on: theme === 'light' }" :aria-pressed="theme === 'light'" title="浅色"
            @click="pick('light')">
            <span class="ico" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="ICON.sun" />
            </span>
            <span class="txt">浅色</span>
          </button>
          <button :class="{ on: theme === 'dark' }" :aria-pressed="theme === 'dark'" title="深色"
            @click="pick('dark')">
            <span class="ico" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="ICON.moon" />
            </span>
            <span class="txt">深色</span>
          </button>
        </div>

        <!-- 改密码入口：放在 nav-foot（不进 GROUPS —— 它不是页面，是个动作） -->
        <button class="foot-btn" @click="pwdOpen = true">
          <span class="ico" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"
              stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="ICON.key" />
          </span>
          <span class="txt">修改密码</span>
        </button>

        <div class="brand-card">
          <span class="ico" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"
              stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="ICON.shield" />
          </span>
          <span class="txt">记忆只留在本机，不出网</span>
        </div>
      </div>
    </nav>

    <main class="main">
      <!-- 面包屑：分组 › 当前页（分组标题与侧栏一致） -->
      <div class="crumb-row">
        <div class="crumb">
          <span>{{ crumb.group }}</span>
          <span class="sep">›</span>
          <b>{{ crumb.item }}</b>
        </div>
        <div class="crumb-acts">
          <span class="status-note">
            <i class="dot-ok" aria-hidden="true"></i>{{ theme === 'dark' ? '深色' : '浅色' }}主题
          </span>
          <!-- 36px 圆形描边图标按钮：退出登录 -->
          <button class="icon-btn" title="退出登录" aria-label="退出登录" @click="signOut">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"
              stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="ICON.logout" />
          </button>
        </div>
      </div>
      <router-view />
    </main>

    <!-- 登出确认：红色竖条 + 逐条后果，红色实心按钮（沿用统一确认组件，不新造弹框） -->
    <ConfirmDialog
      v-model="confirmSignOut"
      title="退出登录？"
      :consequences="signOutConsequences"
      confirm-text="退出"
      tone="danger"
      :busy="signingOut"
      @confirm="onSignOutConfirm"
    />

    <!-- 改密码：旧 / 新 / 确认三字段，零新色（走全局 token 样式） -->
    <ChangePasswordDialog v-model="pwdOpen" />
  </div>
  <router-view v-else />
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ConfirmDialog from './components/ConfirmDialog.vue'
import ChangePasswordDialog from './components/ChangePasswordDialog.vue'
import { api, getToken, setToken } from './api'
import { showToast } from './utils/dialog'
import { readTheme, setTheme } from './utils/theme'

const route = useRoute()
const router = useRouter()

// 图标内联成 SVG 源码：省掉一个图标依赖，也不用为了几个图标背一整个字体。
const ICON = {
  layers:
    '<path d="M8 1.9 1.9 5 8 8.1 14.1 5 8 1.9Z"/><path d="M2.7 8.1 8 10.8l5.3-2.7"/><path d="M2.7 11 8 13.7l5.3-2.7"/>',
  search: '<circle cx="7" cy="7" r="4.3"/><path d="m10.3 10.3 3.4 3.4"/>',
  book: '<path d="M2.6 2.9h3.6a2 2 0 0 1 2 2v8.3a1.7 1.7 0 0 0-1.7-1.7H2.6Z"/><path d="M13.4 2.9H9.8a2 2 0 0 0-2 2v8.3a1.7 1.7 0 0 1 1.7-1.7h3.9Z"/>',
  chart:
    '<path d="M2.4 13.6h11.2"/><path d="M4.1 13.6V8.9"/><path d="M8 13.6V4.6"/><path d="M11.9 13.6V6.9"/>',
  sun: '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.4v1.5M8 13.1v1.5M1.4 8h1.5M13.1 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1"/>',
  moon: '<path d="M13.4 9.5A5.6 5.6 0 0 1 6.5 2.6a5.9 5.9 0 1 0 6.9 6.9Z"/>',
  shield: '<path d="M8 1.9 2.9 3.9v3.7c0 3 2.1 5.4 5.1 6.5 3-1.1 5.1-3.5 5.1-6.5V3.9Z"/><path d="m6.1 7.9 1.4 1.4 2.6-2.7"/>',
  broom:
    '<path d="M10.4 2.9 13.1 5.6 8.6 10.1 5.9 7.4Z"/><path d="m6.7 8.2-3.4 3.4 2.4 2.4 3.4-3.4"/><path d="M4.6 13.4h6.8"/>',
  users:
    '<circle cx="6.1" cy="5.3" r="2.5"/><path d="M2.1 13.4a4.1 4.1 0 0 1 8 0"/><path d="M10.7 3.3a2.5 2.5 0 0 1 0 4.9"/><path d="M11.7 9.7a4.1 4.1 0 0 1 2.2 3.4"/>',
  check:
    '<path d="M8 1.9 14.3 4.6v3.9c0 3.2-2.5 6.1-6.3 7.4-3.8-1.3-6.3-4.2-6.3-7.4V4.6Z"/><path d="m5.5 8.3 1.8 1.8 3.4-3.6"/>',
  logout:
    '<path d="M6.4 2.9H3.6a1 1 0 0 0-1 1v8.2a1 1 0 0 0 1 1h2.8"/><path d="M10.2 5.5 13.1 8l-2.9 2.5"/><path d="M13.1 8H6.6"/>',
  key:
    '<circle cx="5.4" cy="10.6" r="2.9"/><path d="M7.6 8.4 13.4 2.6"/><path d="M11.2 4.8 12.6 6.2"/><path d="M9.8 6.2 11.2 7.6"/>',
}

// 侧栏分组：总览（看板）· 记忆（审核 / 常驻层 / 记忆条目 / 记忆维护 / 代理治理）· 知识库（知识库）
// 看板是总览入口，业界惯例 Overview 置顶，所以它单独成组排第一。
// 面包屑也从这份结构取，所以两处永远说同一句话（看板页：总览 › 看板）。
// 审核排记忆组第一：它是「有东西要处理」的高频动作，不该埋在分组深处（候选量一多
// 记忆维护页太长，审核入口也被合并/遗忘这些危险操作淹没）。
const GROUPS = [
  {
    title: '总览',
    items: [{ path: '/stats', label: '看板', icon: ICON.chart }],
  },
  {
    title: '记忆',
    items: [
      { path: '/review', label: '审核', icon: ICON.check },
      { path: '/core', label: '常驻层', icon: ICON.layers },
      { path: '/fragments', label: '记忆条目', icon: ICON.search },
      { path: '/maintenance', label: '记忆维护', icon: ICON.broom },
      { path: '/agents', label: '代理治理', icon: ICON.users },
    ],
  },
  {
    title: '知识库',
    items: [{ path: '/kb', label: '知识库', icon: ICON.book }],
  },
]

const crumb = computed(() => {
  for (const g of GROUPS) {
    const hit = g.items.find((m) => m.path === route.path)
    if (hit) return { group: g.title, item: hit.label }
  }
  return { group: '控制台', item: route.meta.title || '' }
})
// ===== 待审数（侧栏徽标）===========================================
// 数据源：`GET /api/v1/review/counts`（侧栏常驻，不随路由改而重拉）。
// - 审核项徽标 = promote + demote 合计（晋升候选 + 退居建议均在 ReviewView
//   「记忆审核」页里审）；
// - 知识库项徽标 = kb_pending（子代理提交待批的知识文档数）。
// 拉一次 + 每 60s 静默刷新（与 AgentsView 的 10s 节奏同款但更宽：徽标是
// 「记得处理一下」的提示，秒级滞后也能接受，省流量）。登录页/初始化页免拉
// —— 那两页连壳都套不上，轮询纯属自扰。
const REVIEW_POLL_MS = 60000
const reviewCounts = ref({ promote_candidates: 0, demote_candidates: 0, kb_pending: 0 })
let reviewTimer = null

function reviewBadge() {
  const n = (reviewCounts.value?.promote_candidates || 0)
          + (reviewCounts.value?.demote_candidates || 0)
  return n > 0 ? n : 0
}
function kbBadge() {
  const n = reviewCounts.value?.kb_pending || 0
  return n > 0 ? n : 0
}
/** 给定路径返回 0 / 数字（与模板的 `v-if` 配套）。null/undefined 与负数都退 0。 */
function badgeFor(path) {
  if (path === '/review') return reviewBadge()
  if (path === '/kb') return kbBadge()
  return 0
}

async function loadReviewCounts() {
  // 登录后才需要（未登录 401 已经在 api 拦截器里处理；这里静默吞，保证轮询不污染）
  if (!showShell.value) return
  try {
    const data = await api.reviewCounts()
    reviewCounts.value = {
      promote_candidates: Number(data?.promote_candidates || 0),
      demote_candidates: Number(data?.demote_candidates || 0),
      kb_pending: Number(data?.kb_pending || 0),
    }
  } catch (_) {
    // 失败保持旧值（避免一次抖动把 `N` 清成 `0`，用户以为已清空）
  }
}

if (typeof window !== 'undefined') {
  onMounted(() => {
    loadReviewCounts()
    if (reviewTimer) clearInterval(reviewTimer)
    reviewTimer = setInterval(loadReviewCounts, REVIEW_POLL_MS)
  })
  onUnmounted(() => {
    if (reviewTimer) clearInterval(reviewTimer)
    reviewTimer = null
  })
}
// ===== /待审数 =====================================================

// 登录 / 初始化 / token 三页不套外壳，否则套一个空侧边栏没意义
const BARE_PAGES = ['token', 'login', 'setup']
const showShell = computed(() => !BARE_PAGES.includes(route.name) && !!getToken())

// 主题：首屏已由 main.js 写好 html，这里只需要一份响应式副本给分段控件用
const theme = ref(readTheme())
function pick(t) {
  theme.value = setTheme(t)
}

/** 改密码：侧栏底部一个动作入口（不是页面，所以不进 GROUPS）。 */
const pwdOpen = ref(false)

/** 退出登录是误点代价最高的一下（直接退到登录页），所以先弹 ConfirmDialog。 */
const confirmSignOut = ref(false)
const signingOut = ref(false)
const signOutConsequences = [
  '退出后需重新登录才能管理记忆。',
  '会话 token 会在服务端作废，本地也会清掉；未保存的输入会丢。',
]

/** 点了登出按钮只开确认框，确认回调 onSignOutConfirm 才真的退。 */
async function signOut() {
  confirmSignOut.value = true
}

async function onSignOutConfirm() {
  signingOut.value = true
  // 先让服务端把这个会话作废，再清本地 token —— 只清本地的话 token 还留在服务端文件里
  try {
    await api.logout()
  } catch (e) {
    // 会话本来就过期/不存在时也要能退出去，失败不挡人
  }
  signingOut.value = false
  confirmSignOut.value = false
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
  top: 12px;
  bottom: 12px;
  left: 12px;
  z-index: 20;
  display: flex;
  flex-direction: column;
  width: var(--nav-w);
  padding: 20px 12px 12px;
  background: var(--panel);
  border-radius: var(--r-panel);
}
.nav-top {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 0 8px 4px;
}
.logo {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: var(--r-inset);
  background: var(--accent);
}
.nav-top-txt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.ws {
  color: var(--text);
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.2px;
}
.ws-sub {
  color: var(--text-faint);
  font-size: 12px;
}
.nav-scroll {
  flex: 1;
  overflow-y: auto;
}
/* 分组标题：不可点击的分组说明 */
.nav-group-title {
  margin: 24px 0 8px;
  padding: 0 8px;
  color: var(--text-ghost);
  font-size: 12px;
  font-weight: 500;
  cursor: default;
}
.nav-group:first-child .nav-group-title {
  margin-top: 16px;
}
.item {
  display: flex;
  gap: 10px;
  align-items: center;
  height: 40px;
  padding: 0 10px;
  margin-bottom: 2px;
  border-radius: var(--r-ctl);
  color: var(--text-dim);
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  transition: background 0.12s, color 0.12s;
}
.item:hover {
  background: var(--panel-hover);
  color: var(--text);
}
/* 选中态：整行蓝胶囊 */
.item.on {
  background: var(--accent);
  color: var(--on-accent);
  box-shadow: var(--shadow-nav-on);
}
.item.on:hover {
  background: var(--accent);
  color: var(--on-accent);
}
.ico {
  display: inline-flex;
  flex: none;
}

/* ---------------------------------------------------------------- 待审徽标
 * 侧栏 .item 右侧的小红点+数字；零新色（仅复用既有 danger / on-accent token）：
 *   未选中行：底=--danger, 字=--on-accent；
 *   选中行：整行已是 --accent 蓝胶囊，反白为底=--on-accent, 字=--danger。
 * margin-left:auto 把它推到行末（gap 不动，避免没徽标的行也受 layout 影响）。 */
.item-badge {
  margin-left: auto;
  min-width: 20px;
  height: 18px;
  padding: 0 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--danger);
  color: var(--on-accent);
  border-radius: 9px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  pointer-events: none;          /* 徽标只装饰，点穿透到 RouterLink */
}
.item.on .item-badge {
  background: var(--on-accent);
  color: var(--danger);
}

.nav-foot {
  margin-top: 12px;
  padding: 0 4px;
}
/* 明暗分段控件：轨道浅灰，激活项白底 */
.seg {
  display: flex;
  gap: 2px;
  padding: 3px;
  background: var(--panel-hover);
  border-radius: var(--r-ctl);
}
.seg button {
  display: flex;
  flex: 1;
  gap: 6px;
  align-items: center;
  justify-content: center;
  height: 30px;
  padding: 0 6px;
  background: transparent;
  border: 0;
  border-radius: var(--r-sm);
  color: var(--text-faint);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
}
.seg button:hover {
  color: var(--text);
}
.seg button.on {
  background: var(--bg-elev);
  color: var(--text);
  font-weight: 500;
}
/* 改密码：与分段控件同宽的幽灵按钮，悬停才出色（动作，不是当前页） */
.foot-btn {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  margin-top: 8px;
  padding: 8px 12px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-ctl);
  color: var(--text-faint);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.foot-btn:hover {
  background: var(--panel-hover);
  border-color: var(--border);
  color: var(--text);
}
.foot-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 品牌小卡片：白底 + 蓝色图标 + 一句短语 */
.brand-card {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--bg-elev);
  border-radius: var(--r-inset);
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.5;
}
.brand-card .ico {
  color: var(--accent);
}

/* -------------------------------------------------------------- 内容区 --- */
/* 内容区铺满导航右侧的可用宽度（不居中：左对齐贴导航，右侧只留自身 padding） */
.main {
  margin-left: calc(var(--nav-w) + 36px);
  padding: 24px 24px 40px;
  max-width: none;
}
.crumb-acts {
  display: flex;
  gap: 12px;
  align-items: center;
}

/* < 900px 导航收成图标条：隐藏文字与品牌卡，段控件只留图标 */
@media (max-width: 900px) {
  .nav {
    width: 64px;
    padding: 16px 8px 8px;
  }
  .nav-top-txt,
  .item .txt,
  .seg .txt,
  .brand-card .txt {
    display: none;
  }
  .nav-group-title {
    display: none;
  }
  .nav-top,
  .item {
    justify-content: center;
    padding: 0;
  }
  .seg {
    flex-direction: column;
  }
  .main {
    margin-left: 88px;
    padding: 16px 14px 32px;
  }
}
</style>
