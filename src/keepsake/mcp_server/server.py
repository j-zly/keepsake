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

http 传输上**另挂一条 SSE 长连接**（`GET /events`，同进程同端口）：
私信实时推送的快车道（别人给我投信 ⇒ 顺这条流推一帧**元数据**，
**不推正文**）。见 `events.py`。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import anyio
from mcp.server.fastmcp import FastMCP

from ..govern.registry import CREDENTIAL_HINT_LEN, is_connection_error
from .events import register_events_route
from .factory import DEFAULT_AGENT_ID, default_provider
from .resources import register_resources
from .tools import ToolError, register_tools

__all__ = ["build_server", "main", "AgentGate", "GATE_DENIED",
           "DEFAULT_HTTP_HOST", "DEFAULT_HTTP_PORT"]

logger = logging.getLogger("keepsake.mcp_server")

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

#: 代理互聊信箱的用法说明（**什么时候用信箱，而不是 memory_add**）。
#:
#: 🔴 两条路各有各的语义，混用会把「传话」变成「广播」：
#:   * `memory_add` = 往**自己的记忆**里写一条（检索时能被自己召回）；
#:   * `message_send` = 给**另一个代理**投一封信（对方收信箱里才看得到，
#:     需要对方**即时响应**时才用；不需要即时响应就别投信箱，写进记忆更合适）。
MESSAGING_HINT = (
    "代理互聊信箱：message_send 给另一个已批准代理投私信（thread_id 留空会自动生成并回显，"
    "回复时带上它续同一段对话），message_inbox 取收件箱（取走即算已读），"
    "message_recent 看与某个代理的双向最近消息。"
    "只有在「需要对方即时响应」时才投信箱；只是要自己以后能检索到的内容，写 memory_add。"
)

#: 治理面只在 `build_server(govern=True)` 时存在 —— 那时才把它写进 instructions，
#: 免得模型按一段并不存在的工具说明去调（govern=False 时那段话是假的）。
GOVERN_HINT = (
    "治理面 agent_list/agent_approve/agent_revoke/agent_set_role/agent_set_exclude/"
    "agent_set_share 只对注册表里的 primary 代理开放，与控制台同一套操作。"
)


# --------------------------------------------------------------------------
# 代理闸门（2026-10 ks_agent_registry）
#
# 此前 MCP 是**无认证**的：任何能连上 stdio/http 的客户端都能读写主脑的记忆，
# 且身份全凭它自己的 config 自报。本节把「能不能用」改成注册表说了算：
# 新代理接入默认 pending，读写全拒，管理员在控制台批准并领取 `ksk_` 凭据后才放行。
# --------------------------------------------------------------------------

#: 写类工具（其余算读）：决定 `touch(read|write)` 的口径。
#: `agent_*` 治理写口自然算写 —— 批准/吊销/改角色/改上下文开关都动注册表。
#: `message_*` 三个信箱工具**也算写**：收信即消费（标已读）是状态变更，不是纯读。
GATE_WRITE_TOOLS = frozenset({
    "memory_add", "memory_remove", "memory_merge", "memory_forget",
    "agent_approve", "agent_revoke", "agent_set_role", "agent_set_exclude",
    "agent_set_share",
    "message_send", "message_inbox", "message_recent",
})

#: stdio 的凭据复核周期（秒）。吊销 / 停用最多 60s 生效。
GATE_RECHECK_S = 60.0

#: 未批准 / 凭据不对时的**唯一**对外文案（🔴 不回显任何凭据内容）。
GATE_DENIED = "代理 {agent_id} 未批准/已吊销，请到控制台审核通过后领取凭据"

# --------------------------------------------------------------------------
# 🔴 拒绝文案**三分法**（2026-10-20 用户本机误判两小时的根因）
#
# 此前三种情况都回显成「代理 {agent_id} 未批准」：没带凭据 / 凭据无效 / 代理未批准
# —— 而「反查不到人时 agent_id 回退进程默认名 main-brain」，于是真因（客户端
# **没带 Authorization 头**）被读成「main-brain 没批准」，白查两小时。
# 现在按 `verify_any` 的 reason 各回各话：
# --------------------------------------------------------------------------

#: 没带凭据（客户端 MCP 配置里缺 Authorization 头）
GATE_DENIED_NO_CREDENTIAL = "请求未带凭据（检查客户端 MCP 配置的 Authorization 头）"

#: 凭据无效 / 已换发（旧的那条已经作废）
GATE_DENIED_UNKNOWN_AGENT = "凭据无效或已更换（到控制台重新换发）"

