"""两域总览：常驻层 / 检索层 / 知识库 / 预算占用。

失败语义（任务书硬约束 4）：**任一数据源异常不许吞成空数据装成功**。
本接口的口径：
  * `core` / `retrieval` 走存储层，异常直接抛 ⇒ 整接口 500 + 结构化 detail
  * `kb` 走的是另一个数据源（PG），它不可用时该段**显式标** `{available:false, error:...}`
    —— 是可见的报错，不是「空数组装成功」；而 `/api/v1/kb/*` 本身仍是 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..deps import get_storage, require_token
from ..config import RETRIEVAL_SCAN_CAP
from ..schemas import Overview
from . import kb as kb_router
from .core import core_stats

router = APIRouter(tags=["stats"])


@router.get("/api/v1/stats", response_model=Overview, dependencies=[Depends(require_token)])
def overview(storage: Any = Depends(get_storage)) -> Overview:
    """两域总览。core/retrieval 失败 ⇒ 500；kb 失败 ⇒ 该段显式标 unavailable。"""
    core = core_stats(storage)
    _, keys = storage.scan_fragment_keys("", RETRIEVAL_SCAN_CAP)
    retrieval = {"count": len(keys), "truncated": len(keys) >= RETRIEVAL_SCAN_CAP}
    return Overview(
        core=core,
        retrieval=retrieval,
        kb=kb_router.probe(),
    )
