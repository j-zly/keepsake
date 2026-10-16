"""MCP 工具面 —— 8 个 tool 的实现，**取数只经 `MemoryProvider` 协议**。

本模块不 import 任何宿主的东西（`agent` / `hermes` 一律不许出现），
也不自建存储：数据出口只有 `provider.search/get/add/remove/merge/forget/
capabilities/system_prompt_block`。

--------------------------------------------------------------------------
写语义（与 P2 控制台一致，这几条是硬约束不是建议）
--------------------------------------------------------------------------
🔴 `dry_run` 是一等公民：`merge` / `forget` 默认 `dry_run=True`，只统计不写。
🔴 删除必须 `confirm=True`，否则**抛 ToolError 拒绝**，绝不静默返回 ok。
🔴 merge/forget 实跑必须回填 `confirm` = 当前 core 条数 / 当前记忆条目总数
   （字符串比较）—— 防「以为自己在跑 A 库」与「以为自己在跑 dry-run」。
🔴 失败抛 `ToolError`（结构化错误），**绝不静默 ok**。

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

from typing import Any, Dict, List, Protocol, runtime_checkable

from mcp.server.fastmcp.exceptions import ToolError

__all__ = ["ToolError", "StorageIntrospection", "register_tools",
           "SEARCH_PREVIEW_CHARS", "CORE_PREVIEW_CHARS"]

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


def _list_core(provider: Any) -> List[Dict[str, Any]]:
    return list(_introspect(provider).list_core())


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
    }


def memory_search(provider: Any, query: str, limit: int = 10) -> Dict[str, Any]:
    """按查询检索记忆条目。**后端异常直接往上抛**（契约：检索层绝不静默返回空）。"""
    if not query or not query.strip():
        raise ToolError("query 不能为空")
    limit = max(1, min(int(limit), 100))
    hits = provider.search(query.strip(), limit=limit)
    return {
        "query": query.strip(),
        "count": len(hits),
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


def memory_get(provider: Any, key: str) -> Dict[str, Any]:
    """按 key 取单条全文。key 不存在 ⇒ 抛 ToolError（不是「空 ok」）。"""
    frag = provider.get(key)
    if not frag:
        raise ToolError(f"记忆条目不存在：{key}")
    return {
        "key": str(frag.get("key") or key),
        "content": str(frag.get("content", "")),
        "tags": str(frag.get("tags", "")),
        "updated_at": str(frag.get("updated_at", "")),
    }


def memory_core_list(provider: Any, preview_chars: int = CORE_PREVIEW_CHARS) -> Dict[str, Any]:
    """常驻层（core tag）条数 + 每条预览 —— 与 P1 控制台 `GET /api/v1/core` 同源。"""
    preview_chars = max(1, min(int(preview_chars), 2000))
    rows = _list_core(provider)
    return {
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


def memory_add(provider: Any, text: str, tags: str = "") -> Dict[str, Any]:
    """写入一条记忆并返回它的 key。写层 fail-closed：失败抛错，不谎报成功。"""
    if not text or not text.strip():
        raise ToolError("text 不能为空")
    key = provider.add(text.strip(), tags=tags or "")
    if not key:
        raise ToolError("写入失败：provider 未返回 key（fail-closed）")
    return {"key": key, "tags": tags or ""}


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
# 注册到 FastMCP/MCPServer
# --------------------------------------------------------------------------

def register_tools(server: Any, provider: Any, gate: Any = None) -> Any:
    """把 8 个 tool 挂到 `server` 上。工具实现见本模块同名函数。

    `gate` 非 None 时（默认装配的 keepsake provider 才有），**每个 tool 调用前**
    先过一道身份闸门（`gate.check(工具名)`，未批准即 `ToolError`）——
    闸门负责按 read/write 打活动计数，所以它必须挂在 tool 入口，
    而不是塞进各工具函数体里（漏一处就是一个绕过闸门的入口）。
    """

    def _authorize(tool_name: str) -> None:
        if gate is not None:
            gate.check(tool_name)

    @server.tool(
        name="memory_capabilities",
        description="记忆后端能力声明（version / core_tier / knn / dry_run_merge）。",
    )
    def _capabilities() -> Dict[str, Any]:
        _authorize("memory_capabilities")
        return memory_capabilities(provider)

    @server.tool(
        name="memory_search",
        description="按查询检索记忆条目，返回 key / tags / score / 正文预览（截断 300 字）。",
    )
    def _search(query: str, limit: int = 10) -> Dict[str, Any]:
        _authorize("memory_search")
        return memory_search(provider, query, limit)

    @server.tool(
        name="memory_get",
        description="按 key 取一条记忆的全文；key 不存在则报错。",
    )
    def _get(key: str) -> Dict[str, Any]:
        _authorize("memory_get")
        return memory_get(provider, key)

    @server.tool(
        name="memory_core_list",
        description="列出常驻层（core）记忆：条数 + 每条预览。",
    )
    def _core_list(preview_chars: int = CORE_PREVIEW_CHARS) -> Dict[str, Any]:
        _authorize("memory_core_list")
        return memory_core_list(provider, preview_chars)

    @server.tool(
        name="memory_add",
        description="写入一条记忆，返回它的 key。",
    )
    def _add(text: str, tags: str = "") -> Dict[str, Any]:
        _authorize("memory_add")
        return memory_add(provider, text, tags)

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

    return server