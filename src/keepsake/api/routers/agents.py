"""代理注册表控制台端点：列表 / 批准 / 吊销 / 改角色 / 改上下文开关。

2026-10 keepsake_agent_registry。治理面与 `api/routers/ops.py` 同一套纪律：

* **全 require_session**（读面与写面都是）；四条写端点**再叠 `write_guard`**
  （审计落不下账的机器上这次治理变更不许做）；
* **写前备份**：改角色会连带把另一个代理降级、改批准会顶掉旧凭据 —— 这两样
  都不可逆到「回滚靠记忆」的程度，先 `audit.backup_json` 落一份改前快照；
* **显式二次确认**：approve/exclude 要 `confirm=true`，role 要手输整个 agent_id，
  revoke 要手输 agent_id 前 8 位（与 ops 归档/真删同一道闸）；
* **一次审计**：被拒绝的写也记一行 `ok=false`（`_reject`）。

🔴 **凭据纪律**：明文只在 `approve` 的响应体里出现**一次**，之后库里只有 sha256。
审计 / 备份 / 错误消息里都不许出现明文 —— 本文件没有一处把它写进 `audit.record`
的 `reason`（那里只写「issued」这类与凭据无关的字样）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from keepsake.govern.registry import (
    ROLE_PRIMARY,
    ROLES,
    STATUS_APPROVED,
    STATUS_REVOKED,
    Registry,
)

from .. import audit
from ..deps import get_storage, require_session
from ..schemas import AgentItem, AgentList, AgentActionBody, AgentRoleBody
from .ops import _need_flag, _need_text, _reject, write_guard

router = APIRouter(tags=["agents"])

#: 吊销的确认串 = agent_id **前 8 位**（与 ops 的「key 末 8 位」同一道闸的口径：
#: 手输一段记认得到的前缀，错一个代理不至于按下去）。前端从列表里拿 agent_id 自行截取。
REVOKE_HINT_LEN = 8

#: 批准响应里回显的一次性提示（前端据此弹「只此一次，请立即保存」）
CREDENTIAL_ONCE_NOTE = (
    "凭据只此一次返回，库里只存 sha256 —— 关掉弹框就再也拿不到了；"
    "丢失请重新调用 approve 换一条（旧凭据随即失效）。"
)

#: 置 primary 时的回显说明：必须让操作者看见「旧主将降为 sub」这个后果。
ROLE_PRIMARY_NOTE = "全库唯一 primary：原 primary 已自动降为 sub（可在列表里核对）。"


def _registry(storage: Any) -> Registry:
    """按控制台的存储后端装配注册表（并确保 `ks_agent` 在）。

    `ensure_schema()` 对 SQL 后端就是各后端自己的 `ensure_index()`（幂等），
    对 Redis 是 no-op —— 控制台进程与主脑进程各自构造存储、各自 ensure，
    这正是既有 `get_storage()` 的口径，不另开连接、不另配 DSN。
    """
    registry = Registry(storage)
    registry.ensure_schema()
    return registry


def _need_agent(registry: Registry, agent_id: str, action: str) -> Dict[str, Any]:
    """取代理行；查不到 ⇒ 404（**先审计**再抛，与 ops 的 404 分支同纪律）。"""
    row = registry.get(agent_id)
    if not row:
        _reject(404, "agent_not_found", f"注册表里没有这个代理：{agent_id}", action, agent_id)
    return row


def _backup(action: str, rows: Any) -> str:
    """改前快照（落不进盘 ⇒ 不许改，与 `audit.precheck` 同一 fail-closed 方向）。"""
    return audit.backup_json(f"agents.{action}.{audit.stamp()}.json", rows)


@router.get("/api/v1/agents", response_model=AgentList,
            dependencies=[Depends(require_session)])
def list_agents(storage: Any = Depends(get_storage)) -> AgentList:
    """注册表全量（`items` + `pending_count`）—— 待审数量是这块面板唯一要盯的数。"""
    registry = _registry(storage)
    items: List[AgentItem] = [AgentItem(**a) for a in registry.list_agents()]
    return AgentList(items=items, pending_count=registry.pending_count(), count=len(items))


@router.post("/api/v1/agents/{agent_id}/approve",
             dependencies=[Depends(require_session), Depends(write_guard)])
def approve_agent(agent_id: str, body: AgentActionBody,
                  storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """批准并签发**一次性凭据**（`ksk_` + 32 hex）。

    明文只在这个响应里出现一次；重新调用会换一条新凭据（旧的那条随即失效）。
    🔴 审计只记「issued」，**绝不记明文**。
    """
    action = "agents.approve"
    registry = _registry(storage)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才批准')
    row = _need_agent(registry, agent_id, action)
    backup = _backup("approve", row)
    credential = registry.approve(agent_id)
    if not credential:      # 竞态：行在读与写之间消失 ⇒ 409，不谎报「已批准」
        _reject(409, "agent_race", "代理在批准过程中消失，请重试", action, agent_id)
    audit.record(action, agent_id, ok=True, reason="credential issued (one-time)")
    return {"ok": True, "agent_id": agent_id, "status": STATUS_APPROVED,
            "credential": credential, "credential_hint": credential[:8],
            "note": CREDENTIAL_ONCE_NOTE, "backup": backup}


@router.post("/api/v1/agents/{agent_id}/revoke",
             dependencies=[Depends(require_session), Depends(write_guard)])
def revoke_agent(agent_id: str, body: AgentActionBody,
                 storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """吊销（随时可做）：凭据哈希一并清掉，旧凭据**立即**作废。"""
    action = "agents.revoke"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_text(body.confirm, agent_id[:REVOKE_HINT_LEN], action, agent_id,
               f"agent_id 前 {REVOKE_HINT_LEN} 位（{agent_id[:REVOKE_HINT_LEN]}）")
    backup = _backup("revoke", row)
    registry.revoke(agent_id)
    audit.record(action, agent_id, ok=True, reason=f"backup={backup}")
    return {"ok": True, "agent_id": agent_id, "status": STATUS_REVOKED, "backup": backup}


@router.post("/api/v1/agents/{agent_id}/role",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_role(agent_id: str, body: AgentRoleBody,
                   storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """改角色。**全库唯一 primary**：置 primary 会把原 primary 自动降为 sub。

    确认串 = 完整 agent_id（这道操作改的是全库身份，不是单个代理的开关）。
    改前把**涉及的整份注册表**备份下来（连带降级的那一行也要能人工复原）。
    """
    action = "agents.role"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    role = (body.role or "").strip().lower()
    if role not in ROLES:
        _reject(400, "bad_role", f"role 只能是 {list(ROLES)}，收到 {body.role!r}",
                action, agent_id)
    _need_text(body.confirm, agent_id, action, agent_id, f"完整 agent_id（{agent_id}）")

    backup = _backup("role", registry.list_agents())
    demoted = registry.set_role(agent_id, role)
    audit.record(action, agent_id, ok=True,
                 reason=f"{row['role']} -> {role}; demoted={demoted}; backup={backup}")
    return {"ok": True, "agent_id": agent_id, "role": role, "demoted": demoted,
            "note": ROLE_PRIMARY_NOTE if role == ROLE_PRIMARY else "", "backup": backup}


@router.post("/api/v1/agents/{agent_id}/exclude",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_exclude(agent_id: str, body: AgentActionBody,
                      storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """「这个代理的条目进不进主脑上下文」开关（退役的 `retrieval.exclude_tags` 的新家）。"""
    action = "agents.exclude"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改上下文开关')
    backup = _backup("exclude", row)
    changed = registry.set_exclude(agent_id, bool(body.exclude))
    audit.record(action, agent_id, ok=True,
                 reason=f"exclude={bool(body.exclude)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": agent_id,
            "exclude_from_primary": bool(body.exclude), "changed": changed,
            "backup": backup}
