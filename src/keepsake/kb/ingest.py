"""切块 + 幂等 upsert —— **知识库写入的唯一判据来源**。

收编自 180 本机灌入器 `kb_ingest_v3.py`（见 `scripts/kb_ingest.py`）：切块逻辑
`split_chunks`、内容 hash 判据、`documents`/`chunks` 的落库顺序全部**逐字保持**
v3 口径，控制台的写端点（`api/routers/kb.py`）也走这里 —— 两处各切一次块，
同一篇文档在页面上的切片数和检索命中就会对不上。

**按内容类型切块**（`chunk_document` 注册表）：note/article/skill 逐字走
`split_chunks`（分类只分池子，不改粒度），link 存单块 `标题\\nURL\\n摘要`
（一张卡片一条记录，切碎会命中半截裸 URL）。类型分派与切块必须同一个函数，
否则「link 被切碎」要等到检索命中才看得见。

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

# --------------------------------------------------------------------------
# 内容类型（content_type：**分内容类型**，与 source_type 分「来源」是两回事）
# --------------------------------------------------------------------------
# 统一一张 documents 表 + 一根 content_type 列：一把尺子切所有内容的后果是
# 链接卡片被当长文硬切、技能文档与手记混在一个池子里互相淹没。列有 DEFAULT，
# 存量行不补也能读；新增类型只改这里 + 注册表一行。

CONTENT_TYPE_SKILL = "skill"        # 技能灌入（含 skill_hk 来源），仅灌入器能建
CONTENT_TYPE_NOTE = "note"          # 手记/粘贴正文（现 manual 语义），界面缺省值
CONTENT_TYPE_ARTICLE = "article"    # 长文章（结构预留，本期行为与 note 逐字相同）
CONTENT_TYPE_LINK = "link"          # 链接卡片：URL + 摘要，**不切正文**
CONTENT_TYPE_IMAGE = "image"        # 图文：枚举占位，创建端点 400（依赖向量路，后置）

#: 全部合法枚举（写面校验与过滤白名单共用这一份）
CONTENT_TYPES = (CONTENT_TYPE_SKILL, CONTENT_TYPE_NOTE, CONTENT_TYPE_ARTICLE,
                 CONTENT_TYPE_LINK, CONTENT_TYPE_IMAGE)

#: 缺省类型：存量行与不传 content_type 的调用方一律按手记处理
DEFAULT_CONTENT_TYPE = CONTENT_TYPE_NOTE

#: 单块字符上限（v3 同值）。中文按字符切，不是按字节。
MAX_CHUNK_LEN = 1200

#: 灌入器负责的来源类型（死文档清理范围，只此两个）。清理的**判据本身**在
#: `scripts/kb_ingest.py`（灌入器专属：按 `(source_type, meta->>'node',
#: meta->>'skill')` 元组比对 v3 的 live 集，不属"两处共用"），这里只留类型白名单。
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
    "ctype": "content_type",
    # 自由标签列。🟡 **可选列**（与 ctype 同款纪律）：老库未迁移时这根键探不到，
    # 读路径降级、写路径由调用方先确认（API 侧 `_need_tags()` 给人话 500）。
    # 灌入器不传 tags ⇒ 压根不往这根列写（技能文档恒为 '{}'，界面上不给它开打标入口）。
    "tags": "tags",
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
# 按类型切块（注册表分派）
# --------------------------------------------------------------------------

def link_body(title: str, url: str, summary: str = "") -> str:
    """链接卡片的**唯一一块**正文：`标题\\nURL\\n摘要`（摘要可空 ⇒ 省略该行）。

    🟡 **刻意为之**：链接不进 `split_chunks` 的标题分段/硬切 —— 一张卡片是
    一条记录，硬切出来的后半截是断头链接（"## 标题" 段 + 裸 URL 段），
    检索命中后 snippet 是半句话。摘要再长也不切（`ponytail:` 上限即调用方
    应控的摘要长度；要放开就改成按 max_len 切并保留 URL 行作为每块前缀）。
    """
    return "\n".join(p for p in (title, url, summary) if p)


def _chunker_text(text: str, *, title: str = "", url: str = "") -> List[str]:
    """skill / note / article 共用：`split_chunks` 逐字不变（article 只是结构预留）。"""
    return split_chunks(text)


def _chunker_link(text: str, *, title: str = "", url: str = "") -> List[str]:
    """link：单块组合文本，不切正文。"""
    return [link_body(title, url, text)]


#: content_type → 切块策略。新增类型只加一行 + 上面的枚举常量。
#: 未登记的类型按「文本」处理（存量行/脏值不炸，切块永远有兜底）。
CHUNK_REGISTRY = {
    CONTENT_TYPE_SKILL: _chunker_text,
    CONTENT_TYPE_NOTE: _chunker_text,
    CONTENT_TYPE_ARTICLE: _chunker_text,
    CONTENT_TYPE_LINK: _chunker_link,
    CONTENT_TYPE_IMAGE: _chunker_text,
}


def chunk_document(text: str, content_type: str = DEFAULT_CONTENT_TYPE,
                   *, title: str = "", url: str = "") -> List[str]:
    """按内容类型切块（注册表分派）——**写路径唯一的切块入口**。

    灌入器、控制台创建/编辑都走这里（`split_chunks` 只作为注册表里的文本策略
    出现）：类型分派与切块必须同一个函数，否则「link 被切碎」这种偏差要等到
    检索命中才看得见。
    """
    return CHUNK_REGISTRY.get(content_type, _chunker_text)(text, title=title, url=url)


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


def _tags_col(n: Dict[str, str]) -> str:
    """要写 tags 就必须确认这根列在：不在 ⇒ 抛，**不静默丢标签**。

    「保存成功但标签没存上」是最坏的那种坏（用户以为打了标，筛的时候找不到）。
    API 侧先用 `_need_tags()` 给人话 500（带迁移 SQL），这里只是共用层的兜底断言。
    """
    if not n.get("tags"):
        raise RuntimeError(
            "documents 表未找到 tags 列，拒绝静默丢标签；"
            "请先执行迁移：ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
            "tags text[] NOT NULL DEFAULT '{}'")
    return n["tags"]


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
                   source_path: str = "",
                   content_type: str = DEFAULT_CONTENT_TYPE,
                   title: str = "", url: str = "") -> int:
    """删掉该文档的旧切片 + 按 `content_type` 整体重插，返回新切片条数。

    必须在调用方的事务里（删+插要么都成要么都不成）；`source_path` 写回
    source_key（v3 同款，检索结果里看得出这块来自哪个 key）。

    🔴 **切块走 `chunk_document`（按类型注册表）而不是直接 `split_chunks`**：
    link 是单块，改文档时也必须仍按 link 单块重存，否则「改个摘要把 URL 切没了」。
    """
    n = _names(names)
    cur.execute(f"DELETE FROM {schema}.{n['chunks']} WHERE {n['chunk_doc']} = %s",
                [doc_id])
    rows = [(doc_id, i, c, source_path)
            for i, c in enumerate(chunk_document(text, content_type, title=title, url=url))]
    for row in rows:
        cur.execute(f"INSERT INTO {schema}.{n['chunks']}"
                    f"({n['chunk_doc']}, {n['chunk_ord']}, {n['chunk_text']}, {n['chunk_path']}) "
                    f"VALUES(%s, %s, %s, %s)", row)
    return len(rows)


def upsert_document(cur: Any, schema: str, *, source_type: str, source_key: str,
                    title: str, meta: Optional[Dict[str, Any]], content_hash: str,
                    text: str, names: Optional[Dict[str, str]] = None,
                    content_type: str = DEFAULT_CONTENT_TYPE,
                    url: str = "", tags: Optional[Sequence[str]] = None) -> Tuple[str, Any, int]:
    """按 `(source_type, source_key)` 幂等落一篇文档，返回 `(状态, doc_id, 切片数)`。

    状态判据与 v3 同款，三种：
    * `unchanged`：同键且 `content_hash` 没变 ⇒ **提前返回，连 INSERT 都不发**
      （夜间全量跑的快路径：别把没改的文档重切成新的切片、别刷新 updated_at）；
    * `added`   ：新键；
    * `updated` ：键在但 hash 变了 ⇒ 改行 + 删旧 chunks + 重插。

    `meta` 序列化成 jsonb（v3 同款 `%s::jsonb`）；`updated_at` 交给库里的 `now()`。

    🔴 `content_type` 显式随 INSERT 写死（不走列 DEFAULT）：DEFAULT 只保证
    **存量/别的写入方**能落，本函数的值是调用方的判断（灌入器=skill、控制台按
    用户选的类型），靠 DEFAULT 兜底等于把这判断丢掉。

    🆕 `tags`：🔴 **`None` = 不碰这根列**（灌入器口径：技能文档恒为 `{}`，
    灌入器一个字都不往这写，界面上也不给它开打标入口）；给了列表才整列覆盖写
    （**替换不是追加**，净化已在上游做完）。`%s::text[]` 显式 cast：PG 推不出
    Python 列表的类型，不写 cast 要么驱动乱适配、要么 `could not determine
    data type of parameter`。
    """
    n = _names(names)
    cur.execute(f"SELECT {n['doc_id']}, {n['hash']} FROM {schema}.{n['documents']} "
                f"WHERE {n['src_type']} = %s AND {n['src_key']} = %s",
                [source_type, source_key])
    row = cur.fetchone()
    if row is not None and _val(row, n["hash"], 1) == content_hash:
        return ("unchanged", _val(row, n["doc_id"], 0),
                _count_chunks(cur, schema, n, _val(row, n["doc_id"], 0)))
    cols = [n["src_type"], n["src_key"], n["title"], n["meta"],
            n["hash"], n["ctype"], n["updated"]]
    vals = "%s, %s, %s, %s::jsonb, %s, %s, now()"
    params: List[Any] = [source_type, source_key, title,
                         json.dumps(meta or {}, ensure_ascii=False), content_hash, content_type]
    sets = [f"{n['title']} = EXCLUDED.{n['title']}",
            f"{n['meta']} = EXCLUDED.{n['meta']}",
            f"{n['hash']} = EXCLUDED.{n['hash']}",
            f"{n['ctype']} = EXCLUDED.{n['ctype']}",
            f"{n['updated']} = now()"]
    if tags is not None:
        tcol = _tags_col(n)
        cols.append(tcol)
        vals += ", %s::text[]"
        params.append(list(tags))
        sets.append(f"{tcol} = EXCLUDED.{tcol}")
    cur.execute(
        f"INSERT INTO {schema}.{n['documents']}"
        f"({', '.join(cols)}) VALUES({vals}) "
        f"ON CONFLICT ({n['src_type']}, {n['src_key']}) DO UPDATE "
        f"SET {', '.join(sets)} "
        f"RETURNING {n['doc_id']}", params)
    doc_id = _scalar(cur.fetchone())
    count = replace_chunks(cur, schema, doc_id, text, names=names, source_path=source_key,
                           content_type=content_type, title=title, url=url)
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
                    names: Optional[Dict[str, str]] = None,
                    tags: Optional[Sequence[str]] = None) -> None:
    """按 **id** 改文档行（控制台改文档走这条，不走 `upsert_document`）。

    🔴 **必须按 id 改**：手动域的 `source_key` 是 `sha256(title+content)` 派生，
    改正文/标题后键会变 —— 按新键 upsert 会**另起一行**、把旧行留成孤儿文档。
    这里同时把 `source_key` 改成新值，于是「改完之后再导一次同一篇」仍然是
    命中同一行（内容级幂等不破）。`content_hash=None` 表示只改标题。

    撞上已存在的键 ⇒ 驱动抛 UniqueViolation，**由调用方翻译成 409**
    （别在这里吞成「更新成功」，那会静默把两篇合成一篇）。

    🆕 `tags=None` 表示**不动标签**（只改标题/正文时零影响）；给了列表就是
    **整体替换**（不是追加 —— 见 `upsert_document` 同款语义）。
    """
    n = _names(names)
    sets = [f"{n['title']} = %s", f"{n['src_key']} = %s", f"{n['updated']} = now()"]
    params: List[Any] = [title, source_key]
    if content_hash is not None:
        sets.append(f"{n['hash']} = %s")
        params.append(content_hash)
    if tags is not None:
        sets.append(f"{_tags_col(n)} = %s::text[]")
        params.append(list(tags))
    params.append(doc_id)
    cur.execute(f"UPDATE {schema}.{n['documents']} SET {', '.join(sets)} "
                f"WHERE {n['doc_id']} = %s", params)


def load_document(cur: Any, schema: str, doc_id: Any, *,
                  names: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """取一行文档，**返回逻辑键**（`id`/`source_type`/`source_key`/`title`/
    `content_hash`/`content_type`），不存在 ⇒ None。调用方不用关心真实列名。

    🟡 `content_type` 是**可选投影**：`names["ctype"]` 为假值（调用方探到表里
    没这列）时 SELECT `''` —— 老库未迁移时读路径照常返回，不裸炸（写路径另由
    `_write_names` 的必需列判据挡住，见 `routers/kb.py`）。
    """
    n = _names(names)
    # 显式 AS：dict_row 拿的是列名，裸 `SELECT ''` 的键是 "?column?"，取不到
    sel_ctype = f"{n['ctype']} AS content_type" if n.get("ctype") else "'' AS content_type"
    cur.execute(f"SELECT {n['doc_id']}, {n['src_type']}, {n['src_key']}, "
                f"{n['title']}, {n['hash']}, {sel_ctype} FROM {schema}.{n['documents']} "
                f"WHERE {n['doc_id']} = %s", [doc_id])
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": _val(row, n["doc_id"], 0),
            "source_type": _val(row, n["src_type"], 1),
            "source_key": _val(row, n["src_key"], 2),
            "title": _val(row, n["title"], 3),
            "content_hash": _val(row, n["hash"], 4),
            "content_type": _val(row, "content_type", 5) or DEFAULT_CONTENT_TYPE}


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
