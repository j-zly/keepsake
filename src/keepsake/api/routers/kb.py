"""知识库接口：三表计数 + 全文检索（只读）+ 手动文档 CRUD/导入/导出（写）。

**唯一允许直接连 PG 的地方**（任务书例外条款）：连接串只从 `KEEPSAKE_PG_DSN`
环境变量读，代码里不出现任何连接串/口令。读端点只跑 `SELECT`；写端点只碰
`source_type='manual'` 的文档（skill/skill_hk 是灌入器的地盘，见文末说明）。

🔴 **连接 / 探列 / 写事务这一层在 `keepsake.kb.service`**（本模块 import 它）：
那一层是 **FastAPI-free** 的 —— 代理面的知识库工具（MCP 的 `kb_store` /
`kb_search` / `kb_get` / `kb_remove`）也要用同一份，而 MCP 那边**不一定装了
控制台的 fastapi**（`pyproject.toml` 里 fastapi 压根不是依赖）。**口径只有一份**：
检索 / 归属 / 可见性 / 删除判据全在 service 层（它调 `govern.visibility` 那一把
尺子），控制台与代理面共用。

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
可改删；agent=代理写入；skill/skill_hk=灌入器只读）；`content_type` 分
「这是个什么东西」（skill 技能 / note 手记 / article 长文 / link 链接卡片 /
image 图文占位）。分类的作用是**按类型切块**（link 单块、其余逐字走
`split_chunks`）与**按类型过滤**，不改任何既有的域隔离与幂等判据。类型创建后
不可变（双挡：前端不给入口 + 后端带异值 400）。`image` 本期只占枚举位：
创建端点 400（依赖向量路，后置）。

**自由标签（tags）是第三个维度，不是第二个类型**：content_type 是封闭枚举（写面
校验白名单），标签是开放维度（一篇可挂多个、随时可改、只影响筛选）。标签**不进**
`source_key`/`content_hash`（进了就等于「改个标签变成换一篇文档」），也**不碰**切块与
来源域；skill 文档标签恒空 —— 灌入器一个字都不往那根列写，界面也不给它开打标入口
（沿用 isManual 那套 409 纪律）。

**归属列（documents.agent_id）**：存这篇的**代理 id**（凭据反查的调用方）。
控制台手建**不写**这根列（NULL = 无主，存量语义不变）；代理写入经
`keepsake.kb.service.store_document` 打上归属。列表出参带 `agent_id`（无主时
**不出这个键**），并支持 `?agents=a,b` 服务端筛选（切页之前）。列的定义与迁移
SQL 在 `keepsake.kb.ingest`（唯一定义处）。

**审核状态**（批2）：`status` ∈ pending/effective/rejected + `review_meta` jsonb。
判据只有一条 —— **谁在写**：站内会话（require_session 的界面路径）与灌入器是可信
来源，直进 `effective`（现行为零变化，手建零审核）；**代理身份**（请求头带 `ksk_`
凭据、注册表核出来不是 primary）写进来的落 `pending`，进控制台待审队列，且
**pending 阶段不切块**（正文存 `review_meta.body`，批准时才 `chunk_document` 落
chunks —— 先切块就是让未审正文先污染检索索引，而审核动作只是事后洗白）。
待审/已驳回的文档对列表、检索、导出**一律不可见**（`status='effective'` 谓词），
只有审核视图 `/kb/pending` 看得见。老库未迁移这两根列 ⇒ **读路径一律当全
effective**（与 content_type / tags 同款降级），写路径由 `_need_status()` 人话 500。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi import Path          # 文档切片的路径参数（P9 新增；单独一行，不动上一行的 import)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from keepsake.govern.registry import CREDENTIAL_PREFIX, ROLE_PRIMARY, Registry
from keepsake.kb import ingest
from keepsake.kb import service as kb_service
from keepsake.kb.service import KbError

from .. import audit, config
from ..deps import get_storage, require_session
from ..schemas import KbHit, KbSearchResult, KbSummary, KbTagCount, KbTagCounts, KbTypeCounts
from ..schemas import KbChunkList, KbDocument, KbDocumentList
from ..schemas import KbPendingItem, KbPendingList
from .ops import _need_flag, _need_text, _reject, write_guard

# ---- 数据层（连接 / 探列 / 写事务）搬到 `keepsake.kb.service` ---------------
# 那一层是 **FastAPI-free** 的：MCP 工具面（代理的知识库四工具 kb_store /
# kb_search / kb_get / kb_remove）也要用它，而 MCP 那边**不一定装了控制台的
# fastapi**（`pyproject.toml` 里 fastapi 压根不是依赖）。
# 名字在本模块照旧可见（既有测试与运维脚本按 `kb_router._connect` 这类名字打
# 补丁 / 探针）—— **位置不变、语义不变**，只有实现在 service 里。
from keepsake.kb.service import (  # noqa: F401 —— 搬走但名字不变的公共面
    _CAND_AGENT_ID, _CAND_CTYPE, _CAND_DOC, _CAND_ID, _CAND_RMETA,
    _CAND_SRC_KEY, _CAND_SRC_TYPE, _CAND_STATUS, _CAND_TAGS, _CAND_TEXT,
    _CAND_TIME, _CAND_TITLE, _CAND_TSV,
    _COLS_CACHE,                      # 列名缓存：既有测试按 `kb_router._COLS_CACHE` 清它
    _STATUS_MIGRATION_HINT, _TAGS_MIGRATION_HINT, _CTYPE_MIGRATION_HINT,
    SOURCE_TYPE_RE as _SOURCE_TYPE_RE,
    connect as _connect, ident as _ident, missing as _missing, pick as _pick,
    table_columns as _table_columns, write_conn as _write_conn,
    write_names as _write_names,
)

router = APIRouter(tags=["kb"])

#: 导出护栏：默认 2000 条、封顶 20000（切片正文体积大，别一次拖全库）
EXPORT_LIMIT_DEFAULT = 2000
EXPORT_LIMIT_MAX = 20000

#: 单次导入条数上限（同上：超了 400，不默默截断）
IMPORT_MAX_ITEMS = 2000

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

#: 标签净化后的硬上限（前端即时提示引的就是这两个数 —— 一处定、两处抄，勿各自写死）。
#: **本体在 `keepsake.kb.service`**（代理面与控制台共用同一份净化口径）。
TAG_MAX_ITEMS = kb_service.TAG_MAX_ITEMS
TAG_MAX_LEN = kb_service.TAG_MAX_LEN

#: 待审面板的正文预览长度（前端照抄这个数，别自己定另一档）
PENDING_PREVIEW_LEN = 500

#: 代理写入被拒时的文案（与 `mcp_server.server.GATE_DENIED` 同款口径：只说身份与
#: 去处，**不回显任何凭据内容**）。未批准/已吊销的代理连队列都不进。
AGENT_DENIED_NOTE = (
    "代理 {agent_id} 未批准/已吊销（或凭据无效），知识库写入一律拒绝 —— "
    "请到控制台代理治理页批准该代理并领取 ksk_ 凭据后重试"
)


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
    """知识库全文命中：有 tsvector 列走 tsquery，否则退 ILIKE（只读，不建索引）。

    🔴 **检索本体在 `keepsake.kb.service.search_documents`**（与代理面的
    `kb_search` 工具**同一个函数**）：控制台不过滤（人看全量），代理面传自己的
    可见集合（非 primary 只看得见自己 + 已共享代理名下的 + 无主的老文档）。
    **可见性判据只有一份**（`govern.visibility.visible_agent_ids`），
    两处各写一份必然漂。
    """
    out = kb_service.search_documents(query=q, limit=page_size, page=page)
    items = [
        KbHit(chunk_id=str(r["chunk_id"]), doc_id=str(r["doc_id"] or ""),
              title=str(r["title"] or ""), snippet=str(r["snippet"] or ""),
              # 老表没这列时投影是 '' ⇒ 出参按 schema 规则省略该键（与 source_type 同款）
              content_type=str(r.get("content_type") or ""),
              tags=_tags_out(r.get("tags")),
              rank=round(float(r.get("score") or 0.0), 6))
        for r in out["items"]
    ]
    return KbSearchResult(q=q, items=items, count=len(items), page=page,
                          page_size=page_size, mode=str(out.get("mode") or "ilike"))


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
    # 🆕 **来源代理筛选**（`agents=a,b`）：与记忆页的 `agents` 参数同款 ——
    # 逗号分隔的 agent_id 列表，空 = 不过滤（= 现状不变）。
    agents: str = "",
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

    🆕 `agents` 是**来源代理**的精确过滤（`d.agent_id = ANY(...)`，值走参数）。
    🔴 **这是控制台的读面筛选**（按归属过滤显示哪些文档），**不是权限判定** ——
    权限在 `govern.visibility`（谁看得见）与 MCP 工具面（谁能删）那边，两件事。
    🔴 **过滤必须发生在切页之前**（服务端过滤）：只在前端切当前页 = 看着筛了、
    其实漏了（总数与翻页都会骗人）。
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
    agent_ids = [a.strip() for a in str(agents or "").split(",") if a.strip()]
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
    d_status = next((_ident(c) for c in _CAND_STATUS if c in docs), None)
    d_agent = next((_ident(c) for c in _CAND_AGENT_ID if c in docs), None)

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
    if d_status:
        # 🆕 审核过滤：默认列表**只列已生效的**（待审/已驳回的去审核面板看），
        # 与检索/导出同一口径。放在最后 ⇒ 与 q / source_type / content_type / tag 取 AND。
        # 老库没有 status 列 ⇒ 不加这个谓词（存量行照常可见，与迁移前一字不差）。
        where, params = f"{where} AND d.{d_status} = %s", [*params, ingest.STATUS_EFFECTIVE]
    if agent_ids:
        # 🆕 **来源代理筛选**（控制台读面筛选，与记忆页的 `agents` 参数同款模式）：
        # 服务端过滤（**切页之前**）—— 只在前端切当前页 = 看着筛了、其实漏了。
        # 值走参数（`= ANY(%s::text[])`），不拼进 SQL 文本。
        if not d_agent:
            _reject(400, "kb_schema_unknown",
                    f"{config.KB_TABLE_DOCUMENTS} 表未找到 agent_id 列，无法按来源代理过滤"
                    f"（请先执行迁移：{ingest.AGENT_ID_MIGRATION_HINT}）",
                    "kb.documents", "list")
        where, params = (f"{where} AND d.{d_agent} = ANY(%s::text[])",
                         [*params, agent_ids])

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
    # 归属列同理：老库没这列 ⇒ 投影空串（出参省略该键 = 无主），不假装每篇都有归属
    sel_agent = f"d.{d_agent}" if d_agent else "''"

    sql = (f"SELECT CAST(d.{d_id} AS text) AS doc_id, {sel_title} AS title, "
           f"{sel_time} AS updated_at, {sel_src} AS source_type, "
           f"{sel_ct} AS content_type, {sel_tags} AS tags, {sel_agent} AS agent_id, "
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
                   tags=_tags_out(r.get("tags")),
                   # 同款降级：归属列探不到 ⇒ 空串（= 无主），出参省略该键
                   agent_id=str(r.get("agent_id") or ""))
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


def _now_iso() -> str:
    """审核痕迹里的时间戳（UTC ISO，与注册表 `Registry._now_iso` 同款口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 写入方身份（可信来源 vs 待审来源）
