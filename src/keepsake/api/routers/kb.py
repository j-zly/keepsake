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

**来源 vs 内容类型（两根列，别混）**：`source_type` 分「谁维护」（manual=控制台
可改删；skill/skill_hk=灌入器只读）；`content_type` 分「这是个什么东西」
（skill 技能 / note 手记 / article 长文 / link 链接卡片 / image 图文占位）。
分类的作用是**按类型切块**（link 单块、其余逐字走 `split_chunks`）与**按类型过滤**，
不改任何既有的域隔离与幂等判据。类型创建后不可变（双挡：前端不给入口 +
后端带异值 400）。`image` 本期只占枚举位：创建端点 400（依赖向量路，后置）。

**自由标签（tags）是第三个维度，不是第二个类型**：content_type 是封闭枚举（写面
校验白名单），标签是开放维度（一篇可挂多个、随时可改、只影响筛选）。标签**不进**
`source_key`/`content_hash`（进了就等于「改个标签变成换一篇文档」），也**不碰**切块与
来源域；skill 文档标签恒空 —— 灌入器一个字都不往那根列写，界面也不给它开打标入口
（沿用 isManual 那套 409 纪律）。审核/状态语义是批2的事，本期不碰。
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
from pydantic import BaseModel, ConfigDict, Field

from keepsake.kb import ingest

from .. import audit, config
from ..deps import require_session
from ..schemas import KbHit, KbSearchResult, KbSummary, KbTagCount, KbTagCounts, KbTypeCounts
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

#: 内容类型列（documents）。**可选探测**：老库未迁移时读路径 SELECT '' 降级
#: （出参不带这个键），真要按类型筛/真要写类型时由 `_need_ctype` 给出人话 500。
_CAND_CTYPE = ("content_type",)

#: 老库未迁移 content_type 列时的修复 SQL —— **报给人看的，不自己跑**
#: （生产迁移由主脑执行；代码擅自 ALTER 生产表不是本项目的活）。
_CTYPE_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_type text NOT NULL DEFAULT 'note'; "
    "UPDATE documents SET content_type='skill' WHERE source_type IN ('skill','skill_hk'); "
    "CREATE INDEX IF NOT EXISTS idx_documents_content_type ON documents(content_type);"
)

#: 链接卡片的 URL 形态：只放行 http/https，别的（file://、javascript:）一律 400。
#: 🔴 **不抓取网页**（SSRF 与外网依赖都不要，抓取是接入方/助手的事）—— 这里只存字符串。
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# --------------------------------------------------------------------------
# 自由标签（documents.tags text[] + GIN）
# --------------------------------------------------------------------------
# 自由维度：content_type 是封闭枚举（五个），自由标签是开放维度（量化/部署/投资…），
# 一篇可挂多个。与 content_type **不是一回事**：不改切块、不改来源域、不改幂等判据，
# 只多一根可筛选的列（`'tag' = ANY(tags)` 走 GIN 索引，与 Dify metadata filtering
# 同一套做法）。审核队列是批2另单，本期不碰。

#: 标签列（documents）。兼容纪律与 content_type **一字同款**：读路径缺列降级成
#: 空数组（出参不带 tags 键，不假装每篇都打了标），写路径由 `_need_tags` 给人话 500。
_CAND_TAGS = ("tags",)

#: 老库未迁移 tags 列时的修复 SQL —— **报给人看的，不自己跑**（与 ctype 同款）
_TAGS_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}'; "
    "CREATE INDEX IF NOT EXISTS idx_documents_tags_gin ON documents USING gin(tags);"
)

#: 标签净化后的硬上限（前端即时提示引的就是这两个数 —— 一处定、两处抄，勿各自写死）
TAG_MAX_ITEMS = 12
TAG_MAX_LEN = 32

#: 控制字符（含 `\n` `\r` `\t` `\0`）—— 一律 400：标签要渲成 chip、还要进导出文件，
#: 串进换行/制表能把版面和 JSONL 行一起搞坏。
_TAG_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

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
    d_ctype = next((_ident(c) for c in _CAND_CTYPE if c in docs), None)
    select_ctype = f"COALESCE(d.{d_ctype}, '')" if d_ctype else "''"
    d_tags = next((_ident(c) for c in _CAND_TAGS if c in docs), None)
    # 老库没有 tags 列 ⇒ 投影**空数组**（不是 NULL、不是缺键）：出参据此省掉 tags 键，
    # 前端按「没打标」渲染，而不是把 NULL 当成一个标签。
    select_tags = f"COALESCE(d.{d_tags}, '{{}}'::text[])" if d_tags else "'{}'::text[]"

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
        f"{select_ctype} AS content_type, {select_tags} AS tags, "
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
              # 老表没这列时投影是 '' ⇒ 出参按 schema 规则省略该键（与 source_type 同款）
              content_type=str(r.get("content_type") or ""),
              tags=_tags_out(r.get("tags")),
              rank=round(float(r["score"] or 0.0), 6))
        for r in rows
    ]
    return KbSearchResult(q=q, items=items, count=len(items), page=page,
                          page_size=page_size, mode=mode)


