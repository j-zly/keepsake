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

**审核状态**（status/review_meta）：状态判据与「什么时候切块」都在这里
（`upsert_document(chunk=...)` + `patch_review_meta`），控制台端点只负责判定
「这条写入是谁发起的」并翻译 HTTP 语义 —— 与切块/幂等同一口径，不另起一套。

**归属（agent_id）**：这篇文档是**哪个代理**存进来的（凭据反查的调用方）。
列的定义（`AGENT_ID_COLUMN` / `AGENT_ID_COLUMN_DECL`）与迁移 SQL
（`AGENT_ID_MIGRATION_HINT`）**只此一处**（纪律同 `govern.registry` 的
`share_memory` 列）；写路径经 `upsert_document(agent_id=...)`，**只在 INSERT 时
写**（撞键不改归属 —— 归属是文档的身份）。控制台手建**不传**这根列 ⇒ NULL = 无主。
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

# --------------------------------------------------------------------------
# 审核状态（status：非可信来源写入的知识文档先审后用）
# --------------------------------------------------------------------------
# 判据与列定义：status ∈ pending / effective / rejected，review_meta 存审核痕迹
# （submitted_by / submitted_at / body / dup_of / reject_reason / approved_by…）。
# 灌入器与站内会话写进来的直接 `effective`（可信来源，不劳审）；**代理身份**写进来的
# 落 `pending`，进控制台待审队列。
#
# 🔴 **pending 阶段不切块**：正文存在 review_meta.body 里，等批准时才 chunk_document
# 落 chunks —— 「先入库再等审核」等于让未经审的正文先污染检索索引，等审核人的动作
# 只是事后洗白（而 pending 的文档又被检索过滤掉了，切出来的那堆块永远没人看）。
# 代价是 documents 没有正文列这件事必须靠 review_meta 兜住（见 `patch_review_meta`）。

STATUS_PENDING = "pending"
STATUS_EFFECTIVE = "effective"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_EFFECTIVE, STATUS_REJECTED)

#: 缺省状态：存量行与不显式给 status 的调用方一律按「已生效」处理（老库无该列时
#: 读路径也按这个口径兜底 —— 缺列即「全 effective」）。
DEFAULT_STATUS = STATUS_EFFECTIVE

#: 缺省类型：存量行与不传 content_type 的调用方一律按手记处理
DEFAULT_CONTENT_TYPE = CONTENT_TYPE_NOTE

#: 单块字符上限（v3 同值）。中文按字符切，不是按字节。
MAX_CHUNK_LEN = 1200

#: 灌入器负责的来源类型（死文档清理范围，只此两个）。清理的**判据本身**在
#: `scripts/kb_ingest.py`（灌入器专属：按 `(source_type, meta->>'node',
#: meta->>'skill')` 元组比对 v3 的 live 集，不属"两处共用"），这里只留类型白名单。
INGEST_SOURCE_TYPES = ("skill", "skill_hk")

# --------------------------------------------------------------------------
# 归属列（documents.agent_id）：这篇文档是**哪个代理**存进来的
# --------------------------------------------------------------------------
# 🔴 **本模块是这根列的唯一定义处**（建表与补列共用同一份 ⇒ 两边不会漂），
# 纪律与 `govern.registry` 的 `share_memory` 列同款。
#:
#: 列名。
AGENT_ID_COLUMN = "agent_id"
#: 列声明：**可空** —— NULL = **无主**（历史文档全是无主：加了列也不改存量语义）。
#: 与 `share_memory` 那种 NOT NULL + 默认值不同：这里「没有值」本身就是一种状态
#: （无主 = 收紧前的公共数据）。
AGENT_ID_COLUMN_DECL = "text NULL"
#: 老库补列的迁移 SQL —— **报给人看的，不自己跑**（生产迁移由主脑执行；
#: 代码擅自 ALTER 生产表不是本项目的活）。与 `content_type` / `tags` / `status`
#: 那几条提示同款纪律。
AGENT_ID_MIGRATION_HINT = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS agent_id text NULL;"
)

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
    # 审核状态与审核痕迹。🟡 同样是可选列，但**灌入器一个字都不往这两根写**
    # （不传 status ⇒ 靠列的 DEFAULT 'effective'），老库未迁移时控制台写路径
    # 由 `_need_status()` 给人话 500，读路径按「缺列 = 全 effective」降级。
    "status": "status",
    "rmeta": "review_meta",
    # 归属列（存这篇的代理 id）。🟡 **可选列**：老库未迁移时探不到这根键，
    # 读路径降级（空串=无主）、写路径由调用方先确认（不给就不碰这根列）。
    "agent": AGENT_ID_COLUMN,
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


