"""消息中心「手动 / 批量触发提醒」与「私信实时推送」开关的端点测试。

覆盖判据 ⑦（nudge）与开关那一档：

* nudge：**有流** ⇒ `delivered=true` 且真的收到一帧 `dm-nudge`；
* nudge：**无流** ⇒ `delivered=false` + `reason=no_stream`（**不谎报送达**）；
* nudge：代理关了推送 ⇒ `delivered=false` + `reason=push_disabled`；
* nudge：代理不存在 ⇒ `delivered=false` + `reason=unknown_agent`；
* nudge：**空 agents 列表** ⇒ 400；超过 50 个 ⇒ 400；重复 id ⇒ 去重；
* nudge：无 token ⇒ 401；有审计留痕；
* `push` 开关：往返 / 未知代理 404 / 缺 confirm 400 / 无 token 401 / 审计留痕；
* 列表端点顺带给出**每个收件人的未读数**（前端「按收件代理分组」的唯一取数口）。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` + `ks_message`
两张表）—— 不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.govern.events import BUS
from keepsake.govern.messaging import Messaging
from keepsake.govern.registry import Registry

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite 存储 + 审计/备份目录 + 固定 token。"""
    from keepsake.core.sqlite import SqliteStorage

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    storage = SqliteStorage(path=str(tmp_path / "nudge.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    reg = Registry(storage)
    assert reg.ensure_schema() is True
    reg.register("worker-1", "工人")
    reg.approve("worker-1")
    with TestClient(app) as client:
        yield client, app, storage, reg
    app.dependency_overrides.clear()
    storage.close()


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- nudge

@pytest.mark.anyio
async def test_nudge_有流_送达且收到dm_nudge帧(env):
    """挂着流 ⇒ `delivered=true`，且客户端**真的收到**一帧 `dm-nudge`。"""
    _, app, storage, reg = env
    reg.set_push_enabled("worker-1", True)
    sub = BUS.subscribe("worker-1", asyncio.get_running_loop())
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://console") as c:
            r = await c.post("/api/v1/messages/nudge", headers=AUTH,
                             json={"agents": ["worker-1"]})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["results"] == [
            {"agent_id": "worker-1", "delivered": True, "reason": "", "unread": 0}
        ]
        item = await asyncio.wait_for(sub.queue.get(), timeout=5)
    finally:
        BUS.unsubscribe(sub)
    assert "event: dm-nudge" in item
    data = json.loads(item.split("data: ", 1)[1].split("\n", 1)[0])
    assert data["unread"] == 0
    assert set(data) == {"seq", "from", "unread"}       # 🔴 只有元数据，没有正文


def test_nudge_无流_如实说不送达(env):
    """没人挂着流 ⇒ `delivered=false` + `reason=no_stream`（**不谎报**）。"""
    client, _, _, reg = env
    reg.set_push_enabled("worker-1", True)
    r = client.post("/api/v1/messages/nudge", headers=AUTH, json={"agents": ["worker-1"]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["delivered"] is False and item["reason"] == "no_stream"


def test_nudge_推送关闭_带reason(env):
    """治理页关了「私信实时推送」的代理 ⇒ `delivered=false` + `push_disabled`。"""
    client, _, _, reg = env
    reg.set_push_enabled("worker-1", False)
    r = client.post("/api/v1/messages/nudge", headers=AUTH, json={"agents": ["worker-1"]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["delivered"] is False and item["reason"] == "push_disabled"


def test_nudge_代理不存在_如实说(env):
    client, _, _, _ = env
    r = client.post("/api/v1/messages/nudge", headers=AUTH, json={"agents": ["查无此代理"]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert item["delivered"] is False and item["reason"] == "unknown_agent"


def test_nudge_空列表_400(env):
    client, _, _, _ = env
    r = client.post("/api/v1/messages/nudge", headers=AUTH, json={"agents": []})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_agents"


def test_nudge_超过上限_400(env):
    client, _, _, _ = env
    r = client.post("/api/v1/messages/nudge", headers=AUTH,
                    json={"agents": [f"agent-{i}" for i in range(51)]})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "too_many_agents"


def test_nudge_重复代理去重(env):
    client, _, _, reg = env
    reg.set_push_enabled("worker-1", True)
    r = client.post("/api/v1/messages/nudge", headers=AUTH,
                    json={"agents": ["worker-1", "worker-1", " worker-1 "]})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 1


def test_nudge_无token_401(env):
    client, _, _, _ = env
    r = client.post("/api/v1/messages/nudge", json={"agents": ["worker-1"]})
    assert r.status_code == 401


def test_nudge_有审计留痕(env, tmp_path):
    client, _, _, reg = env
    reg.set_push_enabled("worker-1", True)
    client.post("/api/v1/messages/nudge", headers=AUTH, json={"agents": ["worker-1"]})
    lines = _audit_lines(tmp_path)
    assert any(x.get("action") == "messages.nudge" and x.get("ok") is True for x in lines)


def test_nudge_写端点要挂认证与写闸():
    """判据看依赖表：少挂一道闸就是「绕过 UI 直接打 API 也能推提醒」。"""
    app = create_app()
    route = next(r for r in app.routes
                 if getattr(r, "path", "") == "/api/v1/messages/nudge")
    names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert any("require_session" in n for n in names)
    assert any("write_guard" in n for n in names)


# ---------------------------------------------------------------- push 开关

def test_push开关往返(env):
    client, _, _, reg = env
    r = client.post("/api/v1/agents/worker-1/push", headers=AUTH,
                    json={"enabled": True, "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["push_enabled"] is True and body["changed"] is True
    assert reg.push_enabled("worker-1") is True
    # 读面（代理列表）也跟着变 —— 前端的开关状态就来自那里
    items = client.get("/api/v1/agents", headers=AUTH).json()["items"]
    row = next(a for a in items if a["agent_id"] == "worker-1")
    assert row["push_enabled"] is True
    # 关回去
    r = client.post("/api/v1/agents/worker-1/push", headers=AUTH,
                    json={"enabled": False, "confirm": True})
    assert r.status_code == 200 and r.json()["push_enabled"] is False
    assert reg.push_enabled("worker-1") is False


def test_push默认值是关(env):
    """缺省**关**（往外放行的开关，缺省必须最严）。"""
    client, _, _, _ = env
    items = client.get("/api/v1/agents", headers=AUTH).json()["items"]
    assert all(a["push_enabled"] is False for a in items)


def test_push未知代理404(env):
    client, _, _, _ = env
    r = client.post("/api/v1/agents/查无此代理/push", headers=AUTH,
                    json={"enabled": True, "confirm": True})
    assert r.status_code == 404


def test_push缺confirm_400(env):
    client, _, _, reg = env
    r = client.post("/api/v1/agents/worker-1/push", headers=AUTH, json={"enabled": True})
    assert r.status_code == 400
    assert reg.push_enabled("worker-1") is False      # 被拒的写不许生效


def test_push无token_401(env):
    client, _, _, reg = env
    r = client.post("/api/v1/agents/worker-1/push", json={"enabled": True, "confirm": True})
    assert r.status_code == 401
    assert reg.push_enabled("worker-1") is False


def test_push写有审计留痕(env, tmp_path):
    client, _, _, _ = env
    client.post("/api/v1/agents/worker-1/push", headers=AUTH,
                json={"enabled": True, "confirm": True})
    lines = _audit_lines(tmp_path)
    assert any(x.get("action") == "agents.push" and x.get("target") == "worker-1"
               and x.get("ok") is True for x in lines)


# ---------------------------------------------------------------- 未读水位

def test_列表端点给出每个收件人的未读数(env):
    """前端「按收件代理分组」那栏的未读数**只由后端给**（前端不自己数）。"""
    client, _, storage, _ = env
    box = Messaging(storage)
    box.send("main", "worker-1", "第一条", "t-1")
    box.send("main", "worker-1", "第二条", "t-1")
    body = client.get("/api/v1/messages", headers=AUTH).json()
    assert body["unread"] == {"worker-1": 2}
    # 收走即已读 ⇒ 水位归零（列表端点本身不动 read_at）
    box.fetch_inbox("worker-1")
    body = client.get("/api/v1/messages", headers=AUTH).json()
    assert body["unread"] == {}
