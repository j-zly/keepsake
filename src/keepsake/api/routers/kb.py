"""知识库接口：三表计数 + 全文检索（只读）+ 手动文档 CRUD/导入/导出（写）。

**唯一允许直接连 PG 的地方**（任务书例外条款）：连接串只从 `KEEPSAKE_PG_DSN`
环境变量读，代码里不出现任何连接串/口令。读端点只跑 `SELECT`；写端点只碰
`source_type='manual'` 的文档（skill/skill_hk 是灌入器的地盘，见文末说明）。

表结构按任务书口径：schema 下的 `skill_docs` / `documents` / `chunks`。
列名在本仓库里没有权威定义（KB 库不在 keepsake 里），所以启动后按
`information_schema.columns` **探一次真实列名**再拼查询 —— 表名/列名不能参数化，
所以只认探出来、且过了标识符白名单正则的名字，杜绝拼接注入。

写端点的四条纪律（与 `routers/ops.py` 同款，不另起一套）：

1. 切块与内容 hash 判据走 `keepsake.kb.ingest`（**与灌入器 CLI 同一个函数对象**），
   两处各切一次块，同一篇文档在页面上的切片数与检索命中就会对不上；
2. 手动文档 `source_key='manual:<sha256(title+content)[:12]>'` ⇒ 内容级幂等，
   重复导入同文档 = unchanged；灌入器的死文档清理**只**删 skill/skill_hk，碰不到它；
3. `require_session` + `write_guard` 一条不落（审计落不下 ⇒ 宁可不写）；
4. 真删 = 手输文档标题 + **删前把全文与切片落备份**（`audit.backup_document`），
   备份写不成功 ⇒ 一行都不删。
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path          # 文档切片的路径参数（P9 新增；单独一行，不动上一行的 import）
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from keepsake.kb import ingest

from .. import audit, config
from ..deps import require_session
from ..schemas import KbHit, KbSearchResult, KbSummary
from ..schemas import KbChunkList, KbDocument, KbDocumentList
from .ops import _need_flag, _need_text, _reject, write_guard

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

# ---- 写端点要的列（**必需**：探不到就 500，不许拿别的列凑合着写）----
_CAND_SRC_TYPE = ("source_type",)
_CAND_SRC_KEY = ("source_key",)
_CAND_META = ("meta",)
_CAND_HASH = ("content_hash",)
_CAND_ORD = ("ord", "ordinal", "seq", "chunk_no")
_CAND_SRC_PATH = ("source_path", "path", "key")

#: 导出护栏：默认 2000 条、封顶 20000（切片正文体积大，别一次拖全库）
EXPORT_LIMIT_DEFAULT = 2000
EXPORT_LIMIT_MAX = 20000

#: 单次导入条数上限（同上：超了 400，不默默截断）
IMPORT_MAX_ITEMS = 2000

#: 来源类型的合法形态（只作为 WHERE 参数值校验，挡住 `'; drop` 这类噪声）
_SOURCE_TYPE_RE = _IDENT_RE

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


def _connect_kwargs(pg: Any, row_factory: Any) -> Dict[str, Any]:
    """行工厂传给**哪个参数**取决于驱动版本（同名不同义）：

    * psycopg2：`cursor_factory=RealDictCursor`；
    * psycopg3：`row_factory=dict_row` —— 传 `cursor_factory` 会得到
      `dict_row() got an unexpected keyword argument 'row_factory'`（500）。

    🔴 **按模块名分流，不按签名猜**：两个驱动的 `connect()` 签名里两个参数都有，
    猜不出来；而假驱动（测试里塞进 `sys.modules` 的）没有 `__name__` ⇒ 走
    psycopg2 老口径，88/91 上的既有假驱动测试零改动。
    """
    if getattr(pg, "__name__", "") == "psycopg":
        return {"row_factory": row_factory}
    return {"cursor_factory": row_factory}


def _connect() -> Any:
    """打开只读连接（上下文管理器）。

    🔴 **判断顺序硬约束**：先 `_dsn()` 判未配置，再 `_pg()` 载驱动。
    顺序反了的话，未配置 DSN 会被「驱动没装/不完整」盖住，客户端拿到的是
    `kb_no_driver` 而不是 `kb_unconfigured`，定位不了真因。所有取数都走这里，
    顺序只此一处，不再散落在各接口里。
    """
    dsn = _dsn()
    pg, row_factory = _pg()
    return pg.connect(dsn, connect_timeout=5, **_connect_kwargs(pg, row_factory))


def _table_columns(schema: str, tables: Sequence[str]) -> Dict[str, set]:
    """探一次三表的真实列名（进程级缓存，只读 information_schema）。"""
    key = f"{schema}:{','.join(tables)}"
    cached = _COLS_CACHE.get(key)
    if cached is not None:
        return cached
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # 🔴 显式 cast 到 text[]：ANY 的元素类型 PG 没法从上下文推断，
                # 驱动会替我们选 —— psycopg2 把 str 列表适配成 text[]，
                # `sql_identifier = text` 无操作符 ⇒ 探列直接 500。
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = ANY(%s::text[])",
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
    source_type: str = Query("", description="只列某个来源类型（manual/skill/skill_hk）；空=全部"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
) -> KbDocumentList:
    """文档列表（只读）：不输词也能看到数据 —— 这就是知识库页的默认视图。

    切片条数用相关子查询按 doc_id 计数；q 复用检索那套手法（documents 表有 tsvector
    列走 tsquery，否则退标题 ILIKE）。表名/列名仍只认白名单探出来的名字。

    🔴 **出参必须带 `source_type`**：前端按它判「手动域才渲染写按钮」，字段缺席会
    让 skill 文档看起来也能改/删（`skill 归灌入器、UI 先挡一道` 这条设计就废了）。
    探测不到这列时出参给空串 —— 前端按「非 manual 即只读」保守渲染，不会误开放。

    🔴 `source_type` 过滤口径与导出端点**完全一致**（精确 `= %s` + 同一套标识符
    校验），不在这里发明第二种前缀/模糊语义；与 q 同时给时是 AND 关系，
    `count` 走同一条 WHERE ⇒ 计数口径=过滤后总数。
    """
    st = (source_type or "").strip()
    if st and not _SOURCE_TYPE_RE.match(st):
        _reject(400, "bad_source_type",
                f"source_type 只能是标识符（小写字母/数字/下划线）：{source_type}",
                "kb.documents", "list")
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
    d_src = next((_ident(c) for c in _CAND_SRC_TYPE if c in docs), None)

    keyword = (q or "").strip()
    if keyword and d_tsv:
        # 中文语料同检索：simple 分词
        where, params = f"d.{d_tsv} @@ plainto_tsquery('simple', %s)", [keyword]
    elif keyword and d_title:
        where, params = f"d.{d_title} ILIKE %s", [f"%{keyword}%"]
    else:
        # 没词、或表里既没标题列也没索引列 ⇒ 不过滤（宁可多给，不假装筛过）
        where, params = "TRUE", []
    if st and d_src:
        where, params = f"{where} AND d.{d_src} = %s", [*params, st]
    elif st:
        # 要按来源筛但表里没这列 ⇒ 400，不静默给全量（给全量等于假装筛过）
        _reject(400, "kb_schema_unknown",
                f"{config.KB_TABLE_DOCUMENTS} 表未找到 source_type 列，无法按来源过滤",
                "kb.documents", "list")

    src = f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} d"
    chunk_ct = (f"(SELECT COUNT(*) FROM {schema}.{config.KB_TABLE_CHUNKS} c "
                f"WHERE c.{c_doc} = d.{d_id}) AS chunk_count")
    order = "chunk_count DESC, " + (f"d.{d_time} DESC, " if d_time else "") + "doc_id ASC"
    # 标题/时间列探不到就 SELECT '' —— 出参留空串，不假装有值
    sel_title = f"d.{d_title}" if d_title else "''"
    sel_time = f"d.{d_time}" if d_time else "''"
    sel_src = f"d.{d_src}" if d_src else "''"

    sql = (f"SELECT CAST(d.{d_id} AS text) AS doc_id, {sel_title} AS title, "
           f"{sel_time} AS updated_at, {sel_src} AS source_type, "
           f"{chunk_ct} {src} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [*params, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS n {src} WHERE {where}", list(params))
            total = int((cur.fetchone() or {"n": 0})["n"])

    items = [
        KbDocument(doc_id=str(r["doc_id"] or ""), title=str(r["title"] or ""),
                   updated_at=str(r["updated_at"] or ""), chunk_count=int(r["chunk_count"] or 0),
                   # `get` 与本文件「探不到就降级」同款：行里没这根键（老表/别的驱动投影）
                   # 时给空串，出参按 schema 规则省略该键，而不是 KeyError 把整个列表带崩
                   source_type=str(r.get("source_type") or ""))
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


# --------------------------------------------------------------------------
# 写端点（手动文档域：source_type='manual'）
# --------------------------------------------------------------------------

class KbCreateBody(BaseModel):
    """`{"confirm": true, "title": "...", "content": "..."}` —— 新增知识库文档。"""

    confirm: bool = False
    title: str = ""
    content: str = ""


class KbUpdateBody(BaseModel):
    """`{"confirm": true, "title": "...", "content": "..."}` —— 两者都可省，但不许都不给。"""

    confirm: bool = False
    title: Optional[str] = None
    content: Optional[str] = None


class KbDeleteBody(BaseModel):
    """`{"confirm": "<文档标题>"}` —— 与记忆真删同档防手滑。"""

    confirm: str = ""


class KbImportItem(BaseModel):
    """导入的单条：`{"title": "...", "content": "..."}`。

    🔴 `extra="forbid"`：带 `source_type`（或任何多余字段）直接 422 —— 导入
    **只进手动域**，让调用方指定来源类型等于开一条往 skill 域灌脏数据的路。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    content: str = ""


