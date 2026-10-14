"""MCP 服务契约测试（`keepsake_p4_mcp_generic_v1`）。

覆盖三件事：
  ① **工具面** —— 8 个 tool + 2 个 resource 齐全，名字与任务书逐字一致；
  ② **写语义** —— dry_run 是一等公民、删除/实跑必须 confirm、失败抛结构化错误；
  ③ **不绑宿主** —— 源码零处 `import agent|hermes`，且换一个自造的
     `MemoryProvider` 实现照样能跑通（这就是「别的 agent 怎么接入」的证据）。

全程 hermetic：`tmp_path` 里的临时 SQLite，不连 Redis / PG / 网络 / 生产库。
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any, Dict, List

import anyio
import pytest

from keepsake.adapters.generic.capabilities import Capabilities
from keepsake.mcp_server.server import build_server, main
from keepsake.mcp_server.tools import ToolError

#: 任务书给定的工具面，一个不多一个不少
EXPECTED_TOOLS = {
    "memory_capabilities", "memory_search", "memory_get", "memory_core_list",
    "memory_add", "memory_remove", "memory_merge", "memory_forget",
}


# ---------------------------------------------------------------------------
# 夹具：临时 sqlite 配置 + keepsake 默认装配
# ---------------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path, monkeypatch):
    """真 SqliteStorage（tmp 文件）+ `build_server()` 默认装配。

    `KEEPSAKE_CONFIG` 指到临时目录 —— 本文件绝不连生产库。
    `provider._CORE_CACHE` 是**进程级** 60s 缓存，用例间不共享（否则上一例的
    core 快照会污染下一例的 `memory://core`）。
    """
    # is_primary=true：MCP 是**主脑**的记忆入口，生产 config 顶层键就是 true。
    # 2026-10 写闸（sqlite.py + provider.py）按它判身份 —— 缺省 false ⇒ 非 primary
    # 写 core 被降成 core_candidate，下面那条同源用例就会看到 0 条常驻。
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "mcp.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})
    s = build_server()
    yield s
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})


def call(server, name: str, **arguments: Any) -> Dict[str, Any]:
    """同步调一个 MCP tool，返回结构化结果。

    FastMCP 的 `convert_result=True` 会把 dict 包成 `{"result": {...}}`，
    这里剥掉那层包装 —— 调用方要的是 tool 自己的返回值。
    ToolError 原样往外抛（MCP 协议层会把它转成 isError 响应，不是 ok）。
    """
    _, structured = anyio.run(functools.partial(server.call_tool, name, arguments))
    return structured["result"]


def read_resource(server, uri: str) -> str:
    contents = list(anyio.run(functools.partial(server.read_resource, uri)))
    return "".join(getattr(c, "content", "") for c in contents)


# ---------------------------------------------------------------------------
# 一个与 keepsake 完全无关的 MemoryProvider 实现（证明「不绑宿主」）
# ---------------------------------------------------------------------------

class _ForeignProvider:
    """另一个 agent 自己的记忆系统：内存 dict，零依赖。

    结构上满足 `MemoryProvider` 契约（另加两个可选扩展），用来证明
    「实现协议 → build_server(my_provider) → 起 stdio」这条路真的通。
    """

    def __init__(self) -> None:
        self._frags: Dict[str, Dict[str, Any]] = {}
        self._n = 0

    # 注入层
    def system_prompt_block(self) -> str:
        return "<foreign-core>" + "|".join(f["content"] for f in self._frags.values())

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    # 检索层
    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        return [dict(f, _combined_score=1.0)
                for f in list(self._frags.values())[:limit] if query in f["content"]]

    def get(self, key: str):
        return self._frags.get(key)

    # 写层
    def add(self, text: str, *, tags: str = "", meta=None) -> str:
        self._n += 1
        key = f"foreign:{self._n}"
        self._frags[key] = {"key": key, "content": text, "tags": tags}
        return key

    def update(self, key: str, *, content=None, tags=None) -> bool:
        if key not in self._frags:
            return False
        if content is not None:
            self._frags[key]["content"] = content
        if tags is not None:
            self._frags[key]["tags"] = tags
        return True

    def remove(self, target: str) -> bool:
        return self._frags.pop(target, None) is not None

    # 维护层
    def merge(self, *, dry_run: bool = True) -> Dict[str, Any]:
        return {"status": "ok", "merged": 0, "groups": 0}

    def forget(self, *, dry_run: bool = True) -> Dict[str, Any]:
        return {"status": "ok", "deleted": 0}

    def capabilities(self) -> Capabilities:
        return Capabilities(core_tier=True, knn=False, dry_run_merge=True, version="v1")

    # 可选扩展（tools.StorageIntrospection）
    def list_core(self) -> List[Dict[str, Any]]:
        return [dict(f) for f in self._frags.values()]

    def count_fragments(self) -> int:
        return len(self._frags)


# ---------------------------------------------------------------------------
# ① 工具面
# ---------------------------------------------------------------------------

def test_工具面完整_八个tool(server):
    assert EXPECTED_TOOLS <= {t.name for t in server.list_tools()}


def test_资源面_常驻层与单条模板(server):
    """`list_resources()` 同步视图把 `uri` 落成 str（SDK 原型是 pydantic AnyUrl）——
    字符串操作能直接用；async 协议路径仍拿 SDK 原样。"""
    uris = [r.uri for r in server.list_resources()]
    assert "memory://core" in uris
    assert all(isinstance(u, str) and u.startswith("memory://") for u in uris)
    templates = anyio.run(server.list_resource_templates)
    assert "memory://fragment/{key}" in {str(t.uriTemplate) for t in templates}


def test_宿主进程内枚举不炸_await路径(server):
    """🔴 回归：宿主已经有事件循环时（async 客户端 / async 测试框架托管），
    枚举资源**不许**抛 `Already running asyncio in this thread`。

    这条承诺的依据是「别的 agent 实现 MemoryProvider 就能用」—— 那些 agent
    几乎都在自己的事件循环里直接 `await`，不是在同步上下文里探针一下。
    """
    async def main():
        res = await server.list_resources()
        tmpl = await server.list_resource_templates()
        return ([str(r.uri) for r in res], [str(t.uriTemplate) for t in tmpl])

    uris, templates = anyio.run(main)
    assert uris == ["memory://core"]
    assert templates == ["memory://fragment/{key}"]


def test_宿主进程内枚举不炸_同步路径(server):
    """同一件事的另一半：宿主在事件循环里**同步**读清单（忘了 await）也不炸。

    同步视图不许「换个 API 名字就完事」——真隔离是另起线程跑自己的 loop。
    """
    async def main():
        return ([r.uri for r in server.list_resources()], len(server.list_tools()))

    uris, tool_count = anyio.run(main)
    assert uris == ["memory://core"]
    assert tool_count == len(EXPECTED_TOOLS)


def test_模块可执行_非回环地址拒绝启动():
    """🔴 `python -m keepsake.mcp_server` 必须真的起得来（包里有 `__main__`），
    且绑非回环地址时退出码非 0。子进程里不带任何存储配置 —— 参数校验发生在
    建 provider 之前，所以本用例不连任何库。"""
    import os
    import subprocess
    import sys

    import keepsake

    root = pathlib.Path(keepsake.__file__).parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "keepsake.mcp_server",
         "--transport", "http", "--host", "0.0.0.0", "--port", "8299"],
        capture_output=True, text=True, cwd=str(root), env=env,
    )
    assert r.returncode != 0, r.stdout
    assert "0.0.0.0" in r.stderr and "127.0.0.1" in r.stderr


def test_不绑宿主_源码零处导入agent或hermes():
    """🔴 硬约束 1：`mcp_server/**` 里不许 import 任何宿主的东西。"""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "keepsake" / "mcp_server"
    hits = []
    for py in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import agent", "from agent",
                                    "import hermes", "from hermes")):
                hits.append(f"{py.name}:{lineno}: {stripped}")
    assert hits == []


def test_http只允许回环地址(capsys):
    """🔴 硬约束 3：记忆库无自带认证，不许绑 0.0.0.0。"""
    with pytest.raises(SystemExit):
        main(["--transport", "http", "--host", "0.0.0.0"])
    err = capsys.readouterr().err
    assert "0.0.0.0" in err and "127.0.0.1" in err


# ---------------------------------------------------------------------------
# ② 写语义与端到端（真 SqliteStorage）
# ---------------------------------------------------------------------------

def test_能力声明_返回契约字段(server):
    caps = call(server, "memory_capabilities")
    assert caps["version"] == "v1"
    assert caps["core_tier"] is True and caps["dry_run_merge"] is True
    assert caps["core_listing"] is True


def test_写入后能搜到并取回全文(server):
    key = call(server, "memory_add", text="MCP 契约探针", tags="probe,mcp")["key"]
    assert key.startswith("memory:frag:")

    found = call(server, "memory_search", query="MCP 契约探针")["results"]
    assert any(r["key"] == key for r in found), found
    assert all(len(r["content"]) <= 301 for r in found)

    assert call(server, "memory_get", key=key)["content"] == "MCP 契约探针"


def test_删除不带confirm_报错且数据还在(server):
    """🔴 硬约束 2：不许静默返回 ok。"""
    key = call(server, "memory_add", text="待删除的记忆 探针")["key"]
    with pytest.raises(ToolError) as exc:
        call(server, "memory_remove", key=key)
    assert "confirm" in str(exc.value)
    assert call(server, "memory_get", key=key)["content"] == "待删除的记忆 探针"


def test_删除带confirm_成功且之后取不到(server):
    key = call(server, "memory_add", text="确认删除 探针")["key"]
    assert call(server, "memory_remove", key=key, confirm=True)["removed"] is True
    with pytest.raises(ToolError):
        call(server, "memory_get", key=key)
    # 重复删除 = 写层 fail-closed，不得谎报成功
    with pytest.raises(ToolError):
        call(server, "memory_remove", key=key, confirm=True)


def test_merge_dry_run_条数前后一致(server):
    before = call(server, "memory_core_list")["count"]
    stats = call(server, "memory_merge", dry_run=True)
    assert stats["dry_run"] is True
    assert stats["core_count_before"] == stats["core_count_after"] == before


def test_merge实跑_confirm不符即拒绝(server):
    with pytest.raises(ToolError) as exc:
        call(server, "memory_merge", dry_run=False, confirm="99999")
    assert "confirm" in str(exc.value)


def test_merge实跑_confirm等于当前条数即放行(server):
    n = call(server, "memory_core_list")["count"]
    assert call(server, "memory_merge", dry_run=False, confirm=str(n))["dry_run"] is False


def test_forget_dry_run与confirm门(server):
    call(server, "memory_add", text="遗忘探针 条目")
    stats = call(server, "memory_forget", dry_run=True)
    assert stats["dry_run"] is True
    assert stats["fragment_count_before"] == stats["fragment_count_after"]
    with pytest.raises(ToolError) as exc:
        call(server, "memory_forget", dry_run=False, confirm="不存在的数")
    assert "confirm" in str(exc.value)


def test_查询非法时报错而非空结果(server):
    """检索层铁律：失败形态是抛错 —— 静默空列表是最危险的失败形态。"""
    with pytest.raises(ToolError):
        call(server, "memory_search", query="  ")
    with pytest.raises(ToolError):
        call(server, "memory_get", key="memory:frag:不存在")
    with pytest.raises(ToolError):
        call(server, "memory_add", text="   ")


def test_常驻层列表与core资源同源(server):
    """`memory_core_list` 与 `memory://core` 读的是同一个 provider，不许两处口径分叉。"""
    call(server, "memory_add", text="常驻记忆 甲", tags="core")
    rows = call(server, "memory_core_list")
    assert rows["count"] == 1 and len(rows["items"]) == 1
    assert "常驻记忆 甲" in rows["items"][0]["content"]
    assert "常驻记忆 甲" in read_resource(server, "memory://core")


def test_mcp_非primary写core降候选(tmp_path, monkeypatch):
    """🔴 写闸的 MCP 路径同样生效：非 primary 写 `core` 落库必须变成 `core_candidate`。

    与上面那条同源用例互为正反两面：那边 `is_primary=true` 放行，这边缺省（false）
    拦下。两边都用同一套 `memory_add(tags="core")`，所以经 MCP 走的写请求
    也在闸门管辖之内 —— 不能只靠 provider 单测、漏掉 MCP 入口。

    断言看 `memory_get` 的**落库后** tags：`memory_add` 返回的是请求里的 tags
    （写入前的原值），拿它断言等于什么都没验。
    """
    cfg = {"storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "nonprimary.db")}}}
    cfg_file = tmp_path / "config_nonprimary.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))

    from keepsake.adapters.hermes import provider as prov_mod

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})
    try:
        s = build_server()
        key = call(s, "memory_add", text="子代理想写常驻", tags="core")["key"]
        assert "core_candidate" in call(s, "memory_get", key=key)["tags"]
        # 降下来的候选不进常驻层 —— 每轮注入里看不到它
        assert call(s, "memory_core_list")["count"] == 0
    finally:
        prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})


def test_单条资源返回全文(server):
    key = call(server, "memory_add", text="单条资源 探针")["key"]
    assert json.loads(read_resource(server, f"memory://fragment/{key}"))["content"] == "单条资源 探针"
    # 模板资源取不到时 SDK 会把内部异常包一层 ValueError —— 语义仍是「报错，不是空 ok」
    with pytest.raises((ToolError, ValueError), match="记忆条目不存在"):
        read_resource(server, "memory://fragment/memory:frag:不存在")


# ---------------------------------------------------------------------------
# ③ 不绑宿主：换成别人的 provider 照样跑
# ---------------------------------------------------------------------------

def test_自造provider可直接复用同一套工具():
    s = build_server(_ForeignProvider())
    key = call(s, "memory_add", text="另一个 agent 的记忆", tags="x")["key"]
    assert call(s, "memory_search", query="另一个 agent")["results"][0]["key"] == key
    assert call(s, "memory_core_list")["count"] == 1
    assert call(s, "memory_forget", dry_run=True)["fragment_count_before"] == 1
    with pytest.raises(ToolError):          # 写语义对任何 provider 一视同仁
        call(s, "memory_remove", key=key)


def test_未实现可选扩展的provider显式报错():
    """没有 `list_core` 时不许返回空列表装成功 —— 显式报「未实现」。"""

    class _NoExtension(_ForeignProvider):
        list_core = None
        count_fragments = None

    s = build_server(_NoExtension())
    with pytest.raises(ToolError) as exc:
        call(s, "memory_core_list")
    assert "StorageIntrospection" in str(exc.value)