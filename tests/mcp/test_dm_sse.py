"""私信 SSE 推送通道的测试（`ks_dm_sse`）。

判据逐条对应：

① `push_enabled=0` ⇒ 建流被拒（403）；
② 未批准（凭据有效但 `status≠approved`）⇒ 建流被拒（403）；
③ 有流时 `message_send` 推帧（`event: dm`，**帧里没有正文**）；
④ 无流时发送**不炸**（publish 静默丢弃）；
⑤ **重连追帧**：有未读时建流立刻收到一帧摘要；
⑥ 既有语义回归不动（`push_enabled` 缺省是**关**、老行兼容；`share_memory`
   / core 过滤各自由既有测试守着，这里只钉住「新列没改存量语义」）；
⑦ nudge（控制台侧）—— 见 `tests/api/test_messages_nudge_api.py`。

全程 hermetic：`tmp_path` 里的临时 SQLite + 临时审计目录，
不连 Redis / PG / 网络 / 生产库，也不改主脑的真实 config。

🔴 为什么用**真 HTTP**（uvicorn 起在 127.0.0.1 的随机端口）而不是 ASGI 直调：
SSE 是**流式**响应，httpx 的 `ASGITransport` 会把整个 body 缓冲下来 ——
对一条永远不结束的流来说那就是挂死。真 HTTP 才能一行一行地读帧。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from typing import Any, Dict, Iterator

import httpx
import pytest
import uvicorn

from keepsake.govern.messaging import Messaging
from keepsake.mcp_server.server import build_server
from keepsake.mcp_server.tools import ToolError


# ---------------------------------------------------------------------------
# 夹具 / 帮手
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """MCP 进程环境：临时 sqlite 配置 + 一个已批准的代理 `worker-1`。

    `worker-1` 有**真实凭据**（`approve` 签发的那条）—— SSE 建流就是拿它
    反查身份的。
    """
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "sse.db")}},
        # is_primary=true 只是首次引导的种子；身份一律由注册表说了算
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)

    from keepsake.adapters.hermes import provider as prov_mod
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001 进程级缓存
    server = build_server()
    reg = server.agent_gate.registry
    reg.register("worker-1", "工人")
    credential = reg.approve("worker-1")
    yield server, reg, credential
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001


@contextlib.contextmanager
def _serve(app: Any) -> Iterator[str]:
    """把 ASGI 应用起在 127.0.0.1 的随机端口上（真 HTTP ⇒ SSE 能流式读）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("测试用 uvicorn 没能起来")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def _read_frame(stream: Any, timeout: float = 8.0) -> str:
    """从 SSE 流里读**一帧**（读到空行 `\\n\\n` 为止）。"""
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = await asyncio.wait_for(
            stream.__anext__(), timeout=max(0.1, deadline - time.monotonic()))
        buf += chunk.decode("utf-8", "replace")
        if "\n\n" in buf:
            return buf
    raise AssertionError(f"SSE 没等到帧：{buf!r}")


def _as_caller(server: Any, agent_id: str) -> None:
    """把 stdio 闸门切到某个代理（标成「还没复核过」⇒ 下次调用必走判定链）。"""
    gate = server.agent_gate
    gate.agent_id = agent_id
    gate._stdio_ok = None      # noqa: SLF001
    gate._stdio_at = 0.0       # noqa: SLF001


