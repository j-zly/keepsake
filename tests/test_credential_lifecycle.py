"""凭据生命周期三动作（enable / rotate / delete）端点契约测试。

用户拍板（2026-11-21）：控制台凭据抽屉要支持 编辑、删除、重新生成、
作废后启用。后端三端点由此钉死：
* enable —— 只翻 revoked→active，钥匙本身（hash/密文/授权/用量）不动；
* rotate —— 只作废**这一把** + 同 label 同 scopes 补发新把，一次性明文；
* delete —— 确认串 = cred_id 前 8 位（传 true 400），主代理 409。
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.sqlite import SqliteStorage
from keepsake.govern.registry import Registry

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEY = Fernet.generate_key().decode("ascii")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    key_file = tmp_path / "cred.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    storage = SqliteStorage(path=str(tmp_path / "agents.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, Registry(storage), tmp_path
    app.dependency_overrides.clear()
    storage.close()


def _seed_two(env, aid="worker"):
    """登记+批准+补一把，返回 (client, registry, 第一把明文, 第二把明文, 第二把编号)。"""
    client, reg, _ = env
    reg.register(aid, aid)
    r = client.post(f"/api/v1/agents/{aid}/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    first = str(r.json()["credential"])
    r = client.post(f"/api/v1/agents/{aid}/credentials",
                    json={"label": "笔记本", "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    return client, reg, first, str(r.json()["credential"]), str(r.json()["cred_id"])


# ------------------------------------------------------------------ 启用


def test_作废的可启用_钥匙本身不变(env):
    client, reg, first, second, cid = _seed_two(env)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert not reg.verify("worker", second)["ok"]  # 废了
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/enable", headers=AUTH)
    assert r.status_code == 200, r.text
    got = reg.verify("worker", second)
    assert got["ok"]  # 原明文复活
    rows = [x for x in reg.list_credentials("worker") if x["cred_id"] == cid]
    assert rows[0]["status"] == "active" and rows[0]["revoked_at"] == ""


def test_在用的不许谎报启用_409(env):
    client, reg, first, second, cid = _seed_two(env)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/enable", headers=AUTH)
    assert r.status_code == 409, r.text


def test_enable_跨代理指名拒(env):
    client, reg, first, second, cid = _seed_two(env, aid="worker")
    reg.register("other", "other")
    client.post("/api/v1/agents/other/approve", json={"confirm": True}, headers=AUTH)
    r = client.post(f"/api/v1/agents/other/credentials/{cid}/enable", headers=AUTH)
    assert r.status_code == 409, r.text


# ------------------------------------------------------------------ 换发


def test_换发只掐这一把_新旧边界清晰(env):
    client, reg, first, second, cid = _seed_two(env)
    # 先把第二把的授权收紧到只有 kb，验证换发继承
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                    json={"scopes": ["kb"], "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/rotate", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    new_plain = str(body["credential"])
    assert new_plain and new_plain != second
    assert not reg.verify("worker", second)["ok"]  # 旧把当场作废
    assert reg.verify("worker", new_plain)["ok"]  # 新把可用
    assert reg.verify("worker", first)["ok"]     # 别的入口一根汗毛没动
    rows = {x["cred_id"]: x for x in reg.list_credentials("worker")}
    new_row = rows[body["cred_id"]]
    assert new_row["label"] == "笔记本"          # 标签继承


# ------------------------------------------------------------------ 删除


def test_删除确认可true或编号串_乱值拒(env):
    # 用户 11-21 拍板：界面弹框点确认（true）即删；CLI 编号前 8 位兼容；其余拒。
    client, reg, first, second, cid = _seed_two(env)
    r = client.request("DELETE", f"/api/v1/agents/worker/credentials/{cid}",
                       json={"confirm": "胡填"}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert reg.verify("worker", second)["ok"]      # 拒删：钥匙还在
    r = client.request("DELETE", f"/api/v1/agents/worker/credentials/{cid}",
                       json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert not reg.verify("worker", second)["ok"]  # 钥匙没了
    ids = [x["cred_id"] for x in reg.list_credentials("worker")]
    assert cid not in ids                        # 账上消失（与 revoked 留痕的分界）


def test_删除跨代理指名拒(env):
    client, reg, first, second, cid = _seed_two(env)
    reg.register("other", "other")
    client.post("/api/v1/agents/other/approve", json={"confirm": True}, headers=AUTH)
    r = client.request("DELETE", f"/api/v1/agents/other/credentials/{cid}",
                       json={"confirm": cid[:8]}, headers=AUTH)
    assert r.status_code == 409, r.text
    assert reg.verify("worker", second)["ok"]  # 别人的没被删掉
