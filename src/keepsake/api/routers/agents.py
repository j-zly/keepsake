"""代理注册表控制台端点：列表 / 批准 / 吊销 / 改角色 / 改上下文开关。

2026-10 keepsake_agent_registry。治理面与 `api/routers/ops.py` 同一套纪律：

* **全 require_session**（读面与写面都是）；四条写端点**再叠 `write_guard`**
  （审计落不下账的机器上这次治理变更不许做）；
* **写前备份**：改角色会连带把另一个代理降级、改批准会顶掉旧凭据 —— 这两样
  都不可逆到「回滚靠记忆」的程度，先 `audit.backup_json` 落一份改前快照；
* **显式二次确认**：approve/exclude 要 `confirm=true`，role 要手输整个 agent_id，
  revoke 要手输 agent_id 前 8 位（与 ops 归档/真删同一道闸），
  revoke_credential 要 `confirm=true`，delete 要手输**整个** agent_id（最高一档）；
* **一次审计**：被拒绝的写也记一行 `ok=false`（`_reject`）。

🔴 **凭据纪律**：明文只在 `approve` 的响应体里出现**一次**，之后库里只有 sha256。
审计 / 备份 / 错误消息里都不许出现明文 —— 本文件没有一处把它写进 `audit.record`
的 `reason`（那里只写「issued」这类与凭据无关的字样）。

🔴 **现役 primary 的三道闸**（`revoke` / `role` / `exclude` 一律 409）：前端
不给那三个钮只是「不让人看见」，绕开 UI 直接打 API 一样能点进来 —— 所以同一道
闸必须在后端再挡一次，判据与理由见 `_need_not_primary`。

🔴 **三级语义**（三档后果不同、确认闸也不同高，前端要分开说）：
* `revoke` = **停权限留记录**（`status=revoked`，读写全拒）；
* `revoke_credential` = **只废那把钥匙**（行留在册、状态与角色一个都不动）；
* `delete` = **整行抹掉**（删前备份一份，可人工复原；**primary 拒删** 400）。

🔴 **记忆共享开关**（`share`，2026-10 ks_share_memory）：`POST .../share` 只改
**读可见性**（这个代理的记忆进不进别人的检索范围），不碰状态 / 角色 / 凭据 ——
与私信开关（`dm`）同一档，不是治理动作。

🔴 **per-agent 注入预算**（`budget`，2026-10 ks_agent_budget）：
`POST .../budget` 只改这一个阈值（`core_budget_chars`），NULL = 沿用全局默认。
同样不是治理动作 —— 状态 / 角色 / 凭据一个都不碰。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

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

#: 日志名（与 audit 同款命名：keepsake.api.<模块>）。
logger = logging.getLogger("keepsake.api.agents")

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


class AgentSetIdBody(BaseModel):
    """`POST /api/v1/agents/{agent_id}/set_id` 的请求体：改**代理 id**本身。

    `new_id` 走与登记同一套形态闸（`_valid_agent_id`）；`confirm` 是显式二次确认
    （与本文件其余写端点同一道闸）。
    """

    new_id: str = Field("", description="新 agent_id：小写字母/数字开头，可含连字符，2~40 位")
    confirm: Any = False


class AgentDeleteBody(BaseModel):
    """`DELETE /api/v1/agents/{agent_id}`（及 POST 别名）的请求体：整行删除。

    🔴 `confirm` 必须是**完整 agent_id 字符串**（不是 `true`）—— 这道闸比
    吊销（手输前 8 位）高一档：吊销只作废一把钥匙，删除是整行抹掉。
    """

    confirm: Any = ""


class AgentShareBody(BaseModel):
    """`POST /api/v1/agents/{agent_id}/share` 的请求体：记忆共享开关 + 显式确认。

    🔴 `enabled` 是**读可见性偏好**（共不共享记忆），不是治理动作 ——
    它不碰 `status` / `role` / 已发的凭据（与私信开关 `dm` 同一档）。
    `confirm` 要 `true`（与本文件其余写端点同一道闸）。
    """

    enabled: bool = Field(..., description="true = 共享记忆给其他已批准代理；false = 不共享")
    confirm: Any = False


class AgentBudgetBody(BaseModel):
    """`POST /api/v1/agents/{agent_id}/budget` 的请求体：per-agent 注入预算。

    🔴 `core_budget_chars` 声明成 `Any`（不是 `int`）：pydantic 的类型错误会变成
    **422**，而本模块的写端点一律用 **400** 报「为什么不行」（前端红条只认这个
    口径）。所以这里只收形状，值域判读放在路由里做（`_budget_value`）。

    * `null` / 空串 ⇒ **清空**（回到 NULL = 沿用全局默认）；
    * 正整数 ⇒ 该代理**专用**的注入字符预算；
    * 0 / 负数 / 非整数 ⇒ **400**（拒，不静默当默认值）。
    """

    core_budget_chars: Any = Field(
        None, description="正整数 = 该代理专用预算；null / 空 = 沿用全局默认")
    confirm: Any = False


#: per-agent 注入预算的取值区间。
#: 🔴 **下限只到 1**（不是全局那个 1000）：per-agent 的语义是「这个代理只吃
#: 这么多」，小到 100 字符也是人明确要的（只想让它记住一两句话）。
#: 上限 500000 与全局同档：再大就是「不设预算」。
BUDGET_MIN = 1
BUDGET_MAX = 500000


def _budget_value(raw: Any, action: str, target: str) -> Optional[int]:
    """把请求体里的预算值判读成 `int` 或 `None`（None = 清空 ⇒ 沿用全局）。

    🔴 **非法值一律 400，绝不静默当默认值**：0 / 负数 / 非整数 / 布尔都拒 ——
    静默回落全局的话，操作者以为改成了，其实没改（这正是这套治理要拔掉的
    「以为改成了」）。空串与 null 一样 = **清空**（前端清空输入框就是这个意思）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):           # bool 是 int 的子类 ⇒ 先单独拒
        _reject(400, "bad_core_budget",
                f"注入预算必须是正整数（收到 {raw!r}）", action, target)
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None                   # 空串 = 清空
        if not text.lstrip("-").isdigit():
            _reject(400, "bad_core_budget",
                    f"注入预算必须是正整数（收到 {raw!r}）", action, target)
        value = int(text)
    else:
        _reject(400, "bad_core_budget",
                f"注入预算必须是正整数（收到 {raw!r}）", action, target)
    if not (BUDGET_MIN <= value <= BUDGET_MAX):
        _reject(400, "core_budget_out_of_range",
                f"注入预算必须在 {BUDGET_MIN}~{BUDGET_MAX} 字符之间，本次 {value}",
                action, target)
    return value


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

