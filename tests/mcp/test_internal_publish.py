"""内部投递端点（`POST /internal/publish`）的测试（2026-10 ks_nudge_xproc）。

这个端点是 **nudge 跨进程投递**的那一跳：控制台进程（8200）把「给谁推什么帧」
POST 给 MCP 进程（8210），由**挂着 SSE 流的那台进程**去推。

判据逐条对应：

① **未配共享密钥** ⇒ 端点一律 **503**（fail-closed）；
② 配了密钥但**没带 / 带错** ⇒ **401**；
③ **非回环来源** ⇒ **403**（公网不可达）；
④ 有密钥 + 回环 + **没挂着流** ⇒ 200 `{"delivered": false}`（**不谎报**）；
⑤ 有密钥 + 回环 + **挂着流** ⇒ 200 `{"delivered": true}`，且真的收到一帧 `dm-nudge`；
⑥ body 不合法（缺 `agent_id` / 非法 `kind` / 不是 JSON）⇒ **400**。

全程 hermetic：临时 SQLite + 临时审计目录，不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from keepsake.govern.events import BUS
from keepsake.mcp_server.server import build_server

TOKEN = "internal-token-not-a-real-secret"
AUTH = {"X-Internal-Token": TOKEN}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """MCP 进程环境：临时 sqlite + 一个已批准代理 `worker-1` + 共享密钥。"""
    cfg = {
        "storage": {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "internal.db")}},
        "is_primary": True,
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_INTERNAL_TOKEN", TOKEN)
    monkeypatch.delenv("KEEPSAKE_AGENT_ID", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL", raising=False)

    from keepsake.adapters.hermes import provider as prov_mod
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001 进程级缓存
    server = build_server()
    reg = server.agent_gate.registry
    reg.register("worker-1", "工人")
    reg.approve("worker-1")
    yield server, reg
    server.agent_gate.stop()
    prov_mod._CORE_CACHE.update({"ts": 0.0, "frags": None})   # noqa: SLF001


def _app(env):
    """MCP 的 ASGI 应用（streamable-http ⇒ 自定义路由同进程同端口）。"""
    server, _ = env
    return server.streamable_http_app()


def _client(app, host: str = "127.0.0.1") -> httpx.AsyncClient:
    """ASGI 客户端（`client=` 可以**伪造来源地址** ⇒ 测「非回环拒」）。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 12345)),
        base_url="http://mcp")


# ---------------------------------------------------------------- ① 503

@pytest.mark.anyio
async def test_未配共享密钥_端点一律503(env, monkeypatch):
    """🔴 **未配置 env ⇒ 端点一律 503**（fail-closed：宁可不投递，也不裸奔）。"""
    monkeypatch.delenv("KEEPSAKE_INTERNAL_TOKEN", raising=False)
    async with _client(_app(env)) as c:
        r = await c.post("/internal/publish", headers=AUTH,
                         json={"agent_id": "worker-1", "kind": "dm-nudge", "unread": 1})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "internal_disabled"


# ---------------------------------------------------------------- ② 401

@pytest.mark.anyio
async def test_没带共享密钥_401(env):
    async with _client(_app(env)) as c:
        r = await c.post("/internal/publish",
                         json={"agent_id": "worker-1", "kind": "dm-nudge", "unread": 1})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"


@pytest.mark.anyio
async def test_共享密钥不对_401(env):
    async with _client(_app(env)) as c:
        r = await c.post("/internal/publish", headers={"X-Internal-Token": "wrong-token"},
                         json={"agent_id": "worker-1", "kind": "dm-nudge", "unread": 1})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"


# ---------------------------------------------------------------- ③ 403

@pytest.mark.anyio
async def test_非回环来源_403(env):
    """外网来源 ⇒ 拒（公网不可达的第一道闸）。"""
    async with _client(_app(env), host="10.0.0.9") as c:
        r = await c.post("/internal/publish", headers=AUTH,
                         json={"agent_id": "worker-1", "kind": "dm-nudge", "unread": 1})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "forbidden_source"


# ---------------------------------------------------------------- ④ 无流

@pytest.mark.anyio
async def test_无流_delivered为false(env):
    """没人挂着流 ⇒ `delivered=false`（**如实**回答，不谎报）。"""
    async with _client(_app(env)) as c:
        r = await c.post("/internal/publish", headers=AUTH,
                         json={"agent_id": "worker-1", "kind": "dm-nudge",
                               "unread": 3, "seq": 12, "from": "sender"})
    assert r.status_code == 200
    assert r.json() == {"delivered": False}


# ---------------------------------------------------------------- ⑤ 有流

@pytest.mark.anyio
async def test_有流_delivered为true且收到dm_nudge帧(env):
    """挂着流 ⇒ `delivered=true`，且客户端**真的收到**一帧 `dm-nudge`。"""
    sub = BUS.subscribe("worker-1", asyncio.get_running_loop())
    try:
        async with _client(_app(env)) as c:
            r = await c.post("/internal/publish", headers=AUTH,
                             json={"agent_id": "worker-1", "kind": "dm-nudge",
                                   "unread": 3, "seq": 12, "from": "sender"})
        assert r.status_code == 200
        assert r.json() == {"delivered": True}
        item = await asyncio.wait_for(sub.queue.get(), timeout=5)
    finally:
        BUS.unsubscribe(sub)
    assert "event: dm-nudge" in item
    data = json.loads(item.split("data: ", 1)[1].split("\n", 1)[0])
    assert data == {"seq": 12, "from": "sender", "unread": 3}   # 🔴 只有元数据


# ---------------------------------------------------------------- ⑥ 400

@pytest.mark.anyio
async def test_body不合法_400(env):
    """缺 `agent_id` / 非法 `kind` / 不是 JSON ⇒ 400（不推任何帧）。"""
    async with _client(_app(env)) as c:
        r1 = await c.post("/internal/publish", headers=AUTH,
                          json={"kind": "dm-nudge", "unread": 1})
        r2 = await c.post("/internal/publish", headers=AUTH,
                          json={"agent_id": "worker-1", "kind": "坏事件名"})
        r3 = await c.post("/internal/publish", content=b"not-json",
                          headers={**AUTH, "Content-Type": "application/json"})
    assert r1.status_code == 400 and r1.json()["detail"]["error"] == "no_agent"
    assert r2.status_code == 400 and r2.json()["detail"]["error"] == "bad_kind"
    assert r3.status_code == 400 and r3.json()["detail"]["error"] == "bad_body"


# ---------------------------------------------------------------- 装配

def test_内部端点挂在MCP应用上(env):
    """端点与 SSE 端点**同进程同端口**（路由确实挂上了）。"""
    app = _app(env)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/internal/publish" in paths
    assert "/events" in paths