# --------------------------------------------------------------------------
# 🔴 **判据是「谁在写」，不是「调用方自称是谁」**：请求头带 `ksk_` 凭据 ⇒ 走注册表
# `verify_any` 反查身份（与 `mcp_server.server.AgentGate` 的 http 路径**同一个函数**，
# 不发明第二条身份链）；否则按站内会话 `require_session` 放行。
# 结论只有三种：站内会话/主代理 = 可信（直进 effective）、子代理 = 待审（pending）、
# 未批准/已吊销/凭据无效 = 403（连队列都不进，与 agent_gate 同一 fail-closed 方向）。

def _agent_registry(storage: Any) -> Registry:
    """按控制台的存储后端装配注册表（与 `routers/agents.py` 同一份口径）。"""
    registry = Registry(storage)
    registry.ensure_schema()
    return registry


def _bearer(raw: Optional[str]) -> str:
    """取 `Authorization: Bearer x` 里的 x；不是这种形态给空串（不抛）。"""
    s = str(raw or "").strip()
    return s[len("Bearer "):].strip() if s.startswith("Bearer ") else ""


def writer_identity(authorization: Optional[str] = Header(default=None),
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """写端点的**调用方身份**（认证 + 「这条写入要不要审」的判据，一处判完）。

    * 站内会话（控制台 token / 登录会话）⇒ `kind=console`、`status=effective`：
      **站内人工路径零变化，不给手建加审核**；
    * 主代理凭据（`role=primary`）⇒ `kind=agent`、`status=effective`：可信来源；
    * 子代理凭据（`role=sub`）⇒ `status=pending`：进待审队列，**服务端判定、
      不信调用方自称**（请求体里没有任何「我要不要审」的字段）；
    * 凭据无效 / 未批准 / 已吊销 ⇒ 403（`_reject` 先审计再抛，与闸门同一方向）。

    🟡 `storage` 依赖只在代理凭据那条路上真的用到（`get_storage` 是进程级单例，
    站内会话不碰它）；注册表装不出来（存储不可达）也按**拒绝**处理 —— 核不了身份
    就放行，等于把闸门关着。
    """
    credential = _bearer(authorization)
    if not credential.startswith(CREDENTIAL_PREFIX):
        require_session(authorization)          # 站内会话：原样抛 401，判据一字不改
        return {"kind": "console", "agent_id": "", "role": "",
                "status": ingest.STATUS_EFFECTIVE}
    try:
        res = dict(_agent_registry(storage).verify_any(credential))
    except Exception as e:                       # noqa: BLE001 —— 核不了身份 ⇒ 拒（不放行）
        _reject(403, "agent_denied",
                f"代理身份核验失败（{e.__class__.__name__}），知识库写入一律拒绝",
                "kb.write", "agent")
    if not res.get("ok"):
        _reject(403, "agent_denied",
                AGENT_DENIED_NOTE.format(agent_id=res.get("agent_id") or "未知"),
                "kb.write", str(res.get("agent_id") or "unknown"))
    role = str(res.get("role") or "")
    agent_id = str(res.get("agent_id") or "")
    return {"kind": "agent", "agent_id": agent_id, "role": role,
            "status": (ingest.STATUS_EFFECTIVE if role == ROLE_PRIMARY
                       else ingest.STATUS_PENDING)}


def _pending_of(writer: Dict[str, Any]) -> bool:
    """这次写入要不要进待审队列（只有非主代理写入为真）。"""
    return str((writer or {}).get("status") or "") == ingest.STATUS_PENDING


def _need_status(names: Dict[str, str], action: str, target: str) -> str:
    """写 status / review_meta 前必须确认这两根列在（老库未迁移 ⇒ 人话 500 不裸崩）。"""
    if not names.get("status") or not names.get("rmeta"):
        _reject(500, "kb_schema_unknown",
                f"{config.KB_TABLE_DOCUMENTS} 表未找到 status / review_meta 列，"
                f"无法写入审核状态；请先执行迁移：{_STATUS_MIGRATION_HINT}",
                action, target)
    return names["status"]


def _rmeta_out(raw: Any) -> Dict[str, Any]:
    """review_meta 出参归一：jsonb 可能是对象、字符串（psycopg2/别的客户端），
    或 NULL（列刚建、老行没填）—— 一律成 `dict[str, Any]`，解析不了给空字典。"""
    val = _jsonable(raw)
    return val if isinstance(val, dict) else {}


def _rmeta_of(cur: Any, schema: str, doc_id: Any, names: Dict[str, str]) -> Dict[str, Any]:
    """取该文档的 review_meta（老库没这列 ⇒ 空字典，调用方按「没有审核痕迹」处理）。"""
    if not names.get("rmeta"):
        return {}
    cur.execute(f"SELECT {names['rmeta']} AS rmeta FROM {schema}.{names['documents']} "
                f"WHERE {names['doc_id']} = %s", [doc_id])
    row = cur.fetchone()
    return _rmeta_out(row.get("rmeta")) if isinstance(row, dict) else {}


def _need_effective(doc: Dict[str, Any], doc_id: str, action: str) -> str:
    """改 / 删只对**已生效**的文档开放：待审与已驳回的归审核面板管。

    不挡的话，控制台照 id 改一篇待审文档就会把新正文切进 chunks，而 review_meta.body
    还是旧的 —— 批准时切出来的块与正文对不上，且没人看得见（待审文档检索不到）。
    """
    st = str((doc or {}).get("status") or ingest.STATUS_EFFECTIVE)
    if st != ingest.STATUS_EFFECTIVE:
        _reject(409, "kb_doc_not_effective",
                f"知识库文档还在审核队列里（status={st}），改/删请到待审面板批准或驳回："
                f"{doc_id}", action, doc_id)
    return st


def _dup_of(cur: Any, schema: str, names: Dict[str, str], title: str,
            source_key: str) -> Dict[str, Any]:
    """代理提交前的**查重提示**（title 相同即记一笔，**不拒收**）。

    🔴 为什么记而不是拒：子代理批量提交同名文档是常态（同一套模板多篇），直接拒
    等于让它改标题绕过，反而更脏。审核的人看见「这篇与 #12 同名」才是有用的信息。
    查重范围含近重复（标题被对方包含 / 包含对方），最多 3 条，够提示即可。

    排除自己那行（按 `source_key`）：重复提交同一篇不该把自己当成重复。
    """
    if not title or not names.get("title"):
        return {}
    cur.execute(f"SELECT CAST({names['doc_id']} AS text) AS id, {names['title']} AS title "
                f"FROM {schema}.{names['documents']} "
                f"WHERE {names['src_key']} <> %s AND {names['title']} IS NOT NULL "
                f"AND ({names['title']} = %s OR {names['title']} ILIKE %s "
                f"     OR %s ILIKE {names['title']}) "
                f"ORDER BY {names['doc_id']} ASC LIMIT 3",
                [source_key, title, f"%{title}%", f"%{title}%"])
    rows = cur.fetchall() or []
    exact = [r for r in rows if str(r.get("title") or "") == title]
    hit = exact[0] if exact else (rows[0] if rows else None)
    if hit is None:
        return {}
    return {"dup_of": str(hit.get("id") or ""),
            "dup_of_title": str(hit.get("title") or ""),
            "dup_exact": bool(exact)}


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

    🔴 **净化本体在 `keepsake.kb.service.clean_tags`**（代理面的 `kb_store` 工具
    与控制台三个写端点**共用同一份**规则）—— 这里只把它的错误翻译成控制台的
    400（先审计再抛，与 `_reject` 同纪律）。

    规则（本体那份）：非列表当没给 / 逐项 strip / 空串丢 / 控制字符拒 /
    单项 ≤32 字 / 去重保序 / 最多 12 个。**不静默截断**：用户打了 13 个标却被
    悄悄砍掉一个，回头按那个标筛会以为「标签坏了」。
    """
    try:
        return kb_service.clean_tags(raw)
    except KbError as e:
        _reject(e.status, e.code, e.message, action, target)


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

    🆕 **代理域（source_type='agent'）的文档也不许从控制台改/删**：那是代理自己
    名下的内容（归属写在 `documents.agent_id`），归它自己管（`kb_remove`）——
    控制台的手建域只管手建的。
    """
    if str(doc.get("source_type") or "") != ingest.MANUAL_SOURCE_TYPE:
        _reject(409, "kb_doc_immutable",
                f"只有手动文档（source_type={ingest.MANUAL_SOURCE_TYPE}）能改/删："
                f"{doc_id} 来自 {doc.get('source_type') or '未知来源'}，"
                "由夜间灌入器维护（改动会被覆盖或被死文档清理扫掉）",
                action, doc_id)


def _not_ingester_doc(doc: Dict[str, Any], doc_id: str, action: str) -> None:
    """**审核面**（批准 / 驳回）只挡灌入器的地盘 —— 代理域的文档也得能审。

    🔴 代理通过 `kb_store` 提交的知识文档落 `source_type='agent'`（归属在
    `documents.agent_id`），**本来就进待审队列** —— 审核端点若只认手动域，
    这些文档就永远批不了（进了队列却没人能批 = 死队列）。
    """
    if str(doc.get("source_type") or "") in ingest.INGEST_SOURCE_TYPES:
        _reject(409, "kb_doc_immutable",
                f"{doc_id} 来自 {doc.get('source_type') or '未知来源'}，"
                "由夜间灌入器维护（改动会被覆盖或被死文档清理扫掉），不进审核队列",
                action, doc_id)


@router.post("/api/v1/kb/documents",
             dependencies=[Depends(writer_identity), Depends(write_guard)])
def kb_create_document(body: KbCreateBody,
                       writer: Dict[str, Any] = Depends(writer_identity)) -> Dict[str, Any]:
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

    🆕 **子代理写入 = 进待审队列**（`writer_identity` 判，调用方自称不算数）：
    落 `status='pending'`、正文存 `review_meta.body`、**不切块**（批准时才切），
    并记一条同标题查重提示（`review_meta.dup_of`，不拒收）。站内会话与主代理
    走现路径：`status` 根本不碰（靠列 DEFAULT）、照旧切块入库 —— **零变化**。
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
    pending = _pending_of(writer)
    if pending:
        _need_status(names, action, title or "-")
    basis = _doc_basis(title, content, ctype, url)
    # 🆕 **归属**：代理身份写进来的（请求头带 ksk_ 凭据，**主代理也打**）打上那个
    # 代理的 id；站内会话手建的**不写**这根列（NULL = 无主，存量语义不变）。
    # 🔴 **键不动**（仍是 `manual:<hash>`）：内容级幂等是这条路径的既有判据
    # （「重复提交同一篇 = unchanged，不搅已生效那篇」），改键就改了语义。
    # 归属只在 INSERT 时写 ⇒ 撞键时那一行的归属**不被改**（不许抢别人的归属）。
    agent_id = (str(writer.get("agent_id") or "")
                if str(writer.get("kind") or "") == "agent" else "")
    key = ingest.manual_source_key(title, basis)
    # 老库未迁移归属列 ⇒ **不写这根列**（降级：全库无主，可见性=迁移前）。
    # （代理面 `kb_store` 那条路是 fail-loudly：写不了归属就明确报错拒写。）
    owner = agent_id if (agent_id and names.get("agent")) else ""
    meta = {"origin": "console" if not pending else "agent"}
    if is_link:
        meta["url"] = url
        meta["summary"] = content
    with _write_conn() as conn:
        with conn.cursor() as cur:
            status, doc_id, count = ingest.upsert_document(
                cur, schema, source_type=ingest.MANUAL_SOURCE_TYPE, source_key=key,
                title=title, meta=meta, text=content, names=names,
                content_hash=ingest.content_hash(title, basis),
                content_type=ctype, url=url, tags=tags,
                # 🔴 待审时**不切块**（chunk=False）：正文改存 review_meta.body，
                # 等批准时才 replace_chunks —— 未审正文不进检索索引。
                status=ingest.STATUS_PENDING if pending else None,
                chunk=not pending,
                agent_id=owner or None)
            # `unchanged` = 同键同 hash，一行都没动 ⇒ 这次提交没进队列，也**不许**往
            # 已生效那篇的 review_meta 里塞「待审正文」（那会把一篇在用的文档搅进审核
            # 台账；它已经审过了 —— 键同就意味着内容与被批准的那次逐字相同）。
            staged = pending and status != "unchanged"
            if staged:
                ingest.patch_review_meta(cur, schema, doc_id, {
                    "submitted_by": str(writer.get("agent_id") or ""),
                    "submitted_at": _now_iso(),
                    # 正文只此一份：pending 阶段没切块，documents 又没有正文列，
                    # 不存这儿批准时就无从切块（等审核动作就变成了「空文档入库」）。
                    "body": content,
                    **_dup_of(cur, schema, names, title, key),
                }, names=names)
    audit.record(action, key, ok=True,
                reason=f"status={status}; chunks={count}; content_type={ctype}"
                       + (f"; review=pending; by={writer.get('agent_id') or ''}"
                          if staged else ""))
    return {"ok": True, "action": action, "id": str(doc_id), "source_key": key,
            "status": status, "chunks_count": count, "content_type": ctype,
            "review": ingest.STATUS_PENDING if staged
                      else (status if pending else ingest.STATUS_EFFECTIVE)}


@router.post("/api/v1/kb/documents/{id}/update",
             dependencies=[Depends(writer_identity), Depends(write_guard)])
def kb_update_document(id: str, body: KbUpdateBody,
                       writer: Dict[str, Any] = Depends(writer_identity)) -> Dict[str, Any]:
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

    🆕 **子代理改正文 = 打回待审**（`writer_identity` 判）：`status='pending'`、
    新正文存 `review_meta.body`、**不重切 chunks**（旧块随 status 被检索过滤掉，
    批准时才 `replace_chunks`）。只改标题/标签不改正文则不算新内容，不进审核。
    🟡 站内会话改**已生效**的文档完全照旧；改还在审核队列里的文档 ⇒ 409
    （`_need_effective`，那条路归审核面板：批准或驳回，不许从底下偷改）。
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
    pending = _pending_of(writer)
    # 这次改「正文」吗（只改标题/标签不算新内容，不进审核）：在事务外先落定，
    # 审计与回包都在事务外用它。
    re_review = pending and (body.content is not None or body.url is not None)
    try:
        with _write_conn() as conn:
            with conn.cursor() as cur:
                doc = ingest.load_document(cur, schema, doc_id, names=names)
                if doc is None:
                    _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
                _manual_only(doc, str(id), action)
                _need_effective(doc, str(id), action)
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
                    # 子代理改了正文 ⇒ 这一行重新进待审（status 写 pending），且**不重切**：
                    # 旧块留着但被检索的 status 谓词挡住，批准时才 replace_chunks。
                    if re_review:
                        _need_status(names, action, str(id))
                    ingest.update_document(cur, schema, doc_id, title=title, source_key=key,
                                           content_hash=ingest.content_hash(title, basis),
                                           names=names, tags=tags,
                                           status=ingest.STATUS_PENDING if re_review else None)
                    if is_link:
                        _doc_set_link_meta(cur, schema, doc_id, url, summary, names)
                    if re_review:
                        ingest.patch_review_meta(cur, schema, doc_id, {
                            "submitted_by": str(writer.get("agent_id") or ""),
                            "submitted_at": _now_iso(),
                            "body": summary if is_link else content,
                            **_dup_of(cur, schema, names, title, key),
                        }, names=names)
                        count = ingest.chunks_count(cur, schema, doc_id, names=names)
                    else:
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
                       + ("" if tags is None else f"; tags={len(tags)}")
                       + ("; review=pending" if re_review else ""))
    return {"ok": True, "action": action, "id": str(id), "source_key": key,
            "title": title, "chunks_count": count, "content_type": ctype,
            "review": ingest.STATUS_PENDING if re_review else ingest.STATUS_EFFECTIVE}


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
            # 🔴 待审/已驳回的不许从这里删：那是审核队列的条目，删了就绕过审核动作
            # （驳回是**标记**不是删行，正文还在 review_meta 里，改主意还能再批）。
            _need_effective(doc, str(id), action)
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


