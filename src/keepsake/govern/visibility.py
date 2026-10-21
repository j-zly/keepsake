"""记忆共享开关的**可见性过滤器** —— 唯一一份口径（2026-10 ks_share_memory）。

## 它解决什么

`ks_agent.share_memory` 决定「这个代理名下的记忆**能不能被别人看见**」：

* **开**：它的记忆进入**其他已批准代理**的检索可见范围；
* **关（默认）**：只有它自己 + 主脑（primary）看得见；
* **primary 调用方**：永远全量可见（行为与开关上线前逐字一致）。

🔴 **对称规则**（2026-10 ks_share_tighten 收紧）：**谁都不许白拿别人的** ——
普通条目与 core（常驻层）用的是**同一把尺子**：非 primary 看得见的归属 =
`{自己} ∪ {share_memory=1 的代理}`。primary 自己的记忆（含 core）要外露，
**得由 primary 那行自己把 `share_memory` 打开**（缺省关 ⇒ 不外露）。
旧口径「非 primary 看得见 primary 的 core」**作废**（core 层曾给 primary 开后门）。

🔴 **只影响「读可见性」**：写路径不过滤（谁的凭据写谁的），写闸 / 分层 / 私信
一个都不动（那是别的语义）。

## 为什么单点

可见性判据散在多处就一定漂：检索侧要按 `agent:` 标签过滤，直取侧（按 key 取
单条）要按归属校验，core 资源要按同一口径筛 —— 三处各写一份，第三处迟早
漏掉前两处的收紧。本模块是**唯一**那份口径，三处都调它。

## 依赖纪律

本模块**只依赖标准库 + 本包的 `registry`**（不 import `core.*`）：`core/pg.py`
与 `core/sqlite.py` 反向 import `govern` 的 DDL 建表，`govern` 再 import
`core` 就成环了。归属标签的字面量（`agent:`）从 `registry` 取，不复制一份。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from .registry import (
    AGENT_TAG_PREFIX,
    ROLE_PRIMARY,
    STATUS_APPROVED,
)

__all__ = [
    "visible_agent_ids",
    "shared_agent_ids",
    "fragment_owner",
    "visible_core_fragments",
]


def shared_agent_ids(registry: Any) -> List[str]:
    """**共享集**：已批准 **且** 打开「共享记忆」的代理 id（按 id 排序）。

    🔴 只认 `status=approved`：pending / revoked 的代理的记忆**不进**共享范围
    （共享是「给已批准的代理看」，不是「给谁都看」）。
    查不到代理（空行）也当没共享 —— 「代理存不存在」是**另一道闸**。

    `ponytail:` 每次调用都全表扫一遍 `ks_agent`（代理数量是个位数 ⇒ 一条
    小查询）。**刻意不加缓存**：开关必须**即时生效**（改完立刻能查到），
    而缓存一加就有「改了没生效」的窗口 —— 那种故障最难查。

    🔴 注册表**只读面残缺**（替身 / 半成品）⇒ 返回空集（= 没有共享集），
    绝不抛：可见性是读路径的**收紧**，收紧判据读不到时不该把整个检索打挂。
    """
    lister = getattr(registry, "list_agents", None)
    if registry is None or not callable(lister):
        return []
    out: List[str] = []
    for row in lister():
        if str(row.get("status") or "") != STATUS_APPROVED:
            continue
        if not row.get("share_memory"):
            continue
        out.append(str(row.get("agent_id") or ""))
    return sorted(a for a in out if a)


def visible_agent_ids(
    caller_agent_id: str,
    registry: Any,
    *,
    is_primary: Optional[bool] = None,
) -> Optional[Set[str]]:
    """这个调用方**看得见哪些代理的归属**（`None` = 看不见 ⇒ 过滤掉）。

    * 调用方是 **primary** ⇒ `None`（**不过滤，全量可见**）；
    * 否则 ⇒ `{调用方自己} ∪ {共享集}`。

    `is_primary` 缺省时**问注册表**（谁是 primary 由注册表说了算）；调用方
    已经有权威结论（provider 侧的本进程身份 / MCP 闸门判出的角色）时显式传入，
    免得两边对「谁是 primary」各有一份判据。

    🔴 查不到注册表（治理面整体不可用）⇒ `None`（不过滤）：读路径保持既有
    行为，不因为治理面挂了就让记忆读不出来（写闸那边是 fail-closed，方向相反：
    写要保守，读要照旧）。
    """
    if registry is None:
        return None
    caller = (caller_agent_id or "").strip()
    if is_primary is None:
        role_of = getattr(registry, "role_of", None)
        role = str(role_of(caller) or "") if callable(role_of) else ""
        is_primary = role == ROLE_PRIMARY
    if is_primary:
        return None
    visible: Set[str] = set()
    if caller:
        visible.add(caller)
    visible.update(shared_agent_ids(registry))
    return visible


def fragment_owner(tags: Any) -> str:
    """碎片的**归属代理 id**（`tags` 里的 `agent:<id>`；没有 ⇒ 空串）。

    判据是**逗号边界的完整标签相等**（与存储层 `tag_match_sql` / Redis 的
    TAG 语义同口径）：`agent:win` 不会命中 `agent:win-desktop`。
    """
    if not tags:
        return ""
    norm = str(tags).replace("|", ",")
    for ch in ((" ,", ","), (", ", ",")):
        norm = norm.replace(*ch)
    for part in norm.split(","):
        part = part.strip()
        if part.startswith(AGENT_TAG_PREFIX):
            return part[len(AGENT_TAG_PREFIX):].strip()
    return ""


def visible_core_fragments(
    fragments: Iterable[Dict[str, Any]],
    registry: Any,
    caller_agent_id: str = "",
) -> List[Dict[str, Any]]:
    """core（常驻层）按**同一口径**过滤：非 primary 只留**自己的** + 已共享代理的。

    🔴 **对称规则**（2026-10 ks_share_tighten 收紧）：core 层**不再给 primary
    开后门** —— 旧口径「非 primary 看得见 primary 的 core」作废。primary 自己的
    core 要不要外露，**由 primary 那行的 `share_memory` 开关决定**（缺省关 ⇒
    不外露）。primary **自己**看自己的 core 不受影响（它全量可见）。

    🔴 与检索侧同口径，但**多了「无归属」这一档**：老库里没有 `agent:` 标签的
    core 条目（写它的时候存储还没注入归属标签）**继续可见** —— 把「现状不变」
    这条硬回归线守住。

    有归属的条目按归属判：归属是自己、或归属在共享集里 ⇒ 可见；
    别的代理的 core（含 primary 的，除非它开了共享）⇒ 不可见。
    """
    rows = list(fragments or [])
    if registry is None:
        return rows
    caller = (caller_agent_id or "").strip()
    role_of = getattr(registry, "role_of", None)
    role = str(role_of(caller) or "") if (caller and callable(role_of)) else ""
    if role == ROLE_PRIMARY:
        return rows                      # primary 全量可见（现状逐字不变）
    allowed = set(shared_agent_ids(registry))
    if caller:
        allowed.add(caller)             # 自己的 core 自己看得见
    out: List[Dict[str, Any]] = []
    for row in rows:
        owner = fragment_owner(row.get("tags", ""))
        if not owner or owner in allowed:
            out.append(row)
    return out
