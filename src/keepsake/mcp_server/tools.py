"""MCP 工具面 —— 8 个记忆 tool + 3 个信箱 tool + 6 个治理 tool + 4 个知识库 tool 的实现。

**取数只经 `MemoryProvider` 协议**：数据出口只有
`provider.search/get/add/remove/merge/forget/capabilities/system_prompt_block`。

本模块不 import 任何宿主的东西（`agent` / `hermes` 一律不许出现），
也不自建存储 —— 治理面那 5 个 `agent_*` 工具只经**同一个 `Registry` 实例**
（`gate.registry`，即 `provider._registry`）与控制台共用一套方法，不另开连线。
import 的 `..api.audit` / `..govern.registry` / `..kb.service` 是 keepsake 自家模块
（纯文件 / 纯常量与后端适配），与「宿主」无关：`audit.py` 只 import 标准库，
`kb/service.py` **FastAPI-free**（控制台的 fastapi 是可选依赖，MCP 那边不一定装）。

--------------------------------------------------------------------------
写语义（与 P2 控制台一致，这几条是硬约束不是建议）
--------------------------------------------------------------------------
🔴 `dry_run` 是一等公民：`merge` / `forget` 默认 `dry_run=True`，只统计不写。
🔴 删除必须 `confirm=True`，否则**抛 ToolError 拒绝**，绝不静默返回 ok。
🔴 merge/forget 实跑必须回填 `confirm` = 当前 core 条数 / 当前记忆条目总数
   （字符串比较）—— 防「以为自己在跑 A 库」与「以为自己在跑 dry-run」。
🔴 失败抛 `ToolError`（结构化错误），**绝不静默 ok**。
🔴 **归属章 = 凭据反查出的调用方**（2026-10 ks_owner_stamp）：`memory_add` 落库的
   `agent:<id>` 是**这次调用方**，不是 provider 进程的身份（否则它写完自己搜不到，
   写的还全混进主脑库）；调用方自报的 `agent:xxx` 一律被覆写（**不许自报归属**），
   本机直写（caller 空）仍落进程默认身份。
🔴 **删除按归属收窄**（2026-10 ks_perm_scope）：子代理只能删**自己的**条目
   （无归属的老条目也行）；`memory_forget` 是**批处理治理面** ⇒ **仅主代理可用**。

--------------------------------------------------------------------------
知识库工具面（kb_store / kb_search / kb_get / kb_remove）
--------------------------------------------------------------------------
知识库原来只对控制台开放，现在对**所有代理**开放，治理口径与记忆**同一把尺子**
（`govern.visibility`）：

* **归属**：每篇文档记**存它的那个代理**（`documents.agent_id`，凭据反查的调用方）；
* **可见性**：非 primary 只看得见**自己名下 + 已共享代理名下 + 无主老文档**
  （无主 = 收紧前的公共数据，历史文档全是）；primary 全量；
* **删除**：非 primary 只能删**自己名下**的（或无主老文档）；**看得见 ≠ 能删**；
  拒时只说「不是你的」，**零内容回显**。

🔴 四工具**全走 `_authorize`**（凭据闸）：未批准 / 凭据无效 ⇒ 全部拒绝，
与记忆工具同一道闸（fail-closed 一致）。

--------------------------------------------------------------------------
可选扩展：core 层枚举
--------------------------------------------------------------------------
`MemoryProvider` 契约里没有「列 core 层」与「数记忆条目总数」这两个原语
（它是给注入/检索/写设计的，不该为某一个宿主加成员 —— 加了会连带把
`tests/contract/test_provider_protocol.py` 的契约清单改掉）。
所以这里约定一个**可选**扩展 `StorageIntrospection`：provider 有就提供，
没有就显式报「未实现」，而不是返回空列表装成功。

    class StorageIntrospection(Protocol):
        def list_core(self) -> list[dict]: ...     # [{key, content, tags, updated_at}]
        def count_fragments(self) -> int: ...
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from mcp.server.fastmcp.exceptions import ToolError

from ..api import audit
from ..core import screening
# rev 默认版本号（2026-10 ks_memory_rev_cas）—— 存储层缺列时兜底成 '1'
from ..core.base import BackendUnsupportedError
from ..core.shared import DEFAULT_REV
from ..govern.messaging import CONTENT_MAX as MESSAGE_CONTENT_MAX, Messaging
# 待办（2026-10 ks_todo）：存储层 + 口径单点（到期判定、可见性都在那边）。
from ..govern.todo import (
    DEFAULT_LIMIT as TODO_DEFAULT_LIMIT,
    SCOPE_ALL,
    SCOPE_DONE,
    SCOPE_OPEN,
    SCOPES,
    STATUS_DONE,
    STATUS_OBSOLETE,
    STATUS_OPEN,
    TodoError,
    TodoStore,
)
from ..govern.registry import (
    ALREADY_APPROVED_NOTE,
    AlreadyApprovedError,
    ROLE_PRIMARY,
    ROLES,
    STATUS_APPROVED,
    STATUS_REVOKED,
)
# core 层的两把尺子（归属可见性 + 注入预算）—— 与注入路径同一份口径。
from ..govern.visibility import (
    can_remove_fragment,
    fragment_owner,
    truncate_core_fragments,
)
# 知识库的数据层与代理面服务（**FastAPI-free**：控制台的 fastapi 是可选依赖，
# MCP 那边不一定装 —— 这一层不许 import fastapi）。
from ..kb import service as kb_service
from ..kb.service import KbError

logger = logging.getLogger("keepsake.mcp_server.tools")

__all__ = ["ToolError", "StorageIntrospection", "register_tools",
           "SEARCH_PREVIEW_CHARS", "CORE_PREVIEW_CHARS",
           "GOVERN_TOOLS", "GOVERN_DENIED", "MESSAGE_TOOLS", "KB_TOOLS",
           "TODO_TOOLS"]

#: `memory_search` 结果里单条正文的截断长度（全文走 `memory_get` / resource）
SEARCH_PREVIEW_CHARS = 300
#: `memory_core_list` 默认的预览长度
CORE_PREVIEW_CHARS = 200
#: confirm 要比对的那个整数的取数上限（provider 侧 scan 有上限，超了会显式报错）
_FRAGMENT_CONFIRM_CAP = 100_000


@runtime_checkable
class StorageIntrospection(Protocol):
    """`MemoryProvider` 之外的**可选**扩展，见模块 docstring。"""

    def list_core(self) -> List[Dict[str, Any]]:
        """列出常驻层（core tag）全部条目。"""
        ...

    def count_fragments(self) -> int:
        """当前记忆条目总数 —— `memory_forget` 的 confirm 凭据。"""
        ...


# --------------------------------------------------------------------------
# 取数辅助：拿不到扩展就显式报错，绝不返回空冒充「没有」
# --------------------------------------------------------------------------

def _introspect(provider: Any) -> StorageIntrospection:
    if not callable(getattr(provider, "list_core", None)) \
            or not callable(getattr(provider, "count_fragments", None)):
        raise ToolError(
            "provider 未实现可选扩展 StorageIntrospection"
            "（list_core / count_fragments）—— core 层与维护类工具对它不可用"
        )
    return provider


def _list_core(provider: Any, caller_agent_id: str = "",
               caller_role: str = "") -> List[Dict[str, Any]]:
    """列 core 层条目（带调用方可见性 —— provider 支持才传身份参数）。"""
    lister = _introspect(provider).list_core
    return list(lister(**_caller_kwargs(lister, caller_agent_id, caller_role)))


def _core_budget_of(provider: Any, caller_agent_id: str = "") -> Optional[int]:
    """本次读取方的**注入字符预算**（**可选**扩展：provider 有就提供，没有 ⇒ 不截）。

    🔴 这是 keepsake 自己的可选扩展（不是 `MemoryProvider` 契约的一部分 ——
    别的记忆系统 provider 没有它 ⇒ 照常列，不猜、不 try/except 吞异常）。
    provider 侧的口径见 `adapters/hermes/provider.py::core_budget_for`：
    调用方在注册表里配了自己的 `core_budget_chars` ⇒ 用它的；没配 ⇒ 全局。
    """
    fn = getattr(provider, "core_budget_for", None)
    if not callable(fn):
        return None
    try:
        return int(fn(caller_agent_id or ""))
    except Exception as e:      # noqa: BLE001 —— 读不到预算就不截（照旧全量列出）
        logger.warning("keepsake MCP: 注入预算读取失败（不截断）: %s: %s",
                       type(e).__name__, e)
        return None


def _count_fragments(provider: Any) -> int:
    n = int(_introspect(provider).count_fragments())
    if n >= _FRAGMENT_CONFIRM_CAP:
        raise ToolError(
            f"记忆条目数已达取数上限 {_FRAGMENT_CONFIRM_CAP}，confirm 不可信 —— 拒绝实跑"
        )
    return n


def _preview(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _hit_key(hit: Dict[str, Any]) -> str:
    """检索命中的 key 落在 `key` 或检索期临时字段 `_key`（后端两种都有）。"""
    return str(hit.get("key") or hit.get("_key") or "")


def _caller_kwargs(fn: Any, caller_agent_id: str, caller_role: str) -> Dict[str, Any]:
    """调用方身份参数（2026-10 ks_share_memory）—— **provider 支持才带**。

    `MemoryProvider` 契约里没有 `caller_agent_id` / `caller_role` 这两个形参
    （它们是 keepsake 自己的可见性扩展），所以先看签名再决定带不带：
    别的记忆系统 provider 没这两个参数 ⇒ 照常调，不猜、不 try/except 吞异常。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):       # 签名取不到（内置/C 扩展）⇒ 不带
        return {}
    if "caller_agent_id" not in params:
        return {}
    return {"caller_agent_id": caller_agent_id or "", "caller_role": caller_role or ""}


# --------------------------------------------------------------------------
# 未读私信提醒（2026-10 ks_dm_hint）：**寄生在检索响应里**（零定时器方案）
#
# 私信是拉取式的 —— 接收方无感知。定案：每轮必然发生的检索调用顺路带回未读数，
# 宿主把这一行渲染成提醒。计数口径与插件侧**同一份**（`Messaging.unread_summary`），
# 两处各写一条 SQL 必然漂（漂了就是「面板说 3 条、注入说 1 条」）。
# --------------------------------------------------------------------------