# --------------------------------------------------------------------------
# 审核队列（批2）：待审列表面板 + 批准 / 驳回
# --------------------------------------------------------------------------

class KbApproveBody(BaseModel):
    """`{"confirm": true}` —— 批准一篇子代理提交的知识文档。

    🔴 `confirm` 是**布尔**而不是手输串：批准/驳回与「常驻候选」那套审核同档防
    （记忆维护页的批准/驳回也是点一下确认），而这里的破坏性比真删小得多 ——
    驳回不删正文（只标 rejected，改主意还能再批），批准随时能删。
    """

    confirm: bool = False


class KbRejectBody(BaseModel):
    """`{"confirm": true, "reason": "..."}` —— 驳回；**reason 必填**。

    🔴 reason 不能省：驳回不删正文（只标 rejected，可再审），所以「为什么不收」
    这句话是日后重新审的唯一线索，空理由的驳回等于把一篇文档永久卡在队列里
    还不说缘由。
    """

    confirm: bool = False
    reason: str = ""


def _pending_item(row: Dict[str, Any]) -> KbPendingItem:
    """待审队列的一行（review_meta 只解一次：正文预览 + 来源代理 + 查重提示）。"""
    rmeta = _rmeta_out(row.get("review_meta"))
    return KbPendingItem(
        doc_id=str(row.get("doc_id") or ""),
        title=str(row.get("title") or ""),
        # 老库/别的驱动给不出时间列时给空串（前端按「没时间」渲染，不编一个）
        submitted_at=str(row.get("submitted_at") or ""),
        status=str(row.get("status") or ingest.STATUS_PENDING),
        content_type=str(row.get("content_type") or ingest.DEFAULT_CONTENT_TYPE),
        source_key=str(row.get("source_key") or ""),
        tags=_tags_out(row.get("tags")),
        # 正文预览：pending 阶段正文在 review_meta.body 里（压根没切块），
        # 截到前 500 字够审核的人判「这篇到底在讲什么」。
        preview=str(rmeta.get("body") or "")[:PENDING_PREVIEW_LEN],
        submitted_by=str(rmeta.get("submitted_by") or ""),
        dup_of=str(rmeta.get("dup_of") or ""),
        dup_of_title=str(rmeta.get("dup_of_title") or ""),
        dup_exact=bool(rmeta.get("dup_exact")),
        reject_reason=str(rmeta.get("reject_reason") or ""),
    )


