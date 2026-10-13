"""通用 MCP 服务入口：`build_server(provider) -> FastMCP`。

--------------------------------------------------------------------------
本模块**不绑任何宿主**（Hermes 只是恰好也能用它）
--------------------------------------------------------------------------
全文件零处 `import agent.*` / `import hermes`。它只认一个东西 ——
`keepsake.adapters.generic.protocol.MemoryProvider` 协议：

    system_prompt_block() / prefetch(query)          注入层（fail-open）
    search(query, limit) / get(key)                  检索层（显式抛错）
    add(text, tags) / update(...) / remove(key)      写层（fail-closed）
    merge(dry_run) / forget(dry_run)                 维护层
    capabilities()                                   能力声明
可选扩展 `list_core()` / `count_fragments()`（见 tools.StorageIntrospection）。

**别的 agent 怎么接入 —— 三行**：

    from keepsake.mcp_server.server import build_server
    server = build_server(MyMemoryProvider())        # MyMemoryProvider 实现上面那套协议
    server.run("stdio")                              # 任何 MCP 客户端（Claude Code / 别的 agent / 脚本）连上就能用

默认装配（`build_server()` 不传参）用 keepsake 自己的 provider，见 `factory.py`。

--------------------------------------------------------------------------
传输
--------------------------------------------------------------------------
`stdio` 是默认传输（本地客户端拉起子进程的方式）。
`http`（streamable-http）**只听 127.0.0.1**，默认端口 8210；
绑非回环地址一律拒绝启动 —— 记忆库没有它自己那套认证，
暴露到网络上等于把一个人的长期记忆公开读。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional

import anyio
from mcp.server.fastmcp import FastMCP

from .factory import default_provider
from .resources import register_resources
from .tools import register_tools

__all__ = ["build_server", "main", "DEFAULT_HTTP_HOST", "DEFAULT_HTTP_PORT"]

#: 🔴 HTTP 传输只听回环地址。不给「--host 0.0.0.0」留口子：
#: 要暴露到外面走反向代理 + 控制台那套 Bearer 认证，那是另一条路的职责。
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8210
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

SERVER_NAME = "keepsake-memory"

INSTRUCTIONS = (
    "keepsake 记忆系统。检索用 memory_search，取全文用 memory_get 或 "
    "resource memory://fragment/{key}，常驻记忆在 resource memory://core。"
    "删除必须显式 confirm；merge/forget 默认 dry-run，只统计不写。"
)


def _run_detached(call: Callable[[], Any]) -> Any:
    """把一个 async 方法跑完并同步取回结果 —— **绝不碰调用方的事件循环**。

    原实现是裸 `anyio.run(...)`：宿主进程里只要自己跑着事件循环
    （async 测试、async MCP 客户端、被别的框架托管的 agent），就会抛
    `RuntimeError: Already running asyncio in this thread`。
    而「别的 agent 实现协议就能用」这条承诺要求**进程内**也能安全调用，
    所以这条路径不能留给运气：

      · 本线程没有事件循环 ⇒ 直接 `anyio.run`（零开销，快路径）；
      · 本线程已有事件循环 ⇒ 另起工作线程，在那个线程里跑自己的事件循环。
        跨线程是唯一真安全的隔离方式 —— 换成别的 run API 名字没区别。

    `ponytail:` 工作线程每次枚举现开现关（枚举是探针/脚本路径，不是热路径）。
    升级路径：真热路径可换成线程池复用，或让调用方直接 `await`（`__await__` 已在）。
    """
    async def _call() -> Any:
        return await call()

    try:
        asyncio.get_running_loop()
    except RuntimeError:          # 同步上下文 —— 常见情况，直接就地跑
        return anyio.run(_call)
    with ThreadPoolExecutor(max_workers=1) as pool:   # 宿主进程内：换线程换 loop
        return pool.submit(lambda: anyio.run(_call)).result()


def _as_str_uri(items: List[Any], enabled: bool) -> List[Any]:
    """`str_uri=True`（只给 `list_resources`）：SDK 把 `Resource.uri` 建模成
    pydantic `AnyUrl`，它不是 str，做不了 `startswith` / 字符串排序 ——
    而 JSON-RPC 上线时 uri 本来就是一个字符串。同一个模型对象（`model_copy`），
    只把该字段落成 str；async 协议路径拿到的仍是 SDK 原样。
    """
    if not enabled:
        return items
    return [it.model_copy(update={"uri": str(it.uri)}) if not isinstance(it.uri, str) else it
            for it in items]


class _LazyEnum(list):
    """枚举结果：**既是 list（同步视图）又能 `await`（异步协议）**。

    惰性求值 —— 拿到对象时什么都不跑：

      · 异步（`await server.list_resources()`）⇒ 走 SDK 原生 async 方法，
        拿到 SDK 原样结果，宿主进程里零桥接；
      · 同步（遍历 / `len` / 取下标 / `in`）⇒ 首次访问时才求值，
        且用 `_run_detached` 保证本线程有事件循环时也不炸。

    继承 `list` 是刻意的：探针、单测、脚本照旧当普通列表用，
    而 `await` 拿到的也是真 list（`_run` 里返回的是 SDK 自己的 list）。
    """

    __hash__ = None      # 与 list 一致：list 不可 hash，别假装能

    def __init__(self, call: Callable[[], Any], *, str_uri: bool = False) -> None:
        super().__init__()
        self._call = call
        self._str_uri = str_uri
        self._loaded = False

    # ---- 异步：SDK 原生路径 ----

    def __await__(self):
        async def _run() -> List[Any]:
            return _as_str_uri(await self._call(), self._str_uri)

        return _run().__await__()

    # ---- 同步：首次访问才求值 ----

    def _load(self) -> "_LazyEnum":
        if not self._loaded:
            list.extend(self, _as_str_uri(_run_detached(self._call), self._str_uri))
            self._loaded = True      # 只在成功后才置位：空结果也不会反复重跑
        return self

    def __iter__(self):
        self._load()
        return list.__iter__(self)          # 绕开本类的 __iter__，否则自递归

    def __len__(self) -> int:
        self._load()
        return list.__len__(self)

    def __getitem__(self, idx):
        self._load()
        return list.__getitem__(self, idx)

    def __contains__(self, value: Any) -> bool:
        self._load()
        return list.__contains__(self, value)

    def __bool__(self) -> bool:
        self._load()
        return list.__len__(self) > 0

    def __eq__(self, other: Any) -> bool:
        self._load()
        if isinstance(other, _LazyEnum):
            other._load()
        return list.__eq__(self, other)

    def __repr__(self) -> str:
        self._load()
        return repr(list(self))

    def count(self, *args: Any) -> int:
        self._load()
        return list.count(self, *args)

    def index(self, *args: Any) -> int:
        self._load()
        return list.index(self, *args)


def _sync_view(fn: Any, *, str_uri: bool = False) -> Any:
    """把 SDK 的 async 枚举方法包成「同步 + 异步都能用」的视图。

    `str_uri=True` 只给 `list_resources`（见 `_as_str_uri`）。
    """

    def view(*args: Any, **kwargs: Any) -> _LazyEnum:
        return _LazyEnum(functools.partial(fn, *args, **kwargs), str_uri=str_uri)

    view.__doc__ = fn.__doc__
    view.__name__ = f"sync_{getattr(fn, '__name__', 'view')}"
    return view


def build_server(provider: Optional[Any] = None) -> Any:
    """构造挂好 8 个 tool + 2 个 resource 的 MCP 服务。

    `provider` 省略时用 keepsake 默认装配（`factory.default_provider()`，
    按 `KEEPSAKE_CONFIG` / `~/.config/keepsake/config.json` 连接）。
    传入自己的 `MemoryProvider` 实现即可让任意宿主复用同一套工具面。

    返回值就是 SDK 的 FastMCP/MCPServer 本体（不是包装类），
    另有两处便利改造：SDK 的 `list_tools` / `list_resources` 是 `async def`
    （协议层用），这里换成 `_LazyEnum` 视图（并把 resource 的 `uri` 落成 str），
    让探针、单测、脚本不必起事件循环就能看清单，**而 `await` 依然可用**
    ——宿主进程内（已经在事件循环里）调它不会炸。改的是**实例属性**：
    SDK 在 `__init__` 里已经把原绑定方法注册给底层 server，协议行为一字不变。
    """
    if provider is None:
        provider = default_provider()
    server = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)
    register_tools(server, provider)
    register_resources(server, provider)
    server.list_tools = _sync_view(FastMCP.list_tools.__get__(server))
    server.list_resources = _sync_view(FastMCP.list_resources.__get__(server), str_uri=True)
    return server


def main(argv: Optional[list] = None) -> int:
    """CLI 入口：`python -m keepsake.mcp_server [--transport stdio|http]`（见 `__main__.py`）。"""
    parser = argparse.ArgumentParser(
        prog="keepsake-mcp", description="keepsake 通用 MCP 服务（stdio / http）"
    )
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="stdio=本地客户端拉起子进程（默认）；http=只听 127.0.0.1",
    )
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST, help="http 传输监听地址（只允许回环）")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="http 传输监听端口")
    args = parser.parse_args(argv)

    if args.transport == "http" and args.host not in _LOOPBACK_HOSTS:
        parser.error(
            f"http 传输只允许监听 {_LOOPBACK_HOSTS}，收到 {args.host!r}"
            "（记忆库无自带认证，不许直接暴露到网络上）"
        )

    server = build_server()
    if args.transport == "stdio":
        # stdio 传输下 stdout 就是协议通道：任何 print/log 都会污染协议流。
        server.run("stdio")
    else:
        # SDK 的 http 传输从 `server.settings` 读监听地址，不接受 run() 参数。
        server.settings.host = args.host
        server.settings.port = args.port
        server.run("streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())