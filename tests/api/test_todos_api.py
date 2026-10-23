"""待办控制台端点测试（`ks_todo` 一等公民的控制台面）。

覆盖：列表分页 / 按 owner 与 status 筛 / 计数 / 「建议废弃」只提名 /
新增（owner 缺省 = 主代理、owner 未批准 ⇒ 404、缺 confirm ⇒ 400）/
改状态（三态、非法态 400、缺 confirm 400）/ 真删（确认串 = 标题全文）/
**写端点全部要 token** / 审计留痕。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` + `ks_todo`
两张表）—— 不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.govern.registry import Registry
from keepsake.govern.todo import TodoStore

from conftest import flat_routes

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 待办行回给前端的**固定形状**（凭据字段一个都不许出现）。
ITEM_FIELDS = {"id", "title", "detail", "owner", "creator", "status", "due",
               "due_state", "created", "updated", "done_at"}


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
    storage = SqliteStorage(path=str(tmp_path / "todos.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, storage, Registry(storage)
    app.dependency_overrides.clear()
    storage.close()


def _seed(reg: Registry) -> None:
    """种两个已批准的代理 + 一个待审代理。"""
    reg.register("main", "主代理")
    reg.approve("main")
    reg.register("worker-1", "工人")
    reg.approve("worker-1")
    reg.register("worker-pending", "还没批")
    reg.set_role("main", "primary")


def _add(client, **payload):
    body = {"confirm": True}
    body.update(payload)
    return client.post("/api/v1/todos", headers=AUTH, json=body)


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files
            for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 读面

def test_无token_401(env):
    client, _, _ = env
    assert client.get("/api/v1/todos").status_code == 401
    assert client.get("/api/v1/todos/summary").status_code == 401


def test_空库列表(env):
    client, _, reg = env
    _seed(reg)
    r = client.get("/api/v1/todos", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["total"] == 0


def test_新增后列表形状固定(env):
    client, _, reg = env
    _seed(reg)
    tid = _add(client, title="补回滚手册", detail="缺第 3 章", owner="worker-1").json()["id"]
    body = client.get("/api/v1/todos", headers=AUTH).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == ITEM_FIELDS
    assert item["id"] == tid
    assert item["title"] == "补回滚手册"
    assert item["owner"] == "worker-1"
    assert item["status"] == "open"
    assert item["due_state"] == "none"


def test_列表按owner与status筛(env):
    client, _, reg = env
    _seed(reg)
    _add(client, title="甲", owner="worker-1")
    _add(client, title="乙", owner="main")
    body = client.get("/api/v1/todos?owner=worker-1", headers=AUTH).json()
    assert body["total"] == 1 and body["items"][0]["title"] == "甲"
    tid = client.get("/api/v1/todos?owner=main", headers=AUTH).json()["items"][0]["id"]
    client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                json={"status": "done", "confirm": True})
    body = client.get("/api/v1/todos?status=done", headers=AUTH).json()
    assert body["total"] == 1 and body["items"][0]["title"] == "乙"


def test_列表服务端分页(env):
    client, _, reg = env
    _seed(reg)
    for i in range(5):
        _add(client, title=f"第 {i} 条", owner="worker-1")
    p1 = client.get("/api/v1/todos?page=1&page_size=2", headers=AUTH).json()
    p2 = client.get("/api/v1/todos?page=2&page_size=2", headers=AUTH).json()
    assert p1["total"] == 5 == p2["total"]
    assert len(p1["items"]) == 2 and len(p2["items"]) == 2
    assert {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]} == set()


def test_计数与看板小节口径一致(env):
    """🔴 看板那枚「待办」小节与待办页顶部的计数条**共用这一份**（前端不自己数）。"""
    client, _, reg = env
    _seed(reg)
    _add(client, title="开放的", owner="worker-1")
    tid = _add(client, title="做完的", owner="worker-1").json()["id"]
    client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                json={"status": "done", "confirm": True})
    body = client.get("/api/v1/todos/summary", headers=AUTH).json()
    # 老字段一字不改（前端那几处计数都指着它们）
    assert {k: body[k] for k in ("open", "done", "obsolete", "total", "overdue")} == {
        "open": 1, "done": 1, "obsolete": 0, "total": 2, "overdue": 0}


def test_summary按代理分列(env):
    """🆕 `by_agent`：按归属代理拆开的同一份计数（看板「完成率按代理看」用）。"""
    client, _, reg = env
    _seed(reg)
    _add(client, title="甲开放", owner="worker-1")
    tid = _add(client, title="甲做完", owner="worker-1").json()["id"]
    client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                json={"status": "done", "confirm": True})
    _add(client, title="乙开放", owner="main")
    body = client.get("/api/v1/todos/summary", headers=AUTH).json()
    rows = {r["agent_id"]: r for r in body["by_agent"]}
    assert set(rows) == {"worker-1", "main"}, "两个 owner 各一行"
    w = rows["worker-1"]
    assert (w["open"], w["done"], w["obsolete"], w["total"]) == (1, 1, 0, 2)
    assert w["ratio"] == 50.0, "完成率 = (done+obsolete)/total*100"
    assert w["agent"] == "工人", "显示名来自注册表（ks_agent.name）"
    m = rows["main"]
    assert (m["open"], m["done"], m["total"], m["ratio"]) == (1, 0, 1, 0.0)
    # 降序：完成率高的排前面
    assert body["by_agent"][0]["agent_id"] == "worker-1"


def test_summary未在册的owner归未标注桶(env):
    """🔴 owner 查不到注册表 ⇒ 归「未标注」桶 `-`（不假装它属于某个代理）。"""
    client, storage, reg = env
    _seed(reg)
    store = TodoStore(storage)
    store.add("没主的活", owner="ghost-agent", creator="main")
    store.add("也是没主的", owner="ghost-agent", creator="main")
    body = client.get("/api/v1/todos/summary", headers=AUTH).json()
    rows = {r["agent_id"]: r for r in body["by_agent"]}
    assert "ghost-agent" not in rows, "不在册的 owner 不单独成行"
    assert rows["-"]["total"] == 2, "两个没主的待办合并进未标注桶"
    assert rows["-"]["agent"] == "-"


def test_summary按owner筛时by_agent同口径(env):
    """🔴 `?owner=` 筛谁，`by_agent` 就只剩谁（同一把筛子）。"""
    client, _, reg = env
    _seed(reg)
    _add(client, title="甲", owner="worker-1")
    _add(client, title="乙", owner="main")
    body = client.get("/api/v1/todos/summary?owner=worker-1", headers=AUTH).json()
    assert body["total"] == 1
    assert [r["agent_id"] for r in body["by_agent"]] == ["worker-1"]


def test_建议废弃只提名不改状态(env):
    """🔴 到期日过了 7 天还开着 ⇒ 提名；**状态不许被自动改掉**。"""
    client, storage, reg = env
    _seed(reg)
    store = TodoStore(storage)
    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc).date()
           - _dt.timedelta(days=9)).isoformat()
    tid = store.add("欠很久的一件", owner="worker-1", creator="worker-1", due=old)
    r = client.get("/api/v1/todos/obsolete-suggestions", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["title"] == "欠很久的一件"
    # 🔴 只提名：状态**没变**
    assert store.get(tid)["status"] == "open"
    assert client.get("/api/v1/todos/summary", headers=AUTH).json()["open"] == 1


# ---------------------------------------------------------------- 写面

def test_新增缺confirm_400(env):
    client, _, reg = env
    _seed(reg)
    r = client.post("/api/v1/todos", headers=AUTH,
                    json={"title": "没确认", "owner": "worker-1"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"


def test_新增owner未批准_404(env):
    """🔴 owner 必须在册且已批准 —— 未批准 = 查无此代理（404，不是 400）。"""
    client, _, reg = env
    _seed(reg)
    r = _add(client, title="派给没批的人", owner="worker-pending")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "agent_not_found"
    r = _add(client, title="派给不存在的人", owner="nobody")
    assert r.status_code == 404


def test_新增owner缺省为主代理(env):
    client, _, reg = env
    _seed(reg)
    r = _add(client, title="给主脑的活")
    assert r.status_code == 200
    assert r.json()["item"]["owner"] == "main"


def test_改状态三态与非法态(env):
    client, _, reg = env
    _seed(reg)
    tid = _add(client, title="一件事", owner="worker-1").json()["id"]
    for status in ("done", "obsolete", "open"):
        r = client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                        json={"status": status, "confirm": True})
        assert r.status_code == 200, r.text
        assert r.json()["item"]["status"] == status
    r = client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                    json={"status": "半吊子", "confirm": True})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_status"


def test_改状态缺confirm_400(env):
    client, _, reg = env
    _seed(reg)
    tid = _add(client, title="一件事", owner="worker-1").json()["id"]
    r = client.post(f"/api/v1/todos/{tid}/status", headers=AUTH,
                    json={"status": "done"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"


def test_改不存在_404(env):
    client, _, reg = env
    _seed(reg)
    r = client.post("/api/v1/todos/999999/status", headers=AUTH,
                    json={"status": "done", "confirm": True})
    assert r.status_code == 404


def test_真删要手输标题(env):
    """🔴 确认串 = **标题全文**（手输）：打错一个字就不该生效。"""
    client, _, reg = env
    _seed(reg)
    tid = _add(client, title="删掉我", owner="worker-1").json()["id"]
    r = client.request("DELETE", f"/api/v1/todos/{tid}", headers=AUTH,
                       json={"confirm": "删掉我 "})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_mismatch"
    r = client.request("DELETE", f"/api/v1/todos/{tid}", headers=AUTH,
                       json={"confirm": "删掉我"})
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert client.get("/api/v1/todos", headers=AUTH).json()["total"] == 0


def test_写端点全部要token(env):
    """🔴 写面一条都不许免认证。"""
    client, _, reg = env
    _seed(reg)
    tid = _add(client, title="一件事", owner="worker-1").json()["id"]
    assert client.post("/api/v1/todos", json={"title": "x", "confirm": True}).status_code == 401
    assert client.post(f"/api/v1/todos/{tid}/status",
                       json={"status": "done", "confirm": True}).status_code == 401
    assert client.request("DELETE", f"/api/v1/todos/{tid}",
                           json={"confirm": "一件事"}).status_code == 401


def test_审计留痕(env, tmp_path):
    """🔴 写操作要留审计（含被拒的写）。"""
    client, _, reg = env
    _seed(reg)
    _add(client, title="一件事", owner="worker-1")
    client.post("/api/v1/todos", headers=AUTH, json={"title": "没确认"})
    lines = _audit_lines(tmp_path)
    actions = [l.get("action") for l in lines]
    assert "todos.create" in actions
    assert any(a == "todos.create" and l.get("ok") is False for a, l in zip(actions, lines))


def test_路由挂在应用上(env):
    """🔴 端点真的挂在 app 上（别写完忘了 include_router）。"""
    client, _, reg = env
    paths = {getattr(r, "path", "") for r in flat_routes(client.app)}
    assert {"/api/v1/todos", "/api/v1/todos/summary",
            "/api/v1/todos/obsolete-suggestions",
            "/api/v1/todos/{todo_id}/status",
            "/api/v1/todos/{todo_id}"} <= paths