def agent_source_key(source_type: str, title: str, content: str,
                     agent_id: str = "") -> str:
    """**代理写入**的文档键：`<source_type>:<agent_id>:<sha256(title+content)[:12]>`。

    🔴 **键里必须带归属代理 id**：`(source_type, source_key)` 是唯一约束，
    键不带代理 ⇒ 两个代理存同一篇内容会撞成**同一行**（后写的覆盖先写的，
    归属也被改写成后写的那个 —— 代理的归属被别人的写入悄悄改掉，是最难查的一类坏）。
    带归属 id ⇒ 各自一行、各自的归属；同代理同内容重复提交仍是 `unchanged`
    （内容级幂等不变）。

    无归属（`agent_id` 空）⇒ 退成 `<source_type>:<hash>`（本机手建路径）。
    """
    owner = str(agent_id or "").strip()
    digest = _digest(title, content)[:12]
    return f"{source_type}:{owner}:{digest}" if owner else f"{source_type}:{digest}"


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


def _status_col(n: Dict[str, str]) -> str:
    """要写 status / review_meta 就必须确认这两根列在：不在 ⇒ 抛，**不静默跳过审核**。

    「保存成功但文档其实没进待审队列」是最坏的那种坏（审核面板永远看不到它，
    而它又确实被检索过滤掉了 ⇒ 等于凭空消失）。API 侧先用 `_need_status()` 给人话
    500（带迁移 SQL），这里只是共用层的兜底断言（与 `_tags_col` 同款）。
    """
    if not n.get("status"):
        raise RuntimeError(
            "documents 表未找到 status 列，拒绝把待审文档写成「已生效」；"
            "请先执行迁移：ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
            "status text NOT NULL DEFAULT 'effective'")
    return n["status"]


def _rmeta_col(n: Dict[str, str]) -> str:
    """review_meta 列（提交正文与审核痕迹都存这儿；没有它 pending 文档就没正文）。"""
    if not n.get("rmeta"):
        raise RuntimeError(
            "documents 表未找到 review_meta 列，拒绝静默丢掉审核痕迹；"
            "请先执行迁移：ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
            "review_meta jsonb NOT NULL DEFAULT '{}'")
    return n["rmeta"]