@router.get("/api/v1/kb/pending", response_model=KbPendingList,
            dependencies=[Depends(require_session)])
def kb_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
) -> KbPendingList:
    """待审队列（**非 effective 的文档** = pending + rejected，只读）。

    面板一次拉完两类：pending 是要审的，rejected 是留个台账的（让审核的人看得见
    「这篇被驳回了、原因是这句」，而不是文档凭空消失）。排序把 pending 排在前面
    （status 字典序 `pending` < `rejected`），已驳回的自然沉底。

    `pending_count` 恒为**全量 pending 数**，不受分页影响 —— 顶部那个「待审 N」
    chip 必须永远等于全库待审数（同 `routers/agents.list_agents` 的纪律）。
    🔴 老库未迁移 status 列 ⇒ 队列恒空（缺列 = 全 effective），不编一个假队列。
    """
    schema = _ident(config.pg_schema())
    tabs = _table_columns(schema, (config.KB_TABLE_DOCUMENTS,))
    docs = tabs[config.KB_TABLE_DOCUMENTS]
    if not docs:
        raise HTTPException(status_code=500,
                            detail={"error": "kb_table_missing",
                                    "message": f"知识库表不存在：{config.KB_TABLE_DOCUMENTS}"})
    d_status = next((_ident(x) for x in _CAND_STATUS if x in docs), None)
    if not d_status:
        return KbPendingList(items=[], count=0, pending_count=0,
                             page=page, page_size=page_size)
    d_id = _pick(docs, _CAND_ID, config.KB_TABLE_DOCUMENTS, "主键")
    d_title = next((_ident(t) for t in _CAND_TITLE if t in docs), None)
    d_time = next((_ident(t) for t in _CAND_TIME if t in docs), None)
    d_ct = next((_ident(x) for x in _CAND_CTYPE if x in docs), None)
    d_tags = next((_ident(x) for x in _CAND_TAGS if x in docs), None)
    d_rmeta = next((_ident(x) for x in _CAND_RMETA if x in docs), "")
    d_src_key = next((_ident(x) for x in _CAND_SRC_KEY if x in docs), None)

    where = f"d.{d_status} <> %s"
    src = f"FROM {schema}.{config.KB_TABLE_DOCUMENTS} d"
    order = (f"d.{d_status} ASC, " + (f"d.{d_time} DESC NULLS LAST, " if d_time else "")
             + f"d.{d_id} ASC")
    sel_title = f"d.{d_title}" if d_title else "''"
    sel_time = f"d.{d_time}" if d_time else "''"
    sel_ct = f"d.{d_ct}" if d_ct else "''"
    sel_tags = f"COALESCE(d.{d_tags}, '{{}}'::text[])" if d_tags else "'{}'::text[]"
    sel_key = f"d.{d_src_key}" if d_src_key else "''"
    sel_rmeta = f"d.{_ident(d_rmeta)}" if d_rmeta else "'{}'::jsonb"

    sql = (f"SELECT CAST(d.{d_id} AS text) AS doc_id, {sel_title} AS title, "
           f"{sel_time} AS submitted_at, {sel_ct} AS content_type, "
           f"{sel_tags} AS tags, {sel_key} AS source_key, "
           f"d.{d_status} AS status, {sel_rmeta} AS review_meta "
           f"{src} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [ingest.STATUS_EFFECTIVE, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
            # 面板总数与 chip 计数：一条 SELECT 出两个数（FILTER 参数在 WHERE 之前）
            cur.execute(f"SELECT COUNT(*) AS n, "
                        f"COUNT(*) FILTER (WHERE d.{d_status} = %s) AS pend "
                        f"{src} WHERE {where}",
                        [ingest.STATUS_PENDING, ingest.STATUS_EFFECTIVE])
            row = cur.fetchone() or {}
    items = [_pending_item(r) for r in rows]
    return KbPendingList(items=items, count=int(row.get("n") or 0),
                         pending_count=int(row.get("pend") or 0),
                         page=page, page_size=page_size)


@router.post("/api/v1/kb/documents/{id}/approve",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_approve_document(id: str, body: KbApproveBody,
                        session: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
    """**批准**一篇待审文档：同事务内 改 effective + 按类型切块落 chunks + 记批准痕迹。

    🔴 **切块就发生在这一刻**（pending 阶段一行 chunks 都没有）：审核动作之前
    未审正文不进检索索引，批准之后才按 `content_type` 走 `chunk_document` 落库。
    正文取 `review_meta.body`（提交时存的那份原文），link 的 URL/摘要从 `meta` 取回。
    🟡 已生效的文档再点批准 ⇒ 409（不重复切块、不改 updated_at）。
    🟡 已驳回的**可以再批**（驳回只是标记，不删正文，改主意就重新批一次）。
    """
    action = "kb.document.approve"
    _need_flag(body.confirm, action, str(id), '批准知识库文档必须显式传 {"confirm": true}')
    doc_id = _doc_id(id, action)
    who = str(session.get("username") or "console")
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    _need_status(names, action, str(id))
    with _write_conn() as conn:
        with conn.cursor() as cur:
            doc = ingest.load_document(cur, schema, doc_id, names=names)
            if doc is None:
                _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
            _not_ingester_doc(doc, str(id), action)
            st = str(doc.get("status") or ingest.STATUS_EFFECTIVE)
            if st == ingest.STATUS_EFFECTIVE:
                _reject(409, "kb_doc_not_pending",
                        f"这篇已经生效了（status={st}），不用再批准：{id}", action, str(id))
            body_text = _rmeta_of(cur, schema, doc_id, names).get("body")
            if not isinstance(body_text, str) or not body_text:
                _reject(409, "kb_review_no_body",
                        f"这篇没有可入库的提交正文（review_meta.body 为空），"
                        f"多半是审核元数据被人手改过，请让提交方重发：{id}", action, str(id))
            ctype = str(doc.get("content_type") or ingest.DEFAULT_CONTENT_TYPE)
            title = str(doc.get("title") or "")
            is_link = ctype == ingest.CONTENT_TYPE_LINK
            # link 的正文是组合文本：URL/摘要的真源在 meta，body 里存的只是摘要
            url = _doc_meta_field(cur, schema, doc_id, names, "url") if is_link else ""
            content = _doc_meta_field(cur, schema, doc_id, names, "summary") if is_link \
                else body_text
            key = str(doc.get("source_key") or "")
            cur.execute(f"UPDATE {schema}.{names['documents']} SET {names['status']} = %s, "
                        f"{names['updated']} = now() WHERE {names['doc_id']} = %s",
                        [ingest.STATUS_EFFECTIVE, doc_id])
            count = ingest.replace_chunks(cur, schema, doc_id, content, names=names,
                                          source_path=key, content_type=ctype,
                                          title=title, url=url)
            ingest.patch_review_meta(cur, schema, doc_id, {
                "approved_by": who, "approved_at": _now_iso(),
            }, names=names)
    audit.record(action, str(id), ok=True,
                reason=f"chunks={count}; content_type={ctype}; by={who}; was={st}")
    return {"ok": True, "action": action, "id": str(id), "status": ingest.STATUS_EFFECTIVE,
            "chunks_count": count, "content_type": ctype, "title": title}


@router.post("/api/v1/kb/documents/{id}/reject",
             dependencies=[Depends(require_session), Depends(write_guard)])
def kb_reject_document(id: str, body: KbRejectBody,
                       session: Dict[str, Any] = Depends(require_session)) -> Dict[str, Any]:
    """**驳回**一篇待审文档：`status='rejected'` + 记理由。**不删正文**。

    🔴 标记而不是删行：正文还在 `review_meta.body` 里，审核的人改主意就能再批
    （子代理的提交被一次误驳回整个丢掉，才是真丢数据）。理由必填 ——
    驳回之后唯一的线索就是这句。
    🟡 已生效的文档**不许驳回**（那是删除该干的事，走真删端点）：把在用的文档
    悄悄标成 rejected 等于让它从检索里消失，而没人批准过这件事。
    """
    action = "kb.document.reject"
    _need_flag(body.confirm, action, str(id), '驳回知识库文档必须显式传 {"confirm": true}')
    reason = (body.reason or "").strip()
    if not reason:
        _reject(400, "reason_required",
                "驳回必须写理由（review_meta.reject_reason）—— 驳回不删正文，"
                "日后重新审核时这句是唯一的线索", action, str(id))
    doc_id = _doc_id(id, action)
    who = str(session.get("username") or "console")
    schema = _ident(config.pg_schema())
    names = _write_names(schema)
    _need_status(names, action, str(id))
    with _write_conn() as conn:
        with conn.cursor() as cur:
            doc = ingest.load_document(cur, schema, doc_id, names=names)
            if doc is None:
                _reject(404, "kb_doc_missing", f"知识库文档不存在：{id}", action, str(id))
            _not_ingester_doc(doc, str(id), action)
            st = str(doc.get("status") or ingest.STATUS_EFFECTIVE)
            if st == ingest.STATUS_EFFECTIVE:
                _reject(409, "kb_doc_not_pending",
                        f"这篇已经生效（status={st}），驳回会让它从检索里消失 —— "
                        f"要下架请走真删端点：{id}", action, str(id))
            cur.execute(f"UPDATE {schema}.{names['documents']} SET {names['status']} = %s, "
                        f"{names['updated']} = now() WHERE {names['doc_id']} = %s",
                        [ingest.STATUS_REJECTED, doc_id])
            ingest.patch_review_meta(cur, schema, doc_id, {
                "reject_reason": reason, "rejected_by": who, "rejected_at": _now_iso(),
            }, names=names)
    audit.record(action, str(id), ok=True,
                reason=f"reason={reason[:80]}; by={who}; was={st}")
    return {"ok": True, "action": action, "id": str(id), "status": ingest.STATUS_REJECTED,
            "title": str(doc.get("title") or ""), "reason": reason}


def _export_lines(schema: str, names: Dict[str, str], source_type: str,
                  limit: int) -> Iterator[bytes]:
    """把导出内容逐篇吐成 JSONL 行（流式：不在内存里拼整个文件）。"""
    where = "TRUE"
    params: List[Any] = []
    if source_type:
        where, params = f"{names['src_type']} = %s", [source_type]
    # 🆕 审核过滤：导出**只导已生效的**（与列表/检索同口径 —— 「导出的就是屏幕上
    # 看到的」这条纪律不许因为导出就破功）。老库没 status 列 ⇒ 不加谓词。
    if names.get("status"):
        where, params = f"{where} AND {names['status']} = %s", [*params, ingest.STATUS_EFFECTIVE]
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
    except KbError as e:
        # 数据层抛的结构化错误（未配置 / 没驱动 / 表不存在 / 结构不识别）
        return {"available": False, "error": e.code, "message": e.message}
    except Exception as e:                    # noqa: BLE001 —— 探针不许把总览接口带崩
        return {"available": False, "error": "kb_failed",
                "message": f"{e.__class__.__name__}: {e}"}