#: 未批准 / 已吊销 —— 保持原措辞，外加**真实反查线索**（凭据前 8 位，hint 级）
GATE_DENIED_NOT_APPROVED = (
    "代理 {agent_id} 未批准/已吊销，请到控制台审核通过后领取凭据"
    "（凭据前 8 位 {hint} 未匹配到已批准代理）"
)

#: 注册表**暂时读不到**（PG 抖动 / 池借不到连接）—— 与「查无此人」分开说
GATE_DENIED_UNAVAILABLE = "服务端暂时读不到注册表（PG 抖动），请几十秒后重试"

#: `verify_any` / `verify` 的 reason → 拒绝文案。
#: 🔴 每一类各回各话：**不许**把「没带凭据」「凭据无效」「未批准」「服务端读不到」
#:   揉成同一句 —— 那正是让用户误判两小时的写法。
GATE_DENIED_BY_REASON = {
    "no_credential": GATE_DENIED_NO_CREDENTIAL,
    "unknown_agent": GATE_DENIED_UNKNOWN_AGENT,
    "bad_credential": GATE_DENIED_UNKNOWN_AGENT,
    "not_approved": GATE_DENIED_NOT_APPROVED,
    "registry_unavailable": GATE_DENIED_UNAVAILABLE,
}


def gate_denied_text(reason: str, agent_id: str = "", credential_hint: str = "") -> str:
    """按 reason 生成拒绝文案（🔴 只回显 hint 级信息，绝不回显完整凭据）。

    未在表里的 reason（`not_local` / `verify_error` / 空）⇒ 回落**通用**那句
    （保持旧行为，不给陌生 reason 编新话）。
    """
    key = str(reason or "")
    if key == "not_approved":
        return GATE_DENIED_NOT_APPROVED.format(
            agent_id=agent_id or "-", hint=credential_hint or "-")
    return GATE_DENIED_BY_REASON.get(key) or GATE_DENIED.format(agent_id=agent_id or "-")

#: 环境变量名（stdio 客户端启动时带；`KEEPSAKE_AGENT_ID` 缺省仍是 `DEFAULT_AGENT_ID`）
ENV_AGENT_ID = "KEEPSAKE_AGENT_ID"
ENV_CREDENTIAL = "KEEPSAKE_CREDENTIAL"


