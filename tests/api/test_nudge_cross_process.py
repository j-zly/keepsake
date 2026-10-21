"""nudge **跨进程投递**的集成测试（2026-10 ks_nudge_xproc）。

控制台进程（8200）与 MCP 进程（8210）**分开跑**：SSE 流挂在 MCP 进程的内存
总线上，控制台自己那台总线**永远没有订阅者** ⇒ 提醒必须**跨进程**投递
（控制台 → 内部端点 → 挂着流的那台进程去推）。

本文件起**两个 ASGI 应用**（控制台 + MCP-http），走**真 HTTP**那一跳：

* 控制台 `POST /api/v1/messages/nudge` ⇒ httpx ⇒ `POST /internal/publish`
  ⇒ MCP 进程的 BUS ⇒ 挂着流的客户端**真的收到**一帧 `dm-nudge`；
* 端点**不可达 / 503** ⇒ `delivered=false` + `reason=publish_endpoint_unavailable`
  —— **如实**说没送到，**不谎报**已送达。

全程 hermetic：临时 SQLite + 临时审计目录 + 回环随机端口，不连生产库。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import socket
import threading
import time
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import messages as messages_mod
from keepsake.govern.events import BUS
from keepsake.govern.registry import Registry

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 跨进程投递的共享密钥（控制台与 MCP 两个进程读**同一个** env）。
INTERNAL_TOKEN = "internal-token-not-a-real-secret"


@pytest.fixture()
def console(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite + 一个已批准且开了推送的代理 `worker-1`。"""
    from keepsake.core.sqlite import SqliteStorage

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    monkeypatch.setenv("KEEPSAKE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    storage = SqliteStorage(path=str(tmp_path / "nudge.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    reg = Registry(storage)
    assert reg.ensure_schema() is True
    reg.register("worker-1", "工人")
    reg.approve("worker-1")
    reg.set_push_enabled("worker-1", True)
    yield app, reg
    app.dependency_overrides.clear()
    storage.close()


@pytest.fixture()
def mcp_app(tmp_path, monkeypatch):
    """MCP 进程环境：临时 sqlite + 真实 `build_server()` 装配（含内部端点）。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "mcp.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "mcp_config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("KEEPSAKE_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)

    from keepsake.adapters.hermes import provider as prov_mod
    from keepsake.mcp_server.server import build_server

    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001
    server = build_server()
    yield server.streamable_http_app()
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001


@contextlib.contextmanager
def _serve(app: Any) -> Iterator[str]:
    """把 ASGI 应用起在 127.0.0.1 的随机端口上（真 HTTP ⇒ 那一跳是真的）。"""
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


def _closed_port() -> int:
    """一个**没人监听**的端口（拿来模拟「端点没起来」）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


# ---------------------------------------------------------------- 真链路

@pytest.mark.anyio
async def test_跨进程_nudge经内部端点送达挂流方(console, mcp_app, monkeypatch):
    """🔴 **真链路**：控制台 →（真 HTTP）→ MCP 进程的 BUS → 挂流方收到帧。

    这是这一单的核心判据：控制台进程自己那台总线没有订阅者，提醒**必须**
    跨进程投递才送得到。
    """
    app, _ = console
    with _serve(mcp_app) as base:
        monkeypatch.setenv("KEEPSAKE_NUDGE_PUBLISH_ENDPOINT",
                           f"{base}/internal/publish")
        sub = BUS.subscribe("worker-1", asyncio.get_running_loop())
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://console") as c:
                r = await c.post("/api/v1/messages/nudge", headers=AUTH,
                                 json={"agents": ["worker-1"]})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            # 逐代理结果**结构不变**（前端零改动）
            assert body["results"] == [
                {"agent_id": "worker-1", "delivered": True, "reason": "", "unread": 0}
            ]
            item = await asyncio.wait_for(sub.queue.get(), timeout=10)
        finally:
            BUS.unsubscribe(sub)
    assert "event: dm-nudge" in item
    data = json.loads(item.split("data: ", 1)[1].split("\n", 1)[0])
    assert set(data) == {"seq", "from", "unread"}       # 🔴 只有元数据，没有正文


# ---------------------------------------------------------------- 不可用

def test_投递端点不可达_如实说不可用(console, monkeypatch):
    """端点**没起来** ⇒ `delivered=false` + `reason=publish_endpoint_unavailable`。"""
    app, _ = console
    monkeypatch.setenv("KEEPSAKE_NUDGE_PUBLISH_ENDPOINT",
                       f"http://127.0.0.1:{_closed_port()}/internal/publish")
    with TestClient(app) as client:
        r = client.post("/api/v1/messages/nudge", headers=AUTH,
                        json={"agents": ["worker-1"]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["delivered"] is False
    assert item["reason"] == "publish_endpoint_unavailable"


def test_端点503_如实说不可用(console, monkeypatch):
    """MCP 侧**没配共享密钥** ⇒ 端点 503 ⇒ 控制台如实说不可用（不谎报）。"""
    app, _ = console

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"error": "internal_disabled"}})

    monkeypatch.setattr(messages_mod, "_publish_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(_handler)))
    with TestClient(app) as client:
        r = client.post("/api/v1/messages/nudge", headers=AUTH,
                        json={"agents": ["worker-1"]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["delivered"] is False
    assert item["reason"] == "publish_endpoint_unavailable"


def test_端点不可用_只撞一次不把页面拖死(console, monkeypatch):
    """端点不可用 ⇒ 后面的代理**不再逐个去撞**（50 个 × 超时会拖死页面）。"""
    app, reg = console
    for aid in ("worker-2", "worker-3"):
        reg.register(aid, aid)
        reg.approve(aid)
        reg.set_push_enabled(aid, True)
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("端点没起来")

    monkeypatch.setattr(messages_mod, "_publish_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(_handler)))
    with TestClient(app) as client:
        r = client.post("/api/v1/messages/nudge", headers=AUTH,
                        json={"agents": ["worker-1", "worker-2", "worker-3"]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["reason"] for x in results] == ["publish_endpoint_unavailable"] * 3
    assert all(x["delivered"] is False for x in results)
    assert len(calls) == 1            # 🔴 只撞一次


def test_端点说没人挂流_理由是no_stream(console, monkeypatch):
    """端点回 `delivered=false` ⇒ 理由是 `no_stream`（**不是**端点不可用）。"""
    app, _ = console

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"delivered": False})

    monkeypatch.setattr(messages_mod, "_publish_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(_handler)))
    with TestClient(app) as client:
        r = client.post("/api/v1/messages/nudge", headers=AUTH,
                        json={"agents": ["worker-1"]})
    item = r.json()["results"][0]
    assert item["delivered"] is False
    assert item["reason"] == "no_stream"


# ---------------------------------------------------------------- 架构护栏

def test_控制台不再在本进程推帧():
    """🔴 护栏：控制台**不许**再在本进程调 `publish_nudge`。

    本进程那台总线永远没有订阅者 ⇒ 在本进程推 = 恒 `delivered=false`（坏功能）。
    投递必须走内部端点（跨进程那一跳）。
    """
    src = pathlib.Path(messages_mod.__file__).read_text(encoding="utf-8")
    assert "publish_nudge" not in src
    assert "_publish_via_internal" in src