@router.get("/api/v1/kb/type-counts", response_model=KbTypeCounts,
            dependencies=[Depends(require_session)])
def kb_type_counts() -> KbTypeCounts:
    """各内容类型的文档数（界面 Tab 的计数来源；只读，一条 GROUP BY）。

    🔴 **计数绝不写死在前端**：写死就等于库一变数字就错，而且没人会发现。
    老库未迁移 content_type 列 ⇒ 返回空字典（前端按「无计数」渲染，不编数字）。
    """
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS,))
    d_ct = next((_ident(c) for c in _CAND_CTYPE if c in tabs[config.KB_TABLE_DOCUMENTS]),
                None)
    if not d_ct:
        return KbTypeCounts(counts={})
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {d_ct} AS ct, COUNT(*) AS n "
                        f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} GROUP BY {d_ct}")
            rows = cur.fetchall()
    return KbTypeCounts(counts={str(r["ct"] or ingest.DEFAULT_CONTENT_TYPE): int(r["n"] or 0)
                                for r in rows})


@router.get("/api/v1/kb/tags", response_model=KbTagCounts,
            dependencies=[Depends(require_session)])
def kb_tags() -> KbTagCounts:
    """全部标签 + 各有多少篇文档挂着（界面筛选 chip 行的唯一来源，只读）。

    🔴 **计数绝不写死在前端**（与 `/kb/type-counts` 同款纪律）：写死就等于库一变
    数字就错，而且没人会发现。老库未迁移 tags 列 ⇒ 返回空列表（前端整行隐藏，
    不编一个假 chip）。

    🟡 **口径 = 全部文档**（含 skill 域）：skill 文档本期恒空标签，不影响结果；
    真要把 skill 排除掉得在这里加 WHERE，那是另一件事（用户能看见 chip 行里
    出现「只有 1 篇」的标签时，至少知道那篇是什么）。
    计数用 `COUNT(DISTINCT d.id)`：同一篇里同一个标签重复出现（存量脏数据）也只算一篇。
    """
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS,))
    d_tags = next((_ident(c) for c in _CAND_TAGS
                   if c in tabs[config.KB_TABLE_DOCUMENTS]), None)
    if not d_tags:
        return KbTagCounts(items=[])
    d_id = _pick(tabs[config.KB_TABLE_DOCUMENTS], _CAND_ID,
                 config.KB_TABLE_DOCUMENTS, "主键")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT t AS tag, COUNT(DISTINCT d.{d_id}) AS n "
                        f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} d, "
                        f"unnest(d.{d_tags}) AS t "
                        f"GROUP BY t ORDER BY n DESC, t ASC")
            rows = cur.fetchall()
    return KbTagCounts(items=[KbTagCount(tag=str(r["tag"] or ""), count=int(r["n"] or 0))
                              for r in rows if str(r["tag"] or "").strip()])


@router.get("/api/v1/kb/documents", response_model=KbDocumentList,
            dependencies=[Depends(require_session)])
