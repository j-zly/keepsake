"""检索命中统计（控制台只读）：`GET /api/v1/hits/stats`。

给下一单 core_tiering（常驻层自动升降级）提供「热不热」的数据源 —— 本接口只
**读**命中事件表，不含任何提名/升降级判断。

## 为什么单开一个文件而不是并进 `fragments.py`

`fragments.py` 是「记忆条目本身」的读面（列表/详情），而命中统计是**派生遥测**：
数据源不同（`ks_hit_event` / `keepsake:hits`，不是 `ks_fragment`）、生命周期不同
（90 天 prune，碎片没有这个概念）、失败语义也不同（碎片读面挂了 = 记忆库有问题，
命中统计挂了 = 遥测有问题）。塞进去会让 `fragments.py` 的模块 docstring
（「一律经 search / list_by_tag / scan_fragment_keys 取数」）当场失效。
任务书允许二选一，选**新文件**，理由如上。

## 出参模型为什么也在这里定义、不进 `api/schemas.py`

`schemas.py` 不在本单的改动范围（范围闸 G6 只放行 `core/*` 与 `api/routers/`）。
为一处消费方去动共享模型文件不值当 —— 模型与它的唯一消费方放一起更好维护。

## 失败语义（与 `stats.py` 同款）

存储层异常**直接抛** ⇒ 整接口 500 + 结构化 detail。绝不吞成空 items 装成功：
「空表」与「打点挂了」必须可区分，否则下一单会把整批常驻层误判成冷而全量降级。
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..deps import get_storage, require_session

router = APIRouter(tags=["hits"])

#: 统计项上限（防止 ?limit=999999 一次拉全表；与 fragments 的 MAX_PAGE_SIZE 同款思路）
DEFAULT_HITS_LIMIT = 500
MAX_WINDOW_DAYS = 365
MAX_KEYS = 500


class HitStatItem(BaseModel):
    """单个 key 的窗口命中统计。"""

    key: str
    hits: int = 0
    last_hit: str = Field("", description="最近一次命中时刻（ISO8601 UTC）")


class HitStats(BaseModel):
    """命中统计响应（口径 = 打点行数，受 300 秒去抖影响有 ±5 分钟误差）。"""

    window_days: int
    min_hits: int
    items: List[HitStatItem]


@router.get("/api/v1/hits/stats", response_model=HitStats,
            dependencies=[Depends(require_session)])
def hits_stats(
    window_days: int = Query(7, ge=1, le=MAX_WINDOW_DAYS, description="统计窗口天数"),
    min_hits: int = Query(5, ge=1, description="命中次数门槛（只在不给 keys 时生效）"),
    keys: str = Query("", description="逗号分隔的 key 白名单；给了只算这些 key"),
    storage: Any = Depends(get_storage),
) -> HitStats:
    """窗口内命中次数 ≥ `min_hits` 的条目（`hits` 降序）。

    给了 `keys` 就只统计这些 key 且**不套 `min_hits` 门槛**（候选集已知，
    要的是「各多少热度」，门槛由调用方自己判断）—— 与存储层 `hits_stats` 同口径。
    """
    wanted: Optional[List[str]] = [k.strip() for k in keys.split(",") if k.strip()]
    if wanted is not None and len(wanted) > MAX_KEYS:
        wanted = wanted[:MAX_KEYS]
    rows = storage.hits_stats(
        window_days=window_days, min_hits=min_hits, keys=wanted or None,
        limit=DEFAULT_HITS_LIMIT,
    )
    return HitStats(
        window_days=window_days,
        min_hits=min_hits,
        items=[HitStatItem(**row) for row in rows],
    )
