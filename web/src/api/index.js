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
 *
 * 🔴 拦截器把后端 detail.message 装进 Error，另挂一个 `status`：
 * 调用方要区分 403（旧密码不对）与 400（新密码太弱）时靠状态码，
 * 别去匹配中文文案 —— 文案一改，字符串匹配就跟着烂。
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
    const out = new Error(message)
    out.status = status
    return Promise.reject(out)
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
  // 改口令：三个字段全走 POST body —— 口令绝不能进 URL（浏览器历史 / 访问日志 / Referer）
  changePassword: (oldPassword, newPassword) =>
    http
      .post('/api/v1/auth/change_password', {
        old_password: oldPassword,
        new_password: newPassword,
        confirm: true,
      })
      .then((r) => r.data),
  coreList: (params) => http.get('/api/v1/core', { params }).then((r) => r.data),
  coreStats: () => http.get('/api/v1/core/stats').then((r) => r.data),
  fragments: (params) => http.get('/api/v1/fragments', { params }).then((r) => r.data),
  fragment: (key) => http.get(`/api/v1/fragments/${encodeURIComponent(key)}`).then((r) => r.data),
  kbSummary: () => http.get('/api/v1/kb/summary').then((r) => r.data),
  kbSearch: (params) => http.get('/api/v1/kb/search', { params }).then((r) => r.data),
  // P9 知识库列表：不输词也能看到文档（q 可选，带了才按标题/全文过滤）
  kbDocuments: (params) => http.get('/api/v1/kb/documents', { params }).then((r) => r.data),
  // 分类 Tab 上的计数：content_type -> 文档数。**后端给，前端不写死数字**
  // （老库未迁移该列时后端给 {}，页面按「无计数」渲染，不编）。
  kbTypeCounts: () => http.get('/api/v1/kb/type-counts').then((r) => r.data),
  // 自由标签筛选 chip 行：{items:[{tag,count}]}，按计数倒序。**计数同样来自后端**
  // （与 Tab 计数同纪律：写死就等于库一变数字就错）；老库未迁移 tags 列时后端给
  // 空列表，页面整行隐藏，不编 chip。
  kbTags: () => http.get('/api/v1/kb/tags').then((r) => r.data),
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
  // 勾选批量：{action: promote|demote|archive|delete, keys, confirm:<选中数>}。
  // 原样透传：逐条成败由后端返回 failed，前端只负责报「成功 X · 失败 Y」+ 刷新。
  // （勾选导出走 utils/export.js 的 fetch+blob，不走这里 —— 它是文件下载。）
  bulkOps: (payload) => http.post('/api/v1/ops/bulk', payload).then((r) => r.data),
  forget: (payload) => http.post('/api/v1/ops/forget', payload).then((r) => r.data),

  // ---- 记忆 CRUD（控制台批1 后端）：纯透传，语义与护栏都在后端 ----
  // 新增必须显式带 confirm（与其它写操作同一口径）；打 core 标签后端直 400。
  createFragment: (payload) =>
    http.post('/api/v1/fragments', { confirm: true, ...payload }).then((r) => r.data),
  // 真删：confirm 传 key 末 8 位（手输防手滑），删前备份由后端做。
  deleteFragment: (key, confirm) =>
    http.post(`/api/v1/fragments/${encodeURIComponent(key)}/delete`, { confirm }).then((r) => r.data),
  // 导入：{items, dry_run} 先预演，实跑再补 confirm=<would_add 数>。原样透传。
  // promote=true = 导入即升常驻（后端逐条打 core 标签，与 promote 端点同一条写路径）。
  importFragments: (payload) => http.post('/api/v1/import', payload).then((r) => r.data),
  // 设置常驻层注入预算：confirm 传**当前生效值**的字符串（改的是全局阈值，
  // 回退影响每轮注入，所以确认串是「现在是多少」而不是 key 末 8 位）。
  setCoreBudget: (budget, confirm) =>
    http.put('/api/v1/settings/core-budget', { budget, confirm }).then((r) => r.data),

  // ---- 常驻层分级班车（提名 / 自动升 / 压力降）----
  // 状态：{enabled, last_run, last_stats, budget:{used,total,ratio}, settings}
  tieringStatus: () => http.get('/api/v1/tiering/status').then((r) => r.data),
  // 设置开关与阈值：body 装的是 config.json 里 `tiering` 整节（与表单一一对应），
  // confirm 传**当前生效 enabled** 的字符串 'true'/'false'（开这个开关等于让
  // 班车每天动常驻层，确认串是「现在是不是开着」而不是条数）。
  setTiering: (tiering, confirm) =>
    http.put('/api/v1/settings/tiering', { tiering, confirm }).then((r) => r.data),

  // ---- 定时任务管理（暂停 / 恢复 / 改调度）----
  // 三条都是纯透传：后端拿 job_id 去调官方 `hermes cron` CLI（argv 列表，
  // 不碰 jobs.json），这里只负责把 confirm 显式带上、把 400 的校验原文
  // 原样抛回页面（表达式错在哪由后端那句话说了算）。
  cronPause: (jobId) =>
    http.post(`/api/v1/crons/${encodeURIComponent(jobId)}/pause`, { confirm: true }).then((r) => r.data),
  cronResume: (jobId) =>
    http.post(`/api/v1/crons/${encodeURIComponent(jobId)}/resume`, { confirm: true }).then((r) => r.data),
  cronEdit: (jobId, schedule) =>
    http.post(`/api/v1/crons/${encodeURIComponent(jobId)}/edit`, { confirm: true, schedule }).then((r) => r.data),

  // ---- 代理治理（agent_registry）：五条全透传，闸门口径照抄后端 ----
  // approve / exclude 的 confirm 是 `true`；revoke 要**手输 agent_id 前 8 位**；
  // role 要**手输完整 agent_id**（改的是全库唯一 primary 这件事，不是单个开关）。
  // 🔴 approve 的响应里带**一次性凭据明文**：只许由页面当场展示转交，
  // 不许落任何持久面（不进 URL、不进浏览器存储）。
  listAgents: (params) => http.get('/api/v1/agents', { params }).then((r) => r.data),
  approveAgent: (agentId) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/approve`, { confirm: true }).then((r) => r.data),
  revokeAgent: (agentId, confirm) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/revoke`, { confirm }).then((r) => r.data),
  setAgentRole: (agentId, role, confirm) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/role`, { role, confirm }).then((r) => r.data),
  setAgentExclude: (agentId, exclude) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/exclude`, { confirm: true, exclude }).then((r) => r.data),

  // ---- 知识库 CRUD（控制台批2 后端）：同样纯透传，域隔离与护栏都在后端 ----
  // 新增/改必须显式带 confirm（与其它写操作同一口径）；条目只进手动域。
  // 🆕 payload 可带 content_type（note/article/link，缺省 note）与 url（link 必填）：
  // 原样透传，枚举校验与「类型创建后不可变」的口径全在后端。
  // 可带 tags（自由标签数组）：净化口径（去重/限长/限数/拒控制字符）也在后端。
  kbCreateDocument: (payload) =>
    http.post('/api/v1/kb/documents', { confirm: true, ...payload }).then((r) => r.data),
  // 改文档：title/content 至少给一个；改正文时后端同事务重切切片（只改 documents 行会召回旧片段）。
  // 🔴 **不带 content_type**：类型创建后不可变（后端带异值 400），重切按文档自身类型走。
  // 🆕 带 tags 时是**整体替换**（给空数组 = 清空所有标签），后端语义见 KbUpdateBody。
  kbUpdateDocument: (id, payload) =>
    http.post(`/api/v1/kb/documents/${encodeURIComponent(id)}/update`, { confirm: true, ...payload }).then((r) => r.data),
  // 真删：confirm 传文档标题全文（手输防手滑），删前备份由后端做；skill 域后端 409。
  kbDeleteDocument: (id, confirm) =>
    http.post(`/api/v1/kb/documents/${encodeURIComponent(id)}/delete`, { confirm }).then((r) => r.data),
  // 导入：{items, dry_run} 先预演，实跑再补 confirm=<would_add 数>。原样透传。
  kbImportDocuments: (payload) => http.post('/api/v1/kb/import', payload).then((r) => r.data),

  // ---- 知识库审核队列（批2）：子代理提交的知识文档先审后用 ----
  // 待审面板（pending + rejected 一起给，pending_count 是全量 pending 数）：
  // 🔴 **计数绝不写死在前端**（与 type-counts / tags 同纪律），老库未迁移 status 列
  // 时后端给 0，页面就不显示「待审」chip。
  kbPending: (params) => http.get('/api/v1/kb/pending', { params }).then((r) => r.data),
  // 待审条目计数（侧栏徽标的唯一取数口）：{promote_candidates, demote_candidates, kb_pending}。
  // 三个数全是全库口径，**后端给，前端不写死数字**：lib 一变数字就错。
  // KB 未配置 / status 列缺失时 kb_pending 退 0（与 kb_type_counts 同款降级）。
  reviewCounts: () => http.get('/api/v1/review/counts').then((r) => r.data),
  // 批准：confirm 传 true（与代理治理 approve 同档防）；后端在同一事务里改 effective
  // + **此刻才切块**落 chunks + 记批准痕迹。
  kbApproveDocument: (id) =>
    http.post(`/api/v1/kb/documents/${encodeURIComponent(id)}/approve`, { confirm: true }).then((r) => r.data),
  // 驳回：reason **必填**（后端 400 reason_required）—— 驳回不删正文，理由是日后
  // 重新审核的唯一线索。原样透传，错误文案由后端说了算。
  kbRejectDocument: (id, reason) =>
    http.post(`/api/v1/kb/documents/${encodeURIComponent(id)}/reject`, { confirm: true, reason }).then((r) => r.data),
}

export default http