def kb_documents(
    q: Optional[str] = Query(None, description="标题模糊词，可选；不给就全量浏览"),
    source_type: str = Query("", description="只列某个来源类型（manual/skill/skill_hk）；空=全部"),
    # 🟡 缺省值写裸串而不是 `Query("")`：有护栏测试是**直接调这个函数**（不经过
    # FastAPI）来钉 WHERE 的拼装与参数顺序，`Query` 对象会 `.strip()` 炸掉。
    # 标量类型 + 裸默认值 FastAPI 仍按 query 参数解析（与 page/page_size 之外的旧写法同款）。
    content_type: str = "",
    # 🔴 同上（`Query` 对象会让直接调本函数的护栏测试炸）：裸串默认值 + 参数化取值
    tag: str = "",
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

    🆕 `content_type` 过滤同理（精确 `= %s`），但**值走枚举白名单**而不是标识符
    正则：类型是业务枚举（skill/note/article/link/image），正则放行 `'; drop`
    之外的一切噪声串，枚举只放行真类型。老库未迁移（没这列）而调用方要按类型筛
    ⇒ 400 `kb_schema_unknown`，不静默给全量（给全量等于假装筛过）。

    🆕 `tag` 是**自由标签**的精确过滤（`'量化' = ANY(d.tags)`，值走参数不拼串 ——
    拼串就等于把库里内容当代码解释）。与 q / source_type / content_type 取 AND。
    值不查枚举（标签是开放维度，没有白名单可查），所以只 `strip()` 后原样当参数；
    老库没有 tags 列而调用方要按标签筛 ⇒ 400（筛不了就说筛不了）。
    """
    st = (source_type or "").strip()
    if st and not _SOURCE_TYPE_RE.match(st):
        _reject(400, "bad_source_type",
                f"source_type 只能是标识符（小写字母/数字/下划线）：{source_type}",
                "kb.documents", "list")
    ct = (content_type or "").strip()
    if ct and ct not in ingest.CONTENT_TYPES:
        _reject(400, "bad_content_type",
                f"content_type 只能是：{', '.join(ingest.CONTENT_TYPES)}；收到 {content_type}",
                "kb.documents", "list")
    tg = (tag or "").strip()
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
    d_ct = next((_ident(c) for c in _CAND_CTYPE if c in docs), None)
    d_tags = next((_ident(c) for c in _CAND_TAGS if c in docs), None)

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
    if ct and d_ct:
        where, params = f"{where} AND d.{d_ct} = %s", [*params, ct]
    elif ct:
        # 同上：老库未迁移 content_type 列时，筛不了就说筛不了
        _reject(400, "kb_schema_unknown",
                f"{config.KB_TABLE_DOCUMENTS} 表未找到 content_type 列，无法按内容类型过滤"
                f"（请先执行迁移：{_CTYPE_MIGRATION_HINT}）",
                "kb.documents", "list")
    if tg:
        # 🔴 参数化：值走 `%s::text`，不拼进 SQL 文本（标签来自用户输入的自由文本）
        if d_tags:
            where, params = f"{where} AND %s::text = ANY(d.{d_tags})", [*params, tg]
        else:
            _reject(400, "kb_schema_unknown",
                    f"{config.KB_TABLE_DOCUMENTS} 表未找到 tags 列，无法按标签过滤"
                    f"（请先执行迁移：{_TAGS_MIGRATION_HINT}）",
                    "kb.documents", "list")

    src = f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} d"
    chunk_ct = (f"(SELECT COUNT(*) FROM {schema}.{config.KB_TABLE_CHUNKS} c "
                f"WHERE c.{c_doc} = d.{d_id}) AS chunk_count")
    order = "chunk_count DESC, " + (f"d.{d_time} DESC, " if d_time else "") + "doc_id ASC"
    # 标题/时间列探不到就 SELECT '' —— 出参留空串，不假装有值
    sel_title = f"d.{d_title}" if d_title else "''"
    sel_time = f"d.{d_time}" if d_time else "''"
    sel_src = f"d.{d_src}" if d_src else "''"
    sel_ct = f"d.{d_ct}" if d_ct else "''"
    # 老库没 tags 列 ⇒ 投影空数组（出参按 schema 规则省略 tags 键，不假装每篇都打了标）
    sel_tags = f"d.{d_tags}" if d_tags else "'{}'::text[]"

    sql = (f"SELECT CAST(d.{d_id} AS text) AS doc_id, {sel_title} AS title, "
           f"{sel_time} AS updated_at, {sel_src} AS source_type, "
           f"{sel_ct} AS content_type, {sel_tags} AS tags, "
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
                   source_type=str(r.get("source_type") or ""),
                   # 同款降级：老表没这列时行里没这根键 ⇒ 空串，出参省略该键，
                   # 前端按 note 缺省渲染（不假装每篇都是技能）
                   content_type=str(r.get("content_type") or ""),
                   # 同款降级：老表没这列 ⇒ 行里给的是空数组 ⇒ 出参省略该键
                   tags=_tags_out(r.get("tags")))
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
    """`{"confirm": true, "title": "...", "content": "..."}` —— 新增知识库文档。

    🆕 `content_type`（缺省 note）与 `url`（**仅 link 需要**）：内容类型创建后
    不可变（见 `KbUpdateBody`），link 走「标题 + URL + 摘要」单块卡片，正文不切。
    """

    confirm: bool = False
    title: str = ""
    content: str = ""
    content_type: str = Field("", description="内容类型；空=note（手记）")
    url: str = Field("", description="链接地址；仅 content_type=link 时必填")
    tags: List[str] = Field(default_factory=list,
                             description=f"自由标签（可多个）；净化规则见 `_clean_tags`，"
                                         f"最多 {TAG_MAX_ITEMS} 个、单项 ≤{TAG_MAX_LEN} 字")


class KbUpdateBody(BaseModel):
    """`{"confirm": true, "title": "...", "content": "..."}` —— 两者都可省，但不许都不给。

    🆕 `content_type`：🔴 **只能与文档自身类型相同**（创建后不可变），不同值 400 ——
    类型变了等于换了一篇内容（切块策略、来源域、灌入器归属都跟着变），静默改会让
    「这篇是链接」和「切片被当长文切过」并存。`url` 仅 link 文档用（改 URL 仍单块重存）。

    🆕 `tags`：🔴 **给了就整体替换，不是追加**（`None` = 这次不动标签）。
    追加语义会让「删掉一个标签」变成删不掉（前端每次提交都是全量 chip），
    且库里旧标签会越攒越多、筛选 chip 行最后长到没法看。
    """

    confirm: bool = False
    title: Optional[str] = None
    content: Optional[str] = None
    content_type: Optional[str] = None
    url: Optional[str] = None
    tags: Optional[List[str]] = None


class KbDeleteBody(BaseModel):
    """`{"confirm": "<文档标题>"}` —— 与记忆真删同档防手滑。"""

    confirm: str = ""


class KbImportItem(BaseModel):
    """导入的单条：`{"title": "...", "content": "..."}`（可带 `"type": "note|article|link"`）。

    🔴 `extra="forbid"`：带 `source_type`（或任何多余字段）直接 422 —— 导入
    **只进手动域**，让调用方指定来源类型等于开一条往 skill 域灌脏数据的路。

    🆕 `type` 就是 `content_type`（缺省 note，命名短一点贴合 JSONL 手写场景），
    `url` 仅 `type="link"` 时用；取值口径与创建端点同一份枚举校验。
    `tags` 可选，走与创建端点**同一个** `_clean_tags` 净化函数（别处再写一份口径
    就会漂：导入能塞进界面打不出来的标签）。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    content: str = ""
    type: str = Field("", description="内容类型；空=note（手记）。与 content_type 同义，取短名")
    url: str = Field("", description="链接地址；仅 type=link 时必填")
    tags: List[str] = Field(default_factory=list, description="自由标签（可多个），可省")


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

    🆕 `ctype`（content_type）与 `tags`（自由标签）是**仅有的两个例外：探不到就给
    空串**。老库未迁移时，不写这两样的端点（导出、真删）仍能跑；真要写类型的/
    标签的端点（新增/改/导入）由 `_need_ctype()` / `_need_tags()` 给出**人话 500**
    （提示跑迁移），而不是让 PG 抛 `column "content_type" does not exist` 的裸 500。
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
        "ctype": next((_ident(x) for x in _CAND_CTYPE if x in docs), ""),
        "tags": next((_ident(x) for x in _CAND_TAGS if x in docs), ""),
        "title": _pick(docs, _CAND_TITLE, d, "标题"),
        "meta": _pick(docs, _CAND_META, d, "元数据"),
        "hash": _pick(docs, _CAND_HASH, d, "内容哈希"),
        "updated": _pick(docs, _CAND_TIME, d, "更新时间"),
        "chunk_doc": _pick(chunks, _CAND_DOC, c, "外键(文档)"),
        "chunk_ord": _pick(chunks, _CAND_ORD, c, "切片序号"),
        "chunk_text": _pick(chunks, _CAND_TEXT, c, "正文"),
        "chunk_path": _pick(chunks, _CAND_SRC_PATH, c, "来源路径"),
    }


