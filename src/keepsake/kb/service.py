"""知识库的**数据层**与**代理面**服务（FastAPI-free）。

这一层是控制台路由（`keepsake.api.routers.kb`，人看的那一面）与 MCP 工具面
（`keepsake.mcp_server.tools` 的 `kb_store` / `kb_search` / `kb_get` /
`kb_remove`，代理用的那一面）**共用**的那一份：连接、探列、写事务、切块、
归属、可见性、删除判据，全在这里或 `keepsake.kb.ingest`（写判据的唯一来源）。

🔴 **这一层不 import fastapi**：MCP 服务只依赖 `mcp` + 存储后端，控制台的
fastapi 是**可选依赖**（`pyproject.toml` 里没有它）—— 代理侧的知识库工具
不能因为「这台机器没装控制台」就整个 import 失败。所以错误类型是本模块的
`KbError`（code + 人话 + 状态码），控制台在 `api/app.py` 里把它转成与
`HTTPException` **逐字同形**的 JSON，MCP 工具面把它转成 `ToolError`。
⇒ 代理看到的与人在控制台看到的是同一件事。

🔴 **归属与可见性用记忆那一把尺子**（`keepsake.govern.visibility`）：
`visible_agent_ids`（看得见谁的） / `can_remove_fragment`（能删谁的）——
不在这儿另发明一套。记忆面一个字节都不改（口径共用 ≠ 改记忆面）。

## 三条不可省的纪律

1. **写路径只经 `keepsake.kb.ingest`**：切块 / 内容 hash / 幂等键 / 审核
   status 链都在那儿（与控制台、灌入器 CLI 同一个函数对象）。
2. **列名不写死**：表结构在知识库 PG 里，不在本仓库 ⇒ 启动后按
   `information_schema` 探一次真实列名再拼查询，标识符过白名单正则。
3. **老库缺列 ⇒ 降级或人话报错，绝不裸崩**：读路径降级（缺列 = 没有这根列的
   语义），写路径给带迁移 SQL 的人话错误。
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from keepsake.api import audit, config
from keepsake.govern.visibility import can_remove_fragment, visible_agent_ids
from keepsake.kb import ingest

__all__ = [
    "KbError",
    "clean_tags",
    "connect",
    "get_document",
    "remove_document",
    "search_documents",
    "store_document",
    "table_columns",
    "visible_scope",
    "write_conn",
    "write_names",
]


class KbError(Exception):
    """知识库层的错误：`code`（错误码）+ `message`（人话）+ `status`（HTTP 状态码）。

    控制台路由把它转成 `{"detail": {"error": code, "message": message}}`
    （`api/app.py` 的异常处理器），MCP 工具面把它转成 `ToolError`（只带人话）。
    🔴 message 里**不许**出现连接串 / 口令 / 完整凭据。
    """

    def __init__(self, code: str, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------
# 列名候选（探到哪个用哪个；探不到就降级或人话报错）
# --------------------------------------------------------------------------
#: 标识符白名单：小写字母/数字/下划线，且不以数字开头。
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_CAND_TEXT = ("content", "text", "body", "chunk_text", "page_content")
_CAND_ID = ("id", "chunk_id", "key", "uuid")
_CAND_DOC = ("doc_id", "document_id", "doc", "knowledge_id")
_CAND_TITLE = ("title", "name", "doc_title", "document_title")
_CAND_TSV = ("content_tsv", "tsv", "search_vector", "content_vector_ts")
#: documents 表的时间列（可选）：探到就用，探不到就留空串 —— 时间列不是识别表结构的硬要求
_CAND_TIME = ("updated_at", "modified_at", "last_modified_at", "updated", "created_at", "date")

# ---- 写端点要的列（**必需**：探不到就报错，不许拿别的列凑合着写）----
_CAND_SRC_TYPE = ("source_type",)
_CAND_SRC_KEY = ("source_key",)
_CAND_META = ("meta",)
_CAND_HASH = ("content_hash",)
_CAND_ORD = ("ord", "ordinal", "seq", "chunk_no")
_CAND_SRC_PATH = ("source_path", "path", "key")

#: 内容类型列（documents）。**可选探测**：老库未迁移时读路径 SELECT '' 降级。
_CAND_CTYPE = ("content_type",)
#: 自由标签列（documents）。**可选探测**：同上。
_CAND_TAGS = ("tags",)
#: 审核状态与审核痕迹（documents）。**可选探测**：同上。
_CAND_STATUS = ("status",)
_CAND_RMETA = ("review_meta",)
#: 归属列（documents）：存这篇的代理 id。**可选探测**：老库未迁移时全库无主。
_CAND_AGENT_ID = (ingest.AGENT_ID_COLUMN,)

#: 来源类型的合法形态（只作为 WHERE 参数值校验，挡住 `'; drop` 这类噪声）
SOURCE_TYPE_RE = _IDENT_RE

#: **代理写入**允许的来源类型（`store_document` 的白名单）。
#:
#: 🔴 `skill` / `skill_hk` **不在里面**：那是夜间灌入器的地盘（代理写进去的文档
#: 会被死文档清理扫掉，等于凭空丢数据）。`agent` = 代理域（默认）；`manual` =
#: 控制台手建域（代理写进去的，控制台也能改删 —— 那是管理员的地盘，允许）。
AGENT_WRITABLE_SOURCE_TYPES = ("agent", "manual")

#: 老库未迁移各列时的修复 SQL —— **报给人看的，不自己跑**
#: （生产迁移由主脑执行；代码擅自 ALTER 生产表不是本项目的活）。
_CTYPE_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_type text NOT NULL DEFAULT 'note'; "
    "UPDATE documents SET content_type='skill' WHERE source_type IN ('skill','skill_hk'); "
    "CREATE INDEX IF NOT EXISTS idx_documents_content_type ON documents(content_type);"
)
_TAGS_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}'; "
    "CREATE INDEX IF NOT EXISTS idx_documents_tags_gin ON documents USING gin(tags);"
)
_STATUS_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'effective'; "
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS review_meta jsonb NOT NULL DEFAULT '{}'; "
    "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);"
)

#: 标签净化后的硬上限（前端即时提示引的就是这两个数 —— 一处定、两处抄，勿各自写死）
TAG_MAX_ITEMS = 12
TAG_MAX_LEN = 32

#: 控制字符（含 `\n` `\r` `\t` `\0`）—— 一律拒：标签要渲成 chip、还要进导出文件，
#: 串进换行/制表能把版面和 JSONL 行一起搞坏。
_TAG_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: 列名缓存（进程级，只读 information_schema）。
_COLS_CACHE: Dict[str, Dict[str, set]] = {}
_COLS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 连接 / 探列
# --------------------------------------------------------------------------

def ident(name: str) -> str:
    """标识符过白名单才允许进 SQL 文本（表名/列名无法参数化，只能这样拦）。"""
    if not _IDENT_RE.match(name):
        raise KbError("bad_identifier", f"非法标识符：{name}")
    return name


def _dsn() -> str:
    dsn = config.pg_dsn()
    if not dsn:
        raise KbError("kb_unconfigured",
                      f"知识库连接串未配置（{config.PG_DSN_ENV}）")
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
        raise KbError(
            "kb_no_driver",
            "未安装可用的 PostgreSQL 驱动（psycopg2 或 psycopg）："
            f"{first_error.__class__.__name__}/{e.__class__.__name__}",
        ) from e


def _connect_kwargs(pg: Any, row_factory: Any) -> Dict[str, Any]:
    """行工厂传给**哪个参数**取决于驱动版本（同名不同义）：

    * psycopg2：`cursor_factory=RealDictCursor`；
    * psycopg3：`row_factory=dict_row` —— 传 `cursor_factory` 会得到
      `row_factory() got an unexpected keyword argument 'row_factory'`（500）。

    🔴 **按模块名分流，不按签名猜**：两个驱动的 `connect()` 签名里两个参数都有，
    猜不出来；而假驱动（测试里塞进 `sys.modules` 的）没有 `__name__` ⇒ 走
    psycopg2 老口径，88/91 上的既有假驱动测试零改动。
    """
    if getattr(pg, "__name__", "") == "psycopg":
        return {"row_factory": row_factory}
    return {"cursor_factory": row_factory}


def connect() -> Any:
    """打开只读连接（上下文管理器）。

    🔴 **判断顺序硬约束**：先 `_dsn()` 判未配置，再 `_pg()` 载驱动。
    顺序反了的话，未配置 DSN 会被「驱动没装/不完整」盖住，客户端拿到的是
    `kb_no_driver` 而不是 `kb_unconfigured`，定位不了真因。所有取数都走这里，
    顺序只此一处，不再散落在各接口里。
    """
    dsn = _dsn()
    pg, row_factory = _pg()
    return pg.connect(dsn, connect_timeout=5, **_connect_kwargs(pg, row_factory))


@contextmanager
def write_conn() -> Iterator[Any]:
    """写连接：**成功提交 / 异常回滚 / 结束必关**。

    与读端点的 `connect()` 不同：读端点靠 GC 回收，写端点每请求一条连接不能攒；
    且「删旧 chunks + 插新 chunks」必须同事务 —— 提交权收在这里，调用方不出现
    `commit()`，抛出的错误都会走回滚分支（数据一行没动）。
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


