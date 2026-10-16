"""代理注册表控制台端点测试（`keepsake_agent_registry` 第 1 张单）。

覆盖：读面形状 / 一次性凭据响应 / revoke 与 role 的确认串口径 / exclude 开关 /
未认证 401 / 未知代理 404 / 审计留痕（含被拒绝的写）/ 写面枚举。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` 表，走
`ensure_index` 建表那一路）—— 不连 Redis / PG / 生产库，也不碰主脑的 config。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.govern.registry import CREDENTIAL_PREFIX, Registry
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 四条写端点（写面枚举的另一半在 tests/api/test_api_readonly.py）
WRITE_PATHS = (
    "/api/v1/agents/{agent_id}/approve",
    "/api/v1/agents/{agent_id}/revoke",
    "/api/v1/agents/{agent_id}/role",
    "/api/v1/agents/{agent_id}/exclude",
)


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
    storage = SqliteStorage(path=str(tmp_path / "agents.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, Registry(storage)
    app.dependency_overrides.clear()
    storage.close()


def _seed(reg: Registry, *agent_ids: str) -> None:
    for aid in agent_ids:
        reg.register(aid, aid)


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    import pathlib

    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 读面

def test_无token_401(env):
    client, _ = env
    assert client.get("/api/v1/agents").status_code == 401


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_写端点无token_逐条401(env, path):
    client, _ = env
    assert client.post(path.format(agent_id="a"), json={"confirm": True}).status_code == 401


def test_列表形状与pending计数(env):
    client, reg = env
    _seed(reg, "main", "worker-1")
    body = client.get("/api/v1/agents", headers=AUTH).json()
    assert body["count"] == 2 and body["pending_count"] == 2
    item = body["items"][0]
    assert set(item) == {"agent_id", "name", "role", "status", "first_seen_at",
                         "last_seen_at", "reads", "writes", "exclude_from_primary",
                         "credential_hint", "meta"}
    assert item["role"] == "sub" and item["status"] == "pending"
    # 🔴 读面永远不回显凭据哈希 / 明文
    assert "credential_hash" not in json.dumps(body)


def test_空注册表返回空列表不是报错(env):
    client, _ = env
    body = client.get("/api/v1/agents", headers=AUTH).json()
    assert body["items"] == [] and body["pending_count"] == 0


# ---------------------------------------------------------------- 批准

def test_approve返回一次性凭据(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/approve", headers=AUTH, json={"confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "approved"
    assert body["credential"].startswith(CREDENTIAL_PREFIX)
    assert body["credential_hint"] == body["credential"][:8]
    assert "只此一次" in body["note"]
    # 注册表里那条凭据真的能用
    assert reg.verify("worker-1", body["credential"])["ok"] is True
    assert reg.get("worker-1")["status"] == "approved"


def test_approve两次换凭据旧的即失效(env):
    client, reg = env
    _seed(reg, "worker-1")
    first = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                        json={"confirm": True}).json()["credential"]
    second = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                         json={"confirm": True}).json()["credential"]
    assert first != second
    assert reg.verify("worker-1", first)["ok"] is False
    assert reg.verify("worker-1", second)["ok"] is True


def test_approve不带confirm_400且不改状态(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/approve", headers=AUTH, json={})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert reg.get("worker-1")["status"] == "pending"


def test_未知代理_404(env):
    client, _ = env
    r = client.post("/api/v1/agents/查无此代理/approve", headers=AUTH, json={"confirm": True})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "agent_not_found"


# ---------------------------------------------------------------- 吊销

def test_revoke确认串是agent前8位(env):
    """🔴 确认串 = agent_id **前 8 位**（不是整串、不是凭据提示位）——
    漏掉这个闸 = 顺手点一下就吊销掉一个正在服务的代理。"""
    client, reg = env
    _seed(reg, "worker-alpha-01")
    credential = reg.approve("worker-alpha-01")
    r = client.post("/api/v1/agents/worker-alpha-01/revoke", headers=AUTH,
                    json={"confirm": "worker-alpha-01"})      # 整串不行
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    r = client.post("/api/v1/agents/worker-alpha-01/revoke", headers=AUTH,
                    json={"confirm": "worker-alpha-01"[:8]})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert reg.verify("worker-alpha-01", credential)["ok"] is False


def test_revoke确认串不匹配_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "写错了"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"


# ---------------------------------------------------------------- 角色

def test_置primary回显旧主被降级(env):
    client, reg = env
    _seed(reg, "main", "worker-1")
    client.post("/api/v1/agents/main/role", headers=AUTH,
                json={"role": "primary", "confirm": "main"})
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    body = r.json()
    assert body["role"] == "primary" and body["demoted"] == ["main"]
    assert "旧主已降" in body["note"] or "降为 sub" in body["note"]
    roles = {a["agent_id"]: a["role"]
             for a in client.get("/api/v1/agents", headers=AUTH).json()["items"]}
    assert roles == {"main": "sub", "worker-1": "primary"}


def test_角色非法_400(env):
    client, reg = env
    _seed(reg, "main")
    r = client.post("/api/v1/agents/main/role", headers=AUTH,
                    json={"role": "boss", "confirm": "main"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_role"


def test_角色确认串必须是完整agent_id(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"


def test_置sub不留降级说明(env):
    client, reg = env
    _seed(reg, "main")
    reg.set_role("main", "primary")
    body = client.post("/api/v1/agents/main/role", headers=AUTH,
                       json={"role": "sub", "confirm": "main"}).json()
    assert body["role"] == "sub" and body["demoted"] == [] and body["note"] == ""


# ---------------------------------------------------------------- 上下文开关

def test_exclude开关(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                    json={"exclude": True, "confirm": True})
    assert r.status_code == 200 and r.json()["exclude_from_primary"] is True
    assert reg.get("worker-1")["exclude_from_primary"] is True
    # 再点一次同值 = 无变化（幂等，面板可据此知道「其实没改」）
    again = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                        json={"exclude": True, "confirm": True}).json()
    assert again["changed"] is False
    off = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                      json={"exclude": False, "confirm": True}).json()
    assert off["changed"] is True


def test_exclude不带confirm_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH, json={"exclude": True})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


# ------------------------------------------------- 现役 primary 的三道闸（409）

def _primary(reg: Registry, agent_id: str = "main") -> str:
    """造一个**现役** primary：已批准 + 已上位（= 真正在放行读写的那一位）。"""
    reg.register(agent_id, agent_id)
    reg.approve(agent_id)
    reg.set_role(agent_id, "primary")
    return agent_id


@pytest.mark.parametrize("path,body", [
    ("revoke", {"confirm": "main"}),
    ("role", {"role": "sub", "confirm": "main"}),
    ("exclude", {"confirm": True, "exclude": True}),
])
def test_动现役primary_409(env, path, body):
    """🔴 现役 primary 的 revoke / role / exclude 一律 409。

    前端不给那三个钮只是「不让人看见」；绕开 UI 直接打 API 一样能点进来，
    而吊销/降级主脑 = 写闸 fail-closed 把主脑降成 sub ⇒ 常驻写入全降级、
    候选审批链瘫痪。**必须先设别人为主**（那条路径会自动把旧主降级）。
    """
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    r = client.post(f"/api/v1/agents/main/{path}", headers=AUTH, json=body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "primary_protected"
    row = reg.get("main")
    assert row["status"] == "approved" and row["role"] == "primary"


def test_先设别人为主再动旧主_路径通(env):
    """409 拦的是「直接动它」，不是「永远不能动它」：换完主之后旧主就是普通 sub。"""
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    # 换主：把 worker-1 设为 primary（旧主由 registry 自动降级，不走被拦的那条路）
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    assert r.status_code == 200 and r.json()["demoted"] == ["main"]
    # 现在 main 已是普通 sub，三条路全通
    assert client.post("/api/v1/agents/main/exclude", headers=AUTH,
                       json={"confirm": True, "exclude": True}).status_code == 200


def test_被拦的写也要留审计(env, tmp_path):
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    client.post("/api/v1/agents/main/revoke", headers=AUTH, json={"confirm": "main"})
    actions = {(l["action"], l["ok"]) for l in _audit_lines(tmp_path)}
    assert ("agents.revoke", False) in actions


# ---------------------------------------------------------------- 搜索 / 分页

def test_q按agent_id与name大小写不敏感过滤(env):
    client, reg = env
    reg.register("Main-Brain", "主脑进程")
    reg.register("worker-Alpha", "Alpha 子代理")
    reg.register("worker-beta", "Beta 子代理")
    body = client.get("/api/v1/agents?q=ALPHA", headers=AUTH).json()
    assert [a["agent_id"] for a in body["items"]] == ["worker-Alpha"]
    assert body["count"] == 1
    # name 侧同样命中（子串、忽略大小写）
    body = client.get("/api/v1/agents?q=主脑", headers=AUTH).json()
    assert [a["agent_id"] for a in body["items"]] == ["Main-Brain"]
    body = client.get("/api/v1/agents?q=worker", headers=AUTH).json()
    assert body["count"] == 2


def test_q为空即全量且count是过滤后总数(env):
    client, reg = env
    _seed(reg, "worker-1", "worker-2", "worker-3")
    assert client.get("/api/v1/agents", headers=AUTH).json()["count"] == 3
    assert client.get("/api/v1/agents?q=", headers=AUTH).json()["count"] == 3
    assert client.get("/api/v1/agents?q=worker-2", headers=AUTH).json()["count"] == 1


def test_服务端分页_items是切片count是全量(env):
    """🔴 分页只切 items，**count 恒是过滤后的总数**（PagerBar 靠它算页数）。"""
    client, reg = env
    _seed(reg, *[f"worker-{i:02d}" for i in range(25)])
    p1 = client.get("/api/v1/agents?page=1&page_size=10", headers=AUTH).json()
    assert p1["count"] == 25 and len(p1["items"]) == 10
    p3 = client.get("/api/v1/agents?page=3&page_size=10", headers=AUTH).json()
    assert len(p3["items"]) == 5
    assert p1["items"][0]["agent_id"] == "worker-00"
    assert p3["items"][-1]["agent_id"] == "worker-24"


def test_分页越界返回空items不是报错(env):
    client, reg = env
    _seed(reg, "worker-1")
    body = client.get("/api/v1/agents?page=99&page_size=20", headers=AUTH).json()
    assert body["items"] == [] and body["count"] == 1


def test_pending_count是全量口径不受q影响(env):
    """🔴 红线：红条上的待审数必须永远等于**全库**待审数，不能被搜索词吞掉。

    被 q 过滤掉的待审代理此刻正在闸门外被拒 —— 若它从红条上消失，
    面板上唯一那个「有人正在被拒」的信号就被搜索框关掉了。
    """
    client, reg = env
    _seed(reg, "worker-1", "worker-2")
    reg.approve("worker-2")                      # 只剩 worker-1 是 pending
    body = client.get("/api/v1/agents?q=worker-2", headers=AUTH).json()
    assert body["count"] == 1 and len(body["items"]) == 1
    assert body["pending_count"] == 1, "q 命中的是已批准的那条，待审数仍应是全库的 1"


def test_page_size上限100(env):
    client, _ = env
    assert client.get("/api/v1/agents?page_size=101", headers=AUTH).status_code == 422
    assert client.get("/api/v1/agents?page_size=100", headers=AUTH).status_code == 200


# ---------------------------------------------------------------- 审计

def test_审计留痕且不含凭据明文(env, tmp_path):
    client, reg = env
    _seed(reg, "worker-1")
    credential = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                             json={"confirm": True}).json()["credential"]
    client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "worker-1"})
    client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "写错了"})
    lines = _audit_lines(tmp_path)
    actions = {(l["action"], l["ok"]) for l in lines}
    assert ("agents.approve", True) in actions
    assert ("agents.revoke", True) in actions
    assert ("agents.revoke", False) in actions          # 被拒绝的写也要留痕
    assert credential not in json.dumps(lines, ensure_ascii=False)


def test_写前备份落盘(env, tmp_path):
    """改角色会连带降级别人 ⇒ 改前必须有一份注册表快照可人工复原。"""
    client, reg = env
    _seed(reg, "main", "worker-1")
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    backup = r.json()["backup"]
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert {a["agent_id"] for a in saved} == {"main", "worker-1"}


# ---------------------------------------------------------------- 写面枚举

def test_写面枚举_四条agents写端点都在(env):
    """与 tests/api/test_api_readonly.py 的白名单互为反面：少挂一条依赖就红。"""
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert set(WRITE_PATHS) <= paths
    for p in WRITE_PATHS:
        route = next(r for r in app.routes if getattr(r, "path", "") == p)
        names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        assert any("require_session" in n for n in names), p
        assert any("write_guard" in n for n in names), p
