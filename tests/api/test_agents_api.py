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