class AgentGate:
    """每次 tool 调用的身份闸门 —— 判定源是注册表，不是客户端自报。

    **http 传输**：每次调用从请求头取 `Authorization: Bearer ksk_…` → `verify_any`
    （头里没有 agent_id，按凭据反查）；失败即 `ToolError`。

    **stdio 传输**：启动时校验 `KEEPSAKE_CREDENTIAL`，之后每 60s 复核；期间任何一次
    复核失败 ⇒ **全部工具降级为拒绝**（不退出进程 —— 宿主会反复重启刷屏，人话错误
    留在工具返回里比一个反复重启的进程有用）。

    🟡 **stdio 没配凭据时的兜底**：退到 `verify_local()`（本机文件通道）——
    即「与主脑同机同配置、且注册表里那行带 `meta.bootstrap=local`」。这条兜底的
    边界写在 `tests/test_agent_registry.py` 与闸门文件里：它防的是**远端/陌生
    客户端**，不是同机同配置（那种进程本来就能直连库，闸门对它只是仪式）。
    `ponytail:` 要把本机也卡死，设 `KEEPSAKE_CREDENTIAL` 即可（配了就只认凭据）。
    """

    def __init__(self, registry: Any, server: Any, *,
                 agent_id: str = "", credential: str = "") -> None:
        self._registry = registry
        #: 注册表公开句柄：治理工具面（`tools.agent_*`）与测试/探针用它取同一个
        #: Registry 实例 —— 工具因此不必再去 provider 上摸私有属性。
        self.registry = registry
        self._server = server
        self.agent_id = agent_id
        self._credential = credential
        self._stdio_ok: Optional[bool] = None      # None = 还没复核过
        self._stdio_res: Dict[str, Any] = {}       # 上次复核的**完整**结论（含 role/status）
        self._stdio_at = 0.0                       # 上次复核的单调时刻
        self._stop = threading.Event()

    @classmethod
    def for_provider(cls, provider: Any, server: Any) -> Optional["AgentGate"]:
        """按 provider 装配闸门；**没有注册表就没有闸门**（见下方说明）。

        「别的 agent 实现协议就能用」是本模块的承诺：一个自带记忆系统、自己不碰
        keepsake 存储的 provider（如 `tests/mcp` 里的 `_ForeignProvider`）不该被
        keepsake 的注册表拦住 —— 它压根没有身份可言。
        """
        registry = getattr(provider, "_registry", None)
        if registry is None:
            return None
        agent_id = (os.environ.get(ENV_AGENT_ID)
                    or getattr(provider, "_agent_id", "")
                    or DEFAULT_AGENT_ID)
        gate = cls(registry, server, agent_id=agent_id,
                   credential=(os.environ.get(ENV_CREDENTIAL) or "").strip())
        gate.start()
        return gate

    # ---- 复核（stdio） ------------------------------------------------
    def _recheck(self) -> bool:
        """复核一次并记住结论与时刻（异常一律按未通过 —— fail-closed）。"""
        try:
            res = dict(self._stdio_verify() or {})
        except Exception as e:      # noqa: BLE001
            logger.warning("keepsake MCP: 凭据复核异常（按拒绝处理）: %s", type(e).__name__)
            # 🔴 连接类异常（PG 抖动）**不与「查无此人」混同一句**：
            #   前者重试几十秒就好，后者要重新换发凭据 —— 混了就会把人引到错的方向。
            res = {"ok": False, "role": "", "status": "unknown",
                   "reason": ("registry_unavailable" if is_connection_error(e)
                              else "verify_error")}
        res["ok"] = bool(res.get("ok"))
        self._stdio_res = res
        self._stdio_ok, self._stdio_at = res["ok"], time.monotonic()
        return res["ok"]

    def start(self) -> None:
        """启动时校验一次，并起一个 daemon 线程每 60s 复核。"""
        self._recheck()
        if not self._stdio_ok:
            logger.error("keepsake MCP: 代理 %s 尚未在注册表里批准 —— "
                         "全部工具将被拒绝（到控制台批准并领取 ksk_ 凭据）", self.agent_id)

        def _loop() -> None:
            while not self._stop.wait(GATE_RECHECK_S):
                self._recheck()

        threading.Thread(target=_loop, name="keepsake-agent-gate", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---- 判定 --------------------------------------------------------
    def _stdio_verify(self) -> Dict[str, Any]:
        """stdio 侧的校验：有凭据走凭据，没有就退本机通道。"""
        if self._credential:
            return self._registry.verify(self.agent_id, self._credential)
        return self._registry.verify_local(self.agent_id)

    def _http_headers(self) -> Any:
        """当前调用的 HTTP 请求头；**stdio 下返回 None**（没有请求上下文）。"""
        try:
            request = self._server.get_context().request_context.request
        except Exception:       # noqa: BLE001 —— 请求上下文不存在 ⇒ 非 http 调用
            return None
        return getattr(request, "headers", None)

    @staticmethod
    def _bearer(headers: Any) -> str:
        raw = ""
        try:
            raw = headers.get("authorization") or headers.get("Authorization") or ""
        except Exception:       # noqa: BLE001 —— 畸形头当没带
            return ""
        raw = str(raw).strip()
        return raw[len("Bearer "):].strip() if raw.startswith("Bearer ") else ""

    def _verify_any(self, credential: str) -> Dict[str, Any]:
        """按凭据反查身份；**连接类异常** ⇒ 「服务端暂时读不到」，不与「查无此人」混同。"""
        try:
            return dict(self._registry.verify_any(credential))
        except Exception as e:      # noqa: BLE001 —— 分类后按类说话
            if not is_connection_error(e):
                raise
            logger.warning("keepsake MCP: 注册表暂时读不到（%s: %s）", type(e).__name__, e)
            return {"ok": False, "role": "", "status": "unknown",
                    "reason": "registry_unavailable", "agent_id": ""}

    def identify(self, tool_name: str = "") -> Dict[str, Any]:
        """**本次调用的身份判定结论**（不抛、不记账）：`{ok, agent_id, role, status, reason}`。

        这是身份链的**唯一**入口：`check()` 与治理工具面（`tools.agent_*`）都走它，
        谁都不许在工具里再解一遍凭据。分工是「判定」与「放行」：

        * `check` —— 判定 + 未放行即抛 `ToolError` + 打活动（8 个记忆工具的入口）；
        * `identify` —— 只给结论，让治理工具自己去判 `role == primary` 并回自己的文案
          （pending 与「已批准但不是主」要能分开说，见 `tools.GOVERN_DENIED`）。

        复核期已到就在本次调用里顺带复核一次：吊销/停用的生效延迟因此恒 ≤
        `GATE_RECHECK_S`（哪怕工具一直在被调、复核线程还没轮到）。
        """
        headers = self._http_headers()
        credential = ""
        if headers is not None:
            # http：头里只有 Bearer，身份由凭据反查（`verify_any`）—— 不采信客户端
            # 自报的 agent_id，那正是这套治理要拔掉的东西。
            credential = self._bearer(headers)
            res = self._verify_any(credential)
            # 🔴 反查不到人时**不回退**进程默认名（`main-brain`）——
            #   2026-10-20 用户本机据此误判两小时（真因是请求没带 Authorization 头）。
            if not res.get("agent_id"):
                res["agent_id"] = ""
        else:
            if self._stdio_ok is None or time.monotonic() - self._stdio_at >= GATE_RECHECK_S:
                self._recheck()
            res = dict(self._stdio_res)
            credential = self._credential
            res["agent_id"] = str(res.get("agent_id") or self.agent_id)
        # 🔴 只带**前 8 位**（hint 级）：够人比对「我手上这条对不对」，
        #   绝不足以当凭据用，也绝不回显完整凭据。
        res["credential_hint"] = str(credential or "")[:CREDENTIAL_HINT_LEN]
        return res

    def denied_text(self, res: Dict[str, Any]) -> str:
        """按 reason 生成拒绝文案（记忆面与治理面共用这一份，别处不再解一遍）。"""
        return gate_denied_text(
            str(res.get("reason") or ""),
            agent_id=str(res.get("agent_id") or ""),
            credential_hint=str(res.get("credential_hint") or ""),
        )

    def record_activity(self, caller: str, tool_name: str) -> None:
        """按工具口径打活动（`check` 与治理工具面共用的记账点）。"""
        self._registry.touch(caller, "write" if tool_name in GATE_WRITE_TOOLS else "read")

    def check(self, tool_name: str) -> Dict[str, Any]:
        """tool 调用前的唯一入口：通过则打活动并放行，不通过抛 `ToolError`（人话）。"""
        res = self.identify(tool_name)
        if not res.get("ok"):
            raise ToolError(self.denied_text(res))
        self.record_activity(res["agent_id"], tool_name)
        return res


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


def build_server(provider: Optional[Any] = None, *, govern: bool = False) -> Any:
    """构造挂好 8 个记忆 tool（+ 2 个 resource，可选再加 6 个治理 tool）的 MCP 服务。

    `provider` 省略时用 keepsake 默认装配（`factory.default_provider()`，
    按 `KEEPSAKE_CONFIG` / `~/.config/keepsake/config.json` 连接）。
    传入自己的 `MemoryProvider` 实现即可让任意宿主复用同一套工具面。

    🔴 `govern=True` 才挂 `agent_*` 六个治理工具（列代理/批准/吊销/改角色/exclude/共享，
    **只对注册表里 role=primary 的调用方放行**，见 `tools._govern_identity`）。
    默认关闭是**兼容性**决定：那 5 个工具是本文件对外承诺过的 8 工具契约之外的新面，
    对「把工具面枚举出来喂给模型的宿主」是可见的破坏性变更 —— 宿主若要治理面，
    显式开一个参数即可。CLI 入口 `main()` 恒开（治理是主脑的日常动作）。
    没开 `govern` 时治理面**不存在**（而不是存在却默默全拒），免得调用方
    以为治理可用。

    默认装配的 provider 带代理注册表 ⇒ 工具面挂上 `AgentGate`（每次调用按注册表
    判身份）。传入的第三方 provider 没有注册表 ⇒ **不挂闸门**（它压根不碰 keepsake
    的存储，见 `AgentGate.for_provider`）。

    返回值就是 SDK 的 FastMCP/MCPServer 本体（不是包装类），
    另有两处便利改造：SDK 的 `list_tools` / `list_resources` 是 `async def`
    （协议层用），这里换成 `_LazyEnum` 视图（并把 resource 的 `uri` 落成 str），
    让探针、单测、脚本不必起事件循环就能看清单，**而 `await` 依然可用**
    ——宿主进程内（已经在事件循环里）调它不会炸。改的是**实例属性**：
    SDK 在 `__init__` 里已经把原绑定方法注册给底层 server，协议行为一字不变。
    """
    if provider is None:
        provider = default_provider()
    server = FastMCP(name=SERVER_NAME,
                     instructions=INSTRUCTIONS + MESSAGING_HINT
                     + (GOVERN_HINT if govern else ""))
    gate = AgentGate.for_provider(provider, server)
    register_tools(server, provider, gate=gate, govern=govern)
    register_resources(server, provider, gate=gate)
    # 私信实时推送的 SSE 端点（`GET /events`）—— 与 streamable-http **同进程同端口**。
    # 建流要过三道闸（凭据反查 → approved → push_enabled=1），见 `events.py`。
    register_events_route(server, gate)
    # 闸门句柄挂在 server 上（探针 / 运维脚本看「现在放不放行」用；工具调用本身
    # 走 `gate.check`，不经过这个属性 —— 改它不会放松任何一道闸）。
    server.agent_gate = gate
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

    server = build_server(govern=True)      # 治理面在 CLI 入口恒开（见 build_server 的 govern）
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