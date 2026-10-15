"""常驻核心层（core tag）只读接口。

数据全部来自 `storage.list_by_tag("core")` —— 与 provider 每轮注入走的是
**同一个原语**，所以控制台看到的条数/字符数就是实际注入量。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from .. import config
from ..deps import get_storage, require_session
from ..schemas import CoreItem, CoreList, CoreStats

router = APIRouter(tags=["core"])

#: 常驻层标签（与 `provider._CORE_TAG` 同名；provider 侧是模块私有常量，
#: 这里按任务书口径写死一个字面量，改标签时两边一起改）
CORE_TAG = "core"


def _preview(text: str) -> str:
    """列表态正文截断（详情走 `GET /api/v1/fragments/{key}` 取全文）。"""
    text = text or ""
    return text if len(text) <= config.CONTENT_PREVIEW_CHARS else (
        text[: config.CONTENT_PREVIEW_CHARS] + "…"
    )


def _core_rows(storage: Any) -> List[Dict[str, Any]]:
    """读全量 core 条目。**异常直接往上抛**（全局处理器转 500），
    绝不吞成空列表 —— 「常驻层空了」和「读库失败」必须能区分。"""
    return list(storage.list_by_tag(CORE_TAG))


def _matches(row: Dict[str, Any], needle: str) -> bool:
    """`needle`（已小写）是否出现在该行的正文或标签里。

    常驻层只有百来条，**内存子串过滤足够**（不上 BM25）：这里的诉求是
    「在这屏上按词找」，不是排序召回 —— 命中顺序保持存储层原序，与不分页时一致。
    """
    content = str(row.get("content", "") or "").lower()
    tags = str(row.get("tags", "") or "").lower()
    return needle in content or needle in tags


@router.get("/api/v1/core", response_model=CoreList, dependencies=[Depends(require_session)])
def list_core(
    q: str = Query("", description="搜索词：正文+标签大小写不敏感子串过滤；为空即全量"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    storage: Any = Depends(get_storage),
) -> CoreList:
    """常驻层列表：key / tags / 截断正文 / updated_at（服务端分页）。

    `q` 非空时先在**全量 core 行**上过滤再分页，`count` 是过滤后的条数
    （与不带 q 时的 count 同一口径：都是「本次结果的总数」，前端不另算）。
    """
    rows = _core_rows(storage)
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows if _matches(r, needle)]
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    return CoreList(
        items=[CoreItem(
            key=str(r.get("key", "")),
            tags=str(r.get("tags", "")),
            content=_preview(str(r.get("content", ""))),
            updated_at=str(r.get("updated_at", "")),
        ) for r in page_rows],
        count=len(rows),
        page=page,
        page_size=page_size,
    )


@router.get("/api/v1/core/stats", response_model=CoreStats, dependencies=[Depends(require_session)])
def core_stats(storage: Any = Depends(get_storage)) -> CoreStats:
    """常驻层统计：条数 / 字符数 / 注入预算占用。

    `count` 与 `GET /api/v1/core` 的 `count` 同源同口径（同一原语、同一筛选）；
    区别只有一处：列表的 `?q=` 是**搜索态**的 count，本端点恒为**全量**（预算占用
    本来就是全量口径，拿搜索结果当预算会把「以为没超预算」写进脑子）。
    """
    rows = _core_rows(storage)
    chars = sum(len(str(r.get("content", "") or "")) for r in rows)
    budget = config.core_char_budget()
    percent = round(chars * 100.0 / budget, 2) if budget > 0 else 0.0
    return CoreStats(count=len(rows), chars=chars, budget=budget, percent=percent)