# ---------------------------------------------------------------------------
# ① / ② 建流的三道闸
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_push关闭_建流被拒(env):
    """`push_enabled=0` ⇒ 403（**不建流**）。"""
    server, reg, credential = env
    reg.set_push_enabled("worker-1", False)
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        r = await client.get("/events", headers={"Authorization": f"Bearer {credential}"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "push_disabled"


@pytest.mark.anyio
async def test_未批准_建流被拒(env):
    """凭据有效但 `status≠approved` ⇒ 403（与「凭据无效」的 401 分开说）。"""
    server, reg, credential = env
    # 模拟「批准过又被改回待审」：凭据哈希还在，状态不是 approved
    reg._be.update("worker-1", {"status": "pending"})      # noqa: SLF001
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        r = await client.get("/events", headers={"Authorization": f"Bearer {credential}"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "not_approved"


@pytest.mark.anyio
async def test_没带凭据_401(env):
    """没带 Authorization 头 ⇒ 401（不许建流）。"""
    server, _, _ = env
    app = server.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        r = await client.get("/events")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "no_credential"


# ---------------------------------------------------------------------------
# ③ 有流时 message_send 推帧（帧里**没有正文**）
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_有流时message_send推帧_且不带正文(env):
    server, reg, credential = env
    reg.set_push_enabled("worker-1", True)
    reg.bootstrap_local("sender", "发信代理")     # 本机通道 ⇒ approved
    _as_caller(server, "sender")

    with _serve(server.streamable_http_app()) as base:
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream(
                "GET", f"{base}/events",
                headers={"Authorization": f"Bearer {credential}"},
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                frames = resp.aiter_bytes()
                # 发一条私信 ⇒ 服务端顺这条流推一帧
                await server.call_tool("message_send", {
                    "to": "worker-1", "content": "正文不该出现在事件帧里",
                })
                text = await _read_frame(frames)
    assert "event: dm" in text
    assert "正文不该出现在事件帧里" not in text      # 🔴 只推元数据
    data = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert data["from"] == "sender" and data["unread"] == 1 and data["seq"] >= 1
    assert set(data) == {"seq", "from", "unread"}   # 没有正文、没有别的字段


# ---------------------------------------------------------------------------
# ④ 无流时发送不炸（publish 静默丢弃）
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_无流时发送不炸(env):
    """没人挂着流 ⇒ 这一帧直接丢（消息**照旧送达**、发送**不报错**）。"""
    server, reg, credential = env
    reg.set_push_enabled("worker-1", True)
    reg.bootstrap_local("sender", "发信代理")
    _as_caller(server, "sender")

    out = await server.call_tool("message_send", {"to": "worker-1", "content": "没人收"})
    assert out[1]["result"]["ok"] is True
    # 消息真的落库了（投递钩子失败**不许**把已落库的消息说成没送到）
    box = Messaging(reg.storage)
    assert box.unread_count("worker-1") == 1


# ---------------------------------------------------------------------------
# ⑤ 重连追帧：有未读时建流立刻收到一帧摘要
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_重连追帧_有未读立刻收到摘要(env):
    server, reg, credential = env
    reg.set_push_enabled("worker-1", True)
    box = Messaging(reg.storage)
    box.send("sender", "worker-1", "先来一条", "t-1")
    box.send("sender", "worker-1", "再来一条", "t-1")

    with _serve(server.streamable_http_app()) as base:
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream(
                "GET", f"{base}/events",
                headers={"Authorization": f"Bearer {credential}"},
            ) as resp:
                assert resp.status_code == 200
                frames = resp.aiter_bytes()
                text = await _read_frame(frames)
    assert "event: dm" in text
    data = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert data["unread"] == 2                     # 未读水位
    assert data["from"] == "sender"               # 最近一个发件人
    assert data["seq"] >= 2                       # 最大 seq
    assert "先来一条" not in text and "再来一条" not in text   # 🔴 摘要不带正文


@pytest.mark.anyio
async def test_无未读时建流不补帧_先看到心跳(env, monkeypatch):
    """没有未读 ⇒ **不补帧**（客户端先看到心跳注释帧，而不是一条空摘要）。"""
    server, reg, credential = env
    reg.set_push_enabled("worker-1", True)
    monkeypatch.setenv("KEEPSAKE_SSE_HEARTBEAT_S", "0.5")     # 心跳调快，不必真等 25s
    with _serve(server.streamable_http_app()) as base:
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream(
                "GET", f"{base}/events",
                headers={"Authorization": f"Bearer {credential}"},
            ) as resp:
                assert resp.status_code == 200
                frames = resp.aiter_bytes()
                chunk = await asyncio.wait_for(frames.__anext__(), timeout=5)
    assert b": ping" in chunk                     # 保活注释帧
    assert b"event: dm" not in chunk              # 没有未读 ⇒ 不补摘要帧


@pytest.mark.anyio
async def test_吊销生效边界_一个心跳间隔内断流(env, monkeypatch):
    """🔴 吊销 / 关推送 ⇒ **≤ 一个心跳间隔**内这条流自己断（不是等 30 分钟）。"""
    server, reg, credential = env
    reg.set_push_enabled("worker-1", True)
    monkeypatch.setenv("KEEPSAKE_SSE_HEARTBEAT_S", "0.5")     # 心跳调快，不必真等 25s
    with _serve(server.streamable_http_app()) as base:
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream(
                "GET", f"{base}/events",
                headers={"Authorization": f"Bearer {credential}"},
            ) as resp:
                assert resp.status_code == 200
                frames = resp.aiter_bytes()
                # 心跳一帧 ⇒ 建流资格仍在（第一次复核通过）
                chunk = await asyncio.wait_for(frames.__anext__(), timeout=5)
                assert b": ping" in chunk
                # 吊销 ⇒ 下一次心跳复核不通过 ⇒ 服务端主动断
                reg.revoke("worker-1")
                with pytest.raises((StopAsyncIteration, httpx.RemoteProtocolError,
                                    httpx.ReadError, httpx.StreamError)):
                    while True:
                        await asyncio.wait_for(frames.__anext__(), timeout=5)


def test_心跳与挂起上限的默认口径():
    """心跳 25s、挂起总超时 30 分钟（默认口径写死在 `mcp_server/events.py`）。"""
    from keepsake.mcp_server import events as ev

    assert ev.HEARTBEAT_S == 25.0
    assert ev.MAX_STREAM_S == 30 * 60.0
    assert ev.EVENTS_PATH == "/events"


# ---------------------------------------------------------------------------
# ⑥ 既有语义回归：新增列不许改存量语义
# ---------------------------------------------------------------------------

def test_push_enabled缺省是关_老行兼容(env):
    """新列缺省**关**（往外放行的开关，缺省必须最严）；开关不碰状态 / 角色。"""
    _, reg, _ = env
    assert reg.push_enabled("worker-1") is False
    row = reg.get("worker-1")
    assert row["push_enabled"] is False            # 对外形状也是布尔
    assert row["status"] == "approved" and row["role"] == "sub"

    assert reg.set_push_enabled("worker-1", True) is True
    after = reg.get("worker-1")
    assert after["push_enabled"] is True
    assert after["status"] == "approved"           # 状态一个都不碰
    assert after["role"] == "sub"
    assert reg.set_push_enabled("worker-1", True) is False   # 值没变


def test_push开关不影响私信开关与共享开关(env):
    """三个开关互不干涉（`dm` / `share` / `push` 各管各的）。"""
    _, reg, _ = env
    reg.set_push_enabled("worker-1", True)
    assert reg.dm_enabled("worker-1") is True
    assert reg.share_memory("worker-1") is False


def test_未批准的代理不能订阅(env):
    """待审代理即使有凭据也订阅不了（闸门与工具面同一口径）。"""
    _, reg, _ = env
    reg.register("worker-pending", "待审")
    assert reg.push_enabled("worker-pending") is False
    assert reg.get("worker-pending")["status"] == "pending"