def _need_ctype(names: Dict[str, str], action: str, target: str) -> str:
    """写 content_type 前必须确认这根列在（老库未迁移 ⇒ 人话报错，不裸崩）。"""
    if not names.get("ctype"):
        _reject(500, "kb_schema_unknown",
                f"{config.KB_TABLE_DOCUMENTS} 表未找到 content_type 列，无法写入内容类型；"
                f"请先执行迁移：{_CTYPE_MIGRATION_HINT}",
                action, target)
    return names["ctype"]


def _check_content_type(raw: str, action: str, target: str) -> str:
    """创建端点的类型校验：枚举内 + 界面可建（skill 归灌入器、image 尚未开放）。"""
    ct = (raw or "").strip() or ingest.DEFAULT_CONTENT_TYPE
    if ct not in ingest.CONTENT_TYPES:
        _reject(400, "bad_content_type",
                f"content_type 只能是：{', '.join(ingest.CONTENT_TYPES)}；收到 {raw}",
                action, target)
    if ct == ingest.CONTENT_TYPE_IMAGE:
        _reject(400, "image_not_open", "image 类型暂未开放", action, target)
    if ct == ingest.CONTENT_TYPE_SKILL:
        _reject(400, "content_type_immutable",
                "skill 类型只能由灌入器写入，控制台不能创建（改类型请删掉重建）",
                action, target)
    return ct


