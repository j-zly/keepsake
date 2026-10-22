"""MCP 工具面 —— 8 个记忆 tool + 3 个信箱 tool + 6 个治理 tool 的实现。

**取数只经 `MemoryProvider` 协议**：数据出口只有
`provider.search/get/add/remove/merge/forget/capabilities/system_prompt_block`。

本模块不 import 任何宿主的东西（`agent` / `hermes` 一律不许出现），
也不自建存储 —— 治理面那 5 个 `agent_*` 工具只经**同一个 `Registry` 实例**
（`gate.registry`，即 `provider._registry`）与控制台共用一套方法，不另开连线。
import 的 `..api.audit` / `..govern.registry` 是 keepsake 自家模块（纯文件 /
纯常量与后端适配），与「宿主」无关：`audit.py` 只 import 标准库。

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
from ..govern.messaging import CONTENT_MAX as MESSAGE_CONTENT_MAX, Messaging
from ..govern.registry import (
    ALREADY_APPROVED_NOTE,
    AlreadyApprovedError,
    ROLE_PRIMARY,
    ROLES,
    STATUS_APPROVED,
    STATUS_REVOKED,
)
# core 层的两把尺子（归属可见性 + 注入预算）—— 与注入路径同一份口径。
from ..govern.visibility import truncate_core_fragments

logger = logging.getLogger("keepsake.mcp_server.tools")

__all__ = ["ToolError", "StorageIntrospection", "register_tools",
           "SEARCH_PREVIEW_CHARS", "CORE_PREVIEW_CHARS",
           "GOVERN_TOOLS", "GOVERN_DENIED", "MESSAGE_TOOLS"]

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
# 8 个 tool 的实现（纯函数，provider 显式传参 —— 不靠闭包，方便单测直接调）
# --------------------------------------------------------------------------

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
    }


def memory_search(provider: Any, query: str, limit: int = 10,
                 caller_agent_id: str = "", caller_role: str = "",
                 box: Any = None) -> Dict[str, Any]:
    """按查询检索记忆条目。**后端异常直接往上抛**（契约：检索层绝不静默返回空）。

    `caller_agent_id` / `caller_role`（2026-10 ks_share_memory）：调用方身份
    （凭据反查得出）。非 primary 只看得见自己 + 已共享代理的记忆。

    🔴 `unread_dm`（2026-10 ks_dm_hint）：**顺路带回的未读私信数** ——
    私信是拉取式的，接收方无感知；把提醒寄生在每轮必然发生的检索上，
    零定时器、零额外请求。计数走既有连接池（`box` 与注册表同一个 storage），
    读失败 ⇒ 0（fail-open，**绝不影响检索本体返回**）。
    """
    if not query or not query.strip():
        raise ToolError("query 不能为空")
    limit = max(1, min(int(limit), 100))
    hits = provider.search(query.strip(), limit=limit,
                           **_caller_kwargs(provider.search, caller_agent_id, caller_role))
    if box is None:
        # 没显式给信箱句柄 ⇒ 问 provider 要它自己那份（宿主 provider 懒创建）。
        box = getattr(provider, _PROVIDER_BOX_ATTR, None)
    return {
        "query": query.strip(),
        "count": len(hits),
        "unread_dm": _unread_dm(box, caller_agent_id),
        "results": [
            {
                "key": _hit_key(h),
                "tags": str(h.get("tags", "")),
                "score": float(h.get("_combined_score", 0.0) or 0.0),
                "content": _preview(h.get("content"), SEARCH_PREVIEW_CHARS),
            }
            for h in hits
        ],
    }


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
    return {"key": key, "tags": tags or ""}


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


def memory_remove(provider: Any, key: str, confirm: bool = False) -> Dict[str, Any]:
    """删除一条记忆条目。`confirm=False` ⇒ **拒绝并说明**，绝不动数据。"""
    if not key:
        raise ToolError("key 不能为空")
    if not confirm:
        raise ToolError(
            f"删除需要显式确认：确认后请重新调用 memory_remove(key={key!r}, confirm=True)"
        )
    ok = provider.remove(key)
    if not ok:
        raise ToolError(f"删除失败（key 不存在或后端不可用）：{key}")
    return {"key": key, "removed": True}


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


def memory_forget(provider: Any, dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
    """遗忘（按时间衰减筛过期条目）。默认 dry-run；实跑必须 `confirm` = 当前记忆条目总数。"""
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
# 注册到 FastMCP/MCPServer
# --------------------------------------------------------------------------

def register_tools(server: Any, provider: Any, gate: Any = None, *,
                   govern: bool = False) -> Any:
    """把 8 个记忆 tool + 3 个信箱 tool（+ `govern=True` 时再挂 6 个治理 tool）挂到 `server` 上。

    `gate` 非 None 时（默认装配的 keepsake provider 才有），**每个 tool 调用前**
    先过一道身份闸门（`gate.check(工具名)`，未批准即 `ToolError`）——
    闸门负责按 read/write 打活动计数，所以它必须挂在 tool 入口，
    而不是塞进各工具函数体里（漏一处就是一个绕过闸门的入口）。

    治理那 6 个走 `gate.identify` + `role == primary`（见 `_govern_identity`）：
    自造 provider 没有注册表（`gate is None`）时它们照常注册，只是每个都明确报
    「治理工具需要代理注册表」—— 不静默消失，免得调用方以为治理面不存在。

    信箱那 3 个（`message_*`）与治理面**无关**：所有已批准代理都能用（判定走
    `gate.check` 那条身份链）；没有注册表时同样照常注册并明确报错。
    """
    registry = getattr(gate, "registry", None)
    #: 信箱句柄（与注册表**同一个 storage** ⇒ 不另开连接、不另拼 DSN）。
    #: 优先用 provider 自己那份（`KeepsakeProvider` 在 initialize 时就建好了，
    #: schema 也已就绪 ⇒ 检索热路径上只剩一条 count 查询，没有 DDL）。
    #: 没有注册表时留 None —— 工具照常注册，但每次调用明确报「需要代理注册表」。
    box = getattr(provider, _PROVIDER_BOX_ATTR, None)
    if box is None and registry is not None:
        box = Messaging(registry.storage)

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
                    "unread_dm = 本代理的未读私信条数（0 = 没有）。",
    )
    def _search(query: str, limit: int = 10) -> Dict[str, Any]:
        ident = _authorize("memory_search")
        return memory_search(provider, query, limit,
                             caller_agent_id=str(ident.get("agent_id") or ""),
                             caller_role=str(ident.get("role") or ""),
                             box=box)

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
        name="memory_remove",
        description="删除一条记忆。必须 confirm=True，否则拒绝。",
    )
    def _remove(key: str, confirm: bool = False) -> Dict[str, Any]:
        _authorize("memory_remove")
        return memory_remove(provider, key, confirm)

    @server.tool(
        name="memory_merge",
        description="合并记忆条目。默认 dry_run=True 只统计；实跑需 confirm = 当前 core 条数。",
    )
    def _merge(dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
        _authorize("memory_merge")
        return memory_merge(provider, dry_run, confirm)

    @server.tool(
        name="memory_forget",
        description="遗忘过期记忆条目。默认 dry_run=True 只统计；实跑需 confirm = 当前记忆条目总数。",
    )
    def _forget(dry_run: bool = True, confirm: str = "") -> Dict[str, Any]:
        _authorize("memory_forget")
        return memory_forget(provider, dry_run, confirm)

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