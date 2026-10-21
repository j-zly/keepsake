"""per-agent 注入预算的控制台端点测试（`ks_agent_budget`）。

覆盖：读面字段 / 设置与清空往返 / 非法值 400 / confirm 与未知代理的拒绝 /
审计与备份 / 401。

端点与记忆共享开关（`share`）同一套样板：`require_session` + `write_guard` +
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

BUDGET_URL = "/api/v1/agents/{agent_id}/budget"


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
    storage = SqliteStorage(path=str(tmp_path / "budget.db"))
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


def _set(client, agent_id: str, value: Any, confirm: Any = True):
    return client.post(BUDGET_URL.format(agent_id=agent_id), headers=AUTH,
                       json={"core_budget_chars": value, "confirm": confirm})


# ---------------------------------------------------------------- 读面

def test_列表带回core_budget_chars字段(env):
    """🔴 `GET /api/v1/agents` 要带出这一列（控制台的编辑框读的就是它）。"""
    client, reg = env
    reg.register("worker-1", "工人")
    item = client.get("/api/v1/agents", headers=AUTH).json()["items"][0]
    assert "core_budget_chars" in item
    assert item["core_budget_chars"] is None      # 缺省 = 沿用全局


# ---------------------------------------------------------------- 写面

def test_设置后列表与注册表都跟着变(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = _set(client, "worker-1", 100)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["core_budget_chars"] == 100 and body["changed"] is True
    assert reg.core_budget_chars("worker-1") == 100
    item = client.get("/api/v1/agents", headers=AUTH).json()["items"][0]
    assert item["core_budget_chars"] == 100


def test_清空回null即沿用全局(env):
    """🔴 清空输入框 ⇒ NULL ⇒ 沿用全局（不是「预算 0」）。"""
    client, reg = env
    reg.register("worker-1", "工人")
    _set(client, "worker-1", 100)
    resp = _set(client, "worker-1", None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["core_budget_chars"] is None
    assert reg.core_budget_chars("worker-1") is None


def test_再设一次幂等且报未变化(env):
    client, reg = env
    reg.register("worker-1", "工人")
    _set(client, "worker-1", 100)
    resp = _set(client, "worker-1", 100)
    assert resp.json()["changed"] is False


@pytest.mark.parametrize("bad", [0, -1, "abc", 1.5, True])
def test_非法值400且不改(env, bad):
    """🔴 0 / 负数 / 非整数 / 布尔一律 400，且**一个字节都没改**。

    静默回落全局的话，操作者以为改成了 —— 那正是这套治理要拔掉的「以为改成了」。
    """
    client, reg = env
    reg.register("worker-1", "工人")
    resp = _set(client, "worker-1", bad)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] in ("bad_core_budget", "core_budget_out_of_range")
    assert reg.core_budget_chars("worker-1") is None


def test_不带confirm_400且不改(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = _set(client, "worker-1", 100, confirm=False)
    assert resp.status_code == 400, resp.text
    assert reg.core_budget_chars("worker-1") is None


def test_未知代理_404(env):
    client, _ = env
    resp = _set(client, "查无此代理", 100)
    assert resp.status_code == 404, resp.text


def test_无token_401(env):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = client.post(BUDGET_URL.format(agent_id="worker-1"),
                        json={"core_budget_chars": 100, "confirm": True})
    assert resp.status_code == 401


# ---------------------------------------------------------------- 审计与备份

def test_审计留痕且改前有备份(env, tmp_path):
    client, reg = env
    reg.register("worker-1", "工人")
    resp = _set(client, "worker-1", 100)
    backup = resp.json()["backup"]
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert saved["agent_id"] == "worker-1"
    lines = _audit_lines(tmp_path)
    assert any(l.get("action") == "agents.budget" and l.get("ok") for l in lines), lines


def test_被拒的写也留审计(env, tmp_path):
    client, reg = env
    reg.register("worker-1", "工人")
    _set(client, "worker-1", 0)                 # 非法值 ⇒ 400
    lines = _audit_lines(tmp_path)
    assert any(l.get("action") == "agents.budget" and not l.get("ok") for l in lines), lines