def _agent_col(n: Dict[str, str]) -> str:
    """要写归属就得确认这根列在：不在 ⇒ 抛，**不静默丢归属**。

    与 `_tags_col` / `_status_col` 同款纪律：「存进去了但归属没存上」是最坏的
    那种坏（代理以为这篇归自己，其实成了无主的公共数据，谁都能删）。
    """
    if not n.get("agent"):
        raise RuntimeError(
            "documents 表未找到 agent_id 列，拒绝静默丢掉归属；"
            f"请先执行迁移：{AGENT_ID_MIGRATION_HINT}")
    return n["agent"]


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
                    url: str = "", tags: Optional[Sequence[str]] = None,
                    status: Optional[str] = None, chunk: bool = True,
                    agent_id: Optional[str] = None) -> Tuple[str, Any, int]:
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

    🆕 `status`（审核状态）：**`None` = 不碰这根列**（灌入器与站内会话都走这条 ——
    可信来源直进 effective，不劳审；老库未迁移时它们照样能写）。给了就整列写死，
    并参与 ON CONFLICT 的 SET：**内容变了的代理重提交会把这一行重新打回 pending**
    （新正文还没人看过，不能沿用旧正文的批准）。取值非法直接抛，不写进库里。

    🆕 `chunk=False`：**不切块**（pending 阶段的专用姿势）。返回的切片数是 0，
    正文改由调用方存进 review_meta.body，等批准时才 `replace_chunks` ——
    未经审的正文不落 chunks，检索索引就不会被它污染。

    🆕 `agent_id`：**`None` = 不碰这根列**（站内手建路径：库里留着 NULL = 无主，
    存量语义不变）；给了就在 **INSERT 时**写死归属（**不进 ON CONFLICT 的 SET** ——
    归属是这篇文档的身份，撞键时那一行已经有个归属了，不许被后来的写入改掉）。
    """
    n = _names(names)
    if status is not None and status not in STATUSES:
        raise RuntimeError(f"非法 status：{status!r}（只能是 {list(STATUSES)}）")
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
    if status is not None:
        scol = _status_col(n)
        cols.append(scol)
        vals += ", %s::text"
        params.append(status)
        sets.append(f"{scol} = EXCLUDED.{scol}")
    if agent_id is not None:
        acol = _agent_col(n)
        cols.append(acol)
        vals += ", %s::text"
        params.append(str(agent_id))
        # 🔴 **归属只在 INSERT 时写**（不进 ON CONFLICT 的 SET）：归属是这篇文档的
        # **身份**，写进去就不该被后来的写入悄悄改掉 —— 键相同（内容级幂等）时
        # 那一行已经有个归属了，改它等于「别人的文档被我抢了归属」。
    cur.execute(
        f"INSERT INTO {schema}.{n['documents']}"
        f"({', '.join(cols)}) VALUES({vals}) "
        f"ON CONFLICT ({n['src_type']}, {n['src_key']}) DO UPDATE "
        f"SET {', '.join(sets)} "
        f"RETURNING {n['doc_id']}", params)
    doc_id = _scalar(cur.fetchone())
    if not chunk:
        return ("updated" if row is not None else "added"), doc_id, 0
    count = replace_chunks(cur, schema, doc_id, text, names=names, source_path=source_key,
                           content_type=content_type, title=title, url=url)
    return ("updated" if row is not None else "added"), doc_id, count


def patch_review_meta(cur: Any, schema: str, doc_id: Any,
                      patch: Dict[str, Any], *, names: Optional[Dict[str, str]] = None) -> None:
    """把 `patch` **合并**进该文档的 `review_meta`（jsonb `||`，不覆盖已有键）。

    合并而不是整列替换：审核痕迹是**累积**的（谁提交、什么时候、驳回理由、谁批准），
    整列覆盖会在第二次动作时把第一次的痕迹抹掉 —— 台账就成了一条。
    必须与状态改动同事务（调用方的连接持有提交权）。
    """
    n = _names(names)
    cur.execute(f"UPDATE {schema}.{n['documents']} SET {_rmeta_col(n)} = "
                f"COALESCE({_rmeta_col(n)}, '{{}}'::jsonb) || %s::jsonb "
                f"WHERE {n['doc_id']} = %s",
                [json.dumps(patch or {}, ensure_ascii=False), doc_id])


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
                    tags: Optional[Sequence[str]] = None,
                    status: Optional[str] = None) -> None:
    """按 **id** 改文档行（控制台改文档走这条，不走 `upsert_document`）。

    🔴 **必须按 id 改**：手动域的 `source_key` 是 `sha256(title+content)` 派生，
    改正文/标题后键会变 —— 按新键 upsert 会**另起一行**、把旧行留成孤儿文档。
    这里同时把 `source_key` 改成新值，于是「改完之后再导一次同一篇」仍然是
    命中同一行（内容级幂等不破）。`content_hash=None` 表示只改标题。

    撞上已存在的键 ⇒ 驱动抛 UniqueViolation，**由调用方翻译成 409**
    （别在这里吞成「更新成功」，那会静默把两篇合成一篇）。

    🆕 `tags=None` 表示**不动标签**（只改标题/正文时零影响）；给了列表就是
    **整体替换**（不是追加 —— 见 `upsert_document` 同款语义）。

    🆕 `status=None` 表示**不动审核状态**（站内会话改文档：现行为零变化）；
    代理改了正文才给 `pending`，把这一行打回待审队列。
    """
    n = _names(names)
    if status is not None and status not in STATUSES:
        raise RuntimeError(f"非法 status：{status!r}（只能是 {list(STATUSES)}）")
    sets = [f"{n['title']} = %s", f"{n['src_key']} = %s", f"{n['updated']} = now()"]
    params: List[Any] = [title, source_key]
    if content_hash is not None:
        sets.append(f"{n['hash']} = %s")
        params.append(content_hash)
    if tags is not None:
        sets.append(f"{_tags_col(n)} = %s::text[]")
        params.append(list(tags))
    if status is not None:
        sets.append(f"{_status_col(n)} = %s::text")
        params.append(status)
    params.append(doc_id)
    cur.execute(f"UPDATE {schema}.{n['documents']} SET {', '.join(sets)} "
                f"WHERE {n['doc_id']} = %s", params)


def load_document(cur: Any, schema: str, doc_id: Any, *,
                  names: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """取一行文档，**返回逻辑键**（`id`/`source_type`/`source_key`/`title`/
    `content_hash`/`content_type`/`status`/`agent_id`），不存在 ⇒ None。
    调用方不用关心真实列名。

    🟡 `content_type` / `status` / `agent_id` 是**可选投影**：`names["ctype"]` /
    `names["status"]` / `names["agent"]` 为假值（调用方探到表里没这列）时
    SELECT `''` —— 老库未迁移时读路径照常返回，不裸炸（写路径另由 `_write_names`
    的必需列判据挡住，见 `routers/kb.py`）。
    🔴 缺 status 列时这里给 `effective`：**缺列 = 全 effective**（存量行全可见，
    与未迁移时的既有行为一字不差）。
    🔴 缺 agent_id 列时这里给空串 = **无主**（与「库里就是 NULL」同义）——
    存量文档全是无主，缺列不能把可见性判成「不可见」。
    """
    n = _names(names)
    # 显式 AS：dict_row 拿的是列名，裸 `SELECT ''` 的键是 "?column?"，取不到
    sel_ctype = f"{n['ctype']} AS content_type" if n.get("ctype") else "'' AS content_type"
    sel_status = f"{n['status']} AS status" if n.get("status") else "'' AS status"
    sel_agent = f"{n['agent']} AS agent_id" if n.get("agent") else "'' AS agent_id"
    cur.execute(f"SELECT {n['doc_id']}, {n['src_type']}, {n['src_key']}, "
                f"{n['title']}, {n['hash']}, {sel_ctype}, {sel_status}, {sel_agent} "
                f"FROM {schema}.{n['documents']} WHERE {n['doc_id']} = %s", [doc_id])
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": _val(row, n["doc_id"], 0),
            "source_type": _val(row, n["src_type"], 1),
            "source_key": _val(row, n["src_key"], 2),
            "title": _val(row, n["title"], 3),
            "content_hash": _val(row, n["hash"], 4),
            "content_type": _val(row, "content_type", 5) or DEFAULT_CONTENT_TYPE,
            "status": _val(row, "status", 6) or DEFAULT_STATUS,
            "agent_id": str(_val(row, "agent_id", 7) or "")}


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