#: provider 侧自带的信箱句柄属性名（宿主 provider 懒创建的那一份）。
#: 本模块**不 import 任何宿主**（见模块 docstring），所以只按属性名取。
_PROVIDER_BOX_ATTR = "_keepsake_messaging"

#: provider 上挂待办存储的属性名（没挂就按注册表的 storage 现造一份）。
_PROVIDER_TODO_ATTR = "_keepsake_todo"


def _unread_dm(box: Any, agent_id: str) -> int:
    """调用方的未读私信条数 —— **fail-open**（读不到 ⇒ 0）。

    🔴 提醒是**附加值**：信箱表读失败（PG 抖动 / schema 未就绪）时，
    检索结果必须**照常返回** —— 一个计数查询不该有能力让检索瘫掉。
    """
    if box is None or not (agent_id or "").strip():
        return 0
    try:
        return max(0, int(box.unread_count(agent_id)))
    except Exception as e:      # noqa: BLE001 —— 读不到就不提醒，不影响检索本体
        logger.warning("keepsake MCP: 未读私信计数失败（不影响检索本体）: %s: %s",
                       type(e).__name__, e)
        return 0


# --------------------------------------------------------------------------
# 检索结果 LLM 筛选（2026-10 recall_screening）
#
# 🔴 **只挂在检索调用上**：`memory_search(screen=True)` / `kb_search(screen=True)`。
# 常驻注入路径（prefetch / provider）**一律不接** —— 筛选一次 ~10s，
# 挂在自动路径上会把每轮注入都拖慢；常驻层也不参与筛选（prompt cache 前缀纪律）。
# 🔴 **默认不筛 ⇒ 对现有所有调用方零行为变化**（本单第一红线）。
#    （2026-10 ks_screening_default_on 起：`screen` 的缺省值是**哨兵 None**
#    ——「调用方没传」与「调用方显式传了 false」必须分得开：
#    没传 ⇒ 看 `screening.default_on`；显式传了 ⇒ 照传，调用方意志优先。）
# 🔴 **fail-open**：筛选坏了就把原始结果照返，检索本体绝不受影响。
# --------------------------------------------------------------------------

def _screen_flag(screen: Optional[bool]) -> bool:
    """`screen` 参数的哨兵解析：**None = 调用方没传** ⇒ 看 `screening.default_on`。

    * `screen=True/False`（显式传了）⇒ **照传**（调用方意志优先）；
    * `screen=None` ⇒ `screening.default_on` 且总开关开着 ⇒ True，否则 False。

    🔴 总开关（`screening.enabled`）关着时一律 False —— `default_on` 只是
    「没传时怎么办」的偏好，不是绕过总开关的后门。
    """
    if screen is not None:
        return bool(screen)
    return screening.screen_default_on(screening.load_cfg())


