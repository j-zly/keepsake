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
 * 拉**全部**代理（注册表是服务端分页的 ⇒ 翻页取完，不能只取第一页）。
 * `listAgents` 由调用方注入（api 层带 baseURL / 鉴权，utils 不自己 import）。
 * @returns {Promise<{id: string, name: string}[]>}
 */
export async function loadAgentOptions(listAgents) {
  const out = []
  const pageSize = 100
  for (let page = 1; ; page += 1) {
    const res = await listAgents({ page, page_size: pageSize })
    const items = res?.items || []
    out.push(...items.map((a) => ({ id: a.agent_id, name: a.name || '' })))
    const total = res?.count ?? 0
    if (items.length < pageSize || out.length >= total) break
  }
  return out
}
