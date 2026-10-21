"""控制台两页的「按代理筛选」口径（**单一一份**，core / fragments 共用）。

判据 = 条目 `tags` 里的 `agent:<id>` **归属标签**（逗号边界的完整标签相等）：

* 行侧解析直接调 `govern.visibility.fragment_owner`（唯一那份归属口径）；
* 取数侧用存储层的 `list_by_tag` —— 它的匹配判据就是
  `storage_shared.tag_match_sql` 那份逗号边界 SQL 片段。

🔴 **过滤必须发生在分页之前**：只筛当前页 = 「看着筛了、其实漏了」
（总数与翻页都会骗人）。调用方拿到的已是无别的代理的行。

🔴 **可见性 / 共享开关 / 写闸一个字都不动**：这里只是控制台的**读面筛选**
（按归属过滤显示哪些条目），不是权限判定 —— 与 `govern.visibility` 那套
「谁看得见」是**两件事**，别混。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Set

from keepsake.govern.visibility import fragment_owner

__all__ = [
    "parse_agent_ids",
    "owner_of",
    "filter_rows_by_agents",
    "rows_by_agent_tags",
]


def parse_agent_ids(raw: Any) -> List[str]:
    """`?agents=a,b` → 去重后的 agent_id 列表（空 ⇒ 不过滤 = 现状不变）。

    去重是为了 `?agents=a,a` 不至于把同一批行取两遍（并集里本来就该只算一次）。
    """
    out: List[str] = []
    seen: Set[str] = set()
    for part in str(raw or "").split(","):
        aid = part.strip()
        if aid and aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


def owner_of(row: Dict[str, Any]) -> str:
    """这一行的归属代理 id（没有 `agent:` 归属 ⇒ 空串）。"""
    return fragment_owner(row.get("tags", ""))


def filter_rows_by_agents(rows: Sequence[Dict[str, Any]],
                           agent_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """按归属过滤行（`agent_ids` 为空 ⇒ 原样返回 = 现状不变）。

    🔴 调用方必须**在切页之前**调它 —— 过滤后的行数才是 count 的口径。
    """
    ids = set(agent_ids or ())
    if not ids:
        return list(rows)
    return [r for r in rows if owner_of(r) in ids]


def rows_by_agent_tags(storage: Any, agent_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """按归属标签取行：每个 agent 一次 `list_by_tag`，取**并集**。

    `list_by_tag` 的判据就是 `storage_shared.tag_match_sql` 的逗号边界完整标签相等
    —— 与「归属标签」同一把尺子；并集 = 多个 agent 选中时的 OR。

    ponytail: 每个 agent 一次全表扫（PG/SQLite 各一次，Redis 一次 SCAN）。
    代理数量是个位数 ⇒ 可接受；要更省就加一个 `list_by_tags`（一条 OR 的 SQL），
    届时只换这个函数，调用方（路由层）不用动。
    """
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for aid in agent_ids:
        for r in storage.list_by_tag(f"agent:{aid}"):
            key = str(r.get("key", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    return rows