def _doc_basis(title: str, content: str, ctype: str, url: str) -> str:
    """`source_key` 与 `content_hash` 的**判据文本** = 真正存进切片的那一串。

    🔴 link 必须用组合文本而不是 `content`（摘要）：否则一篇 note（标题+摘要甲）
    与一篇 link（标题+摘要甲、地址不同）会算出**同一个键**，`ON CONFLICT` 撞上就把
    已有那篇的 content_type 悄悄改成 link —— 正好是「类型创建后不可变」要防的那件事。
    键跟内容走（而不是跟类型走）也让「同内容重复提交 = unchanged」的老判据一字不变。
    """
    if ctype == ingest.CONTENT_TYPE_LINK:
        return ingest.link_body(title, url, content)
    return content or ""


def _check_url(url: str, action: str, target: str) -> str:
    """链接卡片的 URL：必填 + 必须 `^https?://`。**不抓取网页**（SSRF 与外网依赖都不要）。"""
    u = (url or "").strip()
    if not u:
        _reject(400, "url_required", "content_type=link 时 url 必填（http:// 或 https:// 开头）",
                action, target)
    if not _URL_RE.match(u):
        _reject(400, "bad_url", f"url 必须以 http:// 或 https:// 开头：{u}", action, target)
    return u


def _clean_tags(raw: Any, action: str, target: str) -> List[str]:
    """自由标签的**唯一净化入口**（新增 / 改 / 导入三处共用，一处一个口径）。

    规则按序执行，违规一律 400 人话（**不静默截断**：用户打了 13 个标却被悄悄
    砍掉一个，回头按那个标筛会以为「标签坏了」）：

    1. 非列表（None / 标量）⇒ 当作没给；元素必须都是字符串（非字符串 400）；
    2. 逐项 `strip()`，空串直接丢（`["量化", ""]` 与 `["量化"]` 等价）；
    3. 含控制字符（含换行 / 制表 / 回车）⇒ 400 `bad_tag`；
    4. 单项 > `TAG_MAX_LEN` 字符 ⇒ 400 `bad_tag`（超长标签在 chip 行里必然折行）；
    5. 去重**保序**（先出现的赢 ⇒ 卡片上的顺序与用户输入一致，可预期）；
    6. 去重后 > `TAG_MAX_ITEMS` 项 ⇒ 400 `too_many_tags`。

    🟡 **长度按字符数**（中文按字算，不是字节）：限的是「chip 行看不看得下」。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        _reject(400, "bad_tag", f"tags 必须是字符串数组：收到 {type(raw).__name__}",
                action, target)
    out: List[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            _reject(400, "bad_tag", f"tags 里每一项都必须是字符串：收到 {type(item).__name__}",
                    action, target)
        t = item.strip()
        if not t:
            continue                       # 空串不是「一个空标签」，直接丢
        if _TAG_CTRL_RE.search(t):
            _reject(400, "bad_tag",
                    f"标签不许含换行/制表等控制字符：{t[:40]!r}", action, target)
        if len(t) > TAG_MAX_LEN:
            _reject(400, "bad_tag",
                    f"单个标签最多 {TAG_MAX_LEN} 字（收到 {len(t)} 字）：{t[:40]!r}",
                    action, target)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if len(out) > TAG_MAX_ITEMS:
        _reject(400, "too_many_tags",
                f"每篇最多 {TAG_MAX_ITEMS} 个标签，本次 {len(out)} 个：请先摘掉几个",
                action, target)
    return out


def _tags_out(raw: Any) -> List[str]:
    """出参归一：库里/驱动给回来的 text[] 一律成 `list[str]`。

    NULL（LEFT JOIN 没匹上文档）、`'{}'`、PG 文本形态 `'{a,b}'`（假驱动/别的客户端
    可能给这种串）都收成空数组或拆开的列表 —— **绝不把内部形态漏给前端**。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        body = raw.strip().lstrip("{").rstrip("}").strip()
        return [x.strip().strip('"') for x in body.split(",") if x.strip()] if body else []
    try:
        return [str(x) for x in raw]
    except TypeError:                                # 既不是 str 也不是序列 ⇒ 当没打标
        return []


