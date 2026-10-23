"""两域总览：常驻层 / 检索层 / 知识库 / 预算占用。

外加一条**按代理分列**的只读端点（`/api/v1/stats/core-per-agent`）——
多代理时代每代理一份注入预算，控制台三页（看板 / 常驻层 / 记忆维护）
共用它那一份口径，别处不重算一遍。

失败语义（任务书硬约束 4）：**任一数据源异常不许吞成空数据装成功**。
本接口的口径：
  * `core` / `retrieval` 走存储层，异常直接抛 ⇒ 整接口 500 + 结构化 detail
  * `kb` 走的是另一个数据源（PG），它不可用时该段**显式标** `{available:false, error:...}`
    —— 是可见的报错，不是「空数组装成功」；而 `/api/v1/kb/*` 本身仍是 500。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from ..deps import get_storage, require_session
from ..config import RETRIEVAL_SCAN_CAP, core_char_budget
from ..schemas import CoreAgentUsage, DailyAgentCount, DailyCount, Overview
from . import kb as kb_router
from .core import CORE_TAG, core_stats

router = APIRouter(tags=["stats"])


#: `?days=` 的上限（再长就不是「近几天」而是「全量历史」了 —— 那该走导出）。
DAILY_MAX_DAYS = 90


def _daily(storage: Any, days: int) -> List[DailyCount]:
    """近 N 天的每日新增（**补齐没写入的那天**：折线图上它是 0，不是空白）。

    🔴 补齐只在这一处做（PG 侧那条 `GROUP BY` 只给有数据的天）——
    横轴的「连续 N 天」是图表口径，存储层不该管。
    """
    if days <= 0:
        return []
    counts = {str(r.get("date") or ""): int(r.get("count") or 0)
              for r in (storage.daily_counts(days) or [])}
    today = datetime.now(timezone.utc).date()
    out: List[DailyCount] = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        out.append(DailyCount(date=day, count=counts.get(day, 0)))
    return out


def _daily_by_agent(storage: Any, days: int) -> List[DailyAgentCount]:
    """近 N 天的每日新增**按代理分列**（`daily_by_agent` 段）。

    🔴 **不补零、不补日**：这一段只给「有写入」的格子（`{d, agent, n}`）——
    横轴（连续 N 天）是**图表口径**，由 `daily` 那一段负责（它补齐了每一天），
    前端拿 `daily` 铺横轴、拿这一段填格子。补零放两处就会漂。

    🔴 **老字段不许动**：`daily` 段一字不改（老前端与既有测试都靠它），
    按代理这一段是**新增**的 `daily_by_agent`。
    """
    if days <= 0:
        return []
    rows = storage.daily_counts_by_agent(days) or []
    return [DailyAgentCount(d=str(r.get("d") or ""), agent=str(r.get("agent") or "-"),
                             n=int(r.get("n") or 0))
            for r in rows if r.get("d")]


@router.get("/api/v1/stats", response_model=Overview, dependencies=[Depends(require_session)])
def overview(days: int = Query(0, ge=0, le=DAILY_MAX_DAYS,
                                description="近 N 天的每日新增（0 = 不查这一段）"),
             storage: Any = Depends(get_storage)) -> Overview:
    """两域总览。core/retrieval 失败 ⇒ 500；kb 失败 ⇒ 该段显式标 unavailable。

    `?days=N` 追加一段 `daily`（近 N 天每日新增条目数）—— 看板折线图用它。
    缺省 `days=0` ⇒ **不查这一段**（老调用方的开销一字不变）。

    🆕 另加一段 `daily_by_agent`（近 N 天每日新增**按代理分列**）——
    `daily` 一字不改（老前端与既有测试不许红），按代理是**新增**的一段。
    """
    core = core_stats(storage)
    _, keys = storage.scan_fragment_keys("", RETRIEVAL_SCAN_CAP)
    retrieval = {"count": len(keys), "truncated": len(keys) >= RETRIEVAL_SCAN_CAP}
    return Overview(
        core=core,
        retrieval=retrieval,
        kb=kb_router.probe(),
        daily=_daily(storage, days),
        daily_by_agent=_daily_by_agent(storage, days),
    )


@router.get("/api/v1/stats/core-per-agent", response_model=Dict[str, CoreAgentUsage],
            dependencies=[Depends(require_session)])
def core_per_agent(storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """常驻层**按代理**分列：`{agent_id: {count, chars, budget_effective}}`。

    🔴 **只读端点**：不写注册表、不改可见性、不碰写闸（与本页其余 stats 同一档）。

    口径（三条都写在这里，别处只引用不重写）：

    * **归属** = 条目 tags 里的 `agent:<id>` —— 判据用
      `govern.visibility.fragment_owner`（逗号边界的完整标签相等，
      `agent:win` 不命中 `agent:win-desktop`），与检索/core 可见性同一把尺子；
    * **预算** = 该代理**自己**的 `core_budget_chars`；没有（NULL / 脏值）⇒
      回退**全局**注入预算（`config.core_char_budget()`，与注入路径同源）；
    * **在册代理一律出现**（哪怕 0 条）—— 分列要看得见「谁还没占常驻层」；
      core 条目上还挂着、注册表里却没有的归属（代理被删 / 改名）**照报不吞**。

    **无归属**的常驻条目（老库里没打 `agent:` 标签的那些）不属于任何代理，
    因此不进这个分列 —— 总量仍以 `GET /api/v1/core/stats` 的 `count`/`chars`
    为准（页面上的占比行自己算差额）。
    """
    from keepsake.govern.registry import Registry
    from keepsake.govern.visibility import fragment_owner

    rows = list(storage.list_by_tag(CORE_TAG))
    buckets: Dict[str, Dict[str, int]] = {}
    for row in rows:
        owner = fragment_owner(row.get("tags", ""))
        if not owner:
            continue                       # 无归属 ⇒ 不计入任何代理
        b = buckets.setdefault(owner, {"count": 0, "chars": 0})
        b["count"] += 1
        b["chars"] += len(str(row.get("content", "") or ""))

    registry = Registry(storage)
    # ensure_schema：老库可能还没有 `core_budget_chars` 列（读这一列会直接 SQL 报错）。
    # 幂等，与 `routers/agents.py` 的读端点同一做法。
    registry.ensure_schema()
    global_budget = int(core_char_budget())

    out: Dict[str, Any] = {}
    for row in registry.list_agents():
        aid = str(row.get("agent_id") or "")
        if not aid or aid in out:
            continue
        b = buckets.pop(aid, {"count": 0, "chars": 0})
        own = row.get("core_budget_chars")
        out[aid] = {
            "count": b["count"],
            "chars": b["chars"],
            "budget_effective": int(own) if own else global_budget,
        }
    # 注册表里没有、core 条目上却还挂着归属的（代理被删 / 改名）：照报，不吞
    for aid, b in buckets.items():
        out[aid] = {
            "count": b["count"],
            "chars": b["chars"],
            "budget_effective": global_budget,
        }
    return out
