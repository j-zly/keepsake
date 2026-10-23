"""待办（`ks_todo`）的控制台面 —— 2026-10 ks_todo 一等公民。

待办此前一直用 `kind:pending` 记忆条目凑合 —— 盘点 12 条里 9 条烂账
（做完没销、过期没人知）。这页给它一个正经的去处：状态机 + 归属 + 到期日。

## 四条口径

1. **状态三态**：开放 / 已完成 / 已废弃 —— 「已废弃」与「已完成」分开摆
   （一个是干完了，一个是这条作废了）。
2. **到期两态**：3 天内到期 = 橙（`--warn`）、已过期 = 红（`--danger`）。
   🔴 判据在**后端**（`govern.todo.due_state`），控制台只按它上色 ——
   两边各算一次必然漂（时区一差就是「页面红了、后端口径里没红」）。
3. **归属必在册**：owner 必须是**在册且已批准**的代理；未批准 ⇒ 404
   （不是 400 —— 在治理面眼里「查无这个代理」就是查无）。
4. **写面纪律**：新增 / 改状态 / 真删都要显式二次确认；改前备份；一次审计
   （被拒绝的写也记一行）。

## 🔴 「建议废弃」只提名不改状态

维护页那份清单是**提名**：到期日过了 7 天还开着的待办列出来，让人决定
要不要标废弃 —— 自动改状态等于替人做决定，而「这条到底还有没有人管」
只有人知道。**本模块没有任何端点会自动改状态。**
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from keepsake.govern.registry import ROLE_PRIMARY, STATUS_APPROVED
from keepsake.govern.todo import (
    DEFAULT_LIMIT,
    LIMIT_MAX,
    OBSOLETE_SUGGEST_DAYS,
    SCOPE_ALL,
    SCOPES,
    STATUSES,
    TodoError,
    TodoStore,
)

from .. import audit
from ..deps import get_storage, require_session
from ..schemas import TodoAgentSummary, TodoSummary
from .agents import _backup, _registry
from .ops import _need_flag, _need_text, _reject, write_guard

router = APIRouter(tags=["todos"])

logger = logging.getLogger("keepsake.api.todos")


class TodoCreateBody(BaseModel):
    """`POST /api/v1/todos` 的请求体。

    * `owner` 缺省 = **主代理**（控制台里加一条待办，多半是给主脑派活）；
    * `due` 是 ISO 日期（`YYYY-MM-DD`），留空 = 不限期；
    * `confirm` 恒为 true —— 控制台的写面统一要这道二次确认。
    """

    title: str = Field(..., description="待办标题（≤200 字）")
    detail: str = Field("", description="正文（可空，≤4000 字）")
    owner: str = Field("", description="归属代理 id（缺省 = 主代理）")
    due: str = Field("", description="到期日（ISO 日期 YYYY-MM-DD，可空）")
    confirm: Any = False


class TodoStatusBody(BaseModel):
    """`POST /api/v1/todos/{id}/status` 的请求体：目标状态 + 二次确认。"""

    status: str = Field(..., description="open / done / obsolete")
    confirm: Any = False


class TodoDeleteBody(BaseModel):
    """`DELETE /api/v1/todos/{id}` 的请求体：确认串 = **待办标题全文**。

    🔴 比 `confirm=true` 高一档：删一条待办是「删掉一件还没干完的事」，
    打错一个字就不该生效 —— 所以要人**手输一遍标题**。
    """

    confirm: str = Field("", description="待办标题全文（手输，防手滑）")


def _store(storage: Any) -> TodoStore:
    """按控制台的存储后端装配待办存储（并确保表在）。

    与 `_registry` 同款口径：控制台进程与主脑进程各自构造、各自 ensure，
    不另开连接、不另配 DSN。
    """
    store = TodoStore(storage)
    store.ensure_schema()
    return store


def _primary_agent(registry: Any) -> str:
    """当前主代理 id（注册表里 role=primary 的那位；查不到 ⇒ 空串）。

    🔴 查不到就**不猜**：owner 缺省时没有主代理 ⇒ 400（明确报「没有主代理」），
    而不是悄悄挂到一个错的 owner 名下。
    """
    for row in registry.list_agents() or []:
        if row.get("role") == ROLE_PRIMARY:
            return str(row.get("agent_id") or "")
    return ""


def _need_owner(registry: Any, owner: str, action: str) -> str:
    """owner 资格闸：不在册 / 未批准 / 已吊销 ⇒ 404（治理面：查无此代理）。"""
    row = registry.get(owner)
    if not row or row.get("status") != STATUS_APPROVED:
        _reject(404, "agent_not_found",
                f"注册表里没有这个已批准的代理：{owner}", action, owner)
    return owner


def _todo_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """待办行的对外形状（`id` 是整数，`due_state` 由后端算好）。"""
    return {
        "id": int(row.get("id") or 0),
        "title": row.get("title", ""),
        "detail": row.get("detail", ""),
        "owner": row.get("owner", ""),
        "creator": row.get("creator", ""),
        "status": row.get("status", "open"),
        "due": row.get("due", ""),
        "due_state": row.get("due_state", "none"),
        "created": row.get("created", ""),
        "updated": row.get("updated", ""),
        "done_at": row.get("done_at", ""),
    }


# ---------------------------------------------------------------- 只读

#: **未标注桶**：owner 为空 / 代理已从注册表删掉的那些待办归这一桶
#: （与 `stats.daily_by_agent` 的 `-` 桶同一口径：不假装它属于某个代理）。
UNATTRIBUTED_BUCKET = "-"


def _by_agent(rows: List[Dict[str, Any]], registry: Any) -> List[TodoAgentSummary]:
    """按代理分列的待办计数（`by_agent` 段）。

    🔴 **归属名只认注册表**（`ks_agent`）：`owner` 那一列存的是 agent_id，
    显示名（`name`）由注册表说了算 —— 待办行自己不带名字，另存一份必然漂。

    🔴 **查不到注册表的合并进「未标注」桶 `-`**（owner 为空 / 代理已删）：
    宁可说「不知道是谁的」，也不把活摊到一个不在册的代理名下。

    排序：完成率降序 → 条数降序 → 名字（稳定，前端不必再排一遍）。
    """
    names: Dict[str, str] = {}
    for row in registry.list_agents() or []:
        aid = str(row.get("agent_id") or "")
        if aid:
            names[aid] = str(row.get("name") or aid)

    merged: Dict[str, Dict[str, int]] = {}
    for r in rows:
        aid = str(r.get("owner") or "")
        key = aid if aid in names else UNATTRIBUTED_BUCKET
        acc = merged.setdefault(key, {"open": 0, "done": 0, "obsolete": 0, "total": 0})
        for f in ("open", "done", "obsolete", "total"):
            acc[f] += int(r.get(f) or 0)

    out: List[TodoAgentSummary] = []
    for key, acc in merged.items():
        total = int(acc["total"] or 0)
        closed = int(acc["done"] or 0) + int(acc["obsolete"] or 0)
        out.append(TodoAgentSummary(
            agent=names.get(key, UNATTRIBUTED_BUCKET),
            agent_id=key,
            open=int(acc["open"] or 0),
            done=int(acc["done"] or 0),
            obsolete=int(acc["obsolete"] or 0),
            total=total,
            ratio=round(closed * 10000 / total) / 100 if total > 0 else 0.0,
        ))
    out.sort(key=lambda r: (-r.ratio, -r.total, r.agent))
    return out

@router.get("/api/v1/todos", dependencies=[Depends(require_session)])
def list_todos(
    page: int = Query(1, ge=1, description="页码（从 1 起）"),
    page_size: int = Query(DEFAULT_LIMIT, ge=1, le=LIMIT_MAX, description="每页条数"),
    owner: str = Query("", description="按归属代理筛（空 = 全部）"),
    status: str = Query("", description="按状态筛：open / done / obsolete（空 = 全部）"),
    scope: str = Query(SCOPE_ALL, description="快捷档：open / done / all"),
    storage: Any = Depends(get_storage),
) -> Dict[str, Any]:
    """待办列表（服务端分页，按 id 倒序：新的在前）。

    🔴 **控制台是治理面 ⇒ 全量可查**（与「消息中心」同款：绝不被背着开小会）。
    代理侧的可见性（owner ∪ creator）由 MCP 工具面守，不在这条读端点上收窄。

    `status`（精确三态）优先于 `scope`（快捷档）。
    """
    store = _store(storage)
    try:
        rows, total = store.list_rows(owner=owner, status=status, scope=scope,
                                      page=page, size=page_size)
    except TodoError as e:
        raise HTTPException(status_code=400,
                            detail={"error": e.code, "message": e.message}) from e
    return {"items": [_todo_item(r) for r in rows], "total": int(total),
            "page": page, "page_size": page_size}


@router.get("/api/v1/todos/summary", response_model=TodoSummary,
            dependencies=[Depends(require_session)])
def todo_summary(owner: str = Query("", description="按归属代理筛（空 = 全部）"),
                 storage: Any = Depends(get_storage)) -> TodoSummary:
    """待办计数：`{open, done, obsolete, total, overdue}` **+ `by_agent` 段**。

    🔴 `overdue` = **开放且已过期**的条数（销掉的不算 —— 过期是活儿没干完）。
    看板那枚「待办」小节与待办页顶部的计数条**共用这一份**（前端不自己数）。

    🆕 `by_agent`：**按代理**分列的同一份计数（看板「完成率按代理看」用）。
    口径：一条 `GROUP BY owner, status`（只读聚合，**不动写路径**）；
    **归属名来自注册表**（`ks_agent`）—— `owner` 是 agent_id，**查不到注册表的
    归「未标注」桶 `-`**（不假装它属于某个代理，与 `stats.daily_by_agent` 的
    `-` 桶同一口径）。
    """
    store = _store(storage)
    base = store.summary(owner=owner)
    by_agent = _by_agent(store.summary_by_owner(owner=owner), _registry(storage))
    return TodoSummary(**base, by_agent=by_agent)


@router.get("/api/v1/todos/obsolete-suggestions", dependencies=[Depends(require_session)])
def todo_obsolete_suggestions(
    days: int = Query(OBSOLETE_SUGGEST_DAYS, ge=1, le=90,
                      description="过期满几天算「建议废弃」"),
    owner: str = Query("", description="按归属代理筛（空 = 全部）"),
    storage: Any = Depends(get_storage),
) -> Dict[str, Any]:
    """「建议废弃」提名清单：**只提名，不改状态**。

    判据：仍开放 + 到期日已过满 `days` 天（默认 7 天）。按到期日升序 ——
    欠得最久的排最前。
    """
    store = _store(storage)
    rows = store.obsolete_suggestions(days=days, owner=owner)
    return {"items": [_todo_item(r) for r in rows], "count": len(rows), "days": days}


# ---------------------------------------------------------------- 写面

@router.post("/api/v1/todos",
             dependencies=[Depends(require_session), Depends(write_guard)])
def create_todo(body: TodoCreateBody, storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """新增一条待办。

    🔴 `owner` 缺省 = **主代理**（控制台里加一条，多半是给主脑派活）；
    owner 不在册 / 未批准 ⇒ 404。`confirm` 必须是 true。
    """
    action = "todos.create"
    _need_flag(body.confirm, action, str(body.title or "")[:40],
               '必须显式传 {"confirm": true} 才新增待办')
    registry = _registry(storage)
    owner = str(body.owner or "").strip() or _primary_agent(registry)
    if not owner:
        _reject(400, "no_primary_agent",
                "注册表里没有主代理 —— 请指定 owner，或先设一个主代理", action, "")
    _need_owner(registry, owner, action)
    store = _store(storage)
    try:
        todo_id = store.add(body.title, body.detail, owner, owner, body.due)
    except TodoError as e:
        _reject(400, e.code, e.message, action, owner)
        raise          # pragma: no cover —— _reject 一定抛
    row = store.get(todo_id) or {}
    audit.record(action, str(todo_id), ok=True,
                 reason=f"owner={owner}; status=open")
    return {"ok": True, "id": int(todo_id), "item": _todo_item(row)}


@router.post("/api/v1/todos/{todo_id}/status",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_todo_status(todo_id: int, body: TodoStatusBody,
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """改一条待办的状态（`open` / `done` / `obsolete`）。

    🔴 `confirm` 必须是 true；状态非法 ⇒ 400；待办不存在 ⇒ 404。
    改前备份一份（人工复原的依据）。
    """
    action = "todos.status"
    _need_flag(body.confirm, action, str(todo_id),
               '必须显式传 {"confirm": true} 才改待办状态')
    store = _store(storage)
    row = store.get(todo_id)
    if not row:
        _reject(404, "todo_not_found", f"待办不存在：{todo_id}", action, str(todo_id))
    backup = _backup("status", row)
    try:
        changed = store.set_status(todo_id, body.status)
    except TodoError as e:
        _reject(400, e.code, e.message, action, str(todo_id))
        raise          # pragma: no cover
    if not changed:
        _reject(409, "todo_race", "待办在改动过程中消失，请重试", action, str(todo_id))
    audit.record(action, str(todo_id), ok=True,
                 reason=f"status={body.status}; backup={backup}")
    after = store.get(todo_id) or {}
    return {"ok": True, "id": int(todo_id), "item": _todo_item(after), "backup": backup}


@router.delete("/api/v1/todos/{todo_id}",
               dependencies=[Depends(require_session), Depends(write_guard)])
def delete_todo(todo_id: int, body: TodoDeleteBody,
                storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """**真删**一条待办。

    🔴 确认串 = **待办标题全文**（手输）：删一件还没干完的事，打错一个字
    就不该生效 —— 比 `confirm=true` 高一档。删前备份一份（人工复原的依据）。
    """
    action = "todos.delete"
    store = _store(storage)
    row = store.get(todo_id)
    if not row:
        _reject(404, "todo_not_found", f"待办不存在：{todo_id}", action, str(todo_id))
    _need_text(str(body.confirm or ""), str(row.get("title") or ""),
               action, str(todo_id), f"待办标题全文（{row.get('title') or ''}）")
    backup = _backup("delete", row)
    if not store.delete(todo_id):
        _reject(409, "todo_race", "待办在删除过程中消失，请重试", action, str(todo_id))
    audit.record(action, str(todo_id), ok=True, reason=f"deleted=1; backup={backup}")
    return {"ok": True, "id": int(todo_id), "deleted": True, "backup": backup}


__all__ = ["router", "TodoCreateBody", "TodoStatusBody", "TodoDeleteBody"]
