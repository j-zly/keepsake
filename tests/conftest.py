"""测试 bootstrap：仅在脱离 Hermes 的独立环境生效。

正式运行环境（pip install keepsake 到 Hermes 镜像里）会从 `agent` /
`tools` 包拿到真实实现；本文件的存在只为让 `pytest` 在 CI / agent 工位
上能直跑 `import keepsake`。
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_stubs() -> None:
    if "agent" in sys.modules and "tools" in sys.modules:
        return

    # agent.memory_provider.MemoryProvider —— KeepsakeProvider 仅 super().__init__()
    agent_mod = types.ModuleType("agent")
    agent_mp_mod = types.ModuleType("agent.memory_provider")

    class _StubMemoryProvider:
        def __init__(self, *args, **kwargs):
            pass

    agent_mp_mod.MemoryProvider = _StubMemoryProvider
    agent_mod.memory_provider = agent_mp_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = agent_mp_mod

    # tools.registry.tool_error —— 调用点只把它当字符串返回用
    tools_mod = types.ModuleType("tools")
    tools_reg_mod = types.ModuleType("tools.registry")

    def _stub_tool_error(msg):
        return f"[tool_error] {msg}"

    tools_reg_mod.tool_error = _stub_tool_error
    tools_mod.registry = tools_reg_mod
    sys.modules["tools"] = tools_mod
    sys.modules["tools.registry"] = tools_reg_mod


_install_stubs()


def flat_routes(app) -> list:
    """把 app 的**全部**路由摊平（兼容 FastAPI ≥ 0.156 的懒 include_router）。

    🔴 FastAPI 0.156 起 `include_router` 不再把子路由摊进 `app.routes`，
    而是放一个 `_IncludedRouter` 壳 —— 只看 `app.routes` 会误判成「路由没挂上」
    （枚举写面路径的用例会集体假红）。这里两种版本都取全：壳里有
    `original_router` 就取它的 routes，没有就当普通路由。
    """
    out = []
    for r in getattr(app, "routes", []):
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out.extend(getattr(inner, "routes", []) or [])
        else:
            out.append(r)
    return out


@pytest.fixture(autouse=True)
def _reset_pg_snapshots():
    """每个用例前后清空 PG 检索快照（`_SNAPSHOTS`）。

    🔴 `storage_pg._SNAPSHOTS` 是**进程级**缓存（PG 侧检索热路径 TTL 快照）。
    用例里那些 fake PG 实例走的是同一个 `_target_key()`，于是一个用例灌进去的
    快照会让下一个用例的首次查询**不发那条 SQL**，而 fake 的返回序列是按
    「每次都发」编排的 ⇒ 表现为「候选集空了」这类看不懂的假红。

    真实进程里这不成立（缓存就该跨调用存活），所以清在 fixture 而不是改实现。
    """
    from keepsake.storage_pg import _SNAPSHOTS

    _SNAPSHOTS.clear()
    yield
    _SNAPSHOTS.clear()
