"""记忆共享开关的控制台端点测试（`ks_share_memory`）。

覆盖：读面字段 / 开关往返 / confirm 与未知代理的拒绝 / 审计与备份 / 401。

端点与私信开关（`dm`）同一套样板：`require_session` + `write_guard` +
改前备份 + 一次审计（含被拒绝的写）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
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
    storage = SqliteStorage(path=str(tmp_path / "share.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, Registry(storage)
    app.dependency_overrides.clear()
    storage.close()


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    import pathlib

    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def test_列表带回share_memory字段(env):
    client, reg = env
    reg.register("worker-1", "工人")
    body = client.get("/api/v1/agents", headers=AUTH).json()
    item = body["items"][0]
    assert "share_memory" in item
    assert item["share_memory"] is False        # 缺省就是关


def test_开关打开后列表与注册表都跟着变(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share",
                        headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["share_memory"] is True and body["changed"] is True
    assert reg.share_memory("worker-1") is True
    item = client.get("/api/v1/agents", headers=AUTH).json()["items"][0]
    assert item["share_memory"] is True


def test_再开一次幂等且报未变化(env):
    client, reg = env
    reg.register("worker-1", "工人")
    client.post("/api/v1/agents/worker-1/share",
                headers=AUTH, json={"enabled": True, "confirm": True})
    resp = client.post("/api/v1/agents/worker-1/share",
                        headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.json()["changed"] is False


def test_不带confirm_400且不改(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share",
                        headers=AUTH, json={"enabled": True})
    assert resp.status_code == 400, resp.text
    assert reg.share_memory("worker-1") is False


def test_未知代理_404(env):
    client, reg = env
    resp = client.post("/api/v1/agents/查无此代理/share",
                        headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.status_code == 404, resp.text


def test_无token_401(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share",
                        json={"enabled": True, "confirm": True})
    assert resp.status_code == 401


def test_审计留痕且改前有备份(env, tmp_path):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share",
                        headers=AUTH, json={"enabled": True, "confirm": True})
    backup = resp.json()["backup"]
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert saved["agent_id"] == "worker-1"
    lines = _audit_lines(tmp_path)
    assert any(l.get("action") == "agents.share" and l.get("ok") for l in lines), lines


def test_被拒的写也留审计(env, tmp_path):
    client, reg = env
    reg.register("worker-1", "工人")
    client.post("/api/v1/agents/worker-1/share",
                headers=AUTH, json={"enabled": True})       # 缺 confirm ⇒ 400
    lines = _audit_lines(tmp_path)
    assert any(l.get("action") == "agents.share" and not l.get("ok") for l in lines), lines
