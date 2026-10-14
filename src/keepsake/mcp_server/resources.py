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

from .tools import ToolError

__all__ = ["register_resources"]


def register_resources(server: Any, provider: Any) -> Any:
    """把两个 resource 挂到 `server` 上。"""

    @server.resource(
        "memory://core",
        name="memory_core",
        description="常驻层（core）记忆全文 —— 与每轮注入上下文的那份渲染逐字相同。",
        mime_type="text/plain",
    )
    def core() -> str:
        return provider.system_prompt_block()

    @server.resource(
        "memory://fragment/{key}",
        name="memory_fragment",
        description="按 key 取单条记忆全文（JSON）。",
        mime_type="application/json",
    )
    def fragment(key: str) -> str:
        frag = provider.get(key)
        if not frag:
            raise ToolError(f"记忆条目不存在：{key}")
        return json.dumps(
            {"key": str(frag.get("key") or key), **dict(frag)},
            ensure_ascii=False,
            default=str,
        )

    return server