class KbImportBody(BaseModel):
    """`{"items": [...], "dry_run": true}` /
    `{"items": [...], "dry_run": false, "confirm": "<would_add 数>"}`。"""

    items: List[KbImportItem] = []
    dry_run: bool = True
    confirm: Optional[str] = None


@contextmanager
def _write_conn() -> Iterator[Any]:
    """写连接：**成功提交 / 异常回滚 / 结束必关**。

    与读端点的 `_connect()` 不同：读端点靠 GC 回收，写端点每请求一条连接不能攒；
    且「删旧 chunks + 插新 chunks」必须同事务 —— 提交权收在这里，路由里不出现
    `commit()`，路由抛出的 400/404/409 都会走回滚分支（数据一行没动）。
    """
    dsn = _dsn()
    pg, row_factory = _pg()
    conn = pg.connect(dsn, connect_timeout=5, **_connect_kwargs(pg, row_factory))
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:                # noqa: BLE001 —— 回滚失败也得报原始异常
            pass
        raise
    finally:
        conn.close()


def _write_names(schema: str) -> Dict[str, str]:
    """写端点要的表名/列名（复用读端点那套进程级探列缓存 + 标识符白名单）。

    写路径**没有降级余地**：缺哪根列都 500 `kb_schema_unknown`，
    不许「这根列探不到就少写一个字段」—— 那会静默丢内容还报成功。
    """
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    gone = _missing(tabs)
    if gone:
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": f"知识库表不存在：{', '.join(gone)}"})
    docs, chunks = tabs[config.KB_TABLE_DOCUMENTS], tabs[config.KB_TABLE_CHUNKS]
    d, c = config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS
    return {
        "documents": _ident(d), "chunks": _ident(c),
        "doc_id": _pick(docs, _CAND_ID, d, "主键"),
        "src_type": _pick(docs, _CAND_SRC_TYPE, d, "来源类型"),
        "src_key": _pick(docs, _CAND_SRC_KEY, d, "来源键"),
        "title": _pick(docs, _CAND_TITLE, d, "标题"),
        "meta": _pick(docs, _CAND_META, d, "元数据"),
        "hash": _pick(docs, _CAND_HASH, d, "内容哈希"),
        "updated": _pick(docs, _CAND_TIME, d, "更新时间"),
        "chunk_doc": _pick(chunks, _CAND_DOC, c, "外键(文档)"),
        "chunk_ord": _pick(chunks, _CAND_ORD, c, "切片序号"),
        "chunk_text": _pick(chunks, _CAND_TEXT, c, "正文"),
        "chunk_path": _pick(chunks, _CAND_SRC_PATH, c, "来源路径"),
    }


