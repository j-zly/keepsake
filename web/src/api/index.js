import axios from 'axios'

/**
 * Keepsake 控制台的 API 客户端。
 *
 * - token 存 localStorage，随请求头 `Authorization: Bearer <token>` 带出
 * - 401 全局拦截：清 token 并跳登录页（不是在每个页面各判一次）
 * - 🔴 token 只进请求头，绝不进 URL / 日志 / 页面回显
 *
 * P2 新增 6 条写操作（全部要二次确认，confirm 由调用方显式传入）：
 * 打标 promote/demote、改 update、归档 archive、批量 merge/forget。
 * merge/forget 默认 dry-run —— 实跑必须再补 `confirm`（当前计数，防手滑）。
 *
 * P3：token 是**登录换来的会话 token**（记住我 = 30 天），不再是手贴的静态串。
 * 登录三连：authStatus（首屏决定去 /setup 还是 /login）、setup、login / logout。
 */
const TOKEN_KEY = 'keepsake.console.token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 20000,
})

http.interceptors.request.use((cfg) => {
  const token = getToken()
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

/** 登录 / 初始化两页本身不发带鉴权的请求 —— 别在这儿把用户踢出去。 */
const NO_AUTH_PAGES = ['#/login', '#/setup', '#/token']

/**
 * 401 = 会话失效/过期/没配 token —— 一律清 token 退回登录页。
 * 429（限速）与 400/409 直接把后端文案抛给调用方，不做跳转。
 * 用 location 而非 router，避开 api ↔ router 的循环依赖。
 */
http.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err?.response?.status
    const detail = err?.response?.data?.detail
    const message = (detail && (detail.message || detail.error)) || err?.message || '请求失败'
    if (status === 401 && !NO_AUTH_PAGES.some((p) => location.hash.startsWith(p))) {
      setToken('')
      location.hash = '#/login'
    }
    return Promise.reject(new Error(message))
  },
)

export const api = {
  health: () => http.get('/health').then((r) => r.data),
  authStatus: () => http.get('/api/v1/auth/status').then((r) => r.data),
  setup: (username, password) =>
    http.post('/api/v1/auth/setup', { username, password }).then((r) => r.data),
  login: (username, password, remember) =>
    http.post('/api/v1/auth/login', { username, password, remember }).then((r) => r.data),
  logout: () => http.post('/api/v1/auth/logout').then((r) => r.data),
  me: () => http.get('/api/v1/auth/me').then((r) => r.data),
  coreList: (params) => http.get('/api/v1/core', { params }).then((r) => r.data),
  coreStats: () => http.get('/api/v1/core/stats').then((r) => r.data),
  fragments: (params) => http.get('/api/v1/fragments', { params }).then((r) => r.data),
  fragment: (key) => http.get(`/api/v1/fragments/${encodeURIComponent(key)}`).then((r) => r.data),
  kbSummary: () => http.get('/api/v1/kb/summary').then((r) => r.data),
  kbSearch: (params) => http.get('/api/v1/kb/search', { params }).then((r) => r.data),
  // P9 知识库列表：不输词也能看到文档（q 可选，带了才按标题/全文过滤）
  kbDocuments: (params) => http.get('/api/v1/kb/documents', { params }).then((r) => r.data),
  kbDocumentChunks: (docId, params) =>
    http.get(`/api/v1/kb/documents/${encodeURIComponent(docId)}/chunks`, { params }).then((r) => r.data),
  stats: () => http.get('/api/v1/stats').then((r) => r.data),
  crons: (params) => http.get('/api/v1/crons', { params }).then((r) => r.data),

  // ---- 写操作（后端会写前自动备份 + 审计，这里只是把 confirm 显式带上）----
  promote: (key) => http.post(`/api/v1/core/${encodeURIComponent(key)}/promote`, { confirm: true }).then((r) => r.data),
  demote: (key) => http.post(`/api/v1/core/${encodeURIComponent(key)}/demote`, { confirm: true }).then((r) => r.data),
  updateFragment: (key, payload) =>
    http.post(`/api/v1/fragments/${encodeURIComponent(key)}/update`, { confirm: true, ...payload }).then((r) => r.data),
  archive: (key, confirm) =>
    http.post(`/api/v1/fragments/${encodeURIComponent(key)}/archive`, { confirm }).then((r) => r.data),
  merge: (payload) => http.post('/api/v1/ops/merge', payload).then((r) => r.data),
  forget: (payload) => http.post('/api/v1/ops/forget', payload).then((r) => r.data),

  // ---- 记忆 CRUD（控制台批1 后端）：纯透传，语义与护栏都在后端 ----
  // 新增必须显式带 confirm（与其它写操作同一口径）；打 core 标签后端直 400。
  createFragment: (payload) =>
    http.post('/api/v1/fragments', { confirm: true, ...payload }).then((r) => r.data),
  // 真删：confirm 传 key 末 8 位（手输防手滑），删前备份由后端做。
  deleteFragment: (key, confirm) =>
    http.post(`/api/v1/fragments/${encodeURIComponent(key)}/delete`, { confirm }).then((r) => r.data),
  // 导入：{items, dry_run} 先预演，实跑再补 confirm=<would_add 数>。原样透传。
  importFragments: (payload) => http.post('/api/v1/import', payload).then((r) => r.data),
}

export default http
