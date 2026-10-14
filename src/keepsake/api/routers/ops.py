"""**写**操作端点：打标 / 改 / 归档 + 合并 / 遗忘（dry-run 一等公民）。

🔴 四条硬约束（任务书 P2），全部在这一层兑现：

1. **写必须走 keepsake 自己的 API** —— `storage.update_fragment_fields`
   （打标 / 改正文 / 归档）与 `Consolidator` / `Forgetter`（合并 / 遗忘，
   它们内部自己走 `storage.write_fragments_batch` / `delete_fragments_batch`）。
   **不自拼 UPDATE/DELETE SQL**：那样会绕开存储层的去重 / supersede /
   失效语义（索引不重算 = 检索静默召回旧内容，而不是召不回）。
   走 `update_fragment_fields` 而**不是** `write_fragments_batch`：后者在
   Sqlite 后端是**迁移专用**的 upsert，未传的列会被写成空串（等于抹字段）。
2. **dry-run 是一等公民**：merge/forget 默认 `dry_run=true`；实跑
   （`dry_run=false`）必须带 `confirm="<当前计数>"`（防手滑）——
   缺 ⇒ 400 `confirm_required`，不匹配 ⇒ 400 `confirm_mismatch`。
   **除 `dry_run=false` 外没有第二条实跑入口。**
3. **写前自动备份**（`audit.backup_fragment`）：备份写不成功 ⇒ 整个写操作
   中止，数据一秒都没动。审计落盘位置在进路由时就自检过（`write_guard`）——
   记不上账 ⇒ 这次写根本不做。
4. **每次写（含被拒绝的）审计一行 JSON**（`audit.record`）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import audit, config
from ..deps import get_storage, require_session
from .core import CORE_TAG

router = APIRouter(tags=["ops"])

#: 归档确认串长度（手输 key 末 8 位，防手滑）
CONFIRM_SUFFIX_LEN = 8


# --------------------------------------------------------------------------
# 入参
# --------------------------------------------------------------------------

class ConfirmFlag(BaseModel):
    """`{"confirm": true}` —— 打标/改正文用。缺 confirm 即 false ⇒ 400。"""

    confirm: bool = False


class ConfirmText(BaseModel):
    """`{"confirm": "<期望串>"}` —— 归档用手输的 key 末 8 位。"""

    confirm: str = ""


class UpdateBody(BaseModel):
    """`{"confirm": true, "content": ..., "tags": ...}`。"""

    confirm: bool = False
    content: Optional[str] = None
    tags: Optional[str] = None


class BulkOpsBody(BaseModel):
    """`{"dry_run": true}` / `{"dry_run": false, "confirm": "<当前计数>"}`。"""

    dry_run: bool = True
    confirm: Optional[str] = None


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
    """把改动落到存储层 —— 一律走 `storage.update_fragment_fields`（局部字段更新）。

    写后**回读校验**：后端可能只认自己白名单里的列（返回 False 或静默丢弃），
    那样控制台会报「成功」而数据没动。回读一次对不上就 500（数据已被后端改动
    前的备份兜着，见 `audit.backup_fragment`）。

    🟡 已知后端差异（控制台不替后端打补丁，也不自拼 SQL 去补索引）：
    Sqlite 后端会在改了 `content`/`tags`/`entities` 时**就地重算** FTS；
    Pg 后端的 `update_fragment_fields` 只 UPDATE 列、**不重算 `content_tsv`** ——
    这是它自己 docstring 里写明的维护语义（基类也把该原语定义为「不动 content /
    content_tsv / embedding」）。真要在 PG 上改正文并保持索引一致，得由后端补一条
    原语（控制台侧的自助 UPDATE 会绕过这道守卫，正是任务书禁止的）。
    """
    if not storage.update_fragment_fields(key, fields):
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