def _doc_id(raw: str, action: str) -> int:
    """路径里的文档 id 必须是整数（PG 的 bigserial）；不是 ⇒ 400，不进 SQL。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        _reject(400, "kb_doc_id_invalid", f"文档 id 必须是整数：{raw}", action, str(raw))
        raise                                    # pragma: no cover —— _reject 已抛


def _is_unique_violation(exc: BaseException) -> bool:
    """驱动报的「唯一约束冲突」（psycopg2/psycopg3 异常类名一致）。"""
    return any(t.__name__ == "UniqueViolation" for t in type(exc).__mro__)


def _manual_only(doc: Dict[str, Any], doc_id: str, action: str) -> None:
    """只有手动域能改/删 —— skill/skill_hk 是灌入器的地盘。

    改了当晚那轮会被**覆盖**（重新灌入）或因技能消失被**清理**掉：
    控制台看着「改成功了」，第二天就没了。所以直接 409 并说明原因，
    别给一个会被静默回滚的假成功。
    """
    if str(doc.get("source_type") or "") != ingest.MANUAL_SOURCE_TYPE:
        _reject(409, "kb_doc_immutable",
                f"只有手动文档（source_type={ingest.MANUAL_SOURCE_TYPE}）能改/删："
                f"{doc_id} 来自 {doc.get('source_type') or '未知来源'}，"
                "由夜间灌入器维护（改动会被覆盖或被死文档清理扫掉）",
                action, doc_id)


@router.post("/api/v1/kb/documents",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_create_document(body: KbCreateBody) -> Dict[str, Any]:
    """新增一篇**手动文档**（`source_type='manual'`）。

    `source_key='manual:<sha256(title+content)[:12]>'` ⇒ 同内容重复提交是
    `unchanged` 而不是新插一条。chunks 与 documents 同一事务落库，
    切块走 `keepsake.kb.ingest.split_chunks`（与灌入器同一个函数）。
    """
    action = "kb.document.create"
    _need_flag(body.confirm, action, "-", '新增知识库文档必须显式传 {"confirm": true}')
    title = (body.title or "").strip()
    content = (body.content or "").strip()
    if not title or not content:
        _reject(400, "empty_document", "title 与 content 都不能为空", action, "-")
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    key = ingest.manual_source_key(title, content)
    with _write_conn() as conn:
        with conn.cursor() as cur:
            status, doc_id, count = ingest.upsert_document(
                cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE, source_key=key,
                title=title, meta={"origin": "console"}, text=content, names=names,
                content_hash=ingest.content_hash(title, content))
    audit.record(action, key, ok=True, reason=f"status={status}; chunks={count}")
    return {"ok": True, "action": action, "id": str(doc_id), "source_key": key,
            "status": status, "chunks_count": count}


@router.post("/api/v1/kb/documents/{id}/update",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_update_document(id: str, body: KbUpdateBody) -> Dict[str, Any]:
    """改手动文档的标题 / 正文（两者至少给一个）。

    🔴 **改正文必须重切 chunks 且同事务**（DELETE 旧 + INSERT 新）：只改
    `documents` 行的话检索会继续召回旧片段 —— 页面显示新正文、检索命中旧正文，
    且不报错，正是最坏的那种坏。
    🟡 **只改标题时不动 `source_key`**：键是文档身份（备份文件、切片
    `source_path` 都按它索引），而改标题时库里没有原文可重算 hash，重算会把
    「内容级幂等」的键对不上。
    """
    action = "kb.document.update"
    _need_flag(body.confirm, action, str(id),
               '改知识库文档必须显式传 {"confirm": true}')
    doc_id = _doc_id(id, action)
    if body.title is None and body.content is None:
        _reject(400, "nothing_to_update", "title 与 content 不能都不传", action, str(id))
    if body.title is not None and not body.title.strip():
        _reject(400, "empty_document", "title 不能为空（要清空请删掉重写）", action, str(id))
    if body.content is not None and not body.content.strip():
        _reject(400, "empty_document", "content 不能为空（要清空请删掉重写）", action, str(id))
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    try:
        with _write_conn() as conn:
            with conn.cursor() as cur:
                doc = ingest.load_document(cur, schema, doc_id, names=names)
                if doc is None:
                    _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
                _manual_only(doc, str(id), action)
                title = body.title.strip() if body.title is not None else str(doc["title"])
                if body.content is None:
                    ingest.update_document(cur, schema, doc_id, title=title,
                                           source_key=str(doc["source_key"]), names=names)
                    count = ingest.chunks_count(cur, schema, doc_id, names=names)
                    key = str(doc["source_key"])
                else:
                    content = body.content.strip()
                    key = ingest.manual_source_key(title, content)
                    ingest.update_document(cur, schema, doc_id, title=title, source_key=key,
                                           content_hash=ingest.content_hash(title, content),
                                           names=names)
                    count = ingest.replace_chunks(cur, schema, doc_id, content,
                                                  names=names, source_path=key)
    except HTTPException:
        raise
    except Exception as e:                    # noqa: BLE001 —— 唯一约束撞车 → 409
        if _is_unique_violation(e):
            _reject(409, "kb_source_key_taken",
                    f"已有文档占用了这个内容键（同名同内容已存在）：{id}", action, str(id))
        raise
    audit.record(action, str(id), ok=True, reason=f"source_key={key}; chunks={count}")
    return {"ok": True, "action": action, "id": str(id), "source_key": key,
            "title": title, "chunks_count": count}


@router.post("/api/v1/kb/documents/{id}/delete",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_delete_document(id: str, body: KbDeleteBody) -> Dict[str, Any]:
    """**真删**一篇手动文档（chunks + documents，同一事务）。

    与记忆真删同档防手滑：必须手输**文档标题全文**。执行序固定：
    取行 → 校验来源类型 → **备份全文与全部切片（失败则不删）** → 删 → 审计。
    备份文件是唯一的回退路径（正文只存在于切片里，删了就真没了）。
    """
    action = "kb.document.delete"
    doc_id = _doc_id(id, action)
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    with _write_conn() as conn:
        with conn.cursor() as cur:
            doc = ingest.load_document(cur, schema, doc_id, names=names)
            if doc is None:
                _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
            _manual_only(doc, str(id), action)
            _need_text(body.confirm, str(doc["title"]), action, str(id), "文档标题")
            snapshot = {"document": dict(doc),
                        "chunks": ingest.document_chunks(cur, schema, doc_id, names=names)}
            backup = audit.backup_document(doc_id, snapshot)
            deleted = ingest.delete_document(cur, schema, doc_id, names=names)
            if deleted != 1:                   # pragma: no cover —— 取到行又删不掉＝并发
                _reject(500, "kb_delete_failed",
                        f"删除了 {deleted} 篇（应为 1），文档可能已被并发删掉：{id}",
                        action, str(id))
    audit.record(action, str(id), ok=True, reason=f"deleted=1; backup={backup}")
    return {"ok": True, "action": action, "id": str(id), "deleted": True,
            "source_key": str(doc["source_key"]), "backup": backup}


def _export_lines(schema: str, names: Dict[str, str], source_type: str,
                  limit: int) -> Iterator[bytes]:
    """把导出内容逐篇吐成 JSONL 行（流式：不在内存里拼整个文件）。"""
    where = "TRUE"
    params: List[Any] = []
    if source_type:
        where, params = f"{names['src_type']} = %s", [source_type]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT CAST({names['doc_id']} AS text) AS id, {names['src_type']} AS source_type, "
                f"{names['src_key']} AS source_key, {names['title']} AS title, "
                f"{names['meta']} AS meta FROM {schema}.{names['documents']} "
                f"WHERE {where} ORDER BY {names['doc_id']} ASC LIMIT %s",
                [*params, limit])
            docs = cur.fetchall()
            by_doc: Dict[str, List[str]] = {}
            # 🔴 `ids` 必须是 **int** 列表，不是 str：上面 SELECT 出来的是
            # `CAST(id AS text)`，直接进未定型的 ANY 会被 psycopg2 适配成 text[]，
            # 而 `chunks.document_id` 是 bigint ⇒ `operator does not exist: bigint = text`
            # （psycopg3 传的是未定型字面量、让 PG 自己推 bigint[]，所以只在 psycopg2 上炸 ——
            #  判据不能依赖驱动的隐式适配，这里把两侧都钉死）。
            # 口径：`by_doc` 的键仍是 str（出参拼 JSONL 用），只有进 SQL 的那份转 int。
            ids = [int(r["id"]) for r in docs]
            if ids:
                cur.execute(
                    f"SELECT CAST({names['chunk_doc']} AS text) AS did, "
                    f"{names['chunk_text']} AS chunk_text FROM {schema}.{names['chunks']} "
                    f"WHERE {names['chunk_doc']} = ANY(%s::bigint[]) "
                    f"ORDER BY {names['chunk_doc']} ASC, "
                    f"{names['chunk_ord']} ASC", [ids])
                for row in cur.fetchall():
                    by_doc.setdefault(str(row["did"]), []).append(str(row["chunk_text"] or ""))
    for r in docs:
        meta = r["meta"]
        yield (json.dumps({
            "id": str(r["id"]), "source_type": str(r["source_type"] or ""),
            "source_key": str(r["source_key"] or ""), "title": str(r["title"] or ""),
            # meta 是 jsonb：psycopg3 已解析成对象，psycopg2 给字符串，两种都能出参
            "meta": _jsonable(meta),
            "chunks": by_doc.get(str(r["id"]), []),
        }, ensure_ascii=False) + "\n").encode("utf-8")


def _jsonable(meta: Any) -> Any:
    """jsonb 出参归一：字符串就 parse，解析不了原样给字符串（绝不静默丢字段）。"""
    if isinstance(meta, (dict, list, type(None))):
        return meta
    try:
        return json.loads(str(meta))
    except (TypeError, ValueError):
        return str(meta)


@router.get("/api/v1/kb/export", dependencies=[Depends(require_session)])
def kb_export(
    source_type: str = Query("", description="只导某个来源类型（manual/skill/...）；空=全部"),
    limit: int = Query(EXPORT_LIMIT_DEFAULT, ge=1, le=EXPORT_LIMIT_MAX,
                       description="单次导出篇数上限；超了调大或按 source_type 分批"),
) -> StreamingResponse:
    """导出知识库文档（JSONL，每行一篇，带全部切片正文）。**只读端点**：不挂 `write_guard`。

    与记忆导出同款护栏：`limit` 默认 2000、封顶 20000，超了直接 422/400
    （参数越界）而不是截断 —— 被截断的文件是「看起来完整」的假数据。
    """
    st = (source_type or "").strip()
    if st and not _SOURCE_TYPE_RE.match(st):
        _reject(400, "bad_source_type",
                f"source_type 只能是标识符（小写字母/数字/下划线）：{source_type}",
                "kb.export", "export")
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    return StreamingResponse(_export_lines(schema, names, st, limit),
                             media_type="application/x-ndjson; charset=utf-8",
                             headers={"Cache-Control": "no-store"})


@router.post("/api/v1/kb/import",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_import(body: KbImportBody) -> Dict[str, Any]:
    """批量导入知识库文档（JSON body）。**默认 dry-run，零写入**。

    实跑必须带 `confirm="<would_add 数>"`（先跑一次 dry-run 拿到数再手输，
    与记忆导入同款）。去重是**内容级**：`source_key=sha256(title+content)`，
    同键同 hash ⇒ `unchanged`，不重复入库。
    🟡 **只进手动域**：条目里的 `source_type` 一律 422（见 `KbImportItem`）。
    """
    action, target = "kb.import", "batch"
    if len(body.items) > IMPORT_MAX_ITEMS:
        _reject(400, "import_too_large",
                f"一次最多导入 {IMPORT_MAX_ITEMS} 篇，本次 {len(body.items)} 篇：请分批导入",
                action, target)
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    counts = {"total": len(body.items), "would_add": 0, "unchanged": 0,
              "rejected_empty": 0}
    plan: List[Tuple[str, str, str, str]] = []      # (title, content, source_key, hash)
    written: List[str] = []
    with _write_conn() as conn:
        with conn.cursor() as cur:
            for item in body.items:
                title = (item.title or "").strip()
                content = (item.content or "").strip()
                if not title or not content:
                    counts["rejected_empty"] += 1
                    continue
                key = ingest.manual_source_key(title, content)
                h = ingest.content_hash(title, content)
                if ingest.lookup_content_hash(
                        cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE,
                        source_key=key, names=names) == h:
                    counts["unchanged"] += 1
                    continue
                plan.append((title, content, key, h))
            counts["would_add"] = len(plan)
            if not body.dry_run:
                _need_text(body.confirm or "", str(counts["would_add"]), action, target,
                           f"本次将新增 {counts['would_add']} 篇（先跑 dry-run 拿 confirm_required）")
                for title, content, key, h in plan:
                    status, doc_id, _n = ingest.upsert_document(
                        cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE,
                        source_key=key, title=title, meta={"origin": "console-import"},
                        text=content, names=names, content_hash=h)
                    counts[status] = counts.get(status, 0) + 1
                    written.append(str(doc_id))
    # dry-run 走完上面那条连接里只有 SELECT（提交一个只读事务不产生写入）
    audit.record(action, target, ok=True,
                 reason=("dry_run; " if body.dry_run else "done; ")
                 + f"total={counts['total']}; would_add={counts['would_add']}; "
                   f"unchanged={counts['unchanged']}; rejected_empty={counts['rejected_empty']}")
    return {"ok": True, "dry_run": body.dry_run, "action": action,
            "ids": written, "confirm_required": str(counts["would_add"]), **counts}


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
