"""MCP 资源面：`memory://core` 与 `memory://fragment/{key}`。

只经 `MemoryProvider` 协议读，不自建存储、不复制渲染逻辑。

* `memory://core` —— 常驻层全文。直接取 `provider.system_prompt_block()`：
  那**就是**每轮注入上下文用的同一份渲染（core 层块 `<keepsake-core>…`）。
  自己再拼一份渲染 = 两处口径迟早分叉，而分叉的那份没人会发现。
* `memory://fragment/{key}` —— 单条全文（JSON）。key 不存在抛 `ToolError`。
"""

from __future__ import annotations

import json
from typing import Any

from .tools import ToolError, _caller_kwargs

__all__ = ["register_resources"]


def register_resources(server: Any, provider: Any, gate: Any = None) -> Any:
    """把两个 resource 挂到 `server` 上。

    `gate` 非 None 时按**调用方身份**收窄可见范围（2026-10 ks_share_memory）：
    非 primary 只拿得到 primary 的 core + 已共享代理的 core（与检索同口径）。
    身份判定走闸门那条链（凭据反查），**不抛、不记账** —— resource 不是工具，
    没有「未批准即拒绝」那道闸（闸门只挂在 tool 入口）。
    """

    def _caller() -> Dict[str, Any]:
        if gate is None:
            return {}
        try:
            return dict(gate.identify("memory_core") or {})
        except Exception:      # noqa: BLE001 —— 判不出身份就按「本进程自己」处理
            return {}

    @server.resource(
        "memory://core",
        name="memory_core",
        description="常驻层（core）记忆全文 —— 与每轮注入上下文的那份渲染逐字相同。",
        mime_type="text/plain",
    )
    def core() -> str:
        ident = _caller()
        kwargs = _caller_kwargs(provider.system_prompt_block,
                               str(ident.get("agent_id") or ""),
                               str(ident.get("role") or ""))
        return provider.system_prompt_block(**kwargs)

    @server.resource(
        "memory://fragment/{key}",
        name="memory_fragment",
        description="按 key 取单条记忆全文（JSON）。",
        mime_type="application/json",
    )
    def fragment(key: str) -> str:
        ident = _caller()
        kwargs = _caller_kwargs(provider.get, str(ident.get("agent_id") or ""),
                               str(ident.get("role") or ""))
        frag = provider.get(key, **kwargs)
        if not frag:
            raise ToolError(f"记忆条目不存在：{key}")
        return json.dumps(
            {"key": str(frag.get("key") or key), **dict(frag)},
            ensure_ascii=False,
            default=str,
        )

    return server