def table_columns(schema: str, tables: Sequence[str]) -> Dict[str, set]:
    """探一次三表的真实列名（进程级缓存，只读 information_schema）。"""
    key = f"{schema}:{','.join(tables)}"
    cached = _COLS_CACHE.get(key)
    if cached is not None:
        return cached
    with connect() as conn:
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


def pick(cols: set, candidates: Sequence[str], table: str, what: str) -> str:
    """在真实列里挑第一个候选；挑不到 ⇒ 报错（结构不识别，不能装作没数据）。"""
    for c in candidates:
        if c in cols:
            return ident(c)
    raise KbError("kb_schema_unknown",
                  f"{table} 表未找到 {what} 列（候选：{', '.join(candidates)}）")


def missing(tables: Dict[str, set]) -> List[str]:
    """探不到列的表（表不存在）。"""
    return [t for t, cols in tables.items() if not cols]


def write_names(schema: str) -> Dict[str, str]:
    """写端点要的表名/列名（复用读端点那套进程级探列缓存 + 标识符白名单）。

    写路径**没有降级余地**：缺哪根列都报 `kb_schema_unknown`，
    不许「这根列探不到就少写一个字段」—— 那会静默丢内容还报成功。

    🆕 `ctype`（content_type）与 `tags`（自由标签）是**仅有的两个例外：探不到就给
    空串**。老库未迁移时，不写这两样的端点（导出、真删）仍能跑；真要写类型的/
    标签的端点由调用方给出**人话错误**（提示跑迁移）。

    🆕 `agent`（归属列）同样**探不到就给空串**：老库未迁移 ⇒ 全库无主（读路径
    照旧），真要写归属的路径（代理存文档）由调用方给出带迁移 SQL 的人话错误。
    """
    tabs = table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    gone = missing(tabs)
    if gone:
        raise KbError("kb_table_missing", f"知识库表不存在：{', '.join(gone)}")
    docs, chunks = tabs[config.KB_TABLE_DOCUMENTS], tabs[config.KB_TABLE_CHUNKS]
    d, c = config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS
    return {
        "documents": ident(d), "chunks": ident(c),
        "doc_id": pick(docs, _CAND_ID, d, "主键"),
        "src_type": pick(docs, _CAND_SRC_TYPE, d, "来源类型"),
        "src_key": pick(docs, _CAND_SRC_KEY, d, "来源键"),
        "ctype": next((ident(x) for x in _CAND_CTYPE if x in docs), ""),
        "tags": next((ident(x) for x in _CAND_TAGS if x in docs), ""),
        # 审核两件套与 ctype/tags 同款降级纪律：**探不到就给空串**。
        "status": next((ident(x) for x in _CAND_STATUS if x in docs), ""),
        "rmeta": next((ident(x) for x in _CAND_RMETA if x in docs), ""),
        # 归属列：探不到 ⇒ 空串（老库全库无主；写归属的路径自己给人话错误）。
        "agent": next((ident(x) for x in _CAND_AGENT_ID if x in docs), ""),
        "title": pick(docs, _CAND_TITLE, d, "标题"),
        "meta": pick(docs, _CAND_META, d, "元数据"),
        "hash": pick(docs, _CAND_HASH, d, "内容哈希"),
        "updated": pick(docs, _CAND_TIME, d, "更新时间"),
        "chunk_doc": pick(chunks, _CAND_DOC, c, "外键(文档)"),
        "chunk_ord": pick(chunks, _CAND_ORD, c, "切片序号"),
        "chunk_text": pick(chunks, _CAND_TEXT, c, "正文"),
        "chunk_path": pick(chunks, _CAND_SRC_PATH, c, "来源路径"),
    }


