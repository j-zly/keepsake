"""**写**操作端点：新增 / 改 / 删 / 归档 / 导入 + 打标 / 合并 / 遗忘
（dry-run 一等公民）。

🔴 四条硬约束（任务书 P2），全部在这一层兑现：

1. **写必须走 keepsake 自己的 API** —— `storage.update_fragment_fields` /
   `update_fragment_content`（打标 / 改正文 / 归档）、`storage.store`
   （新增 / 导入）、`storage.delete_fragments_batch`（真删）与
   `Consolidator` / `Forgetter`（合并 / 遗忘，它们内部自己走
   `storage.write_fragments_batch` / `delete_fragments_batch`）。
   **不自拼 UPDATE/DELETE SQL**：那样会绕开存储层的去重 / supersede /
   失效语义（索引不重算 = 检索静默召回旧内容，而不是召不回）。
   走 `update_fragment_fields` 而**不是** `write_fragments_batch`：后者在
   Sqlite 后端是**迁移专用**的 upsert，未传的列会被写成空串（等于抹字段）。
   🟡 **改正文另走 `update_fragment_content`**：前者契约上不动检索索引，
   PG 后端用它改正文会让 BM25 索引失真（见 `_apply` 的说明段）。
2. **dry-run 是一等公民**：merge/forget/**import** 默认 `dry_run=true`；
   实跑（`dry_run=false`）必须带 `confirm="<当前计数>"`（防手滑）——
   缺 ⇒ 400 `confirm_required`，不匹配 ⇒ 400 `confirm_mismatch`。
   **除 `dry_run=false` 外没有第二条实跑入口。**
3. **写前自动备份**（`audit.backup_fragment`）：备份写不成功 ⇒ 整个写操作
   中止，数据一秒都没动。审计落盘位置在进路由时就自检过（`write_guard`）——
   记不上账 ⇒ 这次写根本不做。
   （批量导入例外：逐条备份没意义，改记一行**批次摘要** count+来源。）
4. **每次写（含被拒绝的）审计一行 JSON**（`audit.record`）。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, NoReturn, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from keepsake.core.shared import fragment_key_for, sanitize_reserved_tags

from .. import audit, config
from ..deps import get_storage, require_session
from .core import CORE_TAG

router = APIRouter(tags=["ops"])

#: 归档确认串长度（手输 key 末 8 位，防手滑）
CONFIRM_SUFFIX_LEN = 8

#: 导出的字段并集（各后端缺哪根就给空串 —— 导出契约不许随后端变）
EXPORT_FIELDS = ("key", "content", "tags", "entities", "created", "updated_at")

#: 导出护栏：默认一万条、封顶五万条，超了 400（提示分页口径，绝不默默截断）
EXPORT_LIMIT_DEFAULT = 10000
EXPORT_LIMIT_MAX = 50000

#: 单次导入条数上限（同上：超了 400，绝不默默截断成「前 N 条」）
IMPORT_MAX_ITEMS = 5000


# --------------------------------------------------------------------------
# 入参
# --------------------------------------------------------------------------

class ConfirmFlag(BaseModel):
    """`{"confirm": true}` —— 打标/改正文用。缺 confirm 即 false ⇒ 400。"""

    confirm: bool = False


class ConfirmText(BaseModel):
    """`{"confirm": "<期望串>"}` —— 归档/真删用手输的 key 末 8 位。"""

    confirm: str = ""


class UpdateBody(BaseModel):
    """`{"confirm": true, "content": ..., "tags": ...}`。"""

    confirm: bool = False
    content: Optional[str] = None
    tags: Optional[str] = None


class CreateBody(BaseModel):
    """`{"confirm": true, "content": "...", "tags": "a,b"}` —— 控制台新增记忆。"""

    confirm: bool = False
    content: str = ""
    tags: str = ""


class ImportItem(BaseModel):
    """导入的单条：`{"content": "...", "tags": "a,b"}`。"""

    content: str = ""
    tags: str = ""


class ImportBody(BaseModel):
    """`{"items": [...], "dry_run": true}` /
    `{"items": [...], "dry_run": false, "confirm": "<will_add 数>"}`。

    `promote=true` 时每条落库后紧接着打 core 标签（导入即升常驻）—— 这是
    **批量版的 promote 端点**，与 `/api/v1/core/{key}/promote` 同一存储路径
    （`_with_core` + `_apply`）；条自带的 `core` 标签仍然拒收（治理口径不变：
    常驻只能由「人在点」这一个入口决定，或由导入时显式勾 promote）。
    """

    items: List[ImportItem] = []
    dry_run: bool = True
    confirm: Optional[str] = None
    promote: bool = False


class CoreBudgetBody(BaseModel):
    """`{"budget": 60000, "confirm": "<当前生效值>"}` —— 设置常驻层注入预算。"""

    budget: int = 0
    confirm: str = ""


class BulkOpsBody(BaseModel):
    """`{"dry_run": true}` / `{"dry_run": false, "confirm": "<当前计数>"}`。"""

    dry_run: bool = True
    confirm: Optional[str] = None


class BulkActionBody(BaseModel):
    """`{"action": "promote", "keys": [...], "confirm": "<勾选数>"}`。

    `confirm` 是**去重后的选中条数**（防手滑口径与 merge/forget 同款），
    不是布尔 —— 批量删 37 条时「按回车就删」是不可接受的。
    """

    action: str = ""
    keys: List[str] = []
    confirm: str = ""


class BulkExportBody(BaseModel):
    """`{"keys": [...], "format": "jsonl|csv"}` —— 勾选导出（走 POST 是因为
    500 个 key 拼进 query 会撞 8K 请求行上限，见 `bulk_export`）。"""

    keys: List[str] = []
    format: str = Field("jsonl", pattern="^(jsonl|csv)$")


class TieringBody(BaseModel):
    """`{"tiering": {...}, "confirm": "<当前 enabled>"}` —— 设置分级班车阈值。

    body 装的是 `tiering` **整节**（与 config.json 里的形状逐字相同），而不是
    摊平成一串顶层键：页面上的表单与配置文件里的节必须长得一样，改一个键不用
    回来猜它原来在哪一层。
    """

    tiering: Dict[str, Any] = Field(default_factory=dict)
    confirm: str = ""


class ScreeningBody(BaseModel):
    """`{"confirm": true, "enabled": ..., "default_on": ..., "model": ..., ...}`。

    摊平成一串顶层键（**不像** tiering 那样装整节）：筛选节只有 9 个键，
    页面上的表单一项对一个键，摊平后校验（越界 400）与回显都能逐字对得上。
    落盘时仍然只写 config.json 的 `screening` **一节**（其余键一字不动）。
    """

    confirm: bool = False
    enabled: bool = False
    default_on: bool = False
    model: str = ""
    base_url: str = ""
    api_key_file: str = ""
    max_pick: int = 10
    timeout_s: int = 90
    max_tokens: int = 4096


# --------------------------------------------------------------------------
# 拒绝 / 确认的公共闸门
# --------------------------------------------------------------------------

def write_guard() -> None:
    """写端点的统一前置闸门：审计落盘位置必须可写，否则**这次写就不做**。

    挂在 6 条 POST 上（认证之后 —— 401 优先于 500）。理由见
    `audit.precheck`：先改完数据再发现记不上账，比拒绝写更糟。
    """
    audit.precheck()


def _reject(status: int, error: str, message: str,
            action: str, target: str) -> NoReturn:
    """先审计一行 `ok=false`，再抛 —— **被拒绝的写操作也必须留痕**。"""
    audit.record(action, target, ok=False, reason=error)
    raise HTTPException(status_code=status,
                        detail={"error": error, "message": message})


def _need_flag(confirm: bool, action: str, target: str, message: str) -> None:
    if confirm is not True:
        _reject(400, "confirm_required", message, action, target)


def _need_text(got: str, expect: str, action: str, target: str,
               what: str) -> None:
    """确认串闸门：**先判缺失、再判不匹配**，两者都是 400（硬约束 2）。"""
    if not got:
        _reject(400, "confirm_required", f"实跑必须手输{what}：{expect}",
                action, target)
    if got != expect:
        _reject(400, "confirm_mismatch", f"确认串不匹配，应为 {expect}",
                action, target)


def _need_count_confirm(body: BulkOpsBody, count_fn: Callable[[], int],
                        action: str, target: str, what: str, hint: str) -> str:
    """批量操作（merge/forget）的计数确认闸门，**返回当前计数**（回包要用）。

    🔴 **「有没有带 confirm」这一步不许碰存储层**（`count_fn` 是惰性的）：
    忘带确认是最常见的拒绝路径，先连一次库的话，存储不可达时该报的是
    400（请求没写意图）而不是 500（数据源故障）—— 那会把「手滑」误报成「故障」。
    拿到 confirm 之后才去数当前条数、比对。
    """
    if not body.dry_run and not (body.confirm or ""):
        _reject(400, "confirm_required",
                f"实跑必须带 confirm（{what}，见 {hint}）", action, target)
    expect = str(count_fn())
    if not body.dry_run and body.confirm != expect:
        _reject(400, "confirm_mismatch", f"确认串不匹配，应为 {expect}",
                action, target)
    return expect


# --------------------------------------------------------------------------
# 存储层读写（**一律走 keepsake 原语**）
# --------------------------------------------------------------------------

def _tag_list(tags: Any) -> List[str]:
    return [t.strip() for t in str(tags or "").split(",") if t.strip()]


def _with_core(tags: Any, on: bool) -> str:
    """逗号串标签的增删 `core`。

    升常驻时 `core` **放最前** —— 与既有常驻行的 `core,shared` 顺序一致，
    控制台列表与 provider 注入看到的口径相同。
    """
    ts = _tag_list(tags)
    if on:
        if CORE_TAG not in ts:
            ts = [CORE_TAG] + ts
    else:
        ts = [t for t in ts if t != CORE_TAG]
    return ",".join(ts)


def _load(storage: Any, key: str, action: str) -> Dict[str, Any]:
    """取改前原文；不存在 ⇒ 404（并留痕）。"""
    row = storage.get_fragment(key)
    if not row:
        _reject(404, "not_found", f"记忆条目不存在：{key}", action, key)
    return row


def _apply(storage: Any, key: str, fields: Dict[str, Any], action: str) -> None:
    """把改动落到存储层 —— 一律走存储层原语（局部字段更新）。

    🔴 **改正文走 `update_fragment_content`，其余字段走 `update_fragment_fields`**：
    后者的契约是「不动 content_tsv / embedding」（维护面更新用它），所以在
    PG 后端用它改正文会让 BM25 索引留着旧词 —— 检索**不报错、不空召回**，只是
    **召回旧内容**，控制台看着「改成功了」而记忆库在骗人。
    `update_fragment_content` 由基类声明、PG 覆写（单事务内重算 `content_tsv`，
    与 `store()` 同一套分词/拼接函数）；SQLite 就地重算 FTS、Redis 的 RediSearch
    索引随 HSET 自动维护，两者用基类的默认实现即语义正确。

    写后**回读校验**：后端可能只认自己白名单里的列（返回 False 或静默丢弃），
    那样控制台会报「成功」而数据没动。回读一次对不上就 500（数据已被后端改动
    前的备份兜着，见 `audit.backup_fragment`）。

    🟡 **只改 tags 时仍走 `update_fragment_fields`**：那是维护面原语，改标签不
    改正文。PG 上它不重算 `content_tsv` ⇒ 标签的 B 权重部分会滞后到下次改正文
    才跟上（改标签本身仍会落库、列表/过滤照常，只是「按标签全文召回」这一点在
    PG 上略有滞后）。收口办法是给基类再开一条 `update_fragment_tags`，把同款
    重算逻辑复用过去 —— 现在不开：一条没人调的原语就是没人测的语义。
    """
    if "content" in fields:
        ok = storage.update_fragment_content(
            key, str(fields["content"]), tags=fields.get("tags"))
    else:
        ok = storage.update_fragment_fields(key, fields)
    if not ok:
        _reject(500, "update_failed",
                f"存储层拒绝了这次写入，未改动任何数据：{key}", action, key)
    now = storage.get_fragment(key) or {}
    missed = [f for f, v in fields.items() if str(now.get(f, "")) != str(v)]
    if missed:
        _reject(500, "update_not_persisted",
                f"写入后回读对不上（后端可能未实现该字段），字段={sorted(missed)}：{key}",
                action, key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# core 打标
# --------------------------------------------------------------------------

@router.post("/api/v1/core/{key}/promote", dependencies=[Depends(require_session), Depends(write_guard)])
def promote_core(key: str, body: ConfirmFlag,
                 storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """给记忆条目打上 `core` 标签（升常驻）。已是常驻 ⇒ 409。"""
    action = "core.promote"
    _need_flag(body.confirm, action, key, '升常驻必须显式传 {"confirm": true}')
    row = _load(storage, key, action)
    if CORE_TAG in _tag_list(row.get("tags")):
        _reject(409, "already_core", f"已在常驻层：{key}", action, key)
    backup = audit.backup_fragment(key, row)
    tags = _with_core(row.get("tags"), True)
    _apply(storage, key, {"tags": tags}, action)
    audit.record(action, key, ok=True, reason=f"backup={backup}")
    return {"ok": True, "action": action, "key": key, "tags": tags, "backup": backup}


@router.post("/api/v1/core/{key}/demote", dependencies=[Depends(require_session), Depends(write_guard)])
def demote_core(key: str, body: ConfirmFlag,
                storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """去掉记忆条目上的 `core` 标签（降常驻）。不在常驻 ⇒ 409。"""
    action = "core.demote"
    _need_flag(body.confirm, action, key, '降常驻必须显式传 {"confirm": true}')
    row = _load(storage, key, action)
    if CORE_TAG not in _tag_list(row.get("tags")):
        _reject(409, "not_core", f"不在常驻层：{key}", action, key)
    backup = audit.backup_fragment(key, row)
    tags = _with_core(row.get("tags"), False)
    _apply(storage, key, {"tags": tags}, action)
    audit.record(action, key, ok=True, reason=f"backup={backup}")
    return {"ok": True, "action": action, "key": key, "tags": tags, "backup": backup}


# --------------------------------------------------------------------------
# 记忆条目改 / 归档
# --------------------------------------------------------------------------

@router.post("/api/v1/fragments/{key}/update", dependencies=[Depends(require_session), Depends(write_guard)])
def update_fragment(key: str, body: UpdateBody,
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """改正文 / 标签。`content` 与 `tags` 同时为空 ⇒ 400。

    🟡 空白串按「没填」处理：传空正文**不会**把记忆清空（那是不可逆的数据丢失，
    且没法从语义上区分「想清空」与「忘了填」）。真要清空走归档。
    """
    action = "fragment.update"
    _need_flag(body.confirm, action, key, '改记忆条目必须显式传 {"confirm": true}')
    row = _load(storage, key, action)
    fields: Dict[str, Any] = {}
    if (body.content or "").strip():
        fields["content"] = (body.content or "").strip()
    if (body.tags or "").strip():
        fields["tags"] = (body.tags or "").strip()
    if not fields:
        _reject(400, "nothing_to_update",
                "content 与 tags 不能同时为空（真要下线这条请走归档）",
                action, key)
    backup = audit.backup_fragment(key, row)
    _apply(storage, key, fields, action)
    audit.record(action, key, ok=True,
                 reason=f"fields={sorted(fields)}; backup={backup}")
    return {"ok": True, "action": action, "key": key,
            "updated": sorted(fields), "backup": backup}


@router.post("/api/v1/fragments/{key}/archive", dependencies=[Depends(require_session), Depends(write_guard)])
def archive_fragment(key: str, body: ConfirmText,
                     storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """归档：置 `is_archived=1` + `valid_until=now`（**与既有 store() 版本化
    失效语义同款，不物理删除**）。确认串 = key 末 8 位。

    重复归档是幂等的（不会 409）—— 归档本身就是「确保这条已下线」。
    """
    action = "fragment.archive"
    expect = key[-CONFIRM_SUFFIX_LEN:]
    _need_text(body.confirm, expect, action, key, "key 末 8 位")
    row = _load(storage, key, action)
    backup = audit.backup_fragment(key, row)
    _apply(storage, key,
           {"is_archived": "1", "valid_until": _now_iso()}, action)
    audit.record(action, key, ok=True, reason=f"backup={backup}")
    return {"ok": True, "action": action, "key": key,
            "is_archived": "1", "physical_delete": False, "backup": backup}


# --------------------------------------------------------------------------
# 增 / 删 / 导入 / 导出（控制台记忆 CRUD）
# --------------------------------------------------------------------------

def _clean_tags(raw: Any, action: str, target: str) -> str:
    """剥除治理保留标签；`core` **直接 400**（不许新增直打常驻）。

    判据与自动提炼（`cron/memory_distill.py`）**共用** `sanitize_reserved_tags`
    —— 谁能声明 `core`/`core_candidate`/`shared`/`agent:*` 就是谁能决定常驻层、
    候选审核队列与代理归属，两处各写一份黑名单 = 迟早漂一份，假申请就进来了。
    差异只在 `core` 的处置：提炼无权声明（剥掉继续写），控制台是**人在点**，
    静默吃掉他打的标签 = 静默丢用户意图 ⇒ 报 400 让他自己选（走既有条目的
    promote，或走候选审核）。
    """
    clean, touched = sanitize_reserved_tags(raw, forbid=(CORE_TAG,))
    for tag, reason in touched:
        if reason == "forbidden":
            _reject(400, "reserved_tag",
                    f"不许直接打 {tag!r} 标签：进常驻请对既有条目走 promote 或候选审核",
                    action, target)
    return clean


def _store_one(storage: Any, content: str, tags: str,
               action: str, target: str) -> str:
    """落一条记忆，返回**真正落库那一行**的 key（新增与导入**同一入库函数**）。

    走 `storage.store()`（与提炼/合并同一条写路径 ⇒ 去重、版本化、分词建索引
    全部同一套），写后**回读校验**正文确实在库里 —— 后端静默丢弃不该被报成成功。

    🔴 **为什么要多这一步找 key**：`store()` 的去重语义是「同内容不覆盖、另起
    `:<epoch>` 新键并把旧版标失效」（三后端一致）。所以同内容第二次提交时，
    新行落在 `base:epoch`，而**内容 hash 算出来的仍是 base** —— 直接把 base
    返回给前端，它指向的是那条刚被标失效的旧行：列表里看不见、导出里不出现、
    删它删的是历史版本。⇒ 先点查 base 是否已存在，存在才去同前缀里取最大 key
    （keyset 升序 ⇒ 版本后缀排在基础 key 之后，末位即最新一版）。
    """
    base = fragment_key_for(content)
    # 三态：True=已存在（会版本化）；False/None 一律按「全新」算，store() 自己把关
    versioned = storage.fragment_exists(base) is True
    if not storage.store(text=content, tags=tags, category="manual",
                         source="console", fragment_type="memory"):
        _reject(500, "store_failed", f"存储层拒绝了这次写入：{target}", action, target)
    key = base
    if versioned:
        _, same = storage.scan_fragment_keys("", 20, prefix=base)
        live = [k for k in same if k != base]
        if live:
            key = live[-1]
    row = storage.get_fragment(key) or {}
    if str(row.get("content", "")) != content:
        _reject(500, "store_not_persisted",
                f"写入后回读对不上（后端可能未落库），key={key}", action, target)
    return key


@router.post("/api/v1/fragments", dependencies=[Depends(require_session), Depends(write_guard)])
def create_fragment(body: CreateBody,
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """新增一条记忆条目（控制台手打，非提炼）。

    正文空 / 纯空白 ⇒ 400（空条目进库就是纯噪音，还会占掉去重 key）。
    标签里的治理保留字：`core` 拒绝（进常驻走 promote 或候选审核），其余剥除。
    """
    action = "fragment.create"
    _need_flag(body.confirm, action, "-", '新增记忆条目必须显式传 {"confirm": true}')
    content = (body.content or "").strip()
    if not content:
        _reject(400, "empty_content", "content 不能为空", action, "-")
    tags = _clean_tags(body.tags, action, "-")
    key = _store_one(storage, content, tags, action, "-")
    audit.record(action, key, ok=True, reason=f"tags={tags!r}")
    return {"ok": True, "action": action, "key": key, "tags": tags, "score": None}


@router.post("/api/v1/fragments/{key}/delete",
             dependencies=[Depends(require_session), Depends(write_guard)])
def delete_fragment(key: str, body: ConfirmText,
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """**真删**一条记忆条目（物理删除，不是归档的软失效）。

    与归档同款二次确认（手输 key 末 8 位）。执行序固定：
    取行 → **备份（失败则不删）** → `delete_fragments_batch` → 审计。
    常驻层条目也可直接删 —— 删完 `list_by_tag` 自然少一条，与 promote/demote 无耦合。

    与归档的分工：归档 = 版本化失效（可回溯、检索层仍能查到历史）；真删 = 这条
    记忆不该存在（如误打、含敏感信息）。备份文件是唯一的回退路径，所以它写不成
    就绝不删。
    """
    action = "fragment.delete"
    expect = key[-CONFIRM_SUFFIX_LEN:]
    _need_text(body.confirm, expect, action, key, "key 末 8 位")
    row = _load(storage, key, action)
    backup = audit.backup_fragment(key, row)
    deleted = storage.delete_fragments_batch([key])
    if deleted != 1:
        _reject(500, "delete_failed",
                f"存储层删除了 {deleted} 条（应为 1），条目可能已被并发删掉：{key}",
                action, key)
    audit.record(action, key, ok=True, reason=f"deleted=1; backup={backup}")
    return {"ok": True, "action": action, "key": key, "deleted": True, "backup": backup}


def _export_rows(storage: Any, q: str, tag: str, limit: int,
                 action: str) -> List[Dict[str, Any]]:
    """按 (q, tag) 取要导出的条目，**多取一条**用来判「超限 ⇒ 400」。

    绝不默默截断：用户拿到的文件必须是他以为的那一份。
    """
    if q:
        keys = [str(r.get("key") or r.get("_key") or "") for r in storage.search(q, tag_filter=tag)]
    elif tag:
        keys = [str(r.get("key") or "") for r in storage.list_by_tag(tag)]
    else:
        _, keys = storage.scan_fragment_keys("", limit + 1)
    keys = [k for k in keys if k]
    if len(keys) > limit:
        _reject(400, "export_too_large",
                f"命中 {len(keys)}+ 条，超过本次导出上限 {limit}："
                "请用 ?q= 检索词 或 ?tag= 标签分批导出", action, "export")
    fetched = storage.get_fragments_batch(keys)
    return [dict(fetched.get(k) or {}, key=k) for k in keys]


def _export_line(row: Dict[str, Any], fmt: str) -> bytes:
    """一行导出记录（JSONL 行 或 CSV 行），UTF-8 字节。"""
    rec = {f: str(row.get(f, "") or "") for f in EXPORT_FIELDS}
    # `updated_at` 各后端字段名不同（sqlite/redis=updated、PG 没有 updated_at，
    # 回落到 created）—— 与 `routers/fragments._UPDATED_KEYS` 同一套回退顺序，
    # 否则导出的时间列在 sqlite/redis 库上整列空（同一份数据、两种口径）。
    if not rec["updated_at"]:
        for k in ("updated", "created"):
            if row.get(k):
                rec["updated_at"] = str(row[k])
                break
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerow([rec[f] for f in EXPORT_FIELDS])
        return buf.getvalue().encode("utf-8")
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")


def _stream(rows: List[Dict[str, Any]], fmt: str) -> Iterator[bytes]:
    """流式吐行（不把整个导出塞进内存缓冲；header 行在首个数据行之前）。"""
    if fmt == "csv":
        # BOM 的动机是 Excel 直开 CSV（不认 UTF-8 会按本地编码解，中文全乱码）。
        # JSONL 的消费方是程序：`utf-8-sig` 之外的严格解析器会被 BOM 噎住，
        # 而它并不存在「用 Excel 打开 JSONL」这种用法 ⇒ BOM 只给 CSV。
        yield b"\xef\xbb\xbf"
        buf = io.StringIO()
        csv.writer(buf).writerow(EXPORT_FIELDS)
        yield buf.getvalue().encode("utf-8")
    for row in rows:
        yield _export_line(row, fmt)


@router.get("/api/v1/export", dependencies=[Depends(require_session)])
def export_fragments(
    q: str = Query("", description="BM25 过滤（走 storage.search）；为空则全库/按标签"),
    tag: str = Query("", description="标签精确过滤（走 storage.list_by_tag）"),
    format: str = Query("jsonl", pattern="^(jsonl|csv)$", description="导出格式"),
    limit: int = Query(EXPORT_LIMIT_DEFAULT, ge=1, le=EXPORT_LIMIT_MAX,
                       description="单次导出条数上限；超了报 400（分批导出，不截断）"),
    storage: Any = Depends(get_storage),
) -> StreamingResponse:
    """导出记忆条目（JSONL / CSV）。**只读端点**：不挂 `write_guard`。

    `?limit=` 默认 10000、封顶 50000；命中条数超了报 400 而不是截断
    （被截断的文件是「看起来完整」的假数据，比报错危险得多）。

    🟡 **`?q=` 是 top-k 口径，不是全量导出**：它走 `storage.search`（检索侧同一条
    路径），而检索的 `final_limit` 默认 5 ⇒ 带 `?q=` 最多导出 5 条（`?limit=`
    管不着它，它限制的是输出条数上限、不是召回条数）。要导出**全部**命中项请用
    `?tag=`（走 `list_by_tag`，全量）或不带过滤（走 keyset 扫描）。
    这里不去私调后端的召回上限 —— 那等于在 API 层另开一套检索参数口径，
    与控制台列表页看到的名次对不上。
    """
    rows = _export_rows(storage, q.strip(), tag.strip(), limit, "memory.export")
    ext = "csv" if format == "csv" else "jsonl"
    media = "text/csv; charset=utf-8" if ext == "csv" else "application/x-ndjson; charset=utf-8"
    fname = f"keepsake-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.{ext}"
    return StreamingResponse(_stream(rows, ext), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        # 导出的是记忆全文，不许被任何中间层缓存
        "Cache-Control": "no-store",
    })


@router.post("/api/v1/import", dependencies=[Depends(require_session), Depends(write_guard)])
def import_fragments(body: ImportBody,
                     storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """批量导入（JSON body）。**默认 dry-run，零写入**。

    实跑必须带 `confirm="<will_add 数>"`（先跑一次 dry-run 拿到数再手输，防手滑，
    与 merge/forget 同款）。去重是**内容级**：按 `store()` 的内容 hash key
    （`shared.fragment_key_for`，与三后端 store 同一函数）查，存在即 skip。

    治理保留字：条级拒 —— 带 `core` 的那条**不导入**并计入 `rejected_reserved`
    （批量场景不能为一条打回整批，那会让「导入了 4999/5000 条」说不清）。

    `promote=true` = 导入即升常驻：每条落库后紧接着打 core 标签（`_promote_batch`，
    与 promote 端点同一条存储路径）。dry_run 时只回 `would_promote` 计数，零写入。
    """
    action, target = "memory.import", "batch"
    if len(body.items) > IMPORT_MAX_ITEMS:
        _reject(400, "import_too_large",
                f"一次最多导入 {IMPORT_MAX_ITEMS} 条，本次 {len(body.items)} 条："
                "请分批导入（超限不截断）", action, target)

    counts = {"total": len(body.items), "would_add": 0, "skipped_duplicate": 0,
              "rejected_reserved": 0, "rejected_empty": 0}
    plan: List[tuple] = []          # (content, tags) —— 待写入的那些
    for item in body.items:
        content = (item.content or "").strip()
        if not content:
            counts["rejected_empty"] += 1
            continue
        clean, touched = sanitize_reserved_tags(item.tags, forbid=(CORE_TAG,))
        if any(reason == "forbidden" for _, reason in touched):
            counts["rejected_reserved"] += 1
            continue
        # `fragment_exists` 三态：None = 后端不可达，本条判据作废、按新条目算，
        # 真写不进去时 store() 自己会失败（不静默当重复跳掉）。
        # 判据打在**未版本化的基础 key** 上 ⇒ 同内容即便已被归档也算「已存在」，
        # 不重复灌库（store() 的版本化会让同内容再存一次变成新 key:epoch）。
        if storage.fragment_exists(fragment_key_for(content)):
            counts["skipped_duplicate"] += 1
            continue
        plan.append((content, clean))

    counts["would_add"] = len(plan)
    if body.dry_run:
        audit.record(action, target, ok=True, reason="dry_run; " + _counts_reason(counts)
                     + f"; promote={body.promote}")
        return {"ok": True, "dry_run": True, "action": action,
                "confirm_required": str(len(plan)),
                "would_promote": len(plan) if body.promote else 0, **counts}

    # 确认串对不上时**一条都不许写**（计数这一步已经惰性做完，与 merge 同口径）
    _need_text(body.confirm or "", str(len(plan)), action, target,
               f"本次将新增 {len(plan)} 条（先跑 dry-run 拿 confirm_required）")
    # 批量写：逐条备份没意义（5000 个备份文件比库还大），记一行批次摘要即可。
    # 记不上账 ⇒ 整批不写（fail-closed，与单条写同一纪律）。
    audit.record(action, target, ok=True, reason="running; " + _counts_reason(counts)
                 + f"; promote={body.promote}")
    keys = [_store_one(storage, c, t, action, target) for c, t in plan]
    promoted = 0
    if body.promote:
        promoted = _promote_batch(storage, keys, action)
    counts["promoted"] = promoted
    audit.record(action, target, ok=True, reason="done; " + _counts_reason(counts)
                 + f"; keys={len(keys)}; promote={body.promote}")
    return {"ok": True, "dry_run": False, "action": action,
            "keys": keys, "promoted": promoted, **counts}


def _promote_batch(storage: Any, keys: List[str], action: str) -> int:
    """给刚导入的这批 key 打 core 标签，返回**实际升上去**的条数。

    🔴 **与 `promote_core` 端点同一条存储路径**（`_with_core` + `_apply`），
    不是另写一份 `tags = tags + ",core"`：打标签必须走 `update_fragment_fields`
    并回读校验，否则「升常驻成功」可能只是标签没落库。
    差异只在备份：批量导入逐条备份没意义（与本模块既有的批次摘要同一纪律），
    真要回退走 manifest 里的原始 JSONL 文本。
    """
    n = 0
    for key in keys:
        row = storage.get_fragment(key) or {}
        if CORE_TAG in _tag_list(row.get("tags")):
            continue
        tags = _with_core(row.get("tags"), True)
        _apply(storage, key, {"tags": tags}, action)
        n += 1
    return n


def _counts_reason(counts: Dict[str, int]) -> str:
    """批次计数摘要（审计行里的短理由；**只写 count，不写内容**）。"""
    return (f"total={counts['total']}; would_add={counts['would_add']}; "
            f"skipped_duplicate={counts['skipped_duplicate']}; "
            f"rejected_reserved={counts['rejected_reserved']}; "
            f"rejected_empty={counts['rejected_empty']}")


# --------------------------------------------------------------------------
# 批量：合并 / 遗忘
# --------------------------------------------------------------------------

def _candidate_keys(engine: Any, method: str, storage: Any) -> List[str]:
    """合并 / 遗忘的「受影响 keys」= 引擎自己扫描出的候选集。

    ponytail: 直接借 `Consolidator` / `Forgetter` 的私有扫描原语 ——
    **同一个库里不另写一套筛选判据**（两边判据漂移才是真危险：备份导出的
    集合和真跑的集合对不上，等于没备份）。取不到（私有原语改名/删除）就退回
    全库扫描：备份更大，但绝不漏。升级路径：给引擎加一个公开的 `plan()`。
    """
    fn = getattr(engine, method, None)
    items: Any = []
    if callable(fn):
        # 两个私有原语签名不同：Forgetter._find_forgettable(stats) 会顺带往
        # stats 里填计数（给它个字典），Consolidator._scan_unconsolidated()
        # 不收参数。TypeError 就退回无参调用。
        try:
            items = fn({"scanned": 0, "skipped_protected": 0}) or []
        except TypeError:
            items = fn() or []
    else:
        _, items = storage.scan_fragment_keys("", config.RETRIEVAL_SCAN_CAP)
    out: List[str] = []
    for it in items:
        # Consolidator._scan_unconsolidated 返回整行（key 落在 _key 上），
        # Forgetter._find_forgettable 直接返回 key 字符串 —— 两种都收。
        if isinstance(it, dict):
            k = it.get("key") or it.get("_key")
        else:
            k = it
        if k:
            out.append(str(k))
    return out


def _core_count(storage: Any) -> int:
    """当前常驻条数（`GET /api/v1/core/stats` 的 count 同一原语、同一口径）。"""
    return len(list(storage.list_by_tag(CORE_TAG)))


def _total_count(storage: Any) -> int:
    """当前记忆条目总数（`GET /api/v1/stats` 的 retrieval.count 同一口径）。"""
    _, keys = storage.scan_fragment_keys("", config.RETRIEVAL_SCAN_CAP)
    return len(keys)


def _export_before(storage: Any, keys: List[str], ts: str) -> str:
    """把受影响 keys 全量导出 `affected.<ts>.before.jsonl`。"""
    rows = storage.get_fragments_batch(keys) if keys else {}
    return audit.backup_jsonl(f"affected.{ts}.before.jsonl",
                              [{"key": k, "row": rows.get(k, {})} for k in keys])


@router.post("/api/v1/ops/merge", dependencies=[Depends(require_session), Depends(write_guard)])
def ops_merge(body: BulkOpsBody, storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """合并（Consolidator）。**默认 dry-run，零写入**。

    实跑必须带 `confirm="<当前常驻条数>"`（取 `GET /api/v1/core/stats` 的
    count，防手滑）。实跑前先跑一次 dry-run 落 `merge.<ts>.dryrun.json`，
    再把受影响 keys 全量导出 `affected.<ts>.before.jsonl`，**然后才真跑**。
    """
    from keepsake.core.ops.consolidator import Consolidator

    action, target = "ops.merge", "all"
    expect = _need_count_confirm(body, lambda: _core_count(storage), action, target,
                                 "当前常驻条数", "GET /api/v1/core/stats")
    engine = Consolidator(storage)

    if body.dry_run:
        stats = engine.consolidate(dry_run=True)
        audit.record(action, target, ok=True, reason="dry_run")
        return {"ok": True, "dry_run": True, "action": action,
                "confirm_required": expect, "stats": stats}

    ts = audit.stamp()
    dry = engine.consolidate(dry_run=True)
    dry_path = audit.backup_json(f"merge.{ts}.dryrun.json", dry)
    keys = _candidate_keys(engine, "_scan_unconsolidated", storage)
    aff_path = _export_before(storage, keys, ts)
    stats = engine.consolidate(dry_run=False)
    audit.record(action, target, ok=True,
                 reason=f"dry_run_report={dry_path}; affected={aff_path}")
    return {"ok": True, "dry_run": False, "action": action, "stats": stats,
            "confirm_required": expect, "dry_run_report": dry_path,
            "affected_backup": aff_path, "affected_keys": len(keys)}


@router.post("/api/v1/ops/forget", dependencies=[Depends(require_session), Depends(write_guard)])
def ops_forget(body: BulkOpsBody, storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """选择性遗忘（Forgetter）。**默认 dry-run，零写入**。

    实跑必须带 `confirm="<当前记忆条目总数>"`（取 `GET /api/v1/stats` 的
    `retrieval.count`）。实跑前同样先落 dry-run 报告 + 受影响 keys 全量导出。
    物理删除由 Forgetter 经 `storage.delete_fragments_batch` 完成。
    """
    from keepsake.core.ops.forgetter import Forgetter

    action, target = "ops.forget", "all"
    expect = _need_count_confirm(body, lambda: _total_count(storage), action, target,
                                 "当前记忆条目总数", "GET /api/v1/stats 的 retrieval.count")

    # Forgetter 构造默认 dry_run=True ⇒ forget() 不删；force=True 才真删
    engine = Forgetter(storage)
    if body.dry_run:
        stats = engine.forget()
        audit.record(action, target, ok=True, reason="dry_run")
        return {"ok": True, "dry_run": True, "action": action,
                "confirm_required": expect, "stats": stats}

    ts = audit.stamp()
    dry = engine.forget()
    dry_path = audit.backup_json(f"forget.{ts}.dryrun.json", dry)
    keys = _candidate_keys(engine, "_find_forgettable", storage)
    aff_path = _export_before(storage, keys, ts)
    stats = engine.forget(force=True)
    audit.record(action, target, ok=True,
                 reason=f"dry_run_report={dry_path}; affected={aff_path}")
    return {"ok": True, "dry_run": False, "action": action, "stats": stats,
            "confirm_required": expect, "dry_run_report": dry_path,
            "affected_backup": aff_path, "affected_keys": len(keys)}

# --------------------------------------------------------------------------
# 常驻层注入预算（设置）
# --------------------------------------------------------------------------

#: 预算取值区间。下限 1000：低于这个量连一两条正常记忆都放不下，等于把常驻层
#: 关掉却还开着口；上限 500000：再大就是「不设预算」，注入路径会退化成全量注入。
CORE_BUDGET_MIN = 1000
CORE_BUDGET_MAX = 500000


def _read_config_or_reject(action: str, target: str) -> Dict[str, Any]:
    """读 config.json 原始 JSON；读不出 / 不是对象 ⇒ 500（绝不覆盖一份坏配置）。

    **写配置的两个端点共用这一份**（`settings/core-budget` 与 `settings/tiering`）：
    各写一份判据迟早漂一份，而漂移的表现是「把存储后端 / LLM 通道的配置连同坏值
    一起回写下去」。
    """
    path = config.keepsake_config_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _reject(500, "config_unreadable",
                f"config.json 读不出来，拒绝覆盖（{path}: {e}）", action, target)
    if not isinstance(raw, dict):
        _reject(500, "config_not_object",
                f"config.json 顶层不是 JSON 对象，拒绝覆盖：{path}", action, target)
    return raw


def _write_config_or_reject(new: Dict[str, Any], action: str, target: str) -> None:
    """把整份配置写回去；写失败 ⇒ 500（备份已经落盘，可人工回退）。"""
    path = config.keepsake_config_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as e:
        _reject(500, "config_write_failed", f"config.json 写失败：{e}", action, target)


@router.put("/api/v1/settings/core-budget",
            dependencies=[Depends(require_session), Depends(write_guard)])
def set_core_budget(body: CoreBudgetBody) -> Dict[str, Any]:
    """设置常驻层注入字符预算（写 config.json 的 `retrieval.core_char_budget`）。

    🔴 **只动这一个键**：读原 JSON → 改 `retrieval` 节的这一个键 → 原样回写，
    其余键与键序都不变（`json.loads` 进 dict 保序，赋值已存在的键不换位）。
    config.json 里还有存储后端、LLM 通道等配置，一个手滑重排/丢键就能让
    provider 读不到 DSN —— 「改预算」绝不能有这种副作用。

    确认串 = **当前生效值**的字符串（不是 key 末 8 位那种记认的）：改的是一个
    全局阈值，回退它影响每一轮注入，所以要人先看一眼界面上现在是多少。
    写前把原 config 整份备份进审计备份目录（`audit.backup_json`），备份写不成
    ⇒ 不写（与本模块其它写端点同一纪律）。

    🔴 **必须打 write_guard**：它第一次改的是**进程外的文件**，审计落不下账的
    机器上这次配置改动将无法追溯。
    """
    action, target = "settings.core_budget", "retrieval.core_char_budget"
    if not (CORE_BUDGET_MIN <= body.budget <= CORE_BUDGET_MAX):
        _reject(400, "budget_out_of_range",
                f"预算必须在 {CORE_BUDGET_MIN}~{CORE_BUDGET_MAX} 字符之间，本次 {body.budget}",
                action, target)
    current = config.core_char_budget()
    _need_text(body.confirm, str(current), action, target,
               f"当前生效预算 {current}")

    raw = _read_config_or_reject(action, target)
    backup = audit.backup_json(f"config.core-budget.{audit.stamp()}.json", raw)

    new = dict(raw)                                  # 顶层保序
    retrieval = dict(new.get("retrieval") or {})     # 节内也保序
    retrieval["core_char_budget"] = int(body.budget)
    new["retrieval"] = retrieval
    _write_config_or_reject(new, action, target)
    config.forget_core_budget_cache()   # 同尺寸改值时指纹可能不变 ⇒ 主动失效

    audit.record(action, target, ok=True,
                 reason=f"{current} -> {body.budget}; backup={backup}; "
                        f"path={config.keepsake_config_file()}")
    return {"ok": True, "action": action, "budget": int(body.budget),
            "path": str(config.keepsake_config_file()), "backup": backup}


# --------------------------------------------------------------------------
# 分级班车（core tiering）：只读状态 + 设置开关/阈值
# --------------------------------------------------------------------------

#: 状态端点带回的固定文案。**默认关死**这件事不许只写在代码注释里 ——
#: 看板与设置页都要能看到「机制已上线但默认没开」这件事本身。
TIERING_DISABLED_NOTE = (
    "分级班车默认关闭（config 缺 tiering 节 / enabled=false 即缺省关）："
    "上线≠机制启动，先用 scripts/tiering_cron.py --dry 观察几天，"
    "看清 stats 再在这里打开总开关。"
)


def _tiering_settings() -> Dict[str, Any]:
    """当前生效的 `tiering` 节（读 config.json；读不出 ⇒ 缺省，即全关）。

    路径走 `config.keepsake_config_file()`（与 PUT 写的是同一个文件），
    只读端点绝不能因为配置坏了就 500 —— 看板要照常显示「默认关」。
    """
    from keepsake.core import tiering as tiering_mod

    return tiering_mod.resolve_tiering(_read_config_quiet())


def _read_config_quiet() -> Dict[str, Any]:
    """config.json 原始 JSON；读不出 / 不是对象 ⇒ `{}`（**不抛**，只读面用）。"""
    try:
        raw = json.loads(config.keepsake_config_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


@router.get("/api/v1/tiering/status", dependencies=[Depends(require_session)])
def tiering_status(storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """分级班车状态（**只读端点**：不挂 `write_guard`）。

    `{enabled, last_run, last_stats, budget:{used,total,ratio}}`。
    * `budget` 与 `GET /api/v1/core/stats` 同源同口径（同一原语 `list_by_tag("core")`
      + 同一个预算函数），页面上的占用率与注入截断永远同口径；
    * `last_run` / `last_stats` 来自班车状态文件（`scripts/tiering_cron.py` 写的）。
      **从没跑过 ⇒ 两个都是 null / 空**，不是「上次成功」—— 一次没跑过的班车
      不能显示成绿灯。
    """
    from keepsake.core import tiering as tiering_mod

    t = _tiering_settings()
    rows = list(storage.list_by_tag(CORE_TAG))
    used = sum(len(str(r.get("content", "") or "")) for r in rows)
    total = config.core_char_budget()
    state = tiering_mod.read_state()
    return {
        "enabled": bool(t["enabled"]),
        "note": "" if t["enabled"] else TIERING_DISABLED_NOTE,
        "last_run": state.get("last_run") or None,
        "last_dry_run": bool(state.get("dry_run", True)),
        "last_stats": _stats_body(state),
        "settings": t,
        "budget": {
            "used": used,
            "total": total,
            "ratio": round(used * 100.0 / total, 2) if total > 0 else 0.0,
        },
    }


def _stats_body(state: Dict[str, Any]) -> Dict[str, Any]:
    """状态文件里给看板看的那部分（去掉 last_run / 路径等自述字段）。"""
    return {k: v for k, v in state.items()
            if k not in ("last_run", "state_file", "enabled", "dry_run")}


@router.put("/api/v1/settings/tiering",
            dependencies=[Depends(require_session), Depends(write_guard)])
def set_tiering(body: TieringBody) -> Dict[str, Any]:
    """设置分级班车的开关与阈值（写 config.json 的 `tiering` 一节）。

    与 `set_core_budget` **同一套纪律**：只动这一节、其余键与键序不变
    （`dict(raw)` 浅拷贝后赋值，已存在的键不换位）；写前把原 config 整份备份进
    审计备份目录，写不成功 ⇒ 一条都不写；必须打 `write_guard`（第一次改的是
    进程外的文件）。

    🔴 **confirm = 当前生效 enabled 的字符串 `"true"` / `"false"`**：这个开关决定
    班车每天会不会动常驻层，回退它的影响面是整个常驻层，所以确认串是「现在是不是
    开着」而不是「条数」（条数在另一张卡上，不防这种手滑）。

    非法值 **400**（不回落）：手改 config.json 才是「回落缺省 + warning」那层
    容错；控制台提交是机器输入，半截阈值绝不许落盘（判据与缺省表在
    `keepsake.core.tiering.validate_tiering`，与班车读配置用的是同一份 schema）。
    """
    from keepsake.core import tiering as tiering_mod

    action, target = "settings.tiering", "tiering"
    try:
        clean = tiering_mod.validate_tiering(body.tiering)
    except ValueError as e:
        _reject(400, "invalid_tiering", f"tiering 配置不合法：{e}", action, target)
    current = bool(_tiering_settings()["enabled"])
    _need_text(body.confirm, str(current).lower(), action, target,
               f"当前生效的 enabled（{str(current).lower()}）")

    raw = _read_config_or_reject(action, target)
    backup = audit.backup_json(f"config.tiering.{audit.stamp()}.json", raw)
    new = dict(raw)                          # 顶层保序
    new["tiering"] = clean                   # 节内由 validate_tiering 补齐成完整一份
    _write_config_or_reject(new, action, target)
    config.forget_tiering_cache()   # 同尺寸改值时指纹可能不变 ⇒ 主动失效

    audit.record(action, target, ok=True,
                 reason=f"enabled={current} -> {clean['enabled']}; backup={backup}")
    return {"ok": True, "action": action, "enabled": clean["enabled"],
            "tiering": clean, "backup": backup}


# --------------------------------------------------------------------------
# 检索结果 LLM 筛选（screening 节）：只读状态 + 设置开关/阈值
# --------------------------------------------------------------------------

#: 控制台可写的值域（**比 `core.screening.BOUNDS` 窄一档**）。
#:
#: 为什么要两套：那一套是「手改 config.json 时的兜底」——手写错了回落缺省 +
#: warning 就够了；这一套是「控制台提交」——机器输入，半截值**绝不许落盘**
#: （落在磁盘上的是下一轮检索真的在用的值）。
SCREENING_BOUNDS: Dict[str, Tuple[int, int]] = {
    "max_pick": (1, 30),
    "timeout_s": (10, 300),
    "max_tokens": (512, 16384),
}


def _screening_key_present(api_key_file: str) -> bool:
    """key 文件在不在（**只看文件在不在，绝不读内容**）。

    路径填错时，筛选会一路 fail-open 到 `skipped:no_key`（检索照常返回，
    只是没筛）—— 那是一种「静默失效」：配置看着对，筛选其实从没跑过。
    所以控制台要把这个状态**显式摆出来**（绿=在、黄=没找到）。
    """
    try:
        return Path(str(api_key_file or "")).expanduser().is_file()
    except OSError:
        return False


def _screening_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """config.json 的 `screening` 节 → 控制台要的扁平视图。

    走 `core.screening.resolve_screening`（**与 MCP 侧读配置同一个函数**）：
    控制台显示的 model / 超时 / token 上限必须与真正发请求时用的**同源**，
    两处各解析一遍，早晚漂成「页面写着 90s、实际发的是 600s」。
    """
    from keepsake.core import screening as screening_mod

    section = raw.get("screening")
    resolved = screening_mod.resolve_screening(raw)
    return {
        "configured": isinstance(section, dict),
        "enabled": bool(resolved["enabled"]),
        "default_on": bool(resolved["default_on"]),
        "model": resolved["model"],
        "base_url": resolved["base_url"],
        "api_key_file": resolved["api_key_file"],
        "max_pick": int(resolved["max_pick"]),
        "timeout_s": int(resolved["timeout_s"]),
        "max_tokens": int(resolved["max_tokens"]),
        "key_present": _screening_key_present(resolved["api_key_file"]),
        "path": str(config.keepsake_config_file()),
        "backup_dir": str(audit.backup_dir()),
        # 值域也由后端给（前端数字输入框的 min/max 照抄它，**前端零写死**）
        "bounds": {k: [int(v[0]), int(v[1])] for k, v in SCREENING_BOUNDS.items()},
    }


@router.get("/api/v1/settings/screening", dependencies=[Depends(require_session)])
def get_screening() -> Dict[str, Any]:
    """检索筛选配置（**只读端点**：不挂 `write_guard`，绝不碰 config.json）。

    返回 `screening` 节的**全量** + 两个状态位：

    * `configured` —— config.json 里**有没有**这一节（没有 = 还没配过，
      页面显示的是缺省骨架，别让人以为已经配好了）；
    * `key_present` —— `api_key_file` 指的那个文件**在不在**（填错路径时
      筛选会一路 fail-open 成「静默不筛」，这个位就是让它当场可见的）。

    🔴 只读端点**绝不因为配置坏了就 500**：config 读不出来时按缺省
    （= 关死）回，页面照常显示「没配」。
    """
    return _screening_payload(_read_config_quiet())


@router.put("/api/v1/settings/screening",
            dependencies=[Depends(require_session), Depends(write_guard)])
def set_screening(body: ScreeningBody) -> Dict[str, Any]:
    """设置检索筛选（写 config.json 的 `screening` **一节**）。

    🔴 **只动这一节**：读整份 → 合并这一节 → 原样回写，其余键与键序都不变
    （`dict(raw)` 浅拷贝后赋值，已存在的键不换位）—— config.json 里还有存储
    后端、LLM 通道等配置，「改筛选」顺手重排/丢键就能让 provider 读不到 DSN。
    写前把原 config 整份备份进审计备份目录（`audit.backup_json`），写不成功
    ⇒ 一条都不写；必须打 `write_guard`（第一次改的是**进程外**的文件）。

    🔴 **热生效走既有 mtime 机制**（改完下一轮检索即生效），本端点**不重载
    任何进程**。同尺寸改值时指纹可能不变 ⇒ 写完主动失效本进程内的配置缓存
    （`screening.invalidate_cfg_cache`）；跨进程那一半靠 mtime。

    校验（**越界 400 带人话**，半截值绝不落盘）：
    * `model` / `base_url` / `api_key_file` 必须是非空串；
    * `max_pick` 1~30、`timeout_s` 10~300、`max_tokens` 512~16384；
    * `api_key_file` **允许暂不存在**（可以先填路径后补文件）—— 保存后
      探测并在回包里带 `key_present`（填错当场可见，不静默失效）。
    """
    action, target = "settings.screening", "screening"
    _need_flag(body.confirm, action, target,
               "保存筛选配置必须带 confirm=true（这是一次配置写操作）")

    model = (body.model or "").strip()
    base_url = (body.base_url or "").strip()
    key_file = (body.api_key_file or "").strip()
    if not model:
        _reject(400, "screening_invalid", "模型名不能为空（筛选靠它发请求）",
                action, target)
    if not base_url:
        _reject(400, "screening_invalid",
                "接口地址不能为空（要带 /v1 这样的完整前缀）", action, target)
    if not key_file:
        _reject(400, "screening_invalid",
                "key 文件路径不能为空（筛选的凭据只从那个文件读）", action, target)
    for name, (lo, hi) in SCREENING_BOUNDS.items():
        value = int(getattr(body, name))
        if not (lo <= value <= hi):
            _reject(400, "screening_out_of_range",
                    f"{name} 必须在 {lo}~{hi} 之间，本次 {value}", action, target)

    raw = _read_config_or_reject(action, target)
    backup = audit.backup_json(f"config.screening.{audit.stamp()}.json", raw)

    section = {
        "enabled": bool(body.enabled),
        "default_on": bool(body.default_on),
        "model": model,
        # 尾斜杠去掉：筛选侧拼的是 `{base_url}/chat/completions`
        "base_url": base_url.rstrip("/"),
        "api_key_file": key_file,
        "max_pick": int(body.max_pick),
        "timeout_s": int(body.timeout_s),
        "max_tokens": int(body.max_tokens),
    }
    new = dict(raw)                       # 顶层保序
    new["screening"] = section            # 节内由表单补齐成完整一份
    _write_config_or_reject(new, action, target)

    from keepsake.core import screening as screening_mod

    screening_mod.invalidate_cfg_cache()   # 同尺寸改值时指纹可能不变 ⇒ 主动失效

    key_present = _screening_key_present(key_file)
    audit.record(action, target, ok=True,
                 reason=f"enabled={section['enabled']}; default_on={section['default_on']}; "
                        f"model={model}; backup={backup}; "
                        f"path={config.keepsake_config_file()}")
    return {"ok": True, "action": action, "screening": section,
            "key_present": key_present, "path": str(config.keepsake_config_file()),
            "backup": backup}


# --------------------------------------------------------------------------
# 勾选批量：改数据（ops/bulk）与纯读（ops/bulk-export）
# --------------------------------------------------------------------------

#: 一次勾选操作的上限。500 条既是交互上限，也压住了单次请求体；超了报 400
#: 而不是截断成「前 500 条」（被截断的批量写 = 用户以为都做了、实际没做）。
BULK_MAX_KEYS = 500

#: 批量动作白名单 —— 每一条都必须有对应的**单条**端点与同一存储路径
BULK_ACTIONS = ("promote", "demote", "archive", "delete")


def _dedup_keys(raw: List[str], action: str, target: str) -> List[str]:
    """勾选的 keys 去重 + 区间校验（保序：与用户勾选顺序一致，回包好对账）。"""
    keys: List[str] = []
    seen = set()
    for k in raw or []:
        s = str(k).strip()
        if s and s not in seen:
            seen.add(s)
            keys.append(s)
    if not keys:
        _reject(400, "bulk_no_keys", "keys 不能为空（请先勾选条目）", action, target)
    if len(keys) > BULK_MAX_KEYS:
        _reject(400, "bulk_too_large",
                f"一次最多 {BULK_MAX_KEYS} 条，本次 {len(keys)} 条：请分批操作（超限不截断）",
                action, target)
    return keys


def _err_code(e: HTTPException) -> str:
    """把 `_apply` 抛出的 HTTPException 收回成可读原因码（批量语义：逐条记，不中断）。"""
    detail = e.detail
    if isinstance(detail, dict):
        return str(detail.get("error") or f"http_{e.status_code}")
    return f"http_{e.status_code}"


@router.post("/api/v1/ops/bulk",
             dependencies=[Depends(require_session), Depends(write_guard)])
def bulk_ops(body: BulkActionBody, storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """**勾选批量**写：promote / demote / archive / delete。

    🔴 **逐条走单条端点的同一存储函数**（`_with_core` + `_apply` 打标签、
    `_apply` 归档、`audit.backup_fragment` + `delete_fragments_batch` 真删），
    不是另写一份 SQL：单条端点是这条路线的唯一语义来源，复制一份迟早漂移。

    确认串 = **去重后的选中条数**（与 merge/forget 的「当前计数」同一防手滑口径）：
    勾了 37 条却手输 38 个都不会放行 ⇒ 「我要删的这一批」和「我点确认的那一批」
    必须是同一个数。

    **单条失败不中断整批**：不存在 / 已是常驻 / 不在常驻 / 后端拒写，各自在
    `failed` 里带原因码返回，其余照常执行 —— 批量里 499 条成功不该被第 500 条
    拖成整批 0 成功。审计另记一条批次汇总（成败数 + 备份目录）。

    真删仍是**先逐条备份**、后删：某条备份写不进（磁盘满）就跳过那一条并记
    `backup_failed`（宁删不成，与单条端点同一纪律）。
    """
    if body.action not in BULK_ACTIONS:
        _reject(400, "bulk_bad_action",
                f"action 只支持 {list(BULK_ACTIONS)}，本次 {body.action!r}",
                "ops.bulk", "-")
    action = f"ops.bulk.{body.action}"
    keys = _dedup_keys(body.keys, action, "batch")
    _need_text(body.confirm, str(len(keys)), action, "batch",
               f"本次勾选的 {len(keys)} 条")

    failed: List[Dict[str, str]] = []
    succeeded = 0
    for key in keys:
        try:
            row = storage.get_fragment(key)
            if not row:
                failed.append({"key": key, "reason": "not_found"})
                continue
            if body.action == "archive":
                # 与 archive_fragment 同一路径：备份 → is_archived=1 + valid_until
                # （版本化失效，不物理删；重复归档幂等，与单条端点一致）
                audit.backup_fragment(key, row)
                _apply(storage, key,
                       {"is_archived": "1", "valid_until": _now_iso()}, action)
            elif body.action == "delete":
                audit.backup_fragment(key, row)        # 写不进 ⇒ OSError ⇒ 该条跳过
                if storage.delete_fragments_batch([key]) != 1:
                    failed.append({"key": key, "reason": "delete_failed"})
                    continue
            else:
                on = body.action == "promote"
                has = CORE_TAG in _tag_list(row.get("tags"))
                if has == on:                          # 已在那一层 / 不在那一层
                    failed.append({"key": key,
                                    "reason": "already_core" if on else "not_core"})
                    continue
                audit.backup_fragment(key, row)
                _apply(storage, key,
                       {"tags": _with_core(row.get("tags"), on)}, action)
            succeeded += 1
        except HTTPException as e:        # _apply 回读对不上 / 后端拒写
            failed.append({"key": key, "reason": _err_code(e)})
        except OSError:                   # 备份落盘失败 —— 这条宁删不成
            failed.append({"key": key, "reason": "backup_failed"})

    backup_dir = str(audit.backup_dir()) if body.action == "delete" else None
    audit.record(action, f"batch:{len(keys)}", ok=bool(succeeded) or not failed,
                 reason=f"requested={len(keys)}; succeeded={succeeded}; "
                        f"failed={len(failed)}; backup_dir={backup_dir or '-'}")
    out: Dict[str, Any] = {"ok": True, "action": body.action,
                           "requested": len(keys), "succeeded": succeeded,
                           "failed": failed}
    if backup_dir:
        out["backup_dir"] = backup_dir
    return out


@router.post("/api/v1/ops/bulk-export", dependencies=[Depends(require_session)])
def bulk_export(body: BulkExportBody,
                storage: Any = Depends(get_storage)) -> StreamingResponse:
    """**勾选批量导出**（JSONL / CSV）。**只读端点**：不挂 `write_guard`。

    为什么是 POST 而不是 `GET /api/v1/export?keys=a,b,c`：500 个 key 拼进 query
    约 11KB，nginx/uvicorn 的请求行上限（默认 8K）会直接 431 —— 「勾选导出」在最
    大的勾选规模上必然失败。走 body 就没有这个上限。

    🔴 **鉴权仍与 GET /api/v1/export 同级**（`require_session`）：它是纯读，但导出
    的是记忆全文，不是公开端点；只是不挂 `write_guard`（不改数据，不必为审计
    落盘位置 fail-closed）。
    """
    keys = _dedup_keys(body.keys, "memory.bulk_export", "batch")
    rows = storage.get_fragments_batch(keys)
    data = [dict(rows.get(k) or {}, key=k) for k in keys]
    ext = "csv" if body.format == "csv" else "jsonl"
    media = ("text/csv; charset=utf-8" if ext == "csv"
             else "application/x-ndjson; charset=utf-8")
    fname = f"keepsake-selected-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.{ext}"
    return StreamingResponse(_stream(data, ext), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store",
    })


# --------------------------------------------------------------------------
# 导入模板（只读下载）
# --------------------------------------------------------------------------

#: 三种导入模板。**每行都是合法可导入的 JSON**（不塞 `#` 注释行 —— 用户多半
#: 直接把模板改完当 JSONL 喂进来，注释行会被解析成坏行）。示例里零 IP、零口令、
#: 零真实业务数字，全是 `示例`/`demo` 这类占位。
IMPORT_TEMPLATES: Dict[str, str] = {
    "memory": (
        '{"content":"示例：一条要进检索层的记忆","tags":"demo,import"}\n'
        '{"content":"示例：第二条记忆。tags 可省略，省略即空标签；'
        'core / core_candidate / shared / agent:* 是治理保留标签，写了会被后端拒收或剥除"}\n'
    ),
    "kb": (
        '{"title":"示例文档标题","content":"示例正文，可多行\\n转义"}\n'
        '{"title":"示例：第二篇文档","content":"标题与正文都必填，任一为空这篇会被拒收（不静默丢）"}\n'
    ),
    "core": (
        '{"content":"示例：一条要进检索层的记忆","tags":"demo,import"}\n'
        '{"content":"示例：想直接进常驻层？在「常驻层」页点「导入」并勾选「导入即升常驻」，'
        '不必手打 core 标签（core / core_candidate 由后端管控，写了也会被拒收或剥除）",'
        '"tags":"demo,core_candidate"}\n'
    ),
}


@router.get("/api/v1/import/template", dependencies=[Depends(require_session)])
def import_template(
    kind: str = Query("memory", pattern="^(memory|kb|core)$", description="模板形态"),
) -> PlainTextResponse:
    """下载导入模板（JSONL 示例）。**只读端点**：不挂 `write_guard`。

    为什么要有它：导入弹框要求每行一个 JSON 对象，但字段名/形状（记忆条目是
    `content`，知识库是 `title`+`content`）只能靠猜 —— 猜错的表现是「导入了 N 条
    但全是 0」，用户无从判断是自己文件错了还是功能坏了。
    """
    return PlainTextResponse(
        IMPORT_TEMPLATES[kind],
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="keepsake-import-template-{kind}.jsonl"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------
# 待审条目计数（read-only · 控制台侧栏徽标的唯一取数口）
# --------------------------------------------------------------------------

@router.get("/api/v1/review/counts", dependencies=[Depends(require_session)])
def review_counts(storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """待审条目的全量计数（**只读端点**：不挂 `write_guard`，零写动作）。

    返回 `{promote_candidates, demote_candidates, kb_pending}`：
      * `promote_candidates` = 检索层条目打着 `core_candidate`（人工升常驻 / 班车
        提名）的待审条数；
      * `demote_candidates`  = 班车压力轨提名退位的待审条数（挂着
        `tier:demote-candidate`）；
      * `kb_pending`         = 知识库 `documents.status='pending'`
        （子代理经 MCP/通用接口提交等审核）。

    三类都是**全库口径**（顶部徽标的数必须永远等于库里实际待审数），
    与既有的 `fragments?tag=core_candidate` / `tier:demote-candidate` 列表
    与 `kb/pending` 端点的口径一字一致 —— 一边动口径、另一边不跟上 =
    看见和动手的数对不上。

    缺列降级（与 `kb_router.kb_type_counts` 同款）：
      * 记忆库缺 `core_candidate` / `tier:demote-candidate` 标签 ⇒ 对应值为 0
        （list_by_tag 本就返回 []，无需特判）；
      * 知识库未配置 / 库缺 `documents` 表 / `status` 列 ⇒ `kb_pending=0`，**不**
        让整页 500 ——
        🔴 单点失败不许把记忆侧的徽标也拖没（侧栏常驻指望的是 promote/demote
        那两个数，知识库挂就挂知识库那颗）。

    复用 `storage.list_by_tag` 这一个原语：三后端（PG / SQLite / Redis）的标签
    计数就是它，不在 ops 层另发明 SQL 方言分支。
    """
    from keepsake.core import tiering as tiering_mod
    from . import kb as kb_mod

    promote = len(list(storage.list_by_tag(tiering_mod.CANDIDATE_TAG) or []))
    demote = len(list(storage.list_by_tag(tiering_mod.DEMOTE_CANDIDATE_TAG) or []))
    kb_pending = 0
    # 🔴 知识库未配置 / 缺列 / 不存在文档表：单点降级，不波及 promote/demote。
    # 与 `kb_router.kb_pending` 行为不同是因为后者是页面入口（KB 真有问题就该
    # 给白话 500），这里只是侧栏计数（一行字挂了 ≠ 整页炸）。
    try:
        tabs = kb_mod._table_columns(kb_mod._ident(config.pg_schema()),
                                     (config.KB_TABLE_DOCUMENTS,))
        docs_cols = tabs.get(config.KB_TABLE_DOCUMENTS) or set()
        d_status = next((kb_mod._ident(c) for c in ("status",) if c in docs_cols), None)
        if d_status:
            with kb_mod._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) AS n "
                        f"FROM {kb_mod._ident(config.pg_schema())}.{config.KB_TABLE_DOCUMENTS} "
                        f"WHERE {d_status} = %s",
                        [kb_mod.ingest.STATUS_PENDING],
                    )
                    row = cur.fetchone() or {}
            kb_pending = int(row.get("n") or 0)
    except Exception:
        # 包括 HTTPException（如 KB 未配置/Schema 不存在）—— 这里是侧栏计数
        # 的一个差分，KB 真有错写由 /kb/pending 端点负责给白话 500，
        # 这里挂 = 整页红，不值。
        kb_pending = 0
    return {
        "promote_candidates": promote,
        "demote_candidates": demote,
        "kb_pending": kb_pending,
    }
