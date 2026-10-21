"""检索层（记忆条目）只读接口：分页列表 + 单条全文。

一律经 `keepsake.storage` 的现成原语取数（`search` / `list_by_tag` /
`scan_fragment_keys` + `get_fragments_batch`），**不拼 SQL、不碰后端专属连接**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import config
from ..agent_filter import filter_rows_by_agents, parse_agent_ids, rows_by_agent_tags
from ..deps import get_storage, require_session
from ..schemas import FragmentDetail, FragmentItem, FragmentList

router = APIRouter(tags=["fragments"])

#: `updated_at` 在各后端字段名不同（sqlite/redis=updated、PG=updated_at），
#: 检索结果里还可能只有 created —— 按优先级取，取不到就空串。
_UPDATED_KEYS = ("updated_at", "updated", "created")


def _updated_at(row: Dict[str, Any]) -> str:
    for k in _UPDATED_KEYS:
        v = row.get(k)
        if v:
            return str(v)
    return ""


def _score(row: Dict[str, Any]) -> Optional[float]:
    """检索打分：RRF 融合分优先，其次 BM25 分 / 向量相似度；都不是则 None。"""
    for k in ("_combined_score", "_bm25_score", "_sim"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return round(float(v), 6)
    return None


def _preview(text: str) -> str:
    text = text or ""
    return text if len(text) <= config.CONTENT_PREVIEW_CHARS else (
        text[: config.CONTENT_PREVIEW_CHARS] + "…"
    )


def _render(row: Dict[str, Any]) -> str:
    """列表项正文 = 预览正文 + （适用条件非空时）一行 `[适用条件] …`。

    🔴 **只加在渲染层，绝不动排序/打分**：一条带适用条件的记忆不该因此排得更靠前
    或更靠后 —— 那是融合算法的事，本单的红线之一就是不碰它。
    适用条件列是契约 v2 的可选列（老库未迁移时存储层读路径会省略这个键），
    所以取不到就当没有，**出参不凭空造一个空标签**。
    """
    text = _preview(str(row.get("content", "")))
    app = str(row.get("applicability", "") or "").strip()
    if not app:
        return text
    return f"{text}\n{config.APPLICABILITY_LABEL} {app}"


def _to_item(row: Dict[str, Any]) -> FragmentItem:
    return FragmentItem(
        key=str(row.get("key") or row.get("_key") or ""),
        tags=str(row.get("tags", "")),
        content=_render(row),
        updated_at=_updated_at(row),
        score=_score(row),
    )


def _collect(storage: Any, q: str, tag: str, agent_ids: List[str], page: int,
             page_size: int) -> Tuple[List[Dict[str, Any]], Optional[int], bool, str]:
    """按 (q, tag, agents) 选数据源并切出当前页。返回 `(rows, total, has_more, count_reason)`。

    * `q` 非空 → BM25/KNN 融合检索（`storage.search`）：count 是候选池条数
    * `q` 空、`tag` 非空 → 按标签列（`storage.list_by_tag`）：count 是该标签全量条数
    * `q`/`tag` 都空、`agents` 非空 → 按归属标签取并集（`storage.list_by_tag`）
    * 三个都空 → 全库浏览：keyset 扫描出**真实总数**（本库几千行，可接受）；
      扫到 `RETRIEVAL_SCAN_CAP` 触顶时 count=null + `count_reason=scan_capped`，
      **不谎报总数，也不默默给 null**。

    🔴 `agents` 的过滤**一律发生在切页之前**（`filter_rows_by_agents`）：
    只筛当前页 = 「看着筛了、其实漏了」，count 与翻页都会骗人。

    ponytail: 全库浏览分支用「从头扫 page*page_size 条」把页码映射到 keyset 游标，
    O(page*page_size)、深翻页线性变慢；存储层暂无 count/offset 原语，
    真要深翻页时给存储层加 `(created,key)` 游标即可，前端契约不用动。
    """
    if q:
        rows = filter_rows_by_agents(list(storage.search(q, tag_filter=tag)), agent_ids)
        total: Optional[int] = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        return page_rows, total, start + len(page_rows) < total, "search_candidates"

    if tag:
        rows = filter_rows_by_agents(list(storage.list_by_tag(tag)), agent_ids)
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        return page_rows, total, start + len(page_rows) < total, "tag_exact"

    if agent_ids:
        # 没有 q 也没有 tag 时，归属筛选自己就是取数口径（`list_by_tag` 的标签匹配
        # 与 `storage_shared.tag_match_sql` 同一判据）
        rows = rows_by_agent_tags(storage, agent_ids)
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        return page_rows, total, start + len(page_rows) < total, "agent_tags"

    cursor, keys = storage.scan_fragment_keys("", page * page_size)
    # 总数单独扫一次（有上限）：只扫到本页深度时扫不干净，count 就会是 null。
    _, all_keys = storage.scan_fragment_keys("", config.RETRIEVAL_SCAN_CAP)
    total = None if len(all_keys) >= config.RETRIEVAL_SCAN_CAP else len(all_keys)
    page_keys = keys[(page - 1) * page_size:]
    fetched = storage.get_fragments_batch(page_keys)
    rows = [dict(fetched[k], key=k) for k in page_keys if k in fetched]
    return (rows, total, bool(cursor) or len(rows) == page_size,
            "scan_capped" if total is None else "scan_total")


@router.get("/api/v1/fragments", response_model=FragmentList,
            dependencies=[Depends(require_session)])
def list_fragments(
    q: str = Query("", description="检索词；为空则退化为按 tag 浏览"),
    tag: str = Query("", description="标签精确过滤（逗号边界匹配，与存储层同判据）"),
    agents: str = Query("", description="按归属代理过滤：逗号分隔 agent_id 列表（并集）；为空即全量"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    storage: Any = Depends(get_storage),
) -> FragmentList:
    """记忆条目列表（服务端分页；BM25 走 `storage.search`）。

    `agents` 是控制台「按代理筛选」的服务端口径（**服务端过滤**，不是前端切当前页）。
    """
    rows, total, has_more, reason = _collect(storage, q.strip(), tag.strip(),
                                              parse_agent_ids(agents), page, page_size)
    return FragmentList(
        items=[_to_item(r) for r in rows],
        count=total,
        page=page,
        page_size=page_size,
        q=q.strip(),
        tag=tag.strip(),
        has_more=has_more,
        count_reason=reason,
    )


def _jsonable(value: Any) -> Any:
    """BLOB（向量字节）不进 JSON：按类型标注后原样返回 bytes 的占位。"""
    if isinstance(value, (bytes, bytearray)):
        return f"<blob {len(value)} bytes>"
    return str(value)


@router.get("/api/v1/fragments/{key}", response_model=FragmentDetail,
            dependencies=[Depends(require_session)])
def get_fragment(key: str, storage: Any = Depends(get_storage)) -> FragmentDetail:
    """单条记忆条目全文（全字段）。不存在 ⇒ 404。

    🔴 契约 v2 的 `applicability` / `is_latest` / `supersedes` / `supersede_reason`
    **走 `raw` 透传，不另开字段** —— 它们是「存储层返回什么这里就带什么」的一类。
    这么做的连带好处正是缺列降级：老库未迁移那四列时，PG 存储层读路径会**省略**
    这几个键（`PgStorage._read_columns`），`raw` 里自然就没有它们 —— 出参不带键、
    不假装每条都有，与 `tags`/`content_type` 的降级纪律一字同款。同一批列在
    PG 写路径缺失时抛 `storage_pg._ContractColumnMissing`（带
    `config.CONTRACT_V2_MIGRATION_HINT` 迁移 SQL），不静默把适用条件丢掉。
    """
    row = storage.get_fragment(key)
    if not row:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "message": f"记忆条目不存在：{key}"})
    return FragmentDetail(
        key=key,
        content=str(row.get("content", "")),
        tags=str(row.get("tags", "")),
        created_at=str(row.get("created", "") or ""),
        updated_at=_updated_at(row),
        score=_score(row),
        raw={k: _jsonable(v) for k, v in row.items() if k != "key"},
    )