def _need_tags(names: Dict[str, str], action: str, target: str) -> str:
    """写 tags 前必须确认这根列在（老库未迁移 ⇒ 人话 500 带迁移 SQL，不裸崩）。"""
    if not names.get("tags"):
        _reject(500, "kb_schema_unknown",
                f"{config.KB_TABLE_DOCUMENTS} 表未找到 tags 列，无法写入标签；"
                f"请先执行迁移：{_TAGS_MIGRATION_HINT}",
                action, target)
    return names["tags"]


def _check_type_unchanged(want: Optional[str], actual: str, action: str, target: str) -> None:
    """🔴 类型创建后不可变：请求带了且与库里不同 ⇒ 400（不给「静默换类型」的口子）。

    枚举外的值先按 `bad_content_type` 拒（口径与创建端点同款），别混成
    「不可变」—— 那是两回事：一个是类型不存在，一个是不许改。
    """
    if want is None or not str(want).strip():
        return
    if str(want).strip() not in ingest.CONTENT_TYPES:
        _reject(400, "bad_content_type",
                f"content_type 只能是：{', '.join(ingest.CONTENT_TYPES)}；收到 {want}",
                action, target)
    if str(want).strip() != actual:
        _reject(400, "content_type_immutable",
                f"内容类型创建后不可变（本篇是 {actual}，请求要改成 {want}）；"
                "要换类型请删掉重建", action, target)


def _doc_meta_field(cur: Any, schema: str, doc_id: Any, names: Dict[str, str],
                    key: str) -> str:
    """取 link 文档 meta 里的某个键（`url` / `summary`）。

    🔴 **改一项不能把另一项丢了**：只换 URL 时摘要得从库里原样取回，
    否则组合文本第三行整条消失（卡片只剩标题和地址，摘要找不回来了）。
    `key` 走参数（`->>%s`），不拼进 SQL 文本。
    """
    cur.execute(f"SELECT {names['meta']}->>%s AS v FROM {schema}.{names['documents']} "
                f"WHERE {names['doc_id']} = %s", [key, doc_id])
    row = cur.fetchone()
    return str(row["v"] or "") if row else ""


