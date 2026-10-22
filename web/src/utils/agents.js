/**
 * 「按代理筛选 + 卡片代理标签」两页共用件（常驻层 / 记忆条目）。
 *
 * 判据 = 条目 `tags` 里的 `agent:<id>` **归属标签**（逗号边界的完整标签相等：
 * `agent:win` 不命中 `agent:win-desktop`），与后端 `keepsake.api.agent_filter`
 * 与存储层 `tag_match_sql` 同一把尺子。
 *
 * 🔴 分层：后端**已经**按 `agents` 参数过滤过（服务端分页的列表只在前端切当前页
 * 就是「看着筛了、其实漏了」）；这里的过滤是**显示层的同一把尺子** ——
 * 后端口径哪天松了，屏上也不许出现别的代理的记忆。
 *
 * 纯函数（不碰 api / DOM）⇒ node --test 可直接跑。
 */

/** 归属标签前缀（与后端 `govern.registry.AGENT_TAG_PREFIX` 同一个字面量）。 */
export const AGENT_TAG_PREFIX = 'agent:'

/** 没有归属的条目在卡片上显示什么。 */
export const NO_OWNER_LABEL = '无归属'

/**
 * 条目的归属代理 id（`tags` 里的 `agent:<id>`；没有 ⇒ 空串）。
 * 逗号边界的完整标签相等 —— 与后端同一判据。
 */
export function ownerOf(tags) {
  for (const part of String(tags || '').replace(/\|/g, ',').split(',')) {
    const t = part.trim()
    if (t.startsWith(AGENT_TAG_PREFIX)) return t.slice(AGENT_TAG_PREFIX.length).trim()
  }
  return ''
}

/**
 * 卡片上那个归属 chip 的文字：显示名 → 拿不到就显示裸 id → 没有归属就「无归属」。
 * @param {string} tags  条目的 tags（逗号串）
 * @param {object} names  agent_id → 显示名（注册表备注名）
 */
export function ownerLabel(tags, names) {
  const id = ownerOf(tags)
  if (!id) return NO_OWNER_LABEL
  return (names && names[id]) || id
}

/**
 * 按选中的代理过滤行（`ids` 为空 ⇒ 原样返回 = 全集，现状不变）。
 */
export function filterByOwners(rows, ids) {
  const want = new Set((ids || []).filter(Boolean))
  if (!want.size) return rows
  return (rows || []).filter((r) => want.has(ownerOf(r?.tags)))
}

/**
 * 知识库文档的「来源代理」chip 文字（`documents.agent_id` 列）。
 *
 * 判据与上面那把尺子同源：知识库的归属**不在 tags 里**（那是记忆的判据），
 * 而在 documents 表自己的一根列上 —— 空值 = **无主**（历史文档全是无主）。
 *
 * @param {string} agentId  documents.agent_id 的值
 * @param {object} names    agent_id → 显示名（注册表备注名）
 */
export function agentOwnerLabel(agentId, names) {
  const id = String(agentId || '').trim()
  if (!id) return NO_OWNER_LABEL
  return (names && names[id]) || id
}

/**
 * 拉**全部**代理（注册表是服务端分页的 ⇒ 翻页取完，不能只取第一页）。
 * `listAgents` 由调用方注入（api 层带 baseURL / 鉴权，utils 不自己 import）。
 * @returns {Promise<{id: string, name: string}[]>}
 */
export async function loadAgentOptions(listAgents) {
  return (await loadAgentRows(listAgents)).map((a) => ({ id: a.agent_id, name: a.name || '' }))
}

/**
 * 拉**全部**代理的**完整行**（注册表字段全给：`status` / `last_seen_at` /
 * `reads` / `writes` …）。看板的「代理」小节要的就是这些，不是只要 id+名字。
 *
 * 与 `loadAgentOptions` 同一份翻页逻辑（一个分页循环写两遍必然漂）。
 * @returns {Promise<object[]>}
 */
export async function loadAgentRows(listAgents) {
  const out = []
  const pageSize = 100
  for (let page = 1; ; page += 1) {
    const res = await listAgents({ page, page_size: pageSize })
    const items = res?.items || []
    out.push(...items)
    const total = res?.count ?? 0
    if (items.length < pageSize || out.length >= total) break
  }
  return out
}