#: 改**主代理 id** 时的回显（400 的 detail 共用这一句口径）。
#: 🔴 与上面那句不是一回事：吊销/降级/排除是「对 primary 这个角色的限制」，
#: 改 id 是「**身份本身**不能换」—— 主脑的引导配置（`agent_id`）就写死在
#: 客户端侧，改了 id 等于让主脑拿着旧 id 上门，直接被写闸拒在外面。
PRIMARY_ID_LOCKED_NOTE = (
    "现役主代理的 id 不能改 —— 主脑的引导配置就绑在这个 id 上，改了等于换了个主代理。"
    "先把另一个代理设为主代理，再回来改这个 id。"
)

#: 新 id 已被占用时的回显（409 的 detail 共用这一句口径）：**点名**是谁占了，
#: 并给出下一步（换一个 / 先去列表里找它）。
AGENT_EXISTS_NOTE = (
    "已经有代理叫 {new_id} 了 —— 换一个别的 id，或者先去列表里找找它是不是已经改过名了。"
)

#: 删除 primary 时的回显（400 的 detail 共用这一句口径）。
#: 🔴 与 `PRIMARY_LOCKED_NOTE`（吊销/降级/排除）不是一回事：那三条是「别动
#: 现役主代理的权限」，这一条是「主代理这一行不许删」—— 删掉它 = 全库没有
#: 主脑可写了，换主的唯一安全路径仍是「先把另一个设成 primary」。
PRIMARY_DELETE_NOTE = (
    "主代理不可删除 —— 删掉它全库就没有主脑可写了；"
    "先把另一个代理设为主代理，再回来删这个。"
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


@router.post("/api/v1/agents/{agent_id}/revoke_credential",
             dependencies=[Depends(require_session), Depends(write_guard)])
def revoke_credential(agent_id: str, body: AgentActionBody,
                      storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """**只停凭据**（停钥匙）：清掉凭据哈希与提示，代理行**留在册**。

    🔴 三级语义里最轻的一档 —— 与 `revoke` 的分界是**状态**：
      * `revoke` = **停权限留记录**：`status` 置 revoked，代理读写全拒，
        列表里留一条「已吊销」；
      * `revoke_credential` = **只废那把钥匙**：`status` / `role` / `name` /
        `meta` 一个都不动（还是 approved、还在册），只是它手里那条凭据
        当场作废 —— 泄露了、想先止血又不想把代理从册子上抹掉时用这个。

    🔴 **没有凭据可停 ⇒ 报 `ok=false` + 人话**（不是 500）：「本来就没有」
    是正常状态（没批准过 / 已经停过了），不是故障 —— 也不该被当成成功。
    🔴 审计里**不记任何凭据内容**（连 hint 都不记，只记动作本身）。
    """
    action = "agents.revoke_credential"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才停凭据')
    if not row.get("credential_hint"):
        # 本来就没有凭据可停（没批准过 / 已经停过）⇒ 幂等地报「没东西可停」，
        # 留一行 ok=false 的痕（没生效的写也要留痕）。
        audit.record(action, agent_id, ok=False, reason="no_credential")
        return {"ok": False, "agent_id": agent_id, "cleared": False,
                "note": "这个代理现在没有凭据可停（没批准过，或已经停过了）"}
    backup = _backup("revoke_credential", row)
    if not registry.clear_credential(agent_id):
        # 竞态：行在读与写之间消失 ⇒ 409，不谎报「已停凭据」
        _reject(409, "agent_race", "代理在停凭据的过程中消失，请重试", action, agent_id)
    audit.record(action, agent_id, ok=True, reason="credential cleared")
    return {"ok": True, "agent_id": agent_id, "cleared": True,
            "status": row.get("status", ""), "backup": backup,
            "note": "凭据已作废（代理行仍在册，状态与角色一个都没动）"}


@router.delete("/api/v1/agents/{agent_id}",
               dependencies=[Depends(require_session), Depends(write_guard)])
@router.post("/api/v1/agents/{agent_id}/delete",
             dependencies=[Depends(require_session), Depends(write_guard)])
def delete_agent(agent_id: str, body: AgentDeleteBody,
                 storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """**整行删除**：这一行从注册表里消失（凭据哈希一起没）。

    🔴 三级语义里最重的一档：`revoke` 停权限留记录、`revoke_credential`
    只废钥匙，删除是**整行抹掉**（删前备份一份，误删可人工复原）。

    🔴 **确认串 = 完整 agent_id**（比吊销的手输前 8 位高一档）：删的是一整行，
    打错一个字符就不该生效；传 `true` 一律 400。
    🔴 **primary 拒删**（400）：删掉主代理 = 全库没有主脑可写了（见
    `PRIMARY_DELETE_NOTE`）。未知代理 ⇒ 404。
    """
    action = "agents.delete"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    if row.get("role") == ROLE_PRIMARY:
        _reject(400, "primary_not_deletable", PRIMARY_DELETE_NOTE, action, agent_id)
    _need_text(body.confirm, agent_id, action, agent_id, f"完整 agent_id（{agent_id}）")
    # 删前备份**这一行**（人工复原的依据：备份目录里那份 JSON 就是原行）。
    backup = _backup("delete", row)
    if not registry.delete(agent_id):
        # 权威判定在 Registry.delete 里（并发 / 角色已变）⇒ 这里只补区分文案。
        _reject(409, "agent_race", "代理在删除过程中消失或角色已变，请重试", action, agent_id)
    audit.record(action, agent_id, ok=True, reason=f"deleted=1; backup={backup}")
    return {"ok": True, "agent_id": agent_id, "deleted": True,
            "row": row, "backup": backup}


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


@router.post("/api/v1/agents/{agent_id}/share",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_share(agent_id: str, body: AgentShareBody,
                    storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """开关「这个代理的记忆**共享不共享**给别的已批准代理」（`share_memory`）。

    🔴 **只影响读可见性**：写路径不过滤（谁的凭据写谁的），写闸 / 分层 / 私信
    一个都不动（那是别的语义）。
    🔴 **不是治理动作**：`status` / `role` / 已发的凭据 / 记忆归属一个都不碰
    （`Registry.set_share_memory` 同纪律）—— 与私信开关（`dm`）同一档。
    🔴 未知代理 ⇒ 404；没带 `confirm` ⇒ 400 且**不改**。
    """
    action = "agents.share"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改共享开关')
    backup = _backup("share", row)
    changed = registry.set_share_memory(agent_id, bool(body.enabled))
    audit.record(action, agent_id, ok=True,
                 reason=f"share_memory={bool(body.enabled)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": agent_id, "share_memory": bool(body.enabled),
            "changed": bool(changed), "backup": backup}


@router.post("/api/v1/agents/{agent_id}/budget",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_budget(agent_id: str, body: AgentBudgetBody,
                     storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """设 / 清这个代理**自己**的常驻层注入字符预算（`core_budget_chars`）。

    🔴 **不是治理动作**：它只改一个阈值 —— `status` / `role` / 已发的凭据 /
    记忆归属一个都不碰（与 `share` / `dm` 同一档）。
    🔴 **NULL = 沿用全局默认**（`retrieval.core_char_budget`）：清空输入框就是
    回到全局，不是「预算 0」。
    🔴 非法值（0 / 负数 / 非整数）⇒ **400 且不改**（`_budget_value`）。
    🔴 **改完立刻生效**：读路径不缓存这一列（`Registry.core_budget_chars` 每次
    读一次库），不存在「改了要等一会儿」。
    未知代理 ⇒ 404；没带 `confirm` ⇒ 400 且**不改**。
    """
    action = "agents.budget"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改注入预算')
    value = _budget_value(body.core_budget_chars, action, agent_id)
    backup = _backup("budget", row)
    changed = registry.set_core_budget_chars(agent_id, value)
    audit.record(action, agent_id, ok=True,
                 reason=f"core_budget_chars={value} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": agent_id, "core_budget_chars": value,
            "changed": bool(changed), "backup": backup}


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


@router.post("/api/v1/agents/{agent_id}/set_id",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_id(agent_id: str, body: AgentSetIdBody,
                 storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """改**代理 id 本身**（换主键 + 归属级联）。

    改 id 不作废凭据：客户端身份由凭据反查（`verify_any`），凭据不动 ⇒
    已发出去的凭据继续有效，**http 客户端不需要重配**。但记忆归属要跟着搬：
    碎片的归属是 `tags` 里的 `agent:<id>` 那个词，搬完还得重算 BM25 索引
    （`reassign_agent`），否则库里写着新归属、检索还按旧标签召回。

    🔴 **顺序**（每一步都有明确的人话理由，不让人猜是哪一条挡的）：
      格式校验 → 旧行存在(404) → 现役 primary 拒改(400) → 新 id 已被占用(409)
      → 显式确认 → 备份整份注册表 → 换主键 → 归属级联。
    唯一性的**最终判定在 `Registry.set_id` 里**（路由层的预检只算体验）。
    """
    action = "agents.set_id"
    registry = _registry(storage)
    old_id = (agent_id or "").strip()
    new_id = (body.new_id or "").strip()
    # 🔴 形态先判（不碰存储）：新 id 会进审计目录的文件名与 Redis key。
    if not _valid_agent_id(new_id):
        _reject(400, "bad_agent_id",
                f"新 agent_id 不合规：只允许小写字母、数字与连字符，"
                f"且必须以字母或数字开头、总长 2~40 位（收到 {new_id!r}）",
                action, old_id)
    if new_id == old_id:
        _reject(400, "same_agent_id",
                "新 id 与当前 id 一样 —— 什么也没改，换一个吧", action, old_id)
    _need_agent(registry, old_id, action)
    row = registry.get(old_id) or {}
    if row.get("role") == ROLE_PRIMARY:
        _reject(400, "primary_id_locked", PRIMARY_ID_LOCKED_NOTE, action, old_id)
    if registry.get(new_id):
        _reject(409, "agent_exists", AGENT_EXISTS_NOTE.format(new_id=new_id),
                action, old_id)
    _need_flag(body.confirm, action, old_id, '必须显式传 {"confirm": true} 才改 id')
    # 改前备份**整份注册表**：换的是主键，快照要能一眼看出「谁换成了谁」。
    backup = _backup("set_id", registry.list_agents())
    if not registry.set_id(old_id, new_id):
        # 权威判定在 Registry.set_id 里（并发下它才是真的）⇒ 这里只补区分文案。
        if registry.get(new_id):
            _reject(409, "agent_exists", AGENT_EXISTS_NOTE.format(new_id=new_id),
                    action, old_id)
        if not registry.get(old_id):
            _reject(404, "agent_not_found", f"注册表里没有这个代理：{old_id}",
                    action, old_id)
        _reject(409, "agent_race", "换 id 时撞上并发改动，请重试", action, old_id)
    # 归属级联：碎片的 `agent:<old>` 词换成 `agent:<new>`，并重算检索索引。
    # 🔴 换 id **已经生效**（主键已换、不可回滚），所以级联失败**不能把它说成失败**：
    # 报 500 会让操作者以为没改、于是重试（重试只会拿到「代理不存在」，更迷惑）。
    # 正确姿态：换 id 成功就 200，把没搬动的那部分**说清楚**（note）。
    note = ""
    try:
        moved = int(storage.reassign_agent(old_id, new_id) or 0)
    except Exception as e:      # noqa: BLE001 —— 换 id 已生效，不许把它说成失败
        logger.warning("keepsake: 代理 %s → %s 换 id 成功，但记忆归属没搬动（%s: %s）",
                       old_id, new_id, type(e).__name__, e)
        moved = 0
        note = ("代理 id 已改成功，但历史记忆的归属没能一起搬（存储层报错）"
                "—— 请重跑一次归属搬迁。")
    audit.record(action, old_id, ok=True,
                 reason=f"agent_id {old_id} -> {new_id}; moved={moved}; backup={backup}")
    return {"ok": True, "agent_id": new_id, "moved_memories": moved,
            "backup": backup, "note": note}


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
    # 🔴 唯一性反馈：备注名**只是给人看的标签，不拦**（拦了就是改治理语义），
    # 但同名要在回包里**点名** —— 否则列表里两个一样的名字，操作者根本分不清
    # 自己刚改的是哪一个。身份唯一性由 `agent_id` 兜底（改名端点不碰它）。
    twins = [a["agent_id"] for a in registry.list_agents()
             if a["agent_id"] != agent_id and a.get("name") == name]
    note = ""
    if twins:
        note = (f"另一个代理的备注名也叫「{name}」（{', '.join(twins)}）—— "
                f"备注名只是给人看的，不影响身份；要分清就改其中一个。")
    return {"ok": True, "agent_id": agent_id, "name": after.get("name", name),
            "status": after.get("status", ""), "changed": changed, "backup": backup,
            "note": note}
