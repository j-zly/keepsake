import axios from 'axios'

/**
 * 只读控制台的 API 客户端。
 *
 * - token 存 localStorage，随请求头 `Authorization: Bearer <token>` 带出
 * - 401 全局拦截：清 token 并跳「输入 token」页（不是在每个页面各判一次）
 * - 🔴 token 只进请求头，绝不进 URL / 日志 / 页面回显
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

/** 401 = token 缺失/错误/未配置 —— 一律退回 token 页（用 location 而非 router，避开循环依赖）。 */
http.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err?.response?.status
    const detail = err?.response?.data?.detail
    const message = (detail && (detail.message || detail.error)) || err?.message || '请求失败'
    if (status === 401) {
      setToken('')
      if (!location.hash.startsWith('#/token')) location.hash = '#/token'
    }
    return Promise.reject(new Error(message))
  },
)

export const api = {
  health: () => http.get('/health').then((r) => r.data),
  coreList: (params) => http.get('/api/v1/core', { params }).then((r) => r.data),
  coreStats: () => http.get('/api/v1/core/stats').then((r) => r.data),
  fragments: (params) => http.get('/api/v1/fragments', { params }).then((r) => r.data),
  fragment: (key) => http.get(`/api/v1/fragments/${encodeURIComponent(key)}`).then((r) => r.data),
  kbSummary: () => http.get('/api/v1/kb/summary').then((r) => r.data),
  kbSearch: (params) => http.get('/api/v1/kb/search', { params }).then((r) => r.data),
  stats: () => http.get('/api/v1/stats').then((r) => r.data),
  crons: (params) => http.get('/api/v1/crons', { params }).then((r) => r.data),
}

export default http
