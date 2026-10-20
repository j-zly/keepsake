"""消息中心（代理互聊信箱的控制台面）+ 代理私信开关。

2026-10 ks_messages_console。信箱的存储层与 MCP 工具由前序单
（`task_ks_agent_messaging`）交付 —— 本文件只做**治理面**：

* `GET /api/v1/messages` —— 所有代理往来消息**全量可查**（用户铁律：
  绝不被背着开小会）。按代理 / 线程 / 关键词筛，服务端分页，按 seq 倒序。
* `POST /api/v1/agents/{agent_id}/dm` —— 每个代理的 `dm_enabled` 开关
  （关掉的代理收不到私信）。

🔴 **只读治理面**：两个端点都不回显任何凭据字段（明文 / 哈希都不出现）。
消息正文按原样返回 —— 那是治理面要看的东西，不是秘密。

🔴 **列表端点一个字段都不改**（尤其不动 `read_at`）：控制台里「看一眼历史」
不许把对方的信标成已读（收信即消费是收件人的动作，不是旁观者的）。
口令在 `govern/messaging.py` 的 `list_messages` 里同款纪律。

写端点（dm 开关）与 `agents.py` 其余写端点**同一套样板**：
`require_session` + `write_guard` + 改前备份 + 一次审计（含被拒绝的写）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from keepsake.govern.messaging import LIMIT_MAX, Messaging

from .. import audit
from ..deps import get_storage, require_session
from .agents import _backup, _need_agent, _registry
from .ops import _need_flag, write_guard

router = APIRouter(tags=["messages"])

#: 日志名（与 audit 同款命名：keepsake.api.<模块>）。
logger = logging.getLogger("keepsake.api.messages")

#: 每页默认条数（信箱量级小：几十条对话，一页 50 条足够看一片）。
DEFAULT_SIZE = 50


class AgentDmBody(BaseModel):
    """`POST /api/v1/agents/{agent_id}/dm` 的请求体：私信开关 + 显式二次确认。

    🔴 `enabled` 是**投递偏好**（收不收私信），不是治理动作 ——
    它不碰 `status` / `role` / 已发的凭据（后端 `Registry.set_dm_enabled` 同纪律）。
    `confirm` 要 `true`（与 `agents.py` 其余写端点同一道闸）。
    """

    enabled: bool = Field(..., description="true = 接收私信；false = 不收（别人投不进来）")
    confirm: Any = False


def _item(row: Dict[str, Any]) -> Dict[str, Any]:
    """一条消息的对外形状。

    🔴 `read_at`（时刻）在这一层压成 `read`（布尔）：控制台只要知道
    「读没读」，要查时刻去存储层 —— 治理面不堆用不上的字段。
    """
    return {
        "seq": int(row.get("seq") or 0),
        "from_id": str(row.get("from_id") or ""),
        "to_id": str(row.get("to_id") or ""),
        "content": str(row.get("content") or ""),
        "thread_id": str(row.get("thread_id") or ""),
        "created": str(row.get("created") or ""),
        "read": bool(row.get("read_at")),
    }


@router.get("/api/v1/messages", dependencies=[Depends(require_session)])
def list_messages(
    agent: str = Query("", description="只看某一个代理（发件或收件都算）；为空即全库"),
    thread: str = Query("", description="只看某一个线程（thread_id）；为空即全部"),
    q: str = Query("", description="正文关键词（大小写不敏感子串）"),
    page: int = Query(1, ge=1, description="页码，从 1 起"),
    size: int = Query(DEFAULT_SIZE, ge=1, le=LIMIT_MAX, description="每页条数（上限 200）"),
    storage: Any = Depends(get_storage),
) -> Dict[str, Any]:
    """代理往来消息（**按 seq 倒序** = 新的在前）：`{items, total}`。

    `total` 是**过滤后**的总数（前端 PagerBar 拿它算页数，不另算）。
    筛不掉任何东西时 `items` 为空数组（不是报错）。

    🔴 只读：本端点**不改任何字段**（含 `read_at`）—— 看历史不吃信。
    """
    box = Messaging(storage)
    data = box.list_messages(agent=agent, thread=thread, q=q, page=page, size=size)
    return {"items": [_item(r) for r in data.get("items") or []],
            "total": int(data.get("total") or 0)}


@router.post("/api/v1/agents/{agent_id}/dm",
             dependencies=[Depends(require_session), Depends(write_guard)])
def set_agent_dm(agent_id: str, body: AgentDmBody,
                 storage: Any = Depends(get_storage)) -> Dict[str, Any]:
    """开关某个代理的**私信接收**（`dm_enabled`）。

    关掉之后，别人往它信箱里投的消息一律拒投（投递闸门在后端，
    与 MCP 的 `message_send` 同一条规矩）；它自己发出去的照旧。

    🔴 它**不是**治理动作：`status` / `role` / 已发的凭据 / 记忆归属
    一个都不碰（`Registry.set_dm_enabled` 同纪律）。
    🔴 主代理也给这个开关：关了自己也收不到私信 —— 合法（用户明确要过）。
    未知代理 ⇒ 404。
    """
    action = "agents.dm"
    registry = _registry(storage)
    row = _need_agent(registry, agent_id, action)
    _need_flag(body.confirm, action, agent_id, '必须显式传 {"confirm": true} 才改私信开关')
    backup = _backup("dm", row)
    changed = registry.set_dm_enabled(agent_id, bool(body.enabled))
    audit.record(action, agent_id, ok=True,
                 reason=f"dm_enabled={bool(body.enabled)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": agent_id, "dm_enabled": bool(body.enabled),
            "changed": bool(changed), "backup": backup}
