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

🔴 **现役 primary 的三道闸**（`revoke` / `role` / `exclude` 一律 409）：前端
不给那三个钮只是「不让人看见」，绕开 UI 直接打 API 一样能点进来 —— 所以同一道
闸必须在后端再挡一次，判据与理由见 `_need_not_primary`。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from keepsake.govern.registry import (
    ROLE_PRIMARY,
    ROLES,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REVOKED,
    Registry,
)

from .. import audit
from .. import config
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

#: 手工登记的 agent_id 形态：小写字母 / 数字开头，其余是字母数字或连字符，
#: 总长 2~40（`^[a-z0-9][a-z0-9-]{1,39}$`）。
#: 🔴 这个串同时是**文件系统路径与 Redis key 的一部分**，也进日志与审计的
#: target 字段 —— 放开大小写与特殊字符就等于给审计目录开口子，所以只收这一档。
AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,39}$"


class AgentRegisterBody(BaseModel):
    """`POST /api/v1/agents` 的请求体：手工登记一个代理。

    `confirm` 要 `true`（与本文件其余写端点同一道闸）；`approve_now` 为真时
    登记完**立刻批准并签发一次性凭据**（明文只在这一条响应里出现一次）。
 """

    agent_id: str = Field("", description="代理标识：小写字母/数字开头，可含连字符，2~40 位")
    name: str = Field("", description="备注名（给人看）；留空则与 agent_id 同名")
    confirm: Any = False
    approve_now: bool = False


class AgentRenameBody(BaseModel):
    """`POST /api/v1/agents/{agent_id}/rename` 的请求体：只改备注名。"""

    name: str = Field("", description="新的备注名")
    confirm: Any = False


def _valid_agent_id(agent_id: str) -> bool:
    """agent_id 是否合规（口径单点：前后端各写一份正则就会漂）。

    用 `fullmatch` 而不是 `match`：`$` 会在**尾随换行前**也匹配，
    `"abc\n"` 会被 `match` 放进来 —— 那是能进目录名的字符。
    """
    return bool(re.fullmatch(AGENT_ID_PATTERN, agent_id or ""))


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


#: 试图动现役 primary 时的回显（前端提示与 409 的 detail 共用这一句口径）。
PRIMARY_LOCKED_NOTE = (
    "现役主代理不可直接吊销/降级/排除 —— 先把另一个代理设为主代理，再回来动它。"
)


def _need_not_primary(registry: Registry, agent_id: str, action: str) -> None:
    """🔴 **现役 primary 的三道闸**（前端不给按钮，这里再挡一次 API）。

    为什么非挡不可：吊销 primary ⇒ 写闸 fail-closed 把它降成 sub ⇒ 主脑的
    常驻写入全降级成候选、候选审批链没人批；exclude 对 primary 更是没有意义
    （过滤是给**别人**看的，它自己就在上下文里）。绕过 UI 直接打 API 也一样
    绕进同一个坑，所以这层必须是后端 —— 前端只负责不让人看见那个钮。

    口径上拦的是「**现役** primary」= `role=primary` 且 `status=approved`：
    尚未批准的 primary 读写本就全拒，动它不改变任何放行状态，拦下来只会
    把「还没批准的主代理行」变成一个既不能批也不能改的死格（`approve` 端点
    本身也不受这道闸管，主代理待审照样能批 —— 见 `approve_agent`）。
    """
    row = registry.get(agent_id)
    if row and row.get("role") == ROLE_PRIMARY and row.get("status") == STATUS_APPROVED:
        _reject(409, "primary_protected", PRIMARY_LOCKED_NOTE, action, agent_id)


@router.get("/api/v1/agents", response_model=AgentList,
            dependencies=[Depends(require_session)])