def _doc_set_link_meta(cur: Any, schema: str, doc_id: Any, url: str, summary: str,
                       names: Dict[str, str]) -> None:
    """改 link 文档时同步 `meta` 里的 url/summary（正文是组合文本，meta 才是可寻址的真源）。

    🔴 参数必须写 `%s::text`：PG 推不出 `jsonb_build_object(..., %s)` 里那个未定型
    参数的类型（`could not determine data type of parameter $1`）。
    """
    cur.execute(f"UPDATE {schema}.{names['documents']} SET {names['meta']} = "
                f"jsonb_build_object('origin','console','url',%s::text,'summary',%s::text) "
                f"WHERE {names['doc_id']} = %s", [url, summary, doc_id])


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
    切块走 `keepsake.kb.ingest.chunk_document`（与灌入器同一个函数，按类型注册表分派）。

    🆕 **按 `content_type` 分派**：
    * `note`/`article`：`content` 就是正文，逐字走 `split_chunks`（与上期一致）；
    * `link`：`content` 视为**摘要**（可空），`url` 必填且 `^https?://`；入库
      `meta={url, summary}`，正文列存单块 `标题\\nURL\\n摘要`（**不抓取网页** ——
      SSRF 与外网依赖都不要，抓取是接入方/助手的事）；
    * `image`：400（枚举占位，依赖向量路，后置）；`skill`：400（仅灌入器能建）。

    🆕 `tags`（自由标签，可多个）：净化走 `_clean_tags`（strip/去空/去重保序/限长/
    限数/拒控制字符，违规 400），**不参与** source_key 与 content_hash ——
    标签是可随时改的检索维度，进了幂等键就等于「改个标签变成换一篇文档」。
    """
    action = "kb.document.create"
    _need_flag(body.confirm, action, "-", '新增知识库文档必须显式传 {"confirm": true}')
    title = (body.title or "").strip()
    content = (body.content or "").strip()
    ctype = _check_content_type(body.content_type, action, title or "-")
    is_link = ctype == ingest.CONTENT_TYPE_LINK
    url = _check_url(body.url, action, title or "-") if is_link else ""
    tags = _clean_tags(body.tags, action, title or "-")
    # link 的摘要可空；其余类型 content 必填（与上期 empty_document 口径一字不变）
    if not title or (not is_link and not content):
        _reject(400, "empty_document", "title 与 content 都不能为空", action, "-")
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    _need_ctype(names, action, title or "-")
    _need_tags(names, action, title or "-")
    basis = _doc_basis(title, content, ctype, url)
    key = ingest.manual_source_key(title, basis)
    meta = {"origin": "console"}
    if is_link:
        meta["url"] = url
        meta["summary"] = content
    with _write_conn() as conn:
        with conn.cursor() as cur:
            status, doc_id, count = ingest.upsert_document(
                cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE, source_key=key,
                title=title, meta=meta, text=content, names=names,
                content_hash=ingest.content_hash(title, basis),
                content_type=ctype, url=url, tags=tags)
    audit.record(action, key, ok=True,
                reason=f"status={status}; chunks={count}; content_type={ctype}")
    return {"ok": True, "action": action, "id": str(doc_id), "source_key": key,
            "status": status, "chunks_count": count, "content_type": ctype}


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

    🆕 **按文档自身类型重切**（`content_type` 取库里那行，不取请求里的）：
    link 改摘要/URL 仍存**单块** `标题\\nURL\\n摘要`，别被当长文切碎；
    请求里带了且与库里的不同 ⇒ 400（类型创建后不可变，前后端双挡）。

    🆕 `tags`：**不带 = 不动**（只改标题/正文时零影响）；带了就是**整体替换**
    （给空数组即「清空所有标签」）。净化与新增端点同一个 `_clean_tags`。
    🟡 标签与切片无关：只改标签**不重切 chunks**（重切是把没变的正文再切一遍，
    徒增 `updated_at` 抖动与写放大）。
    """
    action = "kb.document.update"
    _need_flag(body.confirm, action, str(id),
               '改知识库文档必须显式传 {"confirm": true}')
    doc_id = _doc_id(id, action)
    tags = None if body.tags is None else _clean_tags(body.tags, action, str(id))
    if body.title is None and body.content is None and body.url is None and tags is None:
        _reject(400, "nothing_to_update",
                "title / content / tags 不能都不传（link 文档可只传 url）", action, str(id))
    if body.title is not None and not body.title.strip():
        _reject(400, "empty_document", "title 不能为空（要清空请删掉重写）", action, str(id))
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    if tags is not None:
        _need_tags(names, action, str(id))
    try:
        with _write_conn() as conn:
            with conn.cursor() as cur:
                doc = ingest.load_document(cur, schema, doc_id, names=names)
                if doc is None:
                    _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
                _manual_only(doc, str(id), action)
                ctype = str(doc.get("content_type") or ingest.DEFAULT_CONTENT_TYPE)
                _check_type_unchanged(body.content_type, ctype, action, str(id))
                is_link = ctype == ingest.CONTENT_TYPE_LINK
                if body.url is not None and not is_link:
                    _reject(400, "url_not_allowed",
                            f"url 只对 content_type=link 的文档有意义（本篇是 {ctype}）",
                            action, str(id))
                # link 的「摘要」就是 content，且**允许清空**（留空即只改标题/URL）
                if body.content is not None and not is_link and not body.content.strip():
                    _reject(400, "empty_document", "content 不能为空（要清空请删掉重写）",
                            action, str(id))
                # link 的 URL/摘要：改了给新的；**没给的从库里 meta 取回原值**（不能丢）
                url, summary = "", ""
                if is_link:
                    url = _check_url(body.url, action, str(id)) if body.url is not None \
                        else _doc_meta_field(cur, schema, doc_id, names, "url")
                    summary = body.content.strip() if body.content is not None \
                        else _doc_meta_field(cur, schema, doc_id, names, "summary")
                title = body.title.strip() if body.title is not None else str(doc["title"])
                if body.content is None and body.url is None:
                    ingest.update_document(cur, schema, doc_id, title=title,
                                           source_key=str(doc["source_key"]), names=names,
                                           tags=tags)
                    count = ingest.chunks_count(cur, schema, doc_id, names=names)
                    key = str(doc["source_key"])
                else:
                    # link 的内容键用「标题 + URL + 摘要」三件（摘要空时退化到 URL，
                    # 别让两篇同标题不同地址的链接撞成同一行）
                    content = summary if is_link else (
                        body.content.strip() if body.content is not None else "")
                    basis = _doc_basis(title, content, ctype, url)
                    key = ingest.manual_source_key(title, basis)
                    ingest.update_document(cur, schema, doc_id, title=title, source_key=key,
                                           content_hash=ingest.content_hash(title, basis),
                                           names=names, tags=tags)
                    if is_link:
                        _doc_set_link_meta(cur, schema, doc_id, url, summary, names)
                    # 🔴 重切按**文档自身类型**分派：link 仍是单块
                    count = ingest.replace_chunks(cur, schema, doc_id, content,
                                                  names=names, source_path=key,
                                                  content_type=ctype, title=title, url=url)
    except HTTPException:
        raise
    except Exception as e:                    # noqa: BLE001 —— 唯一约束撞车 → 409
        if _is_unique_violation(e):
            _reject(409, "kb_source_key_taken",
                    f"已有文档占用了这个内容键（同名同内容已存在）：{id}", action, str(id))
        raise
    audit.record(action, str(id), ok=True,
                reason=f"source_key={key}; chunks={count}; content_type={ctype}"
                       + ("" if tags is None else f"; tags={len(tags)}"))
    return {"ok": True, "action": action, "id": str(id), "source_key": key,
            "title": title, "chunks_count": count, "content_type": ctype}


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
            # 🆕 content_type 进导出行（老的导入/其它系统能按它分流）。
            # 老库未迁移时这根列探不到 ⇒ 投影空串，导出行给 ""（不假装有类型）。
            sel_ct = f"{names['ctype']}" if names.get("ctype") else "''"
            # 🆕 tags 同款：导出行带**数组**（JSONL 原生形态，导入端直接回灌同一份
            # 净化规则）；老库未迁移 ⇒ 空数组（不假装每篇都打了标）。
            # 🔴 本端点只有 JSONL 一档（`kb/export` 不接 format 参数），所以「CSV 分号
            # 连接」那档没有调用方 —— 不为此新造一个格式。
            sel_tags = f"COALESCE({names['tags']}, '{{}}'::text[])" if names.get("tags") \
                else "'{}'::text[]"
            cur.execute(
                f"SELECT CAST({names['doc_id']} AS text) AS id, {names['src_type']} AS source_type, "
                f"{names['src_key']} AS source_key, {names['title']} AS title, "
                f"{sel_ct} AS content_type, {sel_tags} AS tags, "
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
            # 空串 = 老库未迁移探不到这根列（不假装每篇都是手记，缺省就是缺省）
            "content_type": str(r.get("content_type") or ""),
            # 标签：数组形态，空数组 = 没打标（老库未迁移时也是空数组，不假装有值）
            "tags": _tags_out(r.get("tags")),
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
    🆕 条目可带 `"type"`（内容类型，缺省 note）与 `"url"`（type=link 时必填），
    口径与创建端点同一份枚举校验；link 条目按单块卡片入库。
    条目还可带 `"tags"`（自由标签数组，走同一个 `_clean_tags`：导出的行原样回灌
    就能往返一致，而另写一份口径迟早漂成「导入能塞进界面打不出来的标签」）。
    """
    action, target = "kb.import", "batch"
    if len(body.items) > IMPORT_MAX_ITEMS:
        _reject(400, "import_too_large",
                f"一次最多导入 {IMPORT_MAX_ITEMS} 篇，本次 {len(body.items)} 篇：请分批导入",
                action, target)
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    _need_ctype(names, action, target)
    _need_tags(names, action, target)
    counts = {"total": len(body.items), "would_add": 0, "unchanged": 0,
              "rejected_empty": 0}
    # (title, content, source_key, hash, content_type, url, tags)
    plan: List[Tuple[str, str, str, str, str, str, List[str]]] = []
    written: List[str] = []
    with _write_conn() as conn:
        with conn.cursor() as cur:
            for item in body.items:
                title = (item.title or "").strip()
                content = (item.content or "").strip()
                ctype = _check_content_type(item.type, action, title or target)
                is_link = ctype == ingest.CONTENT_TYPE_LINK
                url = _check_url(item.url, action, title or target) if is_link else ""
                tags = _clean_tags(item.tags, action, title or target)
                # link 的摘要可空；其余类型正文必填（口径与创建端点一致）
                if not title or (not is_link and not content):
                    counts["rejected_empty"] += 1
                    continue
                basis = _doc_basis(title, content, ctype, url)
                key = ingest.manual_source_key(title, basis)
                h = ingest.content_hash(title, basis)
                if ingest.lookup_content_hash(
                        cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE,
                        source_key=key, names=names) == h:
                    counts["unchanged"] += 1
                    continue
                plan.append((title, content, key, h, ctype, url, tags))
            counts["would_add"] = len(plan)
            if not body.dry_run:
                _need_text(body.confirm or "", str(counts["would_add"]), action, target,
                           f"本次将新增 {counts['would_add']} 篇（先跑 dry-run 拿 confirm_required）")
                for title, content, key, h, ctype, url, tags in plan:
                    meta = {"origin": "console-import"}
                    if ctype == ingest.CONTENT_TYPE_LINK:
                        meta["url"] = url
                        meta["summary"] = content
                    status, doc_id, _n = ingest.upsert_document(
                        cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE,
                        source_key=key, title=title, meta=meta,
                        text=content, names=names, content_hash=h,
                        content_type=ctype, url=url, tags=tags)
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