def _now_iso() -> str:
    """审核痕迹里的时间戳（UTC ISO，与注册表 `Registry._now_iso` 同款口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 标签净化（控制台写端点与代理面共用同一份口径）
# --------------------------------------------------------------------------

def clean_tags(raw: Any) -> List[str]:
    """自由标签的**唯一净化入口**（控制台新增/改/导入 + 代理 `kb_store` 共用）。

    规则按序执行，违规一律报错（**不静默截断**：用户打了 13 个标却被悄悄
    砍掉一个，回头按那个标筛会以为「标签坏了」）：

    1. 非列表（None / 标量）⇒ 当作没给；元素必须都是字符串（非字符串报错）；
    2. 逐项 `strip()`，空串直接丢（`["量化", ""]` 与 `["量化"]` 等价）；
    3. 含控制字符（含换行 / 制表 / 回车）⇒ 拒（`bad_tag`）；
    4. 单项 > `TAG_MAX_LEN` 字符 ⇒ 拒（超长标签在 chip 行里必然折行）；
    5. 去重**保序**（先出现的赢 ⇒ 卡片上的顺序与用户输入一致，可预期）；
    6. 去重后 > `TAG_MAX_ITEMS` 项 ⇒ 拒（`too_many_tags`）。

    🟡 **长度按字符数**（中文按字算，不是字节）：限的是「chip 行看不看得下」。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise KbError("bad_tag", f"tags 必须是字符串数组：收到 {type(raw).__name__}", 400)
    out: List[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            raise KbError("bad_tag", f"tags 里每一项都必须是字符串：收到 {type(item).__name__}", 400)
        t = item.strip()
        if not t:
            continue                       # 空串不是「一个空标签」，直接丢
        if _TAG_CTRL_RE.search(t):
            raise KbError("bad_tag", f"标签不许含换行/制表等控制字符：{t[:40]!r}", 400)
        if len(t) > TAG_MAX_LEN:
            raise KbError("bad_tag", f"单个标签最多 {TAG_MAX_LEN} 字（收到 {len(t)} 字）：{t[:40]!r}", 400)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if len(out) > TAG_MAX_ITEMS:
        raise KbError("too_many_tags",
                      f"每篇最多 {TAG_MAX_ITEMS} 个标签，本次 {len(out)} 个：请分批提交", 400)
    return out


# --------------------------------------------------------------------------
# 可见性 / 归属（**与记忆同一把尺子**）
# --------------------------------------------------------------------------

def visible_scope(caller_agent_id: str, registry: Any, *,
                  is_primary: bool) -> Optional[List[str]]:
    """这个调用方看得见哪些**归属代理**（`None` = 全量，不过滤）。

    判据只有一份：`govern.visibility.visible_agent_ids`（记忆的可见性口径）——
    非 primary = {自己} ∪ {`share_memory=1` 的已批准代理}；primary = 全量。

    **无主文档（`agent_id IS NULL`）人人可见**（收紧前的公共数据，历史 1844 篇
    全是无主）：这条由调用方的 SQL 谓词补上（`agent_id IS NULL OR ...`），
    与记忆侧「无归属的条目人人可见」同一条纪律。
    """
    ids = visible_agent_ids(caller_agent_id, registry, is_primary=is_primary)
    return sorted(ids) if ids is not None else None


def _owner_of(doc: Dict[str, Any]) -> str:
    """文档的归属代理 id（没有 ⇒ 空串 = 无主）。"""
    return str((doc or {}).get("agent_id") or "").strip()


def _visible(owner: str, agent_scope: Optional[Sequence[str]]) -> bool:
    """这篇文档对当前调用方可见吗（`agent_scope=None` ⇒ 全量可见）。"""
    if agent_scope is None:
        return True
    if not owner:
        return True                       # 无主老文档 = 收紧前的公共数据
    return owner in set(agent_scope)


# --------------------------------------------------------------------------
# 代理面服务（kb_store / kb_search / kb_get / kb_remove 的实现）
# --------------------------------------------------------------------------

def store_document(*, title: str, content: str, tags: Sequence[str] = (),
                   source_type: str = "agent", agent_id: str = "",
                   is_primary: bool = False) -> Dict[str, Any]:
    """存一篇文档（**走既有 ingest 管线**：切块 / 幂等 / 审核 status 链全不变）。

    * **归属** = `agent_id`（凭据反查的调用方；primary 也打 —— 主脑写的归主脑）；
    * **审核链不变**：非 primary ⇒ `pending`（**不切块**，正文存 `review_meta.body`，
      等控制台批准时才切）；primary ⇒ `effective`（直进，与站内手建同款）；
    * **键带归属**（`ingest.agent_source_key`）：两个代理存同一篇内容不撞行
      （`(source_type, source_key)` 是唯一约束，撞了就是「后写的覆盖先写的，
      归属还被改掉」——最难查的一类坏）。
    """
    title = str(title or "").strip()
    content = str(content or "").strip()
    if not title or not content:
        raise KbError("empty_document", "title 与 content 都不能为空", 400)
    st = str(source_type or "").strip()
    if not SOURCE_TYPE_RE.match(st):
        raise KbError("bad_source_type",
                      f"source_type 只能是标识符（小写字母/数字/下划线）：{source_type}", 400)
    if st not in AGENT_WRITABLE_SOURCE_TYPES:
        # 🔴 灌入器的地盘（skill / skill_hk）**代理不许写**：写进去的文档会被
        # 死文档清理扫掉（等于凭空丢数据），还会搅乱灌入器的 live 集。
        raise KbError("bad_source_type",
                      f"source_type 只能是：{', '.join(AGENT_WRITABLE_SOURCE_TYPES)}；"
                      f"收到 {source_type}（skill/skill_hk 是夜间灌入器的地盘）", 400)
    clean = clean_tags(tags)
    schema = ident(config.pg_schema())
    names = write_names(schema)
    if not names.get("agent"):
        raise KbError("kb_schema_unknown",
                      f"{config.KB_TABLE_DOCUMENTS} 表未找到 agent_id 列，无法记录归属；"
                      f"请先执行迁移：{ingest.AGENT_ID_MIGRATION_HINT}")
    if not names.get("tags"):
        raise KbError("kb_schema_unknown",
                      f"{config.KB_TABLE_DOCUMENTS} 表未找到 tags 列，无法写入标签；"
                      f"请先执行迁移：{_TAGS_MIGRATION_HINT}")
    pending = not is_primary
    if pending and not (names.get("status") and names.get("rmeta")):
        raise KbError("kb_schema_unknown",
                      f"{config.KB_TABLE_DOCUMENTS} 表未找到 status / review_meta 列，"
                      f"无法写入审核状态；请先执行迁移：{_STATUS_MIGRATION_HINT}")
    key = ingest.agent_source_key(st, title, content, agent_id)
    meta = {"origin": "agent", "agent_id": agent_id}
    with write_conn() as conn:
        with conn.cursor() as cur:
            status, doc_id, count = ingest.upsert_document(
                cur, schema, source_type=st, source_key=key, title=title, meta=meta,
                text=content, names=names,
                content_hash=ingest.content_hash(title, content),
                content_type=ingest.DEFAULT_CONTENT_TYPE, tags=clean,
                # 🔴 待审时**不切块**（chunk=False）：正文改存 review_meta.body，
                # 等批准时才 replace_chunks —— 未审正文不进检索索引。
                status=ingest.STATUS_PENDING if pending else None,
                chunk=not pending,
                agent_id=agent_id or None)
            # `unchanged` = 同键同 hash，一行都没动 ⇒ 不许往已生效那篇的
            # review_meta 里塞「待审正文」（那会把一篇在用的文档搅进审核台账）。
            staged = pending and status != "unchanged"
            if staged:
                ingest.patch_review_meta(cur, schema, doc_id, {
                    "submitted_by": agent_id,
                    "submitted_at": _now_iso(),
                    "body": content,
                }, names=names)
    audit.record("kb.document.create", key, ok=True,
                 reason=f"source=mcp; status={status}; chunks={count}; by={agent_id}"
                        + ("; review=pending" if staged else ""))
    return {"id": str(doc_id), "status": status, "chunks_count": count,
            "review": ingest.STATUS_PENDING if staged else ingest.STATUS_EFFECTIVE,
            "agent_id": agent_id, "source_type": st}


def search_documents(*, query: str, limit: int = 10, page: int = 1,
                     agent_scope: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """全文检索（chunk 级命中），**可见性谓词按 `agent_scope` 收窄**。

    * `agent_scope=None` ⇒ 不过滤（primary / 控制台 / 老库没有归属列）；
    * 否则只回 `agent_id IS NULL`（无主老文档）或 `agent_id ∈ scope` 的文档。

    🔴 **待审 / 已驳回的文档检索一律不可见**（`status='effective'` 谓词）——
    与控制台检索同一口径（pending 阶段压根没切块，这里仍要带上谓词：防的是
    「批准前的行被人绕过写面塞了块」与已驳回行残留的旧块）。
    老库没有 status 列 ⇒ 不加这个谓词（存量行照常可检索，与迁移前一字不差）。
    """
    q = str(query or "").strip()
    if not q:
        raise KbError("empty_query", "检索词不能为空", 400)
    schema = ident(config.pg_schema())
    tabs = table_columns(schema, (config.KB_TABLE_DOCUMENTS, config.KB_TABLE_CHUNKS))
    gone = missing(tabs)
    if gone:
        raise KbError("kb_table_missing", f"知识库表不存在：{', '.join(gone)}")
    chunks = tabs[config.KB_TABLE_CHUNKS]
    docs = tabs[config.KB_TABLE_DOCUMENTS]

    c_text = pick(chunks, _CAND_TEXT, config.KB_TABLE_CHUNKS, "正文")
    c_id = pick(chunks, _CAND_ID, config.KB_TABLE_CHUNKS, "主键")
    c_doc = pick(chunks, _CAND_DOC, config.KB_TABLE_CHUNKS, "外键(文档)")
    d_id = pick(docs, _CAND_ID, config.KB_TABLE_DOCUMENTS, "主键")
    c_tsv = next((ident(c) for c in _CAND_TSV if c in chunks), None)

    joined = (f"FROM {schema}.{config.KB_TABLE_CHUNKS} c "
              f"LEFT JOIN {schema}.{config.KB_TABLE_DOCUMENTS} d "
              f"ON d.{d_id} = c.{c_doc}")
    d_title = next((ident(t) for t in _CAND_TITLE if t in docs), None)
    select_doc = f"COALESCE(d.{d_title}, '')" if d_title else "''"
    d_ctype = next((ident(c) for c in _CAND_CTYPE if c in docs), None)
    select_ctype = f"COALESCE(d.{d_ctype}, '')" if d_ctype else "''"
    d_tags = next((ident(c) for c in _CAND_TAGS if c in docs), None)
    # 老库没有 tags 列 ⇒ 投影**空数组**（不是 NULL、不是缺键）
    select_tags = f"COALESCE(d.{d_tags}, '{{}}'::text[])" if d_tags else "'{}'::text[]"
    d_status = next((ident(c) for c in _CAND_STATUS if c in docs), None)
    d_agent = next((ident(c) for c in _CAND_AGENT_ID if c in docs), None)

    if c_tsv:
        # 中文语料用 simple 分词（PG 默认 english 会把中文切成一坨）
        mode, where, params = "tsvector", f"c.{c_tsv} @@ plainto_tsquery('simple', %s)", [q]
        rank = f"ts_rank(c.{c_tsv}, plainto_tsquery('simple', %s))"
        rank_params: List[Any] = [q]
    else:
        mode, where, params = "ilike", f"c.{c_text} ILIKE %s", [f"%{q}%"]
        rank, rank_params = "0.0::float8", []
    if d_status:
        where, params = f"{where} AND d.{d_status} = %s", [*params, ingest.STATUS_EFFECTIVE]
    if agent_scope is not None and d_agent:
        # 🔴 可见性谓词：**无主人人可见** + 归属在可见集里的。
        # 「无主」= NULL **或空串**（空串 = 没人认领，与记忆侧「空归属 = 无归属」
        # 同一把尺子；判成「有主但没人叫那个名字」会把无主文档藏起来）。
        # 归属列不存在（老库未迁移）⇒ 全库无主 ⇒ 不过滤（与「无主=可见」同口径）。
        where, params = (f"{where} AND (d.{d_agent} IS NULL OR d.{d_agent} = '' OR "
                         f"d.{d_agent} = ANY(%s::text[]))"), [*params, list(agent_scope)]

    sql = (
        f"SELECT c.{c_id} AS chunk_id, CAST(c.{c_doc} AS text) AS doc_id, "
        f"{select_doc} AS title, LEFT(c.{c_text}, 300) AS snippet, "
        f"{select_ctype} AS content_type, {select_tags} AS tags, "
        f"{rank} AS score {joined} WHERE {where} "
        f"ORDER BY score DESC, chunk_id ASC LIMIT %s OFFSET %s"
    )
    with connect() as conn:
        with conn.cursor() as cur:
            # 🔴 占位符顺序 = SQL 文本里的出现顺序：ts_rank(%s) 在 SELECT（第 1 个），
            # WHERE 的 tsquery(%s) 与 status(%s) 在后 ⇒ 必须 **rank_params 在前**、
            # params 在后。批2 前两边都是同一个 q、顺序无所谓；批2 往 params 里追加了
            # status 值后，[*params, *rank_params] 会把 'effective' 喂给 tsquery、
            # 把 q 喂给 status ⇒ 迁移后生产（有 tsv 列）检索恒 0（91 测试库无 tsv 列
            # 走 ILIKE 路径，逮不住这个错位 —— 回归判据见 test_kb_review 的 tsvector 用例）。
            cur.execute(sql, [*rank_params, *params, limit, (page - 1) * limit])
            rows = cur.fetchall()
    return {"mode": mode, "items": [
        {"chunk_id": str(r["chunk_id"]), "doc_id": str(r["doc_id"] or ""),
         "title": str(r["title"] or ""), "snippet": str(r["snippet"] or ""),
         "content_type": str(r.get("content_type") or ""),
         "tags": _tags_out(r.get("tags")),
         "score": round(float(r["score"] or 0.0), 6)}
        for r in rows
    ]}


def _doc_int(raw: Any) -> int:
    """文档 id 必须是整数（PG 的 bigserial）—— 不是 ⇒ 报错，不进 SQL。

    🔴 不做隐式转换：字符串 id 进 `= %s` 会变成 `bigint = text`（无操作符炸库）；
    而「看起来能跑」的驱动适配差异更危险 —— 显式报错最省事。
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise KbError("kb_doc_id_invalid", f"文档 id 必须是整数：{raw}", 400)


def get_document(*, doc_id: Any,
                 agent_scope: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
    """取一篇文档的详情（标题 + 全部切片正文 + 归属 + 状态）。

    🔴 **不可见 ⇒ None**（not-found 语义：存在性本身也不许泄露，与 `memory_get`
    同纪律 —— 未共享代理的条目按「不存在」回）。

    返回的 `content` 是**全部切片正文按序拼起来**（正文只存在于切片里）。
    """
    doc_id = _doc_int(doc_id)
    schema = ident(config.pg_schema())
    names = write_names(schema)
    with connect() as conn:
        with conn.cursor() as cur:
            doc = ingest.load_document(cur, schema, doc_id, names=names)
            if doc is None:
                return None
            if not _visible(_owner_of(doc), agent_scope):
                return None                     # 不可见 = 不存在（不泄露存在性）
            chunks = ingest.document_chunks(cur, schema, doc_id, names=names)
    doc = dict(doc)
    doc["chunks"] = chunks
    doc["content"] = "\n\n".join(c for c in chunks if c)
    return doc


def remove_document(*, doc_id: Any, confirm: bool, caller_agent_id: str,
                     is_primary: bool,
                     agent_scope: Optional[Sequence[str]]) -> Dict[str, Any]:
    """删一篇文档（**真删**：chunks + documents，同一事务；删前备份全文）。

    🔴 **删除口径比读更严**（与记忆同一把尺子，`govern.visibility.can_remove_fragment`）：

    * **primary** ⇒ 全量可删（= 主脑现状逐字不变）；
    * **非 primary** ⇒ 只有**自己名下**的与**无主的老文档**可删；
      别人的 —— **含 `share_memory=1` 的共享代理的** —— 一律拒（**不回显任何
      正文**，只说「不是你的」）。

    🔴 拒的文案**不区分「不存在」与「不是你的」**（与 `memory_remove` 同纪律）：
    存在性本身也不许泄露。

    🔴 **灌入器维护的文档（skill / skill_hk）代理不许删**：那是夜间灌入器的
    地盘（改了当晚就被覆盖、被删的文档也会被清理扫掉）—— 与控制台的
    `kb_doc_immutable` 同一口径。
    """
    if not confirm:
        raise KbError("confirm_required",
                      "删除需要显式确认：确认后请重新调用 kb_remove(id=..., confirm=True)", 400)
    doc_id = _doc_int(doc_id)
    schema = ident(config.pg_schema())
    names = write_names(schema)
    with write_conn() as conn:
        with conn.cursor() as cur:
            doc = ingest.load_document(cur, schema, doc_id, names=names)
            if doc is None or not _visible(_owner_of(doc), agent_scope):
                if is_primary:
                    raise KbError("kb_doc_missing", f"知识库文档不存在：{doc_id}", 404)
                # 不可见（别人的 / 不存在的）⇒ 与「不是你的」同一句，零内容回显
                raise KbError("kb_not_owner",
                              "这篇知识库文档不是你的 —— 子代理只能删自己名下的文档", 403)
            owner = _owner_of(doc)
            if not can_remove_fragment(owner, caller_agent_id, is_primary=is_primary):
                raise KbError("kb_not_owner",
                              "这篇知识库文档不是你的 —— 子代理只能删自己名下的文档", 403)
            if str(doc.get("source_type") or "") in ingest.INGEST_SOURCE_TYPES:
                raise KbError("kb_doc_immutable",
                              f"这篇由夜间灌入器维护（source_type={doc.get('source_type')}），"
                              f"代理不许删：{doc_id}", 409)
            # 🔴 与控制台真删同款：只对**已生效**的文档开放（待审/已驳回归审核
            # 面板管；正文还在 review_meta 里，从这里删就是绕过审核动作）。
            st = str(doc.get("status") or ingest.STATUS_EFFECTIVE)
            if st != ingest.STATUS_EFFECTIVE:
                raise KbError("kb_doc_not_effective",
                              f"知识库文档还在审核队列里（status={st}），"
                              f"删请到待审面板批准或驳回：{doc_id}", 409)
            # 🔴 删前备份全文与全部切片（备份写不成功 ⇒ 一行都不删）
            snapshot = {"document": dict(doc),
                        "chunks": ingest.document_chunks(cur, schema, doc_id, names=names)}
            backup = audit.backup_document(doc_id, snapshot)
            deleted = ingest.delete_document(cur, schema, doc_id, names=names)
            if deleted != 1:                   # pragma: no cover —— 取到行又删不掉＝并发
                raise KbError("kb_delete_failed",
                              f"删除了 {deleted} 篇（应为 1），文档可能已被并发删掉：{doc_id}")
    audit.record("kb.document.delete", str(doc_id), ok=True,
                 reason=f"source=mcp; deleted=1; by={caller_agent_id}; backup={backup}")
    return {"id": str(doc_id), "removed": True, "backup": backup,
            "title": str(doc.get("title") or ""), "agent_id": owner}


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
