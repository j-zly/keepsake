"""知识库只读接口：三表计数 + 全文检索。

**唯一允许直接连 PG 的地方**（任务书例外条款）：只跑 `SELECT`，
连接串只从 `KEEPSAKE_PG_DSN` 环境变量读，代码里不出现任何连接串/口令。

表结构按任务书口径：schema 下的 `skill_docs` / `documents` / `chunks`。
列名在本仓库里没有权威定义（KB 库不在 keepsake 里），所以启动后按
`information_schema.columns` **探一次真实列名**再拼查询 —— 表名/列名不能参数化，
所以只认探出来、且过了标识符白名单正则的名字，杜绝拼接注入。
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path          # 文档切片的路径参数（P9 新增；单独一行，不动上一行的 import）

from .. import config
from ..deps import require_session
from ..schemas import KbHit, KbSearchResult, KbSummary
from ..schemas import KbChunkList, KbDocument, KbDocumentList

router = APIRouter(tags=["kb"])

#: 标识符白名单：小写字母/数字/下划线，且不以数字开头
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: 候选列名（按优先级；探到哪个用哪个）
_CAND_TEXT = ("content", "text", "body", "chunk_text", "page_content")
_CAND_ID = ("id", "chunk_id", "key", "uuid")
_CAND_DOC = ("doc_id", "document_id", "doc", "knowledge_id")
_CAND_TITLE = ("title", "name", "doc_title", "document_title")
_CAND_TSV = ("content_tsv", "tsv", "search_vector", "content_vector_ts")
#: documents 表的时间列（可选）：探到就用，探不到就留空串 —— 时间列不是识别表结构的硬要求
_CAND_TIME = ("updated_at", "modified_at", "last_modified_at", "updated", "created_at", "date")

_COLS_CACHE: Dict[str, Dict[str, set]] = {}
_COLS_LOCK = threading.Lock()


def _ident(name: str) -> str:
    """标识符过白名单才允许进 SQL 文本（表名/列名无法参数化，只能这样拦）。"""
    if not _IDENT_RE.match(name):
        raise HTTPException(status_code=500,
                            detail={"error": "bad_identifier", "message": f"非法标识符：{name}"})
    return name


def _dsn() -> str:
    dsn = config.pg_dsn()
    if not dsn:
        raise HTTPException(
            status_code=500,
            detail={"error": "kb_unconfigured",
                    "message": f"知识库连接串未配置（{config.PG_DSN_ENV}）"},
        )
    return dsn


def _pg() -> Tuple[Any, Any]:
    """返回 (驱动模块, 行工厂)。psycopg2 优先，退 psycopg3 —— 两版 API 基本同构。

    🔴 **两版都要求显式 `import` 子模块**：`import psycopg2` 不会加载
    `psycopg2.extras`，直接取 `.RealDictCursor` 会裸炸
    `AttributeError: module 'psycopg2' has no attribute 'extras'`；
    psycopg3 同理（`psycopg.rows.dict_row`）。所以下面两行 import 都必须写。
    子模块缺失按「这版驱动不可用」处理（`AttributeError` 一起兜住），
    退另一版；两版都不可用 ⇒ 结构化 500 `kb_no_driver`，绝不漏裸 AttributeError。
    """
    try:
        import psycopg2                      # noqa: PLC0415 —— 延迟导入：没装也不影响非 KB 接口
        import psycopg2.extras               # noqa: PLC0415,F401 —— 行工厂在这个子模块里

        return psycopg2, psycopg2.extras.RealDictCursor
    except (ImportError, AttributeError) as e:
        first_error = e
    try:
        import psycopg                       # noqa: PLC0415
        import psycopg.rows                  # noqa: PLC0415,F401 —— 行工厂在这个子模块里

        return psycopg, psycopg.rows.dict_row
    except (ImportError, AttributeError) as e:
        raise HTTPException(
            status_code=500,
            detail={"error": "kb_no_driver",
                    "message": "未安装可用的 PostgreSQL 驱动（psycopg2 或 psycopg）："
                               f"{first_error.__class__.__name__}/{e.__class__.__name__}"},
        ) from e


def _connect() -> Any:
    """打开只读连接（上下文管理器）。

    🔴 **判断顺序硬约束**：先 `_dsn()` 判未配置，再 `_pg()` 载驱动。
    顺序反了的话，未配置 DSN 会被「驱动没装/不完整」盖住，客户端拿到的是
    `kb_no_driver` 而不是 `kb_unconfigured`，定位不了真因。所有取数都走这里，
    顺序只此一处，不再散落在各接口里。
    """
    dsn = _dsn()
    pg, row_factory = _pg()
    return pg.connect(dsn, cursor_factory=row_factory, connect_timeout=5)


def _table_columns(schema: str, tables: Sequence[str]) -> Dict[str, set]:
    """探一次三表的真实列名（进程级缓存，只读 information_schema）。"""
    key = f"{schema}:{','.join(tables)}"
    cached = _COLS_CACHE.get(key)
    if cached is not None:
        return cached
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = ANY(%s)",
                (schema, list(tables)),
            )
            cols: Dict[str, set] = {t: set() for t in tables}
            for row in cur.fetchall():
                cols.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
    with _COLS_LOCK:
        _COLS_CACHE[key] = cols
    return cols


def _pick(cols: set, candidates: Sequence[str], table: str, what: str) -> str:
    """在真实列里挑第一个候选；挑不到 ⇒ 500（结构不识别，不能装作没数据）。"""
    for c in candidates:
        if c in cols:
            return _ident(c)
    raise HTTPException(
        status_code=500,
        detail={"error": "kb_schema_unknown",
                "message": f"{table} 表未找到 {what} 列（候选：{', '.join(candidates)}）"},
    )


def _missing(tables: Dict[str, set]) -> List[str]:
    return [t for t, cols in tables.items() if not cols]


@router.get("/api/v1/kb/summary", response_model=KbSummary,
            dependencies=[Depends(require_session)])
def kb_summary() -> KbSummary:
    """知识库三表计数（一条 SELECT，三个子查询）。"""
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_SKILL_DOCS,
                                   config.KB_TABLE_DOCUMENTS,
                                   config.KB_TABLE_CHUNKS))
    gone = _missing(tabs)
    if gone:
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": f"知识库表不存在：{', '.join(gone)}"})
    sql = (
        "SELECT (SELECT COUNT(*) FROM {s}.{a}) AS skill_docs, "
        "(SELECT COUNT(*) FROM {s}.{b}) AS documents, "
        "(SELECT COUNT(*) FROM {s}.{c}) AS chunks"
    ).format(s=schema, a=config.KB_TABLE_SKILL_DOCS,
             b=config.KB_TABLE_DOCUMENTS, c=config.KB_TABLE_CHUNKS)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    return KbSummary(skill_docs=int(row["skill_docs"]),
                     documents=int(row["documents"]),
                     chunks=int(row["chunks"]))


@router.get("/api/v1/kb/search", response_model=KbSearchResult,
            dependencies=[Depends(require_session)])
def kb_search(
    q: str = Query(..., min_length=1, description="检索词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
) -> KbSearchResult:
    """知识库全文命中：有 tsvector 列走 tsquery，否则退 ILIKE（只读，不建索引）。"""
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    if _missing(tabs):
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": "知识库 documents/chunks 表不存在"})
    chunks = tabs[config.KB_TABLE_CHUNKS]
    docs = tabs[config.KB_TABLE_DOCUMENTS]

    c_text = _pick(chunks, _CAND_TEXT, config.KB_TABLE_CHUNKS, "正文")
    c_id = _pick(chunks, _CAND_ID, config.KB_TABLE_CHUNKS, "主键")
    c_doc = _pick(chunks, _CAND_DOC, config.KB_TABLE_CHUNKS, "外键(文档)")
    d_id = _pick(docs, _CAND_ID, config.KB_TABLE_DOCUMENTS, "主键")
    c_tsv = next((_ident(c) for c in _CAND_TSV if c in chunks), None)

    joined = (f"FROM {schema}.{config.KB_TABLE_CHUNKS} c "
              f"LEFT JOIN {schema}.{config.KB_TABLE_DOCUMENTS} d "
              f"ON d.{d_id} = c.{c_doc}")
    d_title = next((_ident(t) for t in _CAND_TITLE if t in docs), None)
    select_doc = f"COALESCE(d.{d_title}, '')" if d_title else "''"

    if c_tsv:
        # 中文语料用 simple 分词（PG 默认 english 会把中文切成一坨）
        mode, where, params = "tsvector", f"c.{c_tsv} @@ plainto_tsquery('simple', %s)", [q]
        rank = f"ts_rank(c.{c_tsv}, plainto_tsquery('simple', %s))"
        rank_params: List[Any] = [q]
    else:
        mode, where, params = "ilike", f"c.{c_text} ILIKE %s", [f"%{q}%"]
        rank, rank_params = "0.0::float8", []

    sql = (
        f"SELECT c.{c_id} AS chunk_id, CAST(c.{c_doc} AS text) AS doc_id, "
        f"{select_doc} AS title, LEFT(c.{c_text}, 300) AS snippet, "
        f"{rank} AS score {joined} WHERE {where} "
        f"ORDER BY score DESC, chunk_id ASC LIMIT %s OFFSET %s"
    )
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [*params, *rank_params, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
    items = [
        KbHit(chunk_id=str(r["chunk_id"]), doc_id=str(r["doc_id"] or ""),
              title=str(r["title"] or ""), snippet=str(r["snippet"] or ""),
              rank=round(float(r["score"] or 0.0), 6))
        for r in rows
    ]
    return KbSearchResult(q=q, items=items, count=len(items), page=page,
                          page_size=page_size, mode=mode)


@router.get("/api/v1/kb/documents", response_model=KbDocumentList,
            dependencies=[Depends(require_session)])
def kb_documents(
    q: Optional[str] = Query(None, description="标题模糊词，可选；不给就全量浏览"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
) -> KbDocumentList:
    """文档列表（只读）：不输词也能看到数据 —— 这就是知识库页的默认视图。

    切片条数用相关子查询按 doc_id 计数；q 复用检索那套手法（documents 表有 tsvector
    列走 tsquery，否则退标题 ILIKE）。表名/列名仍只认白名单探出来的名字。
    """
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    gone = _missing(tabs)
    if gone:
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": f"知识库表不存在：{', '.join(gone)}"})
    docs = tabs[config.KB_TABLE_DOCUMENTS]
    chunks = tabs[config.KB_TABLE_CHUNKS]

    d_id = _pick(docs, _CAND_ID, config.KB_TABLE_DOCUMENTS, "主键")
    c_doc = _pick(chunks, _CAND_DOC, config.KB_TABLE_CHUNKS, "外键(文档)")
    d_title = next((_ident(t) for t in _CAND_TITLE if t in docs), None)
    d_time = next((_ident(t) for t in _CAND_TIME if t in docs), None)
    d_tsv = next((_ident(c) for c in _CAND_TSV if c in docs), None)

    keyword = (q or "").strip()
    if keyword and d_tsv:
        # 中文语料同检索：simple 分词
        where, params = f"d.{d_tsv} @@ plainto_tsquery('simple', %s)", [keyword]
    elif keyword and d_title:
        where, params = f"d.{d_title} ILIKE %s", [f"%{keyword}%"]
    else:
        # 没词、或表里既没标题列也没索引列 ⇒ 不过滤（宁可多给，不假装筛过）
        where, params = "TRUE", []

    src = f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} d"
    chunk_ct = (f"(SELECT COUNT(*) FROM {schema}.{config.KB_TABLE_CHUNKS} c "
                f"WHERE c.{c_doc} = d.{d_id}) AS chunk_count")
    order = "chunk_count DESC, " + (f"d.{d_time} DESC, " if d_time else "") + "doc_id ASC"
    # 标题/时间列探不到就 SELECT '' —— 出参留空串，不假装有值
    sel_title = f"d.{d_title}" if d_title else "''"
    sel_time = f"d.{d_time}" if d_time else "''"

    sql = (f"SELECT CAST(d.{d_id} AS text) AS doc_id, {sel_title} AS title, "
           f"{sel_time} AS updated_at, "
           f"{chunk_ct} {src} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [*params, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS n {src} WHERE {where}", list(params))
            total = int((cur.fetchone() or {"n": 0})["n"])

    items = [
        KbDocument(doc_id=str(r["doc_id"] or ""), title=str(r["title"] or ""),
                   updated_at=str(r["updated_at"] or ""), chunk_count=int(r["chunk_count"] or 0))
        for r in rows
    ]
    return KbDocumentList(items=items, count=total, page=page, page_size=page_size)


@router.get("/api/v1/kb/documents/{doc_id}/chunks", response_model=KbChunkList,
            dependencies=[Depends(require_session)])
def kb_document_chunks(
    doc_id: str = Path(..., min_length=1, description="文档 id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
) -> KbChunkList:
    """单个文档的切片列表（只读）。文档不存在 ⇒ 404 kb_doc_missing。

    这里不给切片打分：ts_rank 要有检索词才有意义，浏览单个文档没有词，
    所以 rank 恒 0、按 chunk_id 稳定排序（任务书里的 rank 是可选字段）。
    """
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    gone = _missing(tabs)
    if gone:
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": f"知识库表不存在：{', '.join(gone)}"})
    docs = tabs[config.KB_TABLE_DOCUMENTS]
    chunks = tabs[config.KB_TABLE_CHUNKS]

    d_id = _pick(docs, _CAND_ID, config.KB_TABLE_DOCUMENTS, "主键")
    c_id = _pick(chunks, _CAND_ID, config.KB_TABLE_CHUNKS, "主键")
    c_doc = _pick(chunks, _CAND_DOC, config.KB_TABLE_CHUNKS, "外键(文档)")
    c_text = _pick(chunks, _CAND_TEXT, config.KB_TABLE_CHUNKS, "正文")

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {schema}.{config.KB_TABLE_DOCUMENTS} d "
                        f"WHERE d.{d_id} = %s LIMIT 1", [doc_id])
            if cur.fetchone() is None:
                raise HTTPException(status_code=404,
                                    detail={"error": "kb_doc_missing",
                                            "message": f"知识库文档不存在：{doc_id}"})
            sql = (f"SELECT CAST(c.{c_id} AS text) AS chunk_id, LEFT(c.{c_text}, 300) AS snippet, "
                   f"0.0::float8 AS score FROM {schema}.{config.KB_TABLE_CHUNKS} c "
                   f"WHERE c.{c_doc} = %s ORDER BY chunk_id ASC LIMIT %s OFFSET %s")
            cur.execute(sql, [doc_id, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS n FROM {schema}.{config.KB_TABLE_CHUNKS} c "
                        f"WHERE c.{c_doc} = %s", [doc_id])
            total = int((cur.fetchone() or {"n": 0})["n"])

    items = [
        KbHit(chunk_id=str(r["chunk_id"] or ""), doc_id=doc_id,
              snippet=str(r["snippet"] or ""),
              rank=round(float(r["score"] or 0.0), 6))
        for r in rows
    ]
    return KbChunkList(doc_id=doc_id, items=items, count=total, page=page, page_size=page_size)


def probe() -> Optional[Dict[str, Any]]:
    """给总览接口用：探一次知识库是否可用（不可用返回 None，由调用方显式标注 error）。"""
    try:
        s = kb_summary()
        return {"available": True, **s.model_dump()}
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": "kb_failed",
                                                              "message": str(e.detail)}
        return {"available": False, **detail}
    except Exception as e:                    # noqa: BLE001 —— 探针不许把总览接口带崩
        return {"available": False, "error": "kb_failed",
                "message": f"{e.__class__.__name__}: {e}"}