def list_agents(
    q: str = Query("", description="搜索词：agent_id + name 大小写不敏感子串；为空即全量"),
    page: int = Query(1, ge=1),
    page_size: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    storage: Any = Depends(get_storage),
) -> AgentList:
    """注册表（`items` + `pending_count`）—— 待审数量是这块面板唯一要盯的数。

    服务端 `q` + 分页（口径照抄 `core.list_core`）：先在全量行上按
    **agent_id + name** 子串过滤，再切页；`count` 是**过滤后**的总数
    （前端 PagerBar 拿它算页数，不另算）。

    🔴 `pending_count` 恒为**全量口径**，不受 `q` / `page` 影响：红条上的
    「N 个待审核」必须永远等于全库待审数 —— 被搜索词吞掉的待审代理只会
    继续在闸门外被拒，却从红条上消失了，那正是这块面板最不能出的错。
    """
    registry = _registry(storage)
    rows = registry.list_agents()
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in str(r.get("agent_id", "")).lower()
                or needle in str(r.get("name", "")).lower()]
    start = (page - 1) * page_size
    return AgentList(
        items=[AgentItem(**a) for a in rows[start:start + page_size]],
        count=len(rows),
        # 🔴 全量口径：pending_count 走的是注册表原始计数，不是过滤后的 len(rows)
        pending_count=registry.pending_count(),
    )


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
    """吊销（随时可做）：凭据哈希一并清掉，旧凭据**立即**作废。

    🔴 现役 primary 除外 ⇒ 409（见 `_need_not_primary`）：吊销它等于把全库
    写闸 fail-closed 到 sub，主脑的常驻写入从此全降级、候选链瘫痪。
    """
    action = "agents.revoke"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_not_primary(registry, agent_id, action)
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

    🔴 现役 primary 除外 ⇒ 409（见 `_need_not_primary`）：直接把现役主脑降成
    sub 与吊销它是同一个自锁。**先设别人为主**（那条走本端点的另一条路径，
    它会自动把旧主降级，是设计上唯一安全的换主路径），再回来动它。
    """
    action = "agents.role"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_not_primary(registry, agent_id, action)
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
    """「这个代理的条目进不进主脑上下文」开关（退役的 `retrieval.exclude_tags` 的新家）。

    🔴 现役 primary 除外 ⇒ 409（见 `_need_not_primary`）：过滤是给**别人**看的，
    它自己每轮就在上下文里，把主代理自己排除掉没有任何意义只有自锁风险。
    """
    action = "agents.exclude"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_not_primary(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改上下文开关')
    backup = _backup("exclude", row)
    changed = registry.set_exclude(agent_id, bool(body.exclude))
    audit.record(action, agent_id, ok=True,
                 reason=f"exclude={bool(body.exclude)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": agent_id,
            "exclude_from_primary": bool(body.exclude), "changed": changed,
            "backup": backup}


@router.post("/api/v1/agents",
             dependencies=[Depends(require_session), Depends(write_guard)])
def register_agent(body: AgentRegisterBody,
                   storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """手工登记一个代理（不必等它自己连上来）。

    登记 = 建一行 `pending`：**不批准就读写全拒**。`approve_now=true` 时登记完
    紧接着批准并签发一次性凭据 —— 明文只在这一条响应里出现一次（与
    `approve_agent` 同口径：hint 前 8 位、审计只记「issued」）。

    🔴 **已存在 ⇒ 409，绝不静默覆盖**（任何状态都算，含 `revoked`）：
    `Registry.register` 本身幂等且不碰已有行，但「登记」这个动作**必须**让
    操作者看见「这个名字已经有人了」—— 静默复用会把别人的吊销决定洗白
    （改了名看起来像新代理，实际还是那条被吊销的行）。
    """
    action = "agents.register"
    registry = _registry(storage)
    agent_id = (body.agent_id or "").strip()
    # 🔴 形态先判（不碰存储）：不合规的 agent_id 会进审计目录的文件名与
    # Redis key，挡住比事后清理便宜。理由给的是人话，不是「正则不匹配」。
    if not _valid_agent_id(agent_id):
        _reject(400, "bad_agent_id",
                f"agent_id 不合规：只允许小写字母、数字与连字符，"
                f"且必须以字母或数字开头、总长 2~40 位（收到 {agent_id!r}）",
                action, agent_id)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才登记')
    if registry.get(agent_id):
        _reject(409, "agent_exists",
                f"注册表里已经有这个代理了：{agent_id}（改名用 rename，"
                f"重新批准用 approve —— 登记绝不覆盖已有行）",
                action, agent_id)
    # 登记是**新增**行：改前没有这一行可备份，所以备的是改前整份注册表
    # （人工复原时能一眼看出「多出来的是哪一行」）。
    backup = _backup("register", registry.list_agents())
    name = (body.name or "").strip() or agent_id
    registry.register(agent_id, name)
    audit.record(action, agent_id, ok=True, reason=f"registered name={name}; backup={backup}")
    if not body.approve_now:
        return {"ok": True, "agent_id": agent_id, "status": STATUS_PENDING,
                "name": name, "backup": backup}
    # 紧接着批准：与 approve 端点同一条路径（`Registry.approve` 内部换凭据 +
    # 只落 sha256），明文只在这里出现一次。
    credential = registry.approve(agent_id)
    if not credential:      # 竞态：行在登记与批准之间消失 ⇒ 409，不谎报「已批准」
        _reject(409, "agent_race", "代理在批准过程中消失，请重试", action, agent_id)
    audit.record("agents.approve", agent_id, ok=True, reason="credential issued (one-time)")
    return {"ok": True, "agent_id": agent_id, "status": STATUS_APPROVED, "name": name,
            "credential": credential, "credential_hint": credential[:8],
            "note": CREDENTIAL_ONCE_NOTE, "backup": backup}


@router.post("/api/v1/agents/{agent_id}/rename",
             dependencies=[Depends(require_session), Depends(write_guard)])
def rename_agent(agent_id: str, body: AgentRenameBody,
                  storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """改**备注名**（只改 `name` 一个字段）。

    🔴 `status` / `role` / 凭据 / `meta` **一律不碰**：改名是给人看的标签，
    不是治理动作 —— 它既不该放行一个 pending 代理，也不该作废已发的凭据，
    也不受「现役 primary 三道闸」约束（那三道闸管的是**放行与否**）。
    目标行不存在 ⇒ 404。
    """
    action = "agents.rename"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改名')
    name = (body.name or "").strip()
    if not name:
        _reject(400, "bad_name", "备注名不能为空（留空就分不清是哪一个代理了）",
                action, agent_id)
    backup = _backup("rename", row)
    changed = registry.rename(agent_id, name)
    audit.record(action, agent_id, ok=True,
                 reason=f"name={name} (changed={changed}); backup={backup}")
    after = registry.get(agent_id) or {}
    return {"ok": True, "agent_id": agent_id, "name": after.get("name", name),
            "status": after.get("status", ""), "changed": changed, "backup": backup}
