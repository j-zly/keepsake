"""切块 + 幂等 upsert —— **知识库写入的唯一判据来源**。

收编自 180 本机灌入器 `kb_ingest_v3.py`（见 `scripts/kb_ingest.py`）：切块逻辑
`split_chunks`、内容 hash 判据、`documents`/`chunks` 的落库顺序全部**逐字保持**
v3 口径，控制台的写端点（`api/routers/kb.py`）也走这里 —— 两处各切一次块，
同一篇文档在页面上的切片数和检索命中就会对不上。

🔴 **本模块不碰连接、不碰事务、不碰驱动**：`upsert_document` 只往调用方给的
游标上执行 SQL，提交/回滚由调用方持有（同事务里连带删 chunks 才靠得住）。
列名/表名走 `names` 覆盖（生产表结构是默认口径；API 侧按探出来的真实列名传）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 手动文档的 source_type。**灌入器的死文档清理只碰 skill/skill_hk，永不碰它**
#: —— 控制台新增/导入的东西不会被当晚那轮清理扫掉。
MANUAL_SOURCE_TYPE = "manual"

#: 单块字符上限（v3 同值）。中文按字符切，不是按字节。
MAX_CHUNK_LEN = 1200

#: 灌入器负责的来源类型（死文档清理范围，只此两个）
INGEST_SOURCE_TYPES = ("skill", "skill_hk")

#: 表名与列名（生产 `keepsake` 库 documents/chunks 的口径）。
#: 值是**标识符**，会拼进 SQL 文本 ⇒ 调用方传进来的覆盖值必须过白名单
#: （`api/routers/kb.py` 用的是 `_ident()`）。
DEFAULT_NAMES: Dict[str, str] = {
    "documents": "documents",
    "chunks": "chunks",
    "doc_id": "id",
    "src_type": "source_type",
    "src_key": "source_key",
    "title": "title",
    "meta": "meta",
    "hash": "content_hash",
    "updated": "updated_at",
    "chunk_doc": "document_id",
    "chunk_ord": "ord",
    "chunk_text": "text",
    "chunk_path": "source_path",
}

#: 标题行（Markdown 1~4 级）—— 分段的判据
_HEADING_RE = re.compile(r"^#{1,4} ")


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------

def split_chunks(text: str, max_len: int = MAX_CHUNK_LEN) -> List[str]:
    """按「标题行前断段 + 超长硬切」把正文切成检索块（v3 原逻辑，逐字保持）。

    行为等价约定（**不许顺手优化**）：
    1. 只有遇到标题行且当前缓冲区非空时才断段 —— 连续标题行不产生空块；
    2. 每段先 `strip()`，空段丢弃；
    3. 段超长按 `max_len` **硬切**（不按句/不重叠）；
    4. 全空输入兜底返回 `[text[:max_len]]`（空串时是 `[""]`），
       保证调用方永远拿到至少一块，不会把文档写成零切片。
    """
    paras: List[str] = []
    buf = ""
    for line in (text or "").splitlines():
        if _HEADING_RE.match(line) and buf:
            paras.append(buf)
            buf = line + "\n"
        else:
            buf += line + "\n"
    if buf:
        paras.append(buf)
    out: List[str] = []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        while len(p) > max_len:
            out.append(p[:max_len])
            p = p[max_len:]
        if p:
            out.append(p)
    return out or [(text or "")[:max_len]]


# --------------------------------------------------------------------------
# 内容 hash 判据（手动域：内容级幂等）
# --------------------------------------------------------------------------

def _digest(title: str, content: str) -> str:
    """`sha256(title + \\x00 + content)` —— 分隔符防 `("ab","c")` 与 `("a","bc")` 撞同键。"""
    return hashlib.sha256(f"{title or ''}\x00{content or ''}".encode("utf-8")).hexdigest()


def manual_source_key(title: str, content: str) -> str:
    """手动文档的 `source_key` = `manual:<sha256(title+content)[:12]>`。

    **内容级幂等**：同 title+content 反复导入/重复提交都落到同一行
    （`upsert_document` 判 unchanged），改一个字才换键。
    """
    return f"{MANUAL_SOURCE_TYPE}:{_digest(title, content)[:12]}"


def content_hash(title: str, content: str) -> str:
    """手动文档写进 `content_hash` 列的值（判「内容是否变过」用）。"""
    return "sha256:" + _digest(title, content)


# --------------------------------------------------------------------------
# SQL 组装
# --------------------------------------------------------------------------

def _names(names: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {**DEFAULT_NAMES, **(names or {})}


def _scalar(row: Any) -> Any:
    """一行取第一列（`dict_row` / `RealDictCursor` 回字典，默认游标回元组）。"""
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _val(row: Any, key: str, idx: int) -> Any:
    """按列名取值；默认游标（回元组）时退回列序号。"""
    return row[key] if isinstance(row, dict) else row[idx]


def _count_chunks(cur: Any, schema: str, n: Dict[str, str], doc_id: Any) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {schema}.{n['chunks']} "
                f"WHERE {n['chunk_doc']} = %s", [doc_id])
    row = cur.fetchone()
    return int(_scalar(row) or 0)


def chunks_count(cur: Any, schema: str, doc_id: Any,
                 names: Optional[Dict[str, str]] = None) -> int:
    """该文档当前有几块切片（回包里「改完之后切成几块」用它）。"""
    return _count_chunks(cur, schema, _names(names), doc_id)


def replace_chunks(cur: Any, schema: str, doc_id: Any, text: str,
                   *, names: Optional[Dict[str, str]] = None,
                   source_path: str = "") -> int:
    """删掉该文档的旧切片 + 按同一 `split_chunks` 整体重插，返回新切片条数。

    必须在调用方的事务里（删+插要么都成要么都不成）；`source_path` 写回
    source_key（v3 同款，检索结果里看得出这块来自哪个 key）。
    """
    n = _names(names)
    cur.execute(f"DELETE FROM {schema}.{n['chunks']} WHERE {n['chunk_doc']} = %s",
                [doc_id])
    rows = [(doc_id, i, c, source_path) for i, c in enumerate(split_chunks(text))]
    for row in rows:
        cur.execute(f"INSERT INTO {schema}.{n['chunks']}"
                    f"({n['chunk_doc']}, {n['chunk_ord']}, {n['chunk_text']}, {n['chunk_path']}) "
                    f"VALUES(%s, %s, %s, %s)", row)
    return len(rows)


def upsert_document(cur: Any, schema: str, *, source_type: str, source_key: str,
                    title: str, meta: Optional[Dict[str, Any]], content_hash: str,
                    text: str, names: Optional[Dict[str, str]] = None
                    ) -> Tuple[str, Any, int]:
    """按 `(source_type, source_key)` 幂等落一篇文档，返回 `(状态, doc_id, 切片数)`。

    状态判据与 v3 同款，三种：
    * `unchanged`：同键且 `content_hash` 没变 ⇒ **提前返回，连 INSERT 都不发**
      （夜间全量跑的快路径：别把没改的文档重切成新的切片、别刷新 updated_at）；
    * `added`   ：新键；
    * `updated` ：键在但 hash 变了 ⇒ 改行 + 删旧 chunks + 重插。

    `meta` 序列化成 jsonb（v3 同款 `%s::jsonb`）；`updated_at` 交给库里的 `now()`。
    """
    n = _names(names)
    cur.execute(f"SELECT {n['doc_id']}, {n['hash']} FROM {schema}.{n['documents']} "
                f"WHERE {n['src_type']} = %s AND {n['src_key']} = %s",
                [source_type, source_key])
    row = cur.fetchone()
    if row is not None and _val(row, n["hash"], 1) == content_hash:
        return ("unchanged", _val(row, n["doc_id"], 0),
                _count_chunks(cur, schema, n, _val(row, n["doc_id"], 0)))
    cur.execute(
        f"INSERT INTO {schema}.{n['documents']}"
        f"({n['src_type']}, {n['src_key']}, {n['title']}, {n['meta']}, "
        f"{n['hash']}, {n['updated']}) "
        f"VALUES(%s, %s, %s, %s::jsonb, %s, now()) "
        f"ON CONFLICT ({n['src_type']}, {n['src_key']}) DO UPDATE "
        f"SET {n['title']} = EXCLUDED.{n['title']}, "
        f"{n['meta']} = EXCLUDED.{n['meta']}, "
        f"{n['hash']} = EXCLUDED.{n['hash']}, {n['updated']} = now() "
        f"RETURNING {n['doc_id']}",
        [source_type, source_key, title,
         json.dumps(meta or {}, ensure_ascii=False), content_hash])
    doc_id = _scalar(cur.fetchone())
    count = replace_chunks(cur, schema, doc_id, text, names=names, source_path=source_key)
    return ("updated" if row is not None else "added"), doc_id, count


def lookup_content_hash(cur: Any, schema: str, *, source_type: str, source_key: str,
                        names: Optional[Dict[str, str]] = None) -> Optional[str]:
    """只读地取同 `(source_type, source_key)` 那行的 `content_hash`，没有 ⇒ None。

    导入的 dry-run 用它判「会不会是 unchanged」，**不写任何一行**。
    """
    n = _names(names)
    cur.execute(f"SELECT {n['hash']} FROM {schema}.{n['documents']} "
                f"WHERE {n['src_type']} = %s AND {n['src_key']} = %s",
                [source_type, source_key])
    row = cur.fetchone()
    return None if row is None else _scalar(row)


def update_document(cur: Any, schema: str, doc_id: Any, *, title: str,
                    source_key: str, content_hash: Optional[str] = None,
                    names: Optional[Dict[str, str]] = None) -> None:
    """按 **id** 改文档行（控制台改文档走这条，不走 `upsert_document`）。

    🔴 **必须按 id 改**：手动域的 `source_key` 是 `sha256(title+content)` 派生，
    改正文/标题后键会变 —— 按新键 upsert 会**另起一行**、把旧行留成孤儿文档。
    这里同时把 `source_key` 改成新值，于是「改完之后再导一次同一篇」仍然是
    命中同一行（内容级幂等不破）。`content_hash=None` 表示只改标题。

    撞上已存在的键 ⇒ 驱动抛 UniqueViolation，**由调用方翻译成 409**
    （别在这里吞成「更新成功」，那会静默把两篇合成一篇）。
    """
    n = _names(names)
    sets = [f"{n['title']} = %s", f"{n['src_key']} = %s", f"{n['updated']} = now()"]
    params: List[Any] = [title, source_key]
    if content_hash is not None:
        sets.append(f"{n['hash']} = %s")
        params.append(content_hash)
    params.append(doc_id)
    cur.execute(f"UPDATE {schema}.{n['documents']} SET {', '.join(sets)} "
                f"WHERE {n['doc_id']} = %s", params)


def load_document(cur: Any, schema: str, doc_id: Any, *,
                  names: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """取一行文档，**返回逻辑键**（`id`/`source_type`/`source_key`/`title`/
    `content_hash`），不存在 ⇒ None。调用方不用关心真实列名。"""
    n = _names(names)
    cur.execute(f"SELECT {n['doc_id']}, {n['src_type']}, {n['src_key']}, "
                f"{n['title']}, {n['hash']} FROM {schema}.{n['documents']} "
                f"WHERE {n['doc_id']} = %s", [doc_id])
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": _val(row, n["doc_id"], 0),
            "source_type": _val(row, n["src_type"], 1),
            "source_key": _val(row, n["src_key"], 2),
            "title": _val(row, n["title"], 3),
            "content_hash": _val(row, n["hash"], 4)}


def document_chunks(cur: Any, schema: str, doc_id: Any, *,
                    names: Optional[Dict[str, str]] = None) -> List[str]:
    """按 ord 取该文档的切片正文（备份快照 / 导出的 `chunks` 字段用）。"""
    n = _names(names)
    cur.execute(f"SELECT {n['chunk_text']} FROM {schema}.{n['chunks']} "
                f"WHERE {n['chunk_doc']} = %s ORDER BY {n['chunk_ord']} ASC",
                [doc_id])
    rows = cur.fetchall()
    return [str(_val(r, n["chunk_text"], 0)) for r in rows]


def delete_document(cur: Any, schema: str, doc_id: Any, *,
                    names: Optional[Dict[str, str]] = None) -> int:
    """删 chunks + 删 documents，返回删掉的文档数（调用方的事务里）。

    先 chunks 后 documents：不依赖外键上的 `ON DELETE CASCADE` —— 生产表不一定建了。
    """
    n = _names(names)
    cur.execute(f"DELETE FROM {schema}.{n['chunks']} WHERE {n['chunk_doc']} = %s",
                [doc_id])
    cur.execute(f"DELETE FROM {schema}.{n['documents']} WHERE {n['doc_id']} = %s "
                f"RETURNING {n['doc_id']}", [doc_id])
    row = cur.fetchone()
    return 0 if row is None else 1


def prune_ingested(cur: Any, schema: str, live_keys: Sequence[str], *,
                   names: Optional[Dict[str, str]] = None,
                   source_types: Sequence[str] = INGEST_SOURCE_TYPES) -> int:
    """死文档清理：**只**删 `source_type IN ('skill','skill_hk')` 且键不在 live 集里的。

    🔴 手动文档（`source_type='manual'`）永远不在清理范围内 —— 一条 UPDATE 的
    类型白名单就决定了「今晚这轮会不会把控制台写的东西扫掉」，别扩它。
    先删 chunks 再删 documents（无级联依赖），返回删掉的文档数。
    """
    n = _names(names)
    live = list(dict.fromkeys(live_keys))
    where = (f"{n['src_type']} = ANY(%s) AND NOT ({n['src_key']} = ANY(%s))")
    args = [list(source_types), live or [""]]
    cur.execute(f"DELETE FROM {schema}.{n['chunks']} WHERE {n['chunk_doc']} IN "
                f"(SELECT {n['doc_id']} FROM {schema}.{n['documents']} WHERE {where})",
                args)
    cur.execute(f"DELETE FROM {schema}.{n['documents']} WHERE {where} RETURNING {n['doc_id']}",
                args)
    deleted = 0
    while cur.fetchone() is not None:
        deleted += 1
    return deleted
