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
  // 常驻层**按代理**分列：{agent_id: {count, chars, budget_effective}}。
  // 看板分列 / 常驻层页 / 维护页占比行共用这一份口径（别处不重算一遍）。
  corePerAgent: () => http.get('/api/v1/stats/core-per-agent').then((r) => r.data),
  fragments: (params) => http.get('/api/v1/fragments', { params }).then((r) => r.data),
  fragment: (key) => http.get(`/api/v1/fragments/${encodeURIComponent(key)}`).then((r) => r.data),
  kbSummary: () => http.get('/api/v1/kb/summary').then((r) => r.data),
  kbSearch: (params) => http.get('/api/v1/kb/search', { params }).then((r) => r.data),
  // P9 知识库列表：不输词也能看到文档（q 可选，带了才按标题/全文过滤）
  kbDocuments: (params) => http.get('/api/v1/kb/documents', { params }).then((r) => r.data),
  // 分类 Tab 上的计数：content_type -> 文档数。**后端给，前端不写死数字**
  // （老库未迁移该列时后端给 {}，页面按「无计数」渲染，不编）。
  kbTypeCounts: () => http.get('/api/v1/kb/type-counts').then((r) => r.data),
  // 自由标签筛选 chip 行：{items:[{tag,count}], total_distinct}，按计数倒序。
  // **计数同样来自后端**（与 Tab 计数同纪律：写死就等于库一变数字就错）；
  // 老库未迁移 tags 列时后端给空列表，页面整行隐藏，不编 chip。
  // 🔴 后端已剔除机器标签（local-kb-id-* / entry-*）并只回 Top-N，
  // total_distinct = 过滤后的真实去重数（页面据此显示「+N 更多」）。
  kbTags: () => http.get('/api/v1/kb/tags').then((r) => r.data),
  // 「更多」展开：一次性取回**全部**过滤后的标签（分页在前端切）。
  kbTagsAll: () => http.get('/api/v1/kb/tags', { params: { all: 1 } }).then((r) => r.data),
  kbDocumentChunks: (docId, params) =>
    http.get(`/api/v1/kb/documents/${encodeURIComponent(docId)}/chunks`, { params }).then((r) => r.data),
  // 总览：?days=N 追加一段 daily（近 N 天每日新增）—— 看板折线图用它。
  // 🔴 缺省不传 days ⇒ 后端**不查**那一段（老调用方的开销一字不变）。
  stats: (params) => http.get('/api/v1/stats', { params }).then((r) => r.data),
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
  // 晋升候选的复核进度（一页一次批量问）：判据在后端，这里不写阈值也不写窗口天数。
  candidatesProgress: (keys) =>
    http
      .get('/api/v1/tiering/candidates-progress', { params: { keys: (keys || []).join(',') } })
      .then((r) => r.data),
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
  // ---- 定时任务管理面全量开放（scheduler_page_v2）----
  // 新建：整份规格（id/name/schedule/func/timeout）一次给全；func 是
  // 「模块:函数」引用（执行体在同进程里调它）。
  cronCreate: (payload) => http.post('/api/v1/crons', { confirm: true, ...payload }).then((r) => r.data),
  // 全字段改（没传的字段后端沿用现值）：与 cronEdit 同一条端点，
  // cronEdit 只改 schedule（看板那张任务卡在用），这里是「整份改」。
  cronUpdate: (jobId, payload) =>
    http.post(`/api/v1/crons/${encodeURIComponent(jobId)}/edit`, { confirm: true, ...payload }).then((r) => r.data),
  // 删除：**永久生效**（后端写墓碑 ⇒ 重启不复活）。没有请求体 ——
  // 页面上的红色确认弹框就是确认本身（不键入任何字符）。
  cronDelete: (jobId) => http.delete(`/api/v1/crons/${encodeURIComponent(jobId)}`).then((r) => r.data),
  // 立即运行一次：一次性 DateTrigger（不建常驻 job），执行照常落状态表。
  cronTrigger: (jobId) =>
    http.post(`/api/v1/crons/${encodeURIComponent(jobId)}/trigger`, { confirm: true }).then((r) => r.data),
  // 执行记录（近 10 次，只读）。
  cronRuns: (jobId) => http.get(`/api/v1/crons/${encodeURIComponent(jobId)}/runs`).then((r) => r.data),

  // ---- 代理治理（agent_registry）：五条全透传，闸门口径照抄后端 ----
  // approve / exclude 的 confirm 是 `true`；revoke 要**手输 agent_id 前 8 位**；
  // role 要**手输完整 agent_id**（改的是全库唯一 primary 这件事，不是单个开关）。
  // 🔴 approve 的响应里带**一次性凭据明文**：只许由页面当场展示转交，
  // 不许落任何持久面（不进 URL、不进浏览器存储）。
  // 🔴 `approveAgent` = 批准（pending / revoked 行）：**不带 reissue** ⇒ 后端对
  // 已批准的代理一律 409（不换发、不动凭据）—— 重复批准不再静默把线上代理的旧
  // 凭据顶掉。`reissueAgent` = **换发**（同一条端点 + 显式 reissue=true）：
  // 只给「重新生成凭据」那条路用，旧的那条随即失效。
  listAgents: (params) => http.get('/api/v1/agents', { params }).then((r) => r.data),
  approveAgent: (agentId) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/approve`, { confirm: true }).then((r) => r.data),
  reissueAgent: (agentId) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/approve`, { confirm: true, reissue: true }).then((r) => r.data),
  revokeAgent: (agentId, confirm) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/revoke`, { confirm }).then((r) => r.data),
  setAgentRole: (agentId, role, confirm) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/role`, { role, confirm }).then((r) => r.data),
  setAgentExclude: (agentId, exclude) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/exclude`, { confirm: true, exclude }).then((r) => r.data),
  // ---- 消息中心（代理互聊信箱的治理面）--------------------------------
  // 消息列表：**只读**（后端不改任何字段），筛代理 / 线程 / 关键词 + 服务端分页。
  // 参数名是 page / size（不是 page_size —— 那是注册表列表那套口径）。
  listMessages: (params) => http.get('/api/v1/messages', { params }).then((r) => r.data),
  // 私信开关（dm_enabled）：confirm 恒为 true（后端要显式二次确认），enabled 传目标状态。
  // 🔴 失败时**不要**自己改 UI —— 页面负责回滚（回滚逻辑在 AgentsView 里）。
  setAgentDm: (agentId, enabled) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/dm`, { enabled, confirm: true }).then((r) => r.data),
  // 记忆共享开关（share_memory）：同样 confirm 恒为 true；只改**读可见性**
  // （这个代理的记忆进不进别人的检索范围），不碰状态 / 角色 / 凭据。
  setAgentShare: (agentId, enabled) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/share`, { enabled, confirm: true }).then((r) => r.data),
  // 私信实时推送开关（push_enabled）：confirm 恒为 true；只改「能不能被连上来」
  // （SSE 长连接的建流闸），不碰状态 / 角色 / 凭据。
  setAgentPush: (agentId, enabled) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/push`, { enabled, confirm: true }).then((r) => r.data),
  // ---- 凭据可还原（ks_credential_recoverable，2026-11-14 二次定档）----
  // 🔴 用户口径：「复制是直接能复制不用输入什么东西」⇒ **不要求人打任何字**。
  //    但这一层的 `confirm: true` 不许省：它是后端的**显式意图位**（缺位 400），
  //    挡的是不带 body 的盲发调用；由前端自动带上，用户看不见、也不需要填。
  //    防"认错行"不靠打字：回填那条路由 sha256 对账兜（粘错串整体拒绝）。
  // reveal 不改数据但读的是密钥面 ⇒ 后端仍叠 write_guard + 每次取出一行审计（不含内容）。
  // 取某一把的明文。🔴 `credId` 省略时后端只在"名下恰好一把在用"才回 ——
  // 多把在用又不指名 ⇒ 409 ambiguous（宁拒不猜，挑错就是把另一把的钥匙递出去）。
  revealCredential: (agentId, credId = '') =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/reveal_credential`,
      { confirm: true, cred_id: credId }).then((r) => r.data),
  // ---- 多凭据（ks_multi_credential，2026-11-14）：一个代理多把钥匙，逐把监管 ----
  agentCredentials: (agentId) =>
    http.get(`/api/v1/agents/${encodeURIComponent(agentId)}/credentials`).then((r) => r.data),
  // 新建一把：**不顶掉任何旧钥匙**（与「重新生成凭据」换发的分界就在这）
  addCredential: (agentId, label) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/credentials`,
      { label, confirm: true }).then((r) => r.data),
  renameCredential: (agentId, credId, label) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/credentials/${encodeURIComponent(credId)}/rename`,
      { label }).then((r) => r.data),
  // 改这把的功能授权（记忆 / 知识库）。没勾上的就是不给用 —— 与复选框一致。
  setCredentialScopes: (agentId, credId, scopes) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/credentials/${encodeURIComponent(credId)}/scopes`,
      { scopes, confirm: true }).then((r) => r.data),
  // 作废单把（用户拍板：弹框点确认即可，不要求手输编号）
  revokeCredential: (agentId, credId) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/credentials/${encodeURIComponent(credId)}/revoke`,
      { confirm: true }).then((r) => r.data),
  // 把**已经在用**的凭据登记成可还原：不换发、不签发新串（⇒ 线上代理不断线）。
  // 🔴 明文只在这一发请求里出浏览器，前端不留副本（粘贴框提交后立刻清空）。
  backfillCredential: (agentId, credential) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/backfill_credential`,
      { credential, confirm: true }).then((r) => r.data),
  // 手动 / 批量触发提醒：给点名的收件代理推一帧 `dm-nudge` 事件（只元数据）。
  // 🔴 回包里逐代理的 `delivered` 是**后端如实回报**的 —— 没挂着流就是 false，
  //   前端不许自己乐观成「已送达」。
  nudgeMessages: (agents) =>
    http.post('/api/v1/messages/nudge', { agents }).then((r) => r.data),
  // per-agent 注入预算（core_budget_chars）：confirm 恒为 true；只改**一个阈值**，
  // 不碰状态 / 角色 / 凭据。传 null = 清空 ⇒ 沿用全局默认（不是「预算 0」）。
  setAgentBudget: (agentId, coreBudgetChars) =>
    http.post(`/api/v1/agents/${encodeURIComponent(agentId)}/budget`,
      { core_budget_chars: coreBudgetChars ?? null, confirm: true }).then((r) => r.data),

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
  // ---- 知识库勾选批量（与记忆条目页的 ops/bulk 同一口径）----------------
  // {ids:[...], confirm:<选中条数>}：确认串 = 选中数（后端还会再校验一次，
  // 前端不算权威）。逐条成败由后端返回 {succeeded, failed:[{id,reason}], total}，
  // 前端只负责报「成功 X · 失败 Y」+ 刷新（单条失败不拖累其余条目）。
  kbBulkApprove: (payload) =>
    http.post('/api/v1/kb/documents/bulk-approve', payload).then((r) => r.data),
  // 批量驳回：多一个 reason（**必填**，整批一个理由）。
  kbBulkReject: (payload) =>
    http.post('/api/v1/kb/documents/bulk-reject', payload).then((r) => r.data),
  // 批量真删：逐篇备份快照（后端做），确认串同样 = 选中条数。
  kbBulkDelete: (payload) =>
    http.post('/api/v1/kb/documents/bulk-delete', payload).then((r) => r.data),

  // ---- 待办（2026-10 ks_todo 一等公民）--------------------------------
  // 列表：服务端分页 + 按 owner / status 筛（scope 快捷档 open/done/all）。
  // 🔴 计数**全部由后端给**（前端不自己数 —— 数字写死就等于库一变数字就错）。
  listTodos: (params) => http.get('/api/v1/todos', { params }).then((r) => r.data),
  // 看板那枚「待办」小节：{open, done, obsolete, total, overdue}
  todoSummary: () => http.get('/api/v1/todos/summary').then((r) => r.data),
  // 「建议废弃」提名清单（维护页用）：**只提名，不改状态**。
  todoObsoleteSuggestions: (params) =>
    http.get('/api/v1/todos/obsolete-suggestions', { params }).then((r) => r.data),
  // 新增：confirm 恒为 true（后端要显式二次确认）；owner 必须是已批准的代理。
  createTodo: (payload) =>
    http.post('/api/v1/todos', { confirm: true, ...payload }).then((r) => r.data),
  // 改状态：status = open / done / obsolete（后端带 CHECK 校验，非法值 400）。
  setTodoStatus: (id, status) =>
    http.post(`/api/v1/todos/${encodeURIComponent(id)}/status`,
      { status, confirm: true }).then((r) => r.data),
  // 真删：confirm 传**待办标题全文**（手输防手滑，后端比对）。
  deleteTodo: (id, confirm) =>
    http.request({ method: 'delete', url: `/api/v1/todos/${encodeURIComponent(id)}`,
      data: { confirm } }).then((r) => r.data),

  // ---- AI 配置页（五节集中配置 + 连通检测）--------------------------
  // 五节快照：{sections:[{id,label,note,enabled,fields:[{key,type,value,min,max,
  // secret,placeholder}]}]}。🔴 **secret 字段只回「有没有」**（值永不下发），
  // 数值与输入框上下限也由后端给（前端零写死）。
  aiSettings: () => http.get('/api/v1/settings/ai').then((r) => r.data),
  // 保存一节：fields 是该节字段 map，confirm 恒为 true（后端要显式二次确认）；
  // 🔴 凭据字段留空就别传（传空串 = 不改）。
  setAiSettings: (sectionId, fields) =>
    http
      .put(`/api/v1/settings/ai/${encodeURIComponent(sectionId)}`, { confirm: true, ...fields })
      .then((r) => r.data),
  // 连通检测（读性质：不写库、不改 config）：成功 ok=true + 各节细节，
  // 失败 ok=false + HTTP 状态 + 原因摘要（后端已抹掉凭据）。
  probeAiSettings: (sectionId) =>
    http.post(`/api/v1/settings/ai/${encodeURIComponent(sectionId)}/probe`, {}).then((r) => r.data),

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