def _screen_rows(query: str, rows: List[Dict[str, Any]], *,
                 text_of: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """对工具结果做 LLM 候选筛选 → `(新列表, screened 元数据)`。

    * 关着（`screening.enabled=false`）⇒ 原列表 + `{"skipped": "disabled"}`；
    * 结果数 ≤ `max_pick` ⇒ 没必要筛 ⇒ 原列表 + `{"skipped": "not_needed"}`；
    * 筛成功 ⇒ 按选中编号**保序**过滤 + `{"picked": N, "of": M}`；
    * 筛失败（无 key / 429 / 超时 / 坏 JSON / 越界 id）⇒ 原列表照返 +
      `{"skipped": "<原因>"}`（原因可辨识，便于排查）。
    """
    cfg = screening.load_cfg()
    if not screening.screen_enabled(cfg):
        return rows, {"skipped": "disabled"}
    try:
        max_pick = int(cfg.get("max_pick") or 0)
    except (TypeError, ValueError):
        max_pick = int(screening.DEFAULTS["max_pick"])
    if len(rows) <= max_pick:
        return rows, {"skipped": "not_needed"}
    candidates = [{"content": text_of(r)} for r in rows]
    ids, note = screening.pick_relevant(query, candidates, cfg)
    if not ids:
        reason = note.split(":", 1)[-1] if note.startswith("skipped:") else note
        return rows, {"skipped": reason}
    keep = set(ids)
    picked = [r for i, r in enumerate(rows, 1) if i in keep]
    return picked, {"picked": len(picked), "of": len(rows)}


# --------------------------------------------------------------------------
# 8 个 tool 的实现（纯函数，provider 显式传参 —— 不靠闭包，方便单测直接调）
# --------------------------------------------------------------------------

#: 子代理删**别人**条目时的拒绝文案（🔴 **不回显任何正文**，只说「不是你的」）。
REMOVE_NOT_OWNER = "这条记忆不是你的 —— 子代理只能删自己的记忆"

#: 子代理改**别人**条目时的拒绝文案（🔴 **不回显任何正文**）。
UPDATE_NOT_OWNER = "这条记忆不是你的 —— 子代理只能改自己的记忆"

#: 子代理调 `memory_forget`（批处理治理面）时的拒绝文案。
FORGET_DENIED = ("治理动作仅主代理可用：memory_forget 是批处理治理面"
                 "（按时间衰减一次删一批），只有主代理能跑")


def _is_primary_caller(caller_agent_id: str, caller_role: str) -> bool:
    """这个调用方是不是 primary。

    🔴 **判不出身份 ⇒ 当本机直连**（= 主脑现状逐字不变）：MCP 侧的身份永远
    来自闸门（凭据反查），非 primary 的调用方一定带 agent_id + role ⇒ 走不到
    这条兜底。
    """
    if not str(caller_agent_id or "").strip():
        return True
    return str(caller_role or "").strip() == ROLE_PRIMARY


def _can_remove(provider: Any, key: str, caller_agent_id: str,
                caller_role: str) -> bool:
    """这个调用方能不能删这条 —— **按归属收窄**（2026-10 ks_perm_scope）。

    🔴 判据（谁能删谁）在 `govern.visibility.can_remove_fragment` **唯一一份**：
    非 primary 只能删**自己的**条目（`owner == caller`）或**无归属的老条目**
    （收紧前的公共数据，与检索口径一致）；别人的条目 —— **含已共享代理的**
    —— 一律不许（「看得见」≠「能删」）。

    🔴 **用 `provider.get` 取条目**（通用契约里的原语）：它已经按**同一份
    可见性口径**过滤过了 ⇒ 非 primary **看不见**的条目（别人的 / 不存在的）
    一律不许删（fail-closed：判不出归属就不许删）。
    """
    if _is_primary_caller(caller_agent_id, caller_role):
        return True                     # primary 全量可删（= 主脑现状逐字不变）
    try:
        frag = provider.get(
            key, **_caller_kwargs(provider.get, caller_agent_id, caller_role))
    except Exception as e:      # noqa: BLE001 —— 归属判不出来就不许删
        raise ToolError(
            f"删除失败：读不到这条记忆的归属（{type(e).__name__}）—— 拒绝实删，请稍后重试"
        ) from e
    if not frag:
        return False                    # 看不见（别人的 / 不存在）⇒ 不许删
    owner = fragment_owner(frag.get("tags", ""))
    return can_remove_fragment(owner, caller_agent_id, is_primary=False)


def memory_capabilities(provider: Any) -> Dict[str, Any]:
    """能力声明：调用方据此决定能不能问后端要某项能力，而不是 try/except 猜。"""
    caps = provider.capabilities()
    # dataclass → dict（Capabilities 是 frozen dataclass，asdict 逐字段读，不改它）
    return {
        "version": str(getattr(caps, "version", "")),
        "core_tier": bool(getattr(caps, "core_tier", False)),
        "knn": bool(getattr(caps, "knn", False)),
        "dry_run_merge": bool(getattr(caps, "dry_run_merge", False)),
        "core_listing": hasattr(provider, "list_core"),
        # 🔴 归属语义（2026-10 ks_owner_stamp）：写入方身份 = **凭据反查**的调用方
        #    （`caller_credential`）—— 写的记忆归自己，不是 provider 进程的身份。
        "write_owner": "caller_credential",
        # 🆕 知识库工具面（kb_store / kb_search / kb_get / kb_remove）：
        #    声明这一项 = 承诺「知识库对代理开放，且归属 / 可见性 / 删除与记忆
        #    同一把尺子」（`govern.visibility`）。没装知识库连接串时调用会明确
        #    报错，不会静默装成功。
        "kb": True,
        "kb_tools": list(KB_TOOLS),
    }


def memory_search(provider: Any, query: str, limit: int = 10,
                 caller_agent_id: str = "", caller_role: str = "",
                 box: Any = None, screen: Optional[bool] = None) -> Dict[str, Any]:
    """按查询检索记忆条目。**后端异常直接往上抛**（契约：检索层绝不静默返回空）。

    `caller_agent_id` / `caller_role`（2026-10 ks_share_memory）：调用方身份
    （凭据反查得出）。非 primary 只看得见自己 + 已共享代理的记忆。

    🔴 `unread_dm`（2026-10 ks_dm_hint）：**顺路带回的未读私信数** ——
    私信是拉取式的，接收方无感知；把提醒寄生在每轮必然发生的检索上，
    零定时器、零额外请求。计数走既有连接池（`box` 与注册表同一个 storage），
    读失败 ⇒ 0（fail-open，**绝不影响检索本体返回**）。

    🔴 `screen`（2026-10 recall_screening）：**哨兵 None = 调用方没传** ——
    没传时看 `screening.default_on`（默认 false ⇒ 行为与接入前逐字不变）；
    显式传 `screen=true/false` 一律**照传**（调用方意志优先）。
    走 LLM 候选筛选时（见 `_screen_rows`）：
    筛成功按选中编号保序过滤并回 `"screened": {"picked": N, "of": M}`，
    筛失败（关着 / 无 key / 429 / 超时 / 坏 JSON）⇒ 原结果照返 +
    `"screened": {"skipped": "<原因>"}`（fail-open，绝不影响检索本体）。
    """
    if not query or not query.strip():
        raise ToolError("query 不能为空")
    limit = max(1, min(int(limit), 100))
    hits = provider.search(query.strip(), limit=limit,
                           **_caller_kwargs(provider.search, caller_agent_id, caller_role))
    if box is None:
        # 没显式给信箱句柄 ⇒ 问 provider 要它自己那份（宿主 provider 懒创建）。
        box = getattr(provider, _PROVIDER_BOX_ATTR, None)
    results = [
        {
            "key": _hit_key(h),
            "tags": str(h.get("tags", "")),
            "score": float(h.get("_combined_score", 0.0) or 0.0),
            "content": _preview(h.get("content"), SEARCH_PREVIEW_CHARS),
            # rev（2026-10 ks_memory_rev_cas）：命中即带版本号 ⇒ 代理拿到就能
            # 直接带 rev 去 memory_update，不用先 memory_get 一次。
            "rev": str(h.get("rev") or DEFAULT_REV),
        }
        for h in hits
    ]
    out: Dict[str, Any] = {
        "query": query.strip(),
        "count": len(hits),
        "unread_dm": _unread_dm(box, caller_agent_id),
        "results": results,
    }
    if _screen_flag(screen):
        results, meta = _screen_rows(query.strip(), results,
                                      text_of=lambda r: str(r.get("content") or ""))
        out["results"] = results
        out["count"] = len(results)
        out["screened"] = meta
    return out


def memory_get(provider: Any, key: str, caller_agent_id: str = "",
               caller_role: str = "") -> Dict[str, Any]:
    """按 key 取单条全文。key 不存在 ⇒ 抛 ToolError（不是「空 ok」）。

    🔴 **未共享代理的条目按「不存在」回**（2026-10 ks_share_memory）：
    存在性本身也不许泄露。
    """
    frag = provider.get(key, **_caller_kwargs(provider.get, caller_agent_id, caller_role))
    if not frag:
        raise ToolError(f"记忆条目不存在：{key}")
    return {
        "key": str(frag.get("key") or key),
        "content": str(frag.get("content", "")),
        "tags": str(frag.get("tags", "")),
        "updated_at": str(frag.get("updated_at", "")),
        # rev（2026-10 ks_memory_rev_cas）：**改写前必须先拿到它** ——
        # memory_update 要拿它当 expected_rev（版本对不上会拒绝并回最新内容）。
        "rev": str(frag.get("rev") or DEFAULT_REV),
    }


def memory_core_list(provider: Any, preview_chars: int = CORE_PREVIEW_CHARS,
                     caller_agent_id: str = "", caller_role: str = "") -> Dict[str, Any]:
    """常驻层（core tag）条数 + 每条预览 —— 与 P1 控制台 `GET /api/v1/core` 同源。

    🔴 非 primary 只拿得到自己的 core + 已共享代理的 core（同口径；primary 的
    core 要 primary 自己开共享才外露 —— 对称规则）。

    🔴 **注入预算按本次读取方取**（2026-10 ks_agent_budget）：调用方在注册表里
    配了自己的 `core_budget_chars` ⇒ 列表按**它**的预算截；没配 ⇒ 走全局。
    口径与注入路径（`system_prompt_block` → `_core_block`）同一把尺子
    （`govern.visibility.truncate_core_fragments`）—— 注入截了、列表没截就是漂了。
    截断**必带一行可见说明**（不许静默丢条目）。
    """
    preview_chars = max(1, min(int(preview_chars), 2000))
    rows = _list_core(provider, caller_agent_id, caller_role)
    budget = _core_budget_of(provider, caller_agent_id)
    total = len(rows)
    if budget is not None:
        rows, _dropped = truncate_core_fragments(rows, budget)
    out: Dict[str, Any] = {
        "count": len(rows),
        "chars": sum(len(str(r.get("content", "") or "")) for r in rows),
        "items": [
            {
                "key": str(r.get("key", "")),
                "tags": str(r.get("tags", "")),
                "content": _preview(r.get("content"), preview_chars),
            }
            for r in rows
        ],
    }
    if len(rows) < total:
        out["truncated"] = True
        out["note"] = f"[core 层已截断：共 {total} 条，本次列出 {len(rows)} 条]"
    if budget is not None:
        out["budget"] = budget
    return out


def memory_add(provider: Any, text: str, tags: str = "",
               caller_agent_id: str = "", caller_role: str = "") -> Dict[str, Any]:
    """写入一条记忆并返回它的 key。写层 fail-closed：失败抛错，不谎报成功。

    🔴 **「人工显式直写 core」只对 primary 凭据生效**（2026-10 ks_share_tighten
    收紧）：`core_direct` 是「人在对话里明确要求记这一条」的等价动作 —— 但**只有
    凭据反查出来是 primary 的调用方**才算「人」在批。外部代理带 `core` 标签进来
    ⇒ 不带 `core_direct` ⇒ 走既有降级（落 `core_candidate`，**无提名日戳** ⇒
    班车不会自动审它）。

    判不出身份（没有闸门 / 没有凭据）⇒ **不带** `core_direct`（fail-closed：
    判不出就别当 primary）。

    🔴 **归属章按凭据反查的调用方打**（2026-10 ks_owner_stamp）：远端代理写的
    记忆归它自己（`agent:<它>`），不是 provider 进程默认身份 —— 否则它写完
    自己搜不到（可见性收紧后），还混进主脑全库。`caller_agent_id` 为空 ⇒ 本机
    直写 ⇒ 归属仍走进程默认（既有行为逐字不变）。
    """
    if not text or not text.strip():
        raise ToolError("text 不能为空")
    known = bool((caller_agent_id or "").strip())
    role = str(caller_role or "").strip()
    is_primary = known and role == ROLE_PRIMARY
    key = _add_memory(provider, text.strip(), tags or "",
                      core_direct=is_primary,
                      caller_role=(role if known else None),
                      caller_agent_id=(caller_agent_id.strip() if known else ""))
    if not key:
        raise ToolError("写入失败：provider 未返回 key（fail-closed）")
    # rev（2026-10 ks_memory_rev_cas）：写完就把版本号给出去 ⇒ 代理紧接着改写
    # 这条时不必再 memory_get 一次。
    try:
        frag = provider.get(key, **_caller_kwargs(provider.get, caller_agent_id, caller_role))
    except Exception as e:      # noqa: BLE001 —— 拿不到 rev 不影响这次写入
        logger.debug("keepsake MCP: 取新写入条目的 rev 失败（忽略）: %s", e)
        frag = {}
    return {"key": key, "tags": tags or "", "rev": str(frag.get("rev") or DEFAULT_REV)}


def _add_memory(provider: Any, text: str, tags: str, *,
                core_direct: bool = False,
                caller_role: Optional[str] = None,
                caller_agent_id: str = "") -> str:
    """调 `provider.add`；provider 支持 `core_direct` / `caller_role` /
    `caller_agent_id` 就带上（可选扩展）。

    `core_direct` **不在通用契约里**（契约不该为某一个宿主的「显式直写」开洞），
    `caller_role` / `caller_agent_id` 同理（那是 keepsake 自己的治理扩展）——
    所以这里先看签名再决定带不带：别的记忆系统没有这几个参数 ⇒ 照常调，
    不猜、不用 try/except 吞异常（那会把 provider 自己的写失败吞成「没写进去」）。
    """
    try:
        params = inspect.signature(provider.add).parameters
    except (TypeError, ValueError):       # 签名取不到（内置/C 扩展）⇒ 按不支持处理
        params = {}
    kwargs: Dict[str, Any] = {}
    if "core_direct" in params:
        kwargs["core_direct"] = core_direct
    if "caller_role" in params:
        kwargs["caller_role"] = caller_role
    if "caller_agent_id" in params:
        kwargs["caller_agent_id"] = caller_agent_id
    return provider.add(text, tags=tags, **kwargs)


def memory_update(provider: Any, key: str, text: str, expected_rev: str,
                  tags: str = "", *, caller_agent_id: str = "",
                  caller_role: str = "") -> Dict[str, Any]:
    """**乐观并发（CAS）改写**一条记忆条目（2026-10 ks_memory_rev_cas）。

    🔴 **用法（必须按这个顺序）**：
      1. `memory_get(key)` 拿到这条的 `rev`（也可以用 `memory_search` 命中里的 rev）；
      2. 改完内容后带 `expected_rev=<那条 rev>` 调本工具；
      3. 收到 `error="conflict"` ⇒ **别硬写**：返回里带 `current_content` /
         `current_rev`（**对方刚写进去的那一版**）⇒ 重新读、合并后再提交。

    🔴 **为什么必须带 rev**：共享记忆被多个代理同时读写，两个代理基于同一版
    各自改写 ⇒ 各存一条（key=内容哈希）⇒ **互相看不见对方**（内容分叉）。
    带 rev 提交就是对不上就拒 —— 宁可拒一次，也不要静默分叉。

    🔴 **MCP 对外必带版本（fail-closed，2026-10 ks_cas_hard）**：记忆是核心资产，
    **不许留软口子** —— `expected_rev` 为空一律拒（不再兼容「空串 = 跳过版本
    校验」的老路径）。**storage / provider 内部**的 `cas_update` / `cas_remove`
    仍保留空串兼容路径（班车 / 提炼 cron / 控制台直调 provider 的写路**不动**），
    但**不对外** —— 对外只有这一道闸。

    🔴 **归属**：非 owner 不能改别人的条目（primary 全量）。core 内容照旧
    走候选审核队列（**不开直通**）。

    返回：
      * 成功 `{"ok": True, "key": <新 key>, "rev": <新 rev>, "previous_key": …}`
      * 冲突 `{"error": "conflict", "current_rev":…, "current_content":…,
        "updated_by":…, "current_key":…}`
      * 不存在 `{"error": "not_found"}` / 不是你的 `{"error": "not_owner"}`
    """
    if not key:
        raise ToolError("key 不能为空")
    if not text or not text.strip():
        raise ToolError("text 不能为空")
    if not str(expected_rev or "").strip():
        raise ToolError(
            "并发安全要求带版本号改写：先 memory_get(key) 取 rev，"
            "再带 expected_rev 提交（版本对不上会拒并回最新内容，"
            "重读合并再提，勿硬写）"
        )
    out = _update_memory(provider, key, text.strip(), str(expected_rev).strip(), tags or "",
                         caller_agent_id=caller_agent_id, caller_role=caller_role)
    if out.get("ok"):
        return out
    error = str(out.get("error") or "")
    if error == "conflict":
        # 冲突**不是错误**（是「你的版本旧了」）⇒ 结构化回给代理，不抛 ToolError。
        return out
    if error == "not_found":
        raise ToolError(f"记忆条目不存在：{key}")
    if error == "not_owner":
        raise ToolError(UPDATE_NOT_OWNER)
    raise ToolError(f"改写失败（后端不可用）：{key}")


def _update_memory(provider: Any, key: str, text: str, expected_rev: str, tags: str,
                   *, caller_agent_id: str = "", caller_role: str = ""
                   ) -> Dict[str, Any]:
    """调 `provider.update`（带身份参数 —— provider 支持才带）。

    与 `_add_memory` 同一套路：先看签名再决定带不带 —— 别的记忆系统 provider
    没有 `caller_agent_id` / `caller_role` ⇒ 照常调，不猜、不吞异常。
    """
    try:
        params = inspect.signature(provider.update).parameters
    except (TypeError, ValueError):       # 签名取不到 ⇒ 按不支持处理
        params = {}
    # 🔴 **签名不匹配就明确报错**（不猜参数、不 try/except 吞 TypeError）：
    #    猜错 = 静默改成「无条件覆盖」= 并发照样分叉 —— 那正是要防的事。
    if "expected_rev" not in params or "tags" not in params:
        raise ToolError("该记忆后端不支持 CAS 改写（provider.update 签名不匹配）")
    kwargs: Dict[str, Any] = {}
    if "caller_agent_id" in params:
        kwargs["caller_agent_id"] = caller_agent_id or ""
        kwargs["caller_role"] = caller_role or ""
    try:
        return dict(provider.update(key, text, expected_rev, tags=tags, **kwargs) or {})
    except BackendUnsupportedError as e:
        # 🔴 后端不支持 CAS ⇒ **明确报错**（不许静默退化成「无条件覆盖」）
        raise ToolError(str(e)) from e


def memory_remove(provider: Any, key: str, confirm: bool = False, *,
                  expected_rev: str = "", caller_agent_id: str = "",
                  caller_role: str = "") -> Dict[str, Any]:
    """删除一条记忆条目。`confirm=False` ⇒ **拒绝并说明**，绝不动数据。

    🔴 **删除权限按归属收窄**（2026-10 ks_perm_scope）：子代理**只能删自己的**
    条目；**无归属的老条目**也行（收紧前的公共数据，与检索口径一致）；
    别人的条目 —— **含已共享代理的** —— 一律拒（只回「不是你的记忆」，
    **不回显任何正文**）。primary 全量可删（= 主脑现状逐字不变）。

    🔴 **先存在性、后归属**（2026-10 ks_remove_sem）：判定顺序 = 确认闸 →
    版本闸 → **存在性** → 归属。**不存在的条目**回「条目不存在：{key}」，
    **存在的别人的条目**才回「不是你的」—— 两条独立人话，不再混成一条。

    🔴 `expected_rev`（2026-10 ks_memory_rev_cas → ks_cas_hard，**MCP 对外必带**）：
    **CAS 删除** —— 版本对不上就**不删**，并把最新内容回给你（冲突结构同
    `memory_update`）。**空串/不给 ⇒ 一律拒**（记忆是核心资产，不留软口子）；
    **storage / provider 内部**的 `cas_remove` 仍保留空串兼容路径（班车 / 提炼
    cron / 控制台直调 provider 的写路**不动**），但**不对外**。
    """
    if not key:
        raise ToolError("key 不能为空")
    if not confirm:
        raise ToolError(
            f"删除需要显式确认：确认后请重新调用 memory_remove(key={key!r}, confirm=True)"
        )
    if not str(expected_rev or "").strip():
        raise ToolError(
            "并发安全要求带版本号删除：先 memory_get(key) 取 rev，"
            "再带 expected_rev 提交（版本对不上会拒并回最新内容，"
            "重读确认后再删，勿硬删）"
        )
    # 🔴 **存在性探测先行**（2026-10 ks_remove_sem）：先答「这条存不存在」，
    #    再问「是不是你的」—— 否则**不存在的条目**和**别人的条目**都塌成
    #    None ⇒ 一律回「不是你的」（实测 zly-hermes 双向复现）。
    #    `provider.get(key)` **不带 caller kwargs** = 服务端进程身份（本部署
    #    provider 是 primary 全量视角）⇒ 这个探测**只答存不存在**。
    #    🔴 不构成泄露面：key 是**内容哈希**，持有 key 的人已知正文。
    if not provider.get(key):
        raise ToolError(f"条目不存在：{key}")
    if not _can_remove(provider, key, caller_agent_id, caller_role):
        raise ToolError(REMOVE_NOT_OWNER)
    # 🔴 删除**只走 CAS**（无条件删除分支已删 —— 那正是要防的静默覆盖）。
    out = _remove_memory_cas(provider, key, str(expected_rev).strip())
    if out.get("error") == "conflict":
        return out
    if not out.get("ok"):
        raise ToolError(f"删除失败（key 不存在或后端不可用）：{key}")
    return {"key": key, "removed": True}


def _remove_memory_cas(provider: Any, key: str, expected_rev: str) -> Dict[str, Any]:
    """调 `provider.remove(key, expected_rev=…)`（provider 支持才带）。

    与 `_update_memory` 同一套路：先看签名再决定带不带 —— 别的 provider 没有
    `expected_rev` ⇒ 明确报错，**不静默退化成无条件删除**（那正是要防的）。
    """
    try:
        params = inspect.signature(provider.remove).parameters
    except (TypeError, ValueError):
        params = {}
    if "expected_rev" not in params:
        raise ToolError("该记忆后端不支持带版本号的删除（provider.remove 签名不匹配）")
    return dict(provider.remove(key, expected_rev=expected_rev) or {})


def memory_merge(provider: Any, dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
    """合并记忆条目。默认 dry-run；实跑必须 `confirm` = 调用瞬间的 core 条数（字符串）。"""
    core_before = len(_list_core(provider))
    if not dry_run:
        if confirm != str(core_before):
            raise ToolError(
                f"实跑合并需要 confirm = 当前 core 条数（{core_before}）；收到 {confirm!r}"
            )
    stats = dict(provider.merge(dry_run=dry_run))
    stats["dry_run"] = bool(dry_run)
    stats["core_count_before"] = core_before
    stats["core_count_after"] = len(_list_core(provider))
    return stats


def memory_forget(provider: Any, dry_run: bool = True, confirm: str = "", *,
                  caller_agent_id: str = "", caller_role: str = "") -> Dict[str, Any]:
    """遗忘（按时间衰减筛过期条目）。默认 dry-run；实跑必须 `confirm` = 当前记忆条目总数。

    🔴 **仅主代理可用**（2026-10 ks_perm_scope）：`forget` 是**批处理治理面**
    （按时间衰减一次删掉一批条目）—— 子代理调用 ⇒ 拒（「治理动作仅主代理可用」）。
    别让它圈到别人的量：单条删除有归属闸，这条批量闸没有。
    """
    if not _is_primary_caller(caller_agent_id, caller_role):
        raise ToolError(FORGET_DENIED)
    total_before = _count_fragments(provider)
    if not dry_run:
        if confirm != str(total_before):
            raise ToolError(
                f"实跑遗忘需要 confirm = 当前记忆条目总数（{total_before}）；收到 {confirm!r}"
            )
    stats = dict(provider.forget(dry_run=dry_run))
    stats["dry_run"] = bool(dry_run)
    stats["fragment_count_before"] = total_before
    stats["fragment_count_after"] = _count_fragments(provider)
    return stats


# --------------------------------------------------------------------------
# 治理面（agent_*）：主代理在对话里直接审核别的代理 —— 与控制台同一套 registry 方法
#
# 🔴 **只对 role=primary 的调用方放行**：判定源是 `AgentGate` 那条已有的身份链
# （http 头 Bearer → `verify_any`；stdio env/本机通道 → `verify` / `verify_local`），
# 工具里**不发明第二条身份路径**（见 `_govern_identity`）。
# 🔴 **审计与界面同款**：改前 `backup_json` 快照 + 每个写（含被拒绝的）一行审计，
#   来源位恒为 `source=mcp`（`api/audit.record` 的 `actor` 常量写死 "console"，
#   本单不许改那个文件，故来源进 reason 首段 —— 查 `source=mcp` 即本通道发出的治理写）。
# 🔴 **凭据明文只出现在 `agent_approve` 的返回值**（与界面同语义）：不进审计、不进日志。
# --------------------------------------------------------------------------

#: 6 个治理工具的名字（写口 5 个在 `server.GATE_WRITE_TOOLS` 里也登记过一次）
GOVERN_TOOLS = (
    "agent_list", "agent_approve", "agent_revoke", "agent_set_role", "agent_set_exclude",
    "agent_set_share",
)

#: 非 primary 调用方的**唯一**拒绝文案。带上 role 与 status 两个字段 ——
#: 「已批准但只是 sub」与「压根没批准（pending）」必须一眼可分，
#: 而不与记忆工具那道 `GATE_DENIED`（未批准/已吊销）混同。
GOVERN_DENIED = ("治理工具仅主代理可用：{caller} 当前 role={role} status={status}"
                 "（{why}）—— 请到控制台批准该代理并置为 primary")

#: 吊销的确认串 = agent_id **前 8 位**（与 `api/routers/agents.REVOKE_HINT_LEN` 同口径：
#: 手输一段记认得到的前缀，错一个代理不至于按下去）。
REVOKE_CONFIRM_LEN = 8

#: 批准响应里的回显提示（与界面同义，只是把「界面」换成「转交给用户」）
CREDENTIAL_ONCE_NOTE = (
    "凭据只此一次返回，库里只存 sha256 —— 请立即转交并通知用户在界面外妥善保存；"
    "丢失请用 agent_approve 传 reissue=true 换一条（旧凭据随即失效）。"
)

#: 置 primary 时必须让人看见的后果（旧主被降级）
ROLE_PRIMARY_NOTE = "全库唯一 primary：原 primary 已自动降为 sub（可用 agent_list 核对）。"


def _audit(action: str, target: Any, ok: bool, reason: str = "") -> None:
    """审计一行（`source=mcp` 恒在 reason 首段）。"""
    audit.record(action, target, ok=ok, reason=f"source=mcp; {reason}")


def _backup(action: str, rows: Any) -> str:
    """改前快照：落不进盘 ⇒ 不许改（与 `api/routers/agents._backup` 同一纪律）。"""
    return audit.backup_json(f"agents.mcp.{action}.{audit.stamp()}.json", rows)


def _govern_identity(gate: Any, registry: Any, tool_name: str) -> str:
    """治理工具的身份闸：**只放行 role=primary 的调用方**，返回它的 agent_id。

    判定走 `gate.identify`（`check` 的同一份实现），差别只在两点：

    * 不抛 `GATE_DENIED` 而是由调用方回 `GOVERN_DENIED`（要带 status 字段）；
    * 放行**之后**才 `record_activity`（被拒的调用不记账，与记忆工具同款）。

    http/本机/凭据三种装配的差别全部在闸门内部解决，这里只问「是不是 primary」。
    """
    if registry is None or gate is None:
        raise ToolError("治理工具需要代理注册表（当前 provider 未装配 ks_agent）")
    res = gate.identify(tool_name)
    # 🔴 注册表**暂时读不到**（PG 抖动）不是「你不是 primary」——
    #   各回各话：前者重试就好，后者要去控制台改角色。
    if str(res.get("reason") or "") == "registry_unavailable":
        raise ToolError(gate.denied_text(res))
    role = str(res.get("role") or "")
    if not res.get("ok") or role != ROLE_PRIMARY:
        raise ToolError(GOVERN_DENIED.format(
            caller=str(res.get("agent_id") or ""), role=role or "-",
            status=str(res.get("status") or "unknown"),
            why=str(res.get("reason") or "not_primary")))
    gate.record_activity(str(res.get("agent_id") or ""), tool_name)
    return str(res.get("agent_id") or "")


def _govern_row(registry: Any, agent_id: str, action: str) -> Dict[str, Any]:
    """取代理行；查不到 ⇒ 抛 `ToolError`（**先审计**再抛，与控制台 404 同纪律）。"""
    row = registry.get(agent_id)
    if not row:
        _audit(action, agent_id, ok=False, reason="agent_not_found")
        raise ToolError(f"注册表里没有这个代理：{agent_id}")
    return row


def agent_list(registry: Any) -> Dict[str, Any]:
    """注册表全量 + 待审计数（与 `GET /api/v1/agents` 同一形状）。

    🔴 精简口径由 `registry._public` 保证：**永不回显 `credential_hash`**，
    只带 `credential_hint`（凭据前 8 位，供人比对手上那条对不对）。
    """
    items = registry.list_agents()
    return {"count": len(items), "pending_count": registry.pending_count(), "items": items}


def agent_approve(registry: Any, agent_id: str, reissue: bool = False) -> Dict[str, Any]:
    """批准并签发**一次性凭据**（明文只在这个返回值里出现一次，库里只落 sha256）。

    🔴 **已批准的行再 approve ⇒ 拒**（除非显式 `reissue=True`）：治理工具对
    已批准代理「再批准一次」是误伤线上代理的第三条路（另两条：审核页双击 /
    UI 状态未刷新重复点）—— 静默换发会把旧凭据当场顶掉，代理瞬间连不上。
    判定走 `Registry.approve` 那个**后端原语**（与控制台同一份），这里只负责
    把它翻译成 MCP 口径：拒 + 一行 `ok=false` 的审计（`reason=already_approved`）。
    要换发：显式传 `reissue=true`（旧的那条随即失效）。
    """
    action = "agents.approve"
    aid = (agent_id or "").strip()
    row = _govern_row(registry, aid, action)
    audit.precheck()                       # 审计落不下账 ⇒ 这次治理变更不许做
    backup = _backup("approve", row)
    try:
        credential = registry.approve(aid, reissue=bool(reissue))
    except AlreadyApprovedError:
        _audit(action, aid, ok=False, reason="already_approved")
        raise ToolError(f"{ALREADY_APPROVED_NOTE}（agent_approve 请传 reissue=true）")
    if not credential:      # 竞态：行在读与写之间消失 ⇒ 报错，不谎报「已批准」
        _audit(action, aid, ok=False, reason="agent_race")
        raise ToolError("代理在批准过程中消失，请重试")
    _audit(action, aid, ok=True, reason="credential issued (one-time)")
    return {"ok": True, "agent_id": aid, "status": STATUS_APPROVED,
            "credential": credential, "credential_hint": credential[:8],
            "note": CREDENTIAL_ONCE_NOTE, "backup": backup}


def agent_revoke(registry: Any, agent_id: str, confirm: str = "") -> Dict[str, Any]:
    """吊销（随时可做）：凭据哈希一并清掉，旧凭据**立即**作废。

    `confirm` 必须等于 agent_id **前 8 位**（与界面同一道闸的口径）。
    """
    action = "agents.revoke"
    aid = (agent_id or "").strip()
    row = _govern_row(registry, aid, action)
    want = aid[:REVOKE_CONFIRM_LEN]
    if str(confirm or "") != want:
        _audit(action, aid, ok=False, reason="confirm_mismatch")
        raise ToolError(f"吊销需要确认串 = agent_id 前 {REVOKE_CONFIRM_LEN} 位（{want}）")
    audit.precheck()
    backup = _backup("revoke", row)
    registry.revoke(aid)
    _audit(action, aid, ok=True, reason=f"backup={backup}")
    return {"ok": True, "agent_id": aid, "status": STATUS_REVOKED, "backup": backup}


def agent_set_role(registry: Any, agent_id: str, role: str, confirm: str = "") -> Dict[str, Any]:
    """改角色。**全库唯一 primary**：置 primary 会把原 primary 自动降为 sub。

    `confirm` 必须等于**完整 agent_id**（这道操作改的是全库身份，不是单个开关）。
    改前备份**整份注册表**（连带降级的那一行也要能人工复原）。
    """
    action = "agents.role"
    aid = (agent_id or "").strip()
    row = _govern_row(registry, aid, action)
    want = str(role or "").strip().lower()
    if want not in ROLES:
        _audit(action, aid, ok=False, reason="bad_role")
        raise ToolError(f"role 只能是 {list(ROLES)}，收到 {role!r}")
    if str(confirm or "") != aid:
        _audit(action, aid, ok=False, reason="confirm_mismatch")
        raise ToolError(f"改角色需要确认串 = 完整 agent_id（{aid}）")
    audit.precheck()
    backup = _backup("role", registry.list_agents())
    demoted = registry.set_role(aid, want)
    _audit(action, aid, ok=True,
           reason=f"{row['role']} -> {want}; demoted={demoted}; backup={backup}")
    return {"ok": True, "agent_id": aid, "role": want, "demoted": demoted,
            "note": ROLE_PRIMARY_NOTE if want == ROLE_PRIMARY else "", "backup": backup}


def agent_set_exclude(registry: Any, agent_id: str, exclude: bool = False) -> Dict[str, Any]:
    """「这个代理的条目进不进主脑上下文」开关（退役的 `exclude_tags` 的新家）。"""
    action = "agents.exclude"
    aid = (agent_id or "").strip()
    row = _govern_row(registry, aid, action)
    audit.precheck()
    backup = _backup("exclude", row)
    changed = registry.set_exclude(aid, bool(exclude))
    _audit(action, aid, ok=True,
           reason=f"exclude={bool(exclude)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": aid, "exclude_from_primary": bool(exclude),
            "changed": changed, "backup": backup}


def agent_set_share(registry: Any, agent_id: str, enabled: bool = False) -> Dict[str, Any]:
    """开关「这个代理的记忆**共享不共享**给别的已批准代理」（`share_memory`）。

    🔴 只影响**读可见性**：写路径不过滤（谁的凭据写谁的），写闸 / 分层 / 私信
    一个都不动（那是别的语义）。
    🔴 不是治理动作：`status` / `role` / 已发的凭据一个都不碰
    （与 `Registry.set_share_memory` 同纪律）。
    """
    action = "agents.share"
    aid = (agent_id or "").strip()
    row = _govern_row(registry, aid, action)
    audit.precheck()
    backup = _backup("share", row)
    changed = registry.set_share_memory(aid, bool(enabled))
    _audit(action, aid, ok=True,
           reason=f"share_memory={bool(enabled)} (changed={changed}); backup={backup}")
    return {"ok": True, "agent_id": aid, "share_memory": bool(enabled),
            "changed": changed, "backup": backup}


# --------------------------------------------------------------------------
# 代理互聊信箱（message_*）：**所有已批准代理**可用，与治理面无关
#
# 🔴 **发件人身份只认凭据反查**（`gate.check` 那条链）：工具签名里**没有 from**
#   —— 调用侧自报身份正是这套治理要拔掉的东西。
# 🔴 **收件人资格先判后投**：目标代理不存在 / 未批准 / 已吊销 ⇒ 一条都不投；
#   目标关了私信（`dm_enabled=0`）⇒ 拒投并说明为什么。
# 🔴 **三个工具都算写**（`server.GATE_WRITE_TOOLS`）：收信即消费（标已读）
#   是状态变更，不是纯读。
# --------------------------------------------------------------------------

#: 信箱三工具的名字（都在 `server.GATE_WRITE_TOOLS` 里登记为写口）
MESSAGE_TOOLS = ("message_send", "message_inbox", "message_recent")

#: 线程号前缀（空 thread_id 时自动生成 `th_<8 位 hex>`）
THREAD_PREFIX = "th_"


def _message_ctx(gate: Any, box: Any, tool_name: str) -> Tuple[str, Any]:
    """信箱工具的入口：取**调用方身份**（凭据反查）与信箱句柄。

    没有注册表 ⇒ 明确报错（与治理面同款：不让调用方以为工具凭空消失）。
    """
    if gate is None or box is None:
        raise ToolError("代理互聊信箱需要代理注册表（当前 provider 未装配 ks_agent）")
    res = gate.check(tool_name)
    return str(res.get("agent_id") or ""), box


def _message_target(registry: Any, to_id: str) -> str:
    """收件人资格闸：不存在 / 未批准 / 已吊销 ⇒ 拒投；关了私信 ⇒ 拒投。"""
    target = (to_id or "").strip()
    if not target:
        raise ToolError("to 不能为空")
    row = registry.get(target)
    if not row or row.get("status") != STATUS_APPROVED:
        raise ToolError(
            f"目标代理未批准，消息未投递：{target}（代理不存在 / 未批准 / 已吊销）")
    if not registry.dm_enabled(target):
        raise ToolError(
            f"目标代理 {target} 已关闭私信接收（dm_enabled=0），消息未投递")
    return target


def message_send(registry: Any, box: Any, caller: str, to: str, content: str,
                 thread_id: str = "") -> Dict[str, Any]:
    """投一条私信给 `to`。返回 `{ok, seq, thread_id}`。

    `thread_id` 留空 ⇒ 自动生成 `th_<8 位 hex>` 并**回显**（回复时带上它即可续上
    同一段对话）。正文超长 ⇒ 报错（不静默截断 —— 截断的信会让人读错话）。
    """
    target = _message_target(registry, to)
    body = (content or "").strip()
    if not body:
        raise ToolError("content 不能为空")
    if len(body) > MESSAGE_CONTENT_MAX:
        raise ToolError(
            f"content 超长（{len(body)} 字符 > 上限 {MESSAGE_CONTENT_MAX}）")
    tid = (thread_id or "").strip() or f"{THREAD_PREFIX}{uuid.uuid4().hex[:8]}"
    seq = box.send(caller, target, body, tid)
    return {"ok": True, "seq": seq, "thread_id": tid}


def _message_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """信箱行 → 对外形状（`from` 而不是 `from_id`：模型读起来更像一句话）。"""
    return [
        {
            "seq": r.get("seq"),
            "from": r.get("from_id", ""),
            "to": r.get("to_id", ""),
            "content": r.get("content", ""),
            "thread_id": r.get("thread_id", ""),
            "created": r.get("created", ""),
        }
        for r in rows
    ]


def message_inbox(box: Any, caller: str, unread_only: bool = True,
                  limit: int = 20) -> List[Dict[str, Any]]:
    """收件箱（**fetch 即标读**：取到的消息当场算已读）。"""
    return _message_rows(box.fetch_inbox(caller, unread_only=unread_only, limit=limit))


def message_recent(box: Any, caller: str, peer: str, limit: int = 20) -> List[Dict[str, Any]]:
    """与 `peer` 的**双向**最近消息（含自己发的；**不动**对方的已读状态）。"""
    return _message_rows(box.recent_between(caller, (peer or "").strip(), limit=limit))


# --------------------------------------------------------------------------
# 待办工具面（todo_add / todo_list / todo_done）：所有**已批准**代理可用
#
# 🔴 三条纪律（与治理面同一把尺子，见 `govern/todo.py`）：
#   1) **归属两列**：`owner`（谁该干这件事）+ `creator`（谁记下的这件事）——
#      非 primary 看得见的是 **owner = 自己 ∪ creator = 自己**（协作台账：
#      被指派者必见自己的活），primary 全量；
#   2) **owner 必须在册且已批准**（不存在 / 未批准 / 已吊销 ⇒ 拒投，不静默挂空）；
#   3) **改状态只有 owner / creator / primary 能改**（待办不是共享记忆）。
#
# 🔴 三个工具**都走 `_authorize`**（凭据闸）：未批准 / 凭据无效 ⇒ 全部拒绝。
# --------------------------------------------------------------------------

#: 待办三工具的名字（`todo_add` / `todo_done` 在 `server.GATE_WRITE_TOOLS` 里登记为写口）。
TODO_TOOLS = ("todo_add", "todo_list", "todo_done")


def _todo_store(provider: Any, registry: Any) -> Any:
    """取待办存储句柄：provider 上挂了就用，没挂就按注册表的 storage 现造一份。

    与信箱同款（`Messaging(registry.storage)`）—— **不另开连接、不另配 DSN**。
    """
    store = getattr(provider, _PROVIDER_TODO_ATTR, None)
    if store is None and registry is not None:
        store = TodoStore(registry.storage)
    return store


def _todo_owner_check(registry: Any, owner: str) -> str:
    """owner 资格闸：不在册 / 未批准 / 已吊销 ⇒ 拒（不静默挂一个空 owner）。"""
    target = (owner or "").strip()
    if not target:
        raise ToolError("owner 不能为空")
    if registry is None:
        raise ToolError("待办需要代理注册表（当前 provider 未装配 ks_agent）")
    row = registry.get(target)
    if not row or row.get("status") != STATUS_APPROVED:
        raise ToolError(
            f"待办归属代理未批准：{target}（不存在 / 未批准 / 已吊销）")
    return target


def todo_add(store: Any, registry: Any, caller: str, title: str,
             detail: str = "", owner: str = "", due: str = "") -> Dict[str, Any]:
    """记一条待办，返回 `{ok, id, owner, creator, status}`。

    * `owner` 缺省 = **调用方自己**（凭据反查出来的那个）；
    * `owner` 可以指他人 ⇒ 但**必须在册且已批准**（否则拒）；
    * `creator` 一律是**调用方**（工具签名里没有 creator —— 不给自报留口子）。
    """
    if store is None:
        raise ToolError("待办存储未装配（当前 provider 没有 ks_todo）")
    who = (caller or "").strip()
    target = (owner or "").strip() or who
    if not target:
        raise ToolError("owner 不能为空")
    _todo_owner_check(registry, target)
    try:
        todo_id = store.add(title, detail, target, who, due)
    except TodoError as e:
        raise ToolError(e.message) from e
    return {"ok": True, "id": todo_id, "owner": target, "creator": who,
            "status": STATUS_OPEN}


def todo_list(store: Any, registry: Any, caller: str, caller_role: str,
              scope: str = SCOPE_OPEN, limit: int = TODO_DEFAULT_LIMIT
              ) -> List[Dict[str, Any]]:
    """看待办（默认只看**开放**的）：`scope` = open / done / all。

    🔴 非 primary 只见**与自己有关的**（owner = 自己 ∪ creator = 自己）；
    primary 全量 —— 判据在 `govern.todo.can_access`，不在这儿重写一遍。
    """
    if store is None:
        raise ToolError("待办存储未装配（当前 provider 没有 ks_todo）")
    sc = (scope or "").strip() or SCOPE_OPEN
    if sc not in SCOPES:
        raise ToolError(f"scope 只能是 {'/'.join(SCOPES)}，收到：{sc}")
    primary = _is_primary_caller(caller, caller_role)
    rows, _total = store.list_rows(
        status="", scope=sc, page=1, size=limit,
        visible_to="" if primary else str(caller or "").strip())
    out: List[Dict[str, Any]] = []
    for row in rows[:max(1, int(limit or TODO_DEFAULT_LIMIT))]:
        out.append({
            "id": row.get("id"),
            "title": row.get("title", ""),
            "detail": row.get("detail", ""),
            "owner": row.get("owner", ""),
            "creator": row.get("creator", ""),
            "status": row.get("status", STATUS_OPEN),
            "due": row.get("due", ""),
            "due_state": row.get("due_state", "none"),
            "created": row.get("created", ""),
            "done_at": row.get("done_at", ""),
        })
    return out


def todo_set_status(store: Any, registry: Any, caller: str, caller_role: str,
                    todo_id: int, status: str = STATUS_DONE) -> Dict[str, Any]:
    """改一条待办的状态（`done` / `obsolete` / `open`）。

    🔴 **只有 owner / creator / primary 能改**（`govern.todo.can_access`）——
    别人的待办既看不见也改不了（看不见 ⇒ 自然改不了）。
    """
    if store is None:
        raise ToolError("待办存储未装配（当前 provider 没有 ks_todo）")
    from ..govern.todo import can_access

    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        raise ToolError(f"id 必须是整数，收到：{todo_id}") from None
    row = store.get(tid)
    if not row:
        raise ToolError(f"待办不存在：{tid}")
    primary = _is_primary_caller(caller, caller_role)
    if not can_access(row, str(caller or ""), primary):
        raise ToolError("这条待办与你无关 —— 只有归属人、记录人或主代理能改")
    st = (status or "").strip() or STATUS_DONE
    try:
        changed = store.set_status(tid, st)
    except TodoError as e:
        raise ToolError(e.message) from e
    if not changed:
        raise ToolError(f"待办状态没改成（{tid} 可能刚被别人改掉），请重试")
    return {"ok": True, "id": tid, "status": st}


# --------------------------------------------------------------------------
# 知识库工具面（kb_store / kb_search / kb_get / kb_remove）
#
# 🔴 **归属 / 可见性 / 删除口径与记忆同一把尺子**（`govern.visibility`）：
#   非 primary 看得见的归属 = {自己} ∪ {share_memory=1 的已批准代理} ∪ {无主}；
#   非 primary 能删的 = {自己} ∪ {无主}。**看得见 ≠ 能删**。
#   判据本体在 `keepsake.kb.service`（那里调 `govern.visibility` 的那两份），
#   这里只做「身份 → 可见集合」的翻译与错误翻译。
# --------------------------------------------------------------------------

#: 知识库四工具的名字（`kb_store` / `kb_remove` 也登记在 `server.GATE_WRITE_TOOLS`）
KB_TOOLS = ("kb_store", "kb_search", "kb_get", "kb_remove")

#: 子代理删**别人**的知识库文档时的拒绝文案（🔴 **不回显任何正文**，
#: 与记忆面 `REMOVE_NOT_OWNER` 同款口径）。
KB_REMOVE_NOT_OWNER = "这篇知识库文档不是你的 —— 子代理只能删自己名下的文档"

#: 知识库工具需要代理注册表（没有 ⇒ 明确报错，不静默放行）
KB_NEED_REGISTRY = "知识库工具需要代理注册表（当前 provider 未装配 ks_agent）"


def _kb_registry(registry: Any) -> Any:
    """知识库工具的**身份前提**：没有注册表就没有「谁是谁」⇒ 明确报错。

    🔴 **fail-closed**：知识库装的是用户与主脑的东西，「判不出身份就当本机」
    那条兜底（`_is_primary_caller`）在这儿**不适用** —— 那是记忆面「本机直连」
    的兼容口径，不是「谁都能读知识库」的许可证。
    """
    if registry is None:
        raise ToolError(KB_NEED_REGISTRY)
    return registry


def _kb_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """把数据层的 `KbError` 翻译成 `ToolError`（只带人话，不带内部结构）。"""
    try:
        return fn(*args, **kwargs)
    except KbError as e:
        raise ToolError(e.message) from e


def _split_tags(raw: str) -> List[str]:
    """`"a,b"` → `["a","b"]`（与记忆面的 `tags` 参数同款：逗号分隔的字符串）。"""
    return [t.strip() for t in str(raw or "").replace("|", ",").split(",") if t.strip()]


def kb_store(title: str, content: str, tags: str = "", source_type: str = "agent", *,
             caller_agent_id: str = "", caller_role: str = "",
             registry: Any = None) -> Dict[str, Any]:
    """存一篇知识库文档（**走既有 ingest 管线**：切块 / 幂等 / 审核 status 链不变）。

    * **归属** = 凭据反查出的调用方（primary 也打 —— 主脑写的归主脑）；
    * **审核链不变**：非 primary 写进来的落 `pending`（不切块，正文在审核队列里，
      控制台批准后才可检索）；primary 直进 `effective`；
    * 返回 `{id, status, review, agent_id, chunks_count}`。
    """
    _kb_registry(registry)
    is_primary = _is_primary_caller(caller_agent_id, caller_role)
    return _kb_call(kb_service.store_document,
                    title=title, content=content, tags=_split_tags(tags),
                    source_type=source_type, agent_id=caller_agent_id,
                    is_primary=is_primary)


def kb_search(query: str, limit: int = 10, *, caller_agent_id: str = "",
              caller_role: str = "", registry: Any = None,
              screen: Optional[bool] = None) -> Dict[str, Any]:
    """检索知识库。返回 `id` / `title` / 摘要（**不含全文** —— 要全文用 `kb_get`）。

    🔴 可见范围 = **自己名下 + 已共享代理名下 + 无主老文档**（primary 全量）。

    🔴 `screen`（2026-10 recall_screening）：**哨兵 None = 调用方没传** ——
    没传时看 `screening.default_on`（默认 false ⇒ 行为与接入前逐字不变）；
    显式传 `screen=true/false` 一律**照传**（调用方意志优先）。
    走 LLM 候选筛选时语义与 `memory_search` 完全一致
    （筛成功保序过滤 + `"screened": {"picked": N, "of": M}`；
    筛失败 ⇒ 原结果照返 + `"screened": {"skipped": "<原因>"}`）。
    候选正文取知识库检索条目里的**摘要文本**（`snippet` —— 知识库路由没有全文字段）。
    """
    _kb_registry(registry)
    is_primary = _is_primary_caller(caller_agent_id, caller_role)
    scope = kb_service.visible_scope(caller_agent_id, registry, is_primary=is_primary)
    out = _kb_call(kb_service.search_documents, query=query,
                    limit=max(1, min(int(limit), 100)), agent_scope=scope)
    results = [
        {"id": r["doc_id"], "title": r["title"], "snippet": r["snippet"],
         "chunk_id": r["chunk_id"]}
        for r in out["items"]
    ]
    res: Dict[str, Any] = {
        "query": str(query or "").strip(),
        "count": len(results),
        "results": results,
    }
    if _screen_flag(screen):
        results, meta = _screen_rows(
            str(query or "").strip(), results,
            text_of=lambda r: str(r.get("snippet") or r.get("title") or ""))
        res["results"] = results
        res["count"] = len(results)
        res["screened"] = meta
    return res


def kb_get(id: str, *, caller_agent_id: str = "", caller_role: str = "",
           registry: Any = None) -> Dict[str, Any]:
    """取一篇知识库文档的详情（标题 + 全文 + 归属 + 状态）。

    🔴 **不可见 = 不存在**（存在性本身也不许泄露，与 `memory_get` 同纪律）。
    """
    _kb_registry(registry)
    is_primary = _is_primary_caller(caller_agent_id, caller_role)
    scope = kb_service.visible_scope(caller_agent_id, registry, is_primary=is_primary)
    doc = _kb_call(kb_service.get_document, doc_id=id, agent_scope=scope)
    if not doc:
        raise ToolError(f"知识库文档不存在：{id}")
    return {"id": str(doc.get("id") or id),
            "title": str(doc.get("title") or ""),
            "content": str(doc.get("content") or ""),
            "chunks": list(doc.get("chunks") or []),
            "source_type": str(doc.get("source_type") or ""),
            "content_type": str(doc.get("content_type") or ""),
            "status": str(doc.get("status") or ""),
            "agent_id": str(doc.get("agent_id") or "")}


def kb_remove(id: str, confirm: bool = False, *, caller_agent_id: str = "",
              caller_role: str = "", registry: Any = None) -> Dict[str, Any]:
    """删一篇知识库文档。**必须 `confirm=True`**，否则拒绝（绝不动数据）。

    🔴 **删除比读可见性更严**：非 primary 只能删**自己名下**的（或无主老文档）；
    别人的 —— **含已共享代理的** —— 一律拒（只回「不是你的」，**零内容回显**）。
    primary 全量可删。
    """
    _kb_registry(registry)
    if not confirm:
        raise ToolError(
            f"删除需要显式确认：确认后请重新调用 kb_remove(id={id!r}, confirm=True)")
    is_primary = _is_primary_caller(caller_agent_id, caller_role)
    scope = kb_service.visible_scope(caller_agent_id, registry, is_primary=is_primary)
    try:
        out = _kb_call(kb_service.remove_document, doc_id=id, confirm=True,
                       caller_agent_id=caller_agent_id, is_primary=is_primary,
                       agent_scope=scope)
    except KbError as e:
        # 拒时**只回人话**（正文/标题一律不回显）—— 与记忆面同款。
        if e.code == "kb_not_owner":
            raise ToolError(KB_REMOVE_NOT_OWNER) from e
        raise ToolError(e.message) from e
    return out


# --------------------------------------------------------------------------
# 注册到 FastMCP/MCPServer
# --------------------------------------------------------------------------

def register_tools(server: Any, provider: Any, gate: Any = None, *,
                   govern: bool = False) -> Any:
    """把 8 个记忆 tool + 3 个信箱 tool + 4 个知识库 tool + 3 个待办 tool
    （+ `govern=True` 时再挂 6 个治理 tool）挂到 `server` 上。

    `gate` 非 None 时（默认装配的 keepsake provider 才有），**每个 tool 调用前**
    先过一道身份闸门（`gate.check(工具名)`，未批准即 `ToolError`）——
    闸门负责按 read/write 打活动计数，所以它必须挂在 tool 入口，
    而不是塞进各工具函数体里（漏一处就是一个绕过闸门的入口）。

    治理那 6 个走 `gate.identify` + `role == primary`（见 `_govern_identity`）：
    自造 provider 没有注册表（`gate is None`）时它们照常注册，只是每个都明确报
    「治理工具需要代理注册表」—— 不静默消失，免得调用方以为治理面不存在。

    信箱那 3 个（`message_*`）与治理面**无关**：所有已批准代理都能用（判定走
    `gate.check` 那条身份链）；没有注册表时同样照常注册并明确报错。

    知识库那 4 个（`kb_*`）同样与治理面**无关**：所有已批准代理都能用，归属 /
    可见性 / 删除与记忆同一把尺子（`govern.visibility`）；没有注册表时照常注册，
    但每次调用明确报「需要代理注册表」（fail-closed：判不出身份就不许碰知识库）。
    """
    registry = getattr(gate, "registry", None)
    #: 信箱句柄（与注册表**同一个 storage** ⇒ 不另开连接、不另拼 DSN）。
    #: 优先用 provider 自己那份（`KeepsakeProvider` 在 initialize 时就建好了，
    #: schema 也已就绪 ⇒ 检索热路径上只剩一条 count 查询，没有 DDL）。
    #: 没有注册表时留 None —— 工具照常注册，但每次调用明确报「需要代理注册表」。
    box = getattr(provider, _PROVIDER_BOX_ATTR, None)
    if box is None and registry is not None:
        box = Messaging(registry.storage)

    # 待办存储（与信箱同款：provider 上挂了就用，没挂就按注册表的 storage 现造）
    todos = _todo_store(provider, registry)

    def _authorize(tool_name: str) -> Dict[str, Any]:
        """过身份闸门，并**把身份结论带回来**（可见性过滤要用，2026-10 ks_share_memory）。

        🔴 身份只认闸门那条链（凭据反查）—— 调用方自报身份正是这套治理要拔掉的
        东西，所以这里的 caller 一律来自 `gate.check` 的结论。
        """
        if gate is None:
            return {}
        return dict(gate.check(tool_name) or {})

    @server.tool(
        name="memory_capabilities",
        description="记忆后端能力声明（version / core_tier / knn / dry_run_merge）。",
    )
    def _capabilities() -> Dict[str, Any]:
        _authorize("memory_capabilities")
        return memory_capabilities(provider)

    @server.tool(
        name="memory_search",
        description="按查询检索记忆条目，返回 key / tags / score / 正文预览（截断 300 字）；"
                    "unread_dm = 本代理的未读私信条数（0 = 没有）。"
                    "screen=true 时先经 LLM 候选筛选（默认不筛；开启后结果里带 screened 说明）。"
                    "不传 screen 时按服务端的 default_on 偏好走；显式传 false 一律不筛。",
    )
    def _search(query: str, limit: int = 10,
                screen: Optional[bool] = None) -> Dict[str, Any]:
        ident = _authorize("memory_search")
        return memory_search(provider, query, limit,
                             caller_agent_id=str(ident.get("agent_id") or ""),
                             caller_role=str(ident.get("role") or ""),
                             box=box, screen=screen)

    @server.tool(
        name="memory_get",
        description="按 key 取一条记忆的全文；key 不存在则报错。",
    )
    def _get(key: str) -> Dict[str, Any]:
        ident = _authorize("memory_get")
        return memory_get(provider, key,
                          caller_agent_id=str(ident.get("agent_id") or ""),
                          caller_role=str(ident.get("role") or ""))

    @server.tool(
        name="memory_core_list",
        description="列出常驻层（core）记忆：条数 + 每条预览。",
    )
    def _core_list(preview_chars: int = CORE_PREVIEW_CHARS) -> Dict[str, Any]:
        ident = _authorize("memory_core_list")
        return memory_core_list(provider, preview_chars,
                                caller_agent_id=str(ident.get("agent_id") or ""),
                                caller_role=str(ident.get("role") or ""))

    @server.tool(
        name="memory_add",
        description="写入一条记忆，返回它的 key。归属按凭据反查出的调用方打"
                    "（写进的是你自己的记忆，别人默认看不见）。",
    )
    def _add(text: str, tags: str = "") -> Dict[str, Any]:
        ident = _authorize("memory_add")
        return memory_add(provider, text, tags,
                          caller_agent_id=str(ident.get("agent_id") or ""),
                          caller_role=str(ident.get("role") or ""))

    @server.tool(
        name="memory_update",
        description="乐观并发改写一条记忆（CAS）：必须先 memory_get 拿它的 rev，"
                    "改完内容后带 expected_rev 提交（不带版本号一律拒）。"
                    "版本对不上就拒绝并回最新内容"
                    "（error=conflict，带 current_content/current_rev）—— "
                    "收到冲突请重新读、合并后再提交，不要硬写。"
                    "子代理只能改自己的条目（别人的条目一律拒）。",
    )
    def _update(key: str, text: str, expected_rev: str,
                tags: str = "") -> Dict[str, Any]:
        ident = _authorize("memory_update")
        return memory_update(provider, key, text, expected_rev, tags,
                             caller_agent_id=str(ident.get("agent_id") or ""),
                             caller_role=str(ident.get("role") or ""))

    @server.tool(
        name="memory_remove",
        description="删除一条记忆。必须 confirm=True，否则拒绝；"
                    "必须带 expected_rev（先 memory_get 拿 rev，版本对不上就不删，"
                    "并回最新内容）。"
                    "子代理只能删自己的条目（别人的条目一律拒）。",
    )
    def _remove(key: str, expected_rev: str, confirm: bool = False) -> Dict[str, Any]:
        ident = _authorize("memory_remove")
        return memory_remove(provider, key, confirm,
                             expected_rev=expected_rev,
                             caller_agent_id=str(ident.get("agent_id") or ""),
                             caller_role=str(ident.get("role") or ""))

    @server.tool(
        name="memory_merge",
        description="合并记忆条目。默认 dry_run=True 只统计；实跑需 confirm = 当前 core 条数。",
    )
    def _merge(dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
        _authorize("memory_merge")
        return memory_merge(provider, dry_run, confirm)

    @server.tool(
        name="memory_forget",
        description="遗忘过期记忆条目。默认 dry_run=True 只统计；实跑需 confirm = 当前记忆条目总数。"
                    "仅主代理可用（批处理治理面）。",
    )
    def _forget(dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
        ident = _authorize("memory_forget")
        return memory_forget(provider, dry_run, confirm,
                             caller_agent_id=str(ident.get("agent_id") or ""),
                             caller_role=str(ident.get("role") or ""))

    # ---- 知识库工具面：所有**已批准**代理可用（与 govern 无关）----------
    # 身份判定走 `gate.check`（与记忆工具同一条身份链）：归属 = 凭据反查出来的
    # 调用方，**工具签名里没有 agent_id** —— 不给调用侧自报身份留口子。
    @server.tool(
        name="kb_store",
        description="往知识库存一篇文档（标题 + 正文 + 可选逗号分隔的标签）。"
                    "归属按凭据反查出的调用方打：非主代理写进来的进待审队列"
                    "（控制台批准后才可检索），主代理直进。返回 {id, status}。"
                    "source_type 缺省 agent（只能 agent / manual —— skill 是夜间"
                    "灌入器的地盘，代理不许写）。",
    )
    def _kb_store(title: str, content: str, tags: str = "",
                  source_type: str = "agent") -> Dict[str, Any]:
        ident = _authorize("kb_store")
        return kb_store(title, content, tags, source_type,
                        caller_agent_id=str(ident.get("agent_id") or ""),
                        caller_role=str(ident.get("role") or ""),
                        registry=registry)

    @server.tool(
        name="kb_search",
        description="检索知识库，返回 id / title / 摘要（不含全文；要全文用 kb_get）。"
                    "只看得到自己名下 + 已共享代理名下 + 无主的老文档。"
                    "screen=true 时先经 LLM 候选筛选（默认不筛；开启后结果里带 screened 说明）。"
                    "不传 screen 时按服务端的 default_on 偏好走；显式传 false 一律不筛。",
    )
    def _kb_search(query: str, limit: int = 10,
                   screen: Optional[bool] = None) -> Dict[str, Any]:
        ident = _authorize("kb_search")
        return kb_search(query, limit,
                         caller_agent_id=str(ident.get("agent_id") or ""),
                         caller_role=str(ident.get("role") or ""),
                         registry=registry, screen=screen)

    @server.tool(
        name="kb_get",
        description="按 id 取一篇知识库文档的详情（标题 + 全文 + 归属）。"
                    "看不到的文档按「不存在」回。",
    )
    def _kb_get(id: str) -> Dict[str, Any]:
        ident = _authorize("kb_get")
        return kb_get(id,
                      caller_agent_id=str(ident.get("agent_id") or ""),
                      caller_role=str(ident.get("role") or ""),
                      registry=registry)

    @server.tool(
        name="kb_remove",
        description="删一篇知识库文档。必须 confirm=True，否则拒绝。"
                    "子代理只能删自己名下的文档（别人的一律拒）。",
    )
    def _kb_remove(id: str, confirm: bool = False) -> Dict[str, Any]:
        ident = _authorize("kb_remove")
        return kb_remove(id, confirm,
                         caller_agent_id=str(ident.get("agent_id") or ""),
                         caller_role=str(ident.get("role") or ""),
                         registry=registry)

    # ---- 代理互聊信箱：所有**已批准**代理可用（与 govern 无关）----------
    # 身份判定走 `gate.check`（与记忆工具同一条身份链）：发件人 = 凭据反查出来的
    # 调用方，**工具签名里没有 from** —— 不给调用侧自报身份留口子。
    @server.tool(
        name="message_send",
        description="给另一个已批准代理发一条私信（对方用 message_inbox 收）。"
                    "正文 ≤4000 字符；thread_id 留空自动生成并回显。",
    )
    def _message_send(to: str, content: str, thread_id: str = "") -> Dict[str, Any]:
        caller, mbox = _message_ctx(gate, box, "message_send")
        return message_send(registry, mbox, caller, to, content, thread_id)

    @server.tool(
        name="message_inbox",
        description="取收件箱里的私信（默认只取未读；**取走即算已读**）。",
    )
    def _message_inbox(unread_only: bool = True, limit: int = 20) -> List[Dict[str, Any]]:
        caller, mbox = _message_ctx(gate, box, "message_inbox")
        return message_inbox(mbox, caller, unread_only=unread_only, limit=limit)

    @server.tool(
        name="message_recent",
        description="看与某个代理的双向最近消息（含自己发的；不会改动已读状态）。",
    )
    def _message_recent(peer: str, limit: int = 20) -> List[Dict[str, Any]]:
        caller, mbox = _message_ctx(gate, box, "message_recent")
        return message_recent(mbox, caller, peer, limit=limit)

    # ---- 待办：所有**已批准**代理可用（与治理面无关）--------------------
    # 身份判定走 `gate.check`（与记忆工具同一条身份链）：owner / creator 一律
    # 由**凭据反查**得来，工具签名里没有这两个字段 —— 不给调用侧自报留口子。
    @server.tool(
        name="todo_add",
        description="记一条待办。owner 缺省 = 你自己（可以指给别人，但那个代理"
                    "必须在册且已批准）。due 是 ISO 日期（YYYY-MM-DD，可空）。"
                    "返回 {id, owner, status}。",
    )
    def _todo_add(title: str, detail: str = "", owner: str = "",
                  due: str = "") -> Dict[str, Any]:
        ident = _authorize("todo_add")
        return todo_add(todos, registry, str(ident.get("agent_id") or ""),
                        title, detail, owner, due)

    @server.tool(
        name="todo_list",
        description="看待办。scope = open（默认）/ done / all。你只看得到"
                    "与你有关的（owner 或 creator 是你）；主代理看得到全部。"
                    "每条带 due_state（none/soon/overdue，到期口径由后端算）。",
    )
    def _todo_list(scope: str = SCOPE_OPEN,
                   limit: int = TODO_DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        ident = _authorize("todo_list")
        return todo_list(todos, registry, str(ident.get("agent_id") or ""),
                         str(ident.get("role") or ""), scope, limit)

    @server.tool(
        name="todo_done",
        description="把一条待办标成已完成（status=done）；也可传 status=obsolete "
                    "标废弃、status=open 重新打开。只有归属人、记录人或主代理能改。",
    )
    def _todo_done(id: int, status: str = STATUS_DONE) -> Dict[str, Any]:
        ident = _authorize("todo_done")
        return todo_set_status(todos, registry, str(ident.get("agent_id") or ""),
                               str(ident.get("role") or ""), id, status)

    # ---- 治理面：仅 primary，且只在本单显式开了 govern 时才存在 ----------
    # 二次判定在每个治理工具**函数体第一行**（`_govern_identity`）而不是靠
    # `_authorize`：那 5 个要回带 status 的专用文案，与记忆工具的拒绝不是一句。
    if govern:
        @server.tool(
            name="agent_list",
            description="列出全部代理（role/status/凭据提示/读写计数），含待审数量。仅主代理可用。",
        )
        def _agents() -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_list")
            return agent_list(registry)

        @server.tool(
            name="agent_approve",
            description="批准一个 pending 代理并签发一次性 ksk_ 凭据（明文只此一次返回）。"
                        "已批准的代理再批会拒（不换发）；要换发请显式 reissue=true（旧凭据随即失效）。"
                        "仅主代理可用。",
        )
        def _agent_approve(agent_id: str, reissue: bool = False) -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_approve")
            return agent_approve(registry, agent_id, reissue=reissue)

        @server.tool(
            name="agent_revoke",
            description="吊销代理（旧凭据立即作废）。confirm 必须等于 agent_id 前 8 位。仅主代理可用。",
        )
        def _agent_revoke(agent_id: str, confirm: str = "") -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_revoke")
            return agent_revoke(registry, agent_id, confirm)

        @server.tool(
            name="agent_set_role",
            description="改角色（primary 全库唯一，置 primary 自动降旧主）。confirm 必须等于完整 agent_id。仅主代理可用。",
        )
        def _agent_set_role(agent_id: str, role: str, confirm: str = "") -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_set_role")
            return agent_set_role(registry, agent_id, role, confirm)

        @server.tool(
            name="agent_set_exclude",
            description="开关「该代理的条目进不进主脑上下文」。exclude=true 表示排除。仅主代理可用。",
        )
        def _agent_set_exclude(agent_id: str, exclude: bool = False) -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_set_exclude")
            return agent_set_exclude(registry, agent_id, exclude)

        @server.tool(
            name="agent_set_share",
            description="开关「该代理的记忆共享不共享给别的已批准代理」。"
                        "enabled=true 表示共享（只影响读可见性）。仅主代理可用。",
        )
        def _agent_set_share(agent_id: str, enabled: bool = False) -> Dict[str, Any]:
            _govern_identity(gate, registry, "agent_set_share")
            return agent_set_share(registry, agent_id, enabled)

    return server