/**
 * 「在线」的判据：**注册表 `last_seen_at` 落在最近 5 分钟内**。
 *
 * 🔴 口径写在代码里也得写在页面上（看板那张卡的 hint 里明写），否则「在线」
 *   是个没人知道怎么算的词。`last_seen_at` 是注册表的活动时间戳（读写都算，
 *   60s 限频），不是「SSE 连着」—— 注册表没有单独的 SSE 活动列，别假装有。
 */
export const ONLINE_WINDOW_MS = 5 * 60 * 1000

/** 在线口径的**人话**（页面上照这句显示，别自己另编一套）。 */
export const ONLINE_HINT = '口径：注册表 last_seen_at 在最近 5 分钟内（最近活动）'

/**
 * 某个代理现在算不算「在线」（`last_seen_at` 解析不出来 ⇒ 离线，不瞎猜）。
 * @param {object} row  注册表一行
 * @param {number} now  当前时刻（毫秒）；默认取本机时间
 */
export function isOnline(row, now = Date.now()) {
  const t = Date.parse(row?.last_seen_at || '')
  if (!Number.isFinite(t)) return false
  return now - t <= ONLINE_WINDOW_MS
}

/**
 * 注入预算占用的配色档：≥100% danger / ≥80% accent / 其余 ok
 * （与看板、常驻层页的全局占用条同一套阈值 —— 差一处就会出现两个页面
 * 同一份数据一个红一个绿）。
 */
export function usageTone(percent) {
  const p = Number(percent)
  if (!Number.isFinite(p)) return 'ok'
  if (p >= 100) return 'danger'
  if (p >= 80) return 'accent'
  return 'ok'
}

/**
 * `GET /api/v1/stats/core-per-agent` 的返回 → 页面好用的行。
 *
 * 输入 `{agent_id: {count, chars, budget_effective}}`，输出每行
 * `{agent_id, label, count, chars, budget, percent, tone}`：
 * `label` 用注册表备注名（没有就裸 id），`percent` = chars / budget（两位小数，
 * 与后端同一精度）。
 *
 * @param {object} perAgent  后端分列返回
 * @param {object} names     agent_id → 显示名
 */
export function agentUsageRows(perAgent, names = {}) {
  return Object.entries(perAgent || {}).map(([agent_id, u]) => {
    const budget = Number(u?.budget_effective || 0)
    const chars = Number(u?.chars || 0)
    const percent = budget > 0 ? Math.round((chars * 10000) / budget) / 100 : 0
    return {
      agent_id,
      label: (names && names[agent_id]) || agent_id,
      count: Number(u?.count || 0),
      chars,
      budget,
      percent,
      tone: usageTone(percent),
    }
  })
}

/**
 * 分列摘要按「当前筛选的代理」收窄（**空筛选 = 全部分列**，与列表同口径）。
 *
 * 常驻层页顶部那条预算条要跟这一屏看到的东西对齐：无筛选时给全库分列，
 * 选了代理就只留那个代理的占用（不选 = 现状不变 = 全量）。
 *
 * @param {object[]} rows      agentUsageRows 的行
 * @param {string[]} agentIds  选中的 agent_id（空 = 全选）
 */
export function usageRowsForFilter(rows, agentIds) {
  const want = new Set((agentIds || []).filter(Boolean))
  if (!want.size) return rows || []
  return (rows || []).filter((r) => want.has(r.agent_id))
}

/**
 * 某代理的字符占**总量**（水位）的百分比 —— 与 `usageTone` 的「占自己预算」
 * 是两个口径，别混：这里分母是全库常驻字符数。
 *
 * @param {number} chars 该代理的常驻字符数
 * @param {number} total 全库常驻字符数（水位总量）
 */
export function sharePercent(chars, total) {
  const c = Number(chars) || 0
  const t = Number(total) || 0
  return t > 0 ? Math.round((c * 10000) / t) / 100 : 0
}

/**
 * 注册表行 → `agent_id → 显示名` 的映射（看板 / 常驻层 / 维护页共用一份）。
 *
 * @param {object[]} rows 注册表行（loadAgentRows 的返回）
 */
export function agentNameMap(rows) {
  const out = {}
  for (const r of rows || []) {
    if (r?.agent_id) out[r.agent_id] = r.name || r.agent_id
  }
  return out
}
