"""消息中心控制台端点测试（`ks_messages_console`）。

覆盖：列表按 seq 倒序 / 按 agent 筛（发件与收件都算）/ 按 thread 筛 /
按关键词筛 / 分页边界 / size 上限 / 无 token 401 /
私信开关往返（改完注册表与读面都跟着变）/ 未知代理 404 / 缺 confirm 400 /
审计留痕 / **列表不动已读状态**。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` + `ks_message`
两张表，走各自的 `ensure_index` / `ensure_schema` 建表那一路）——
不连 Redis / PG / 生产库，也不碰主脑的 config。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.govern.messaging import Messaging
from keepsake.govern.registry import Registry

from conftest import flat_routes

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 列表端点回给前端的**固定形状**（凭据字段一个都不许出现）。
ITEM_FIELDS = {"seq", "from_id", "to_id", "content", "thread_id", "created", "read"}


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
    storage = SqliteStorage(path=str(tmp_path / "messages.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, storage, Registry(storage)
    app.dependency_overrides.clear()
    storage.close()


def _seed(reg: Registry, box: Messaging) -> None:
    """种两个代理 + 三条消息（其中两条同线程）。"""
    reg.register("main", "主代理")
    reg.register("worker-1", "工人")
    box.send("main", "worker-1", "第一条 你好工人", "t-1")
    box.send("worker-1", "main", "第二条 收到主代理", "t-1")
    box.send("main", "worker-1", "第三条 另一件事", "t-2")


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 读面

def test_无token_401(env):
    client, _, _ = env
    assert client.get("/api/v1/messages").status_code == 401


def test_列表按seq倒序且形状固定(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    r = client.get("/api/v1/messages", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    # 🔴 倒序 = 新的在前
    assert [i["seq"] for i in body["items"]] == [3, 2, 1]
    item = body["items"][0]
    assert set(item) == ITEM_FIELDS
    assert item["from_id"] == "main" and item["to_id"] == "worker-1"
    assert item["thread_id"] == "t-2" and item["content"] == "第三条 另一件事"
    assert item["read"] is False
    # 读面不回显任何凭据字段
    assert "credential" not in json.dumps(body)


def test_空信箱返回空列表不是报错(env):
    client, _, _ = env
    body = client.get("/api/v1/messages", headers=AUTH).json()
    assert body["items"] == [] and body["total"] == 0


def test_按代理筛_发件与收件都算(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    # worker-1 既发过也收过 ⇒ 两条都算它的
    body = client.get("/api/v1/messages?agent=worker-1", headers=AUTH).json()
    assert body["total"] == 3
    body = client.get("/api/v1/messages?agent=main", headers=AUTH).json()
    assert body["total"] == 3
    body = client.get("/api/v1/messages?agent=查无此代理", headers=AUTH).json()
    assert body["total"] == 0 and body["items"] == []


def test_按线程筛(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    body = client.get("/api/v1/messages?thread=t-1", headers=AUTH).json()
    assert body["total"] == 2
    assert {i["thread_id"] for i in body["items"]} == {"t-1"}
    body = client.get("/api/v1/messages?thread=t-2", headers=AUTH).json()
    assert body["total"] == 1


def test_按关键词筛(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    body = client.get("/api/v1/messages?q=收到", headers=AUTH).json()
    assert body["total"] == 1 and body["items"][0]["seq"] == 2


def test_分页边界(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    p1 = client.get("/api/v1/messages?page=1&size=2", headers=AUTH).json()
    assert [i["seq"] for i in p1["items"]] == [3, 2] and p1["total"] == 3
    p2 = client.get("/api/v1/messages?page=2&size=2", headers=AUTH).json()
    assert [i["seq"] for i in p2["items"]] == [1] and p2["total"] == 3
    # 翻过页 ⇒ 空列表（不是报错），total 仍是过滤后的总数
    p3 = client.get("/api/v1/messages?page=9&size=2", headers=AUTH).json()
    assert p3["items"] == [] and p3["total"] == 3


def test_分页参数非法被拒(env):
    client, _, _ = env
    # size 上限 200
    assert client.get("/api/v1/messages?size=201", headers=AUTH).status_code == 422
    assert client.get("/api/v1/messages?page=0", headers=AUTH).status_code == 422


def test_列表不动已读状态(env):
    """🔴 在控制台里「看一眼历史」不许把对方的信标成已读。"""
    client, storage, reg = env
    box = Messaging(storage)
    _seed(reg, box)
    client.get("/api/v1/messages", headers=AUTH)
    # 未读数不变 ⇒ 列表端点没碰 read_at
    assert box.unread_count("worker-1") == 2
    # 收信（fetch 即标读）之后再看列表，已读状态照旧显示：
    # 收件人是 worker-1 的两条（seq 3 与 seq 1）标成已读；
    # seq 2 是 worker-1 **发出去**的 —— 躺在 main 的收件箱里，仍是未读。
    box.fetch_inbox("worker-1")
    body = client.get("/api/v1/messages", headers=AUTH).json()
    assert [i["read"] for i in body["items"]] == [True, False, True]


# ---------------------------------------------------------------- 私信开关

def test_dm开关往返(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    r = client.post("/api/v1/agents/worker-1/dm", headers=AUTH,
                    json={"enabled": False, "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["dm_enabled"] is False and body["changed"] is True
    # 注册表真的改了
    assert reg.dm_enabled("worker-1") is False
    # 读面（代理列表）也跟着变 —— 前端的开关状态就来自那里
    items = client.get("/api/v1/agents", headers=AUTH).json()["items"]
    row = next(a for a in items if a["agent_id"] == "worker-1")
    assert row["dm_enabled"] is False
    # 改回来
    r = client.post("/api/v1/agents/worker-1/dm", headers=AUTH,
                    json={"enabled": True, "confirm": True})
    assert r.status_code == 200 and r.json()["dm_enabled"] is True
    assert reg.dm_enabled("worker-1") is True


def test_dm默认值是开(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    items = client.get("/api/v1/agents", headers=AUTH).json()["items"]
    assert all(a["dm_enabled"] is True for a in items)


def test_dm未知代理404(env):
    client, _, _ = env
    r = client.post("/api/v1/agents/查无此代理/dm", headers=AUTH,
                    json={"enabled": False, "confirm": True})
    assert r.status_code == 404


def test_dm缺confirm_400(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    r = client.post("/api/v1/agents/worker-1/dm", headers=AUTH, json={"enabled": False})
    assert r.status_code == 400
    # 被拒的写不许生效
    assert reg.dm_enabled("worker-1") is True


def test_dm无token_401(env):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    r = client.post("/api/v1/agents/worker-1/dm", json={"enabled": False, "confirm": True})
    assert r.status_code == 401
    assert reg.dm_enabled("worker-1") is True


def test_dm写端点要挂认证与写闸(env):
    """判据看依赖表：少挂一道闸就是「绕过 UI 直接打 API 也能改」。"""
    app = create_app()
    route = next(r for r in flat_routes(app)
                 if getattr(r, "path", "") == "/api/v1/agents/{agent_id}/dm")
    names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert any("require_session" in n for n in names)
    assert any("write_guard" in n for n in names)


def test_dm写有审计留痕(env, tmp_path):
    client, storage, reg = env
    _seed(reg, Messaging(storage))
    client.post("/api/v1/agents/worker-1/dm", headers=AUTH,
                json={"enabled": False, "confirm": True})
    lines = _audit_lines(tmp_path)
    assert any(x.get("action") == "agents.dm" and x.get("target") == "worker-1"
               and x.get("ok") is True for x in lines)
