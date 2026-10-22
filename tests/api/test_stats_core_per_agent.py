"""`GET /api/v1/stats/core-per-agent` 的单测（控制台三页的代理维度共用一份口径）。

覆盖：
  * 两代理各造数据 ⇒ 按 `agent:` 归属聚合正确（条数 / 字符数）；
  * **NULL 预算回退全局**、自设预算生效（各用各自生效预算）；
  * 在册代理哪怕 0 条也出现在分列里；
  * 无归属条目（老库没打 `agent:` 标签）**不进**任何代理的分列；
  * core 条目上还挂着、注册表里已经没有的归属 ⇒ 照报不吞；
  * 只读端点：鉴权同页内其他 stats（无 token ⇒ 401）。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` 表）——
不连 Redis / PG / 生产库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.govern.registry import Registry

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 测试里把全局注入预算钉死（生产口径是 config.json，测试不许漂）
GLOBAL_BUDGET = 60000

URL = "/api/v1/stats/core-per-agent"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite 存储 + 固定 token + 钉死的全局预算。"""
    from keepsake.core.sqlite import SqliteStorage
    from keepsake.api.routers import stats as stats_router

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    monkeypatch.setattr(stats_router, "core_char_budget", lambda: GLOBAL_BUDGET)
    storage = SqliteStorage(path=str(tmp_path / "core-per-agent.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, Registry(storage), storage
    app.dependency_overrides.clear()
    storage.close()


def _core(storage, rows) -> None:
    """写几条常驻条目（`tags` 里带 `agent:` 归属标签）。"""
    assert storage.write_fragments_batch([
        {"key": key, "content": content, "tags": tags} for key, content, tags in rows
    ]) == len(rows)


def _get(client):
    resp = client.get(URL, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 聚合

def test_两代理各造数据_聚合正确(env):
    client, reg, storage = env
    reg.register("main-brain", "主脑")
    reg.register("zly-hermes", "子代理")
    _core(storage, [
        ("memory:frag:a1", "第一条常驻", "core,agent:main-brain"),
        ("memory:frag:a2", "第二条常驻", "core,agent:main-brain"),
        ("memory:frag:b1", "子代理的常驻", "core,agent:zly-hermes"),
    ])
    body = _get(client)
    assert body["main-brain"]["count"] == 2
    assert body["main-brain"]["chars"] == len("第一条常驻") + len("第二条常驻")
    assert body["zly-hermes"]["count"] == 1
    assert body["zly-hermes"]["chars"] == len("子代理的常驻")


def test_归属判据是逗号边界的完整标签(env):
    """🔴 `agent:zly-hermes` 不该命中 `agent:zly-hermes-2`（与可见性同一把尺子）。

    两个归属各归各的：`zly-hermes` 一条都没有，`zly-hermes-2` 自己一格。
    """
    client, reg, storage = env
    reg.register("zly-hermes", "子代理")
    _core(storage, [("memory:frag:c1", "别人的", "core,agent:zly-hermes-2")])
    body = _get(client)
    assert body["zly-hermes"]["count"] == 0
    assert body["zly-hermes-2"]["count"] == 1


def test_无归属条目不进任何代理的分列(env):
    """老库里没打 `agent:` 标签的条目不属于任何代理（总量仍看 core/stats）。"""
    client, reg, storage = env
    reg.register("main-brain", "主脑")
    _core(storage, [("memory:frag:d1", "无归属的一条", "core")])
    body = _get(client)
    assert body["main-brain"]["count"] == 0
    total = client.get("/api/v1/core/stats", headers=AUTH).json()
    assert total["count"] == 1 and total["chars"] == len("无归属的一条")


# ---------------------------------------------------------------- 预算

def test_预算NULL回退全局_自设预算生效(env):
    client, reg, storage = env
    reg.register("main-brain", "主脑")
    reg.register("zly-hermes", "子代理")
    assert reg.set_core_budget_chars("zly-hermes", 30000) is True
    body = _get(client)
    assert body["main-brain"]["budget_effective"] == GLOBAL_BUDGET   # NULL ⇒ 沿用全局
    assert body["zly-hermes"]["budget_effective"] == 30000            # 自己设的生效


def test_在册代理哪怕0条也出现在分列里(env):
    """分列要看得见「谁还没占常驻层」—— 否则「zly 0/60000」这种行根本没地方显示。"""
    client, reg, storage = env
    reg.register("main-brain", "主脑")
    reg.register("zly-hermes", "子代理")
    body = _get(client)
    assert set(body) == {"main-brain", "zly-hermes"}
    assert body["zly-hermes"]["count"] == 0 and body["zly-hermes"]["chars"] == 0


def test_注册表没有的归属照报不吞(env):
    """代理被删 / 改名后，core 条目上的归属标签还在 ⇒ 照报（不静默丢）。"""
    client, reg, storage = env
    reg.register("main-brain", "主脑")
    _core(storage, [("memory:frag:e1", "老代理的常驻", "core,agent:gone-agent")])
    body = _get(client)
    assert body["gone-agent"]["count"] == 1
    assert body["gone-agent"]["chars"] == len("老代理的常驻")
    assert body["gone-agent"]["budget_effective"] == GLOBAL_BUDGET


# ---------------------------------------------------------------- 鉴权

def test_无token_401(env):
    client, _, _ = env
    assert client.get(URL).status_code == 401


def test_只读端点_不改任何状态(env):
    """🔴 只读：调完再查注册表，行不许有任何变化。"""
    client, reg, _ = env
    reg.register("main-brain", "主脑")
    before = reg.list_agents()
    _get(client)
    assert reg.list_agents() == before
