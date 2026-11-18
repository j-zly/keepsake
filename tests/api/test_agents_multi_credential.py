"""一代理多凭据（`ks_multi_credential`，2026-11-14 用户拍板）——
「一个代理可以生成多个凭证让多个代理连接，到后面我们可以透过凭证做权限」。

钉住的纪律（每条都是一个真会出错的口子）：

1. **多把并存**：新建一把**不顶掉**任何旧钥匙（这是与「重新生成凭据」的分界；
   换机器时新旧要同时在线）。两把都能鉴权才算这个功能成立。
2. **作废单把只掐这一把**：其余各把照旧能用、代理照旧在册；重复作废不谎报成功。
3. 🔴 **跨代理越权防线**：所有按 `cred_id` 的动作都必须**同时对上 agent_id**——
   只认编号的话，改一个 URL 就能废掉别人家的钥匙。
4. 🔴 **取出不许猜**：名下多把在用又没指名 ⇒ 409 `ambiguous_credential`。
   猜错就是把另一把的钥匙递出去，宁拒不猜。
5. 🔴 **单一鉴权面**：代理行的老三列（`credential_hash` 等）自迁移起**不再被读**。
   把一把"格式正确、只写在老三列上"的串喂给 `verify_any` ⇒ 必须拒。
   这条是整个改造最危险的回归点：两处都认 = 两本账迟早漂成两个安全等级。
6. **不绕过会话闸**：四条新端点无 token 一律 401；写面三条缺 `confirm` 一律 400。
7. **pending 行不发钥匙**：批准时才签发（否则等于绕过审核页放行）。
8. **改名/删除带走凭据**：`set_id` 后原串必须还能连（否则改个名 = 全部入口掉线）；
   `delete` 后凭据表不许留孤儿行（无人认领的活钥匙）。
9. **监管面真的有数**：逐把 `reads/used_at` 随打点增长，且**只涨这一把**。
10. 🔴 **不泄密**：读面 JSON 与审计行里不许出现明文与密文列。

存储用临时 SQLite（同 `test_agents_credential_reveal.py`），不连 PG / Redis / 生产库。
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.sqlite import SqliteStorage
from keepsake.govern.registry import ENC_COLUMN, Registry, hash_credential

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEY = Fernet.generate_key().decode("ascii")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 SQLite + 审计目录 + 主密钥在场（可还原档）。"""
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


def _audit_lines(tmp_path) -> Dict[str, Any]:
    import pathlib

    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    rows = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]
    return {"all": rows}


def _approved(client, reg, aid="worker",
              label="默认入口") -> "tuple[str, str, str]":
    """登记 + 批准 + 再加一把，返回第二把的明文（多数用例要的就是"两把并存"）。"""
    reg.register(aid, aid)
    r = client.post(f"/api/v1/agents/{aid}/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    first = str(r.json()["credential"])
    r = client.post(f"/api/v1/agents/{aid}/credentials",
                    json={"label": label, "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    return first, str(r.json()["credential"]), str(r.json()["cred_id"])


# ------------------------------------------------------------------ 并存

def test_新建一把不顶掉旧钥匙(env):
    client, reg, _ = env
    first, second, cid = _approved(client, reg)
    assert first != second
    # 两把都能鉴权（"多入口"这条承诺的全部内容）
    assert reg.verify_any(first)["ok"] is True
    assert reg.verify_any(second)["ok"] is True
    # 且**反查到同一个身份**：多把钥匙 = 同一个代理（记忆池共用，用户已确认）
    assert reg.verify_any(first)["agent_id"] == reg.verify_any(second)["agent_id"] == "worker"
    assert reg.verify_any(second)["cred_id"] == cid
    item = [x for x in client.get("/api/v1/agents", headers=AUTH).json()["items"]
            if x["agent_id"] == "worker"][0]
    assert item["credential_count"] == 2 and item["credential_active"] == 2


def test_未批准的行不发钥匙(env):
    client, reg, _ = env
    reg.register("pending-one", "p")
    r = client.post("/api/v1/agents/pending-one/credentials",
                    json={"label": "x", "confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_approved"
    assert reg.list_credentials("pending-one") == []


# ------------------------------------------------------------------ 作废单把

def test_作废一把另一把照旧连(env):
    client, reg, tmp_path = env
    first, second, cid = _approved(client, reg)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert reg.verify_any(second)["ok"] is False          # 掐掉了
    assert reg.verify_any(second)["reason"] == "bad_credential"
    assert reg.verify_any(first)["ok"] is True            # 另一把不受牵连
    # 作废的这把**密文也一起清**（留着就随时能取出明文，作废是假的）
    got = [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]
    assert got["status"] == "revoked" and got["recoverable"] is False
    # 重复作废 ⇒ 409（不谎报"刚废掉"）
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "credential_not_found"


def test_跨代理的编号一律动不了(env):
    client, reg, _ = env
    _first, _second, cid = _approved(client, reg, "worker")
    reg.register("other", "o")
    r = client.post("/api/v1/agents/other/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    # 🔴 只改 URL 里的 agent_id 就去废别人家的钥匙 ⇒ 必须拒
    for path, body in ((f"/api/v1/agents/other/credentials/{cid}/revoke", {"confirm": True}),
                       (f"/api/v1/agents/other/credentials/{cid}/rename", {"label": "劫持"})):
        r = client.post(path, json=body, headers=AUTH)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"]["error"] == "credential_not_found", r.text
    assert reg.verify_any(_second)["ok"] is True           # 那把照旧能用


# ------------------------------------------------------------------ 取出不许猜

def test_多把在用不指名就拒(env):
    client, reg, _ = env
    first, second, cid = _approved(client, reg)
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "ambiguous_credential"
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True, "cred_id": cid}, headers=AUTH)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["credential"] == second and got["cred_id"] == cid
    assert got["label"] == "默认入口"
    # 唯一在用那把可以不指名（旧调用方行为不变）
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                    json={"confirm": True}, headers=AUTH)
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["credential"] == first


# ------------------------------------------------------------------ 单一鉴权面

def test_老三列不再是鉴权面(env):
    """🔴 本次改造最危险的回归点：两本账都认 = 迟早漂成两个安全等级。

    手工把一把"格式正确"的凭据哈希只写进代理行的老列（不动凭据表），
    `verify_any` 必须认不出来 —— 否则旧列就是第二张鉴权面，
    作废/吊销在一张账上生效、另一张账上照旧连。
    """
    client, reg, _ = env
    reg.register("legacy", "l")
    r = client.post("/api/v1/agents/legacy/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    ghost = "ksk_" + "a" * 32
    reg._be.update("legacy", {          # noqa: SLF001（就是要打这条老列）
        "credential_hash": hash_credential(ghost),
        "credential_hint": ghost[:8],
        ENC_COLUMN: "",
    })
    assert reg.verify_any(ghost)["ok"] is False
    assert reg.verify_any(ghost)["reason"] == "unknown_agent"
    # 批准时真发的那把照旧能用（证明拒的是"只写老列"，不是整个代理挂了）
    assert reg.verify_any(str(r.json()["credential"]))["ok"] is True


def test_签发只动凭据账(env):
    client, reg, _ = env
    reg.register("worker", "w")
    r = client.post("/api/v1/agents/worker/approve", json={"confirm": True}, headers=AUTH)
    cred = str(r.json()["credential"])
    assert reg.verify("worker", cred)["ok"] is True
    row = reg._be.load("worker")        # noqa: SLF001（读原始行）
    assert not row["credential_hash"] and not row[ENC_COLUMN], "代理行老三列不许再被写"


# ------------------------------------------------------------------ 闸的形状

def test_四条新端点没有绕过会话闸的旁路(env):
    client, reg, _ = env
    _first, _second, cid = _approved(client, reg)
    paths = (
        ("/api/v1/agents/worker/credentials", "get", None),
        ("/api/v1/agents/worker/credentials", "post", {"label": "x"}),
        (f"/api/v1/agents/worker/credentials/{cid}/revoke", "post", {}),
        (f"/api/v1/agents/worker/credentials/{cid}/rename", "post", {"label": "y"}),
    )
    for path, verb, body in paths:
        call = getattr(client, verb)
        kwargs = {"json": body} if verb == "post" else {}
        assert call(path, **kwargs).status_code == 401, path      # 无 token ⇒ 先 401
    # 写面三条缺意图位 ⇒ 400（GET 是读，不需要）
    # 带 token 但缺意图位 ⇒ 才轮到确认闸说话（上面无 token 先 401 是顺序正确的表现）
    assert client.post("/api/v1/agents/worker/credentials",
                       json={"label": "x"}, headers=AUTH).status_code == 400
    assert client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                       json={}, headers=AUTH).status_code == 400


# ------------------------------------------------------------------ 搬账本

def test_改名带走凭据删除不留孤儿(env):
    client, reg, _ = env
    first, second, cid = _approved(client, reg)
    assert client.post("/api/v1/agents/worker/set_id",
                       json={"new_id": "worker2", "confirm": True},
                       headers=AUTH).status_code == 200
    # 改个名 = 全部入口掉线？绝不允许
    assert reg.verify_any(first)["ok"] is True and reg.verify_any(second)["ok"] is True
    assert reg.verify_any(second)["agent_id"] == "worker2"
    assert len(reg.list_credentials("worker2")) == 2
    # 吊销的手输档 = **当前** id 的前 8 位（改名后是 worker2；拿旧名来签 ⇒ 400，正是这道闸该做的）
    assert client.post("/api/v1/agents/worker2/revoke",
                       json={"confirm": "worker2"}, headers=AUTH).status_code == 200
    assert client.request("DELETE", "/api/v1/agents/worker2",
                          json={"confirm": "worker2"},
                          headers=AUTH).status_code == 200
    # 🔴 代理行没了，凭据表不许留孤儿（无人认领的活钥匙）
    assert reg.list_credentials("worker2") == []
    assert reg._cred.list("worker2") == []     # noqa: SLF001


# ------------------------------------------------------------------ 监管面

def test_逐把用量只涨这一把(env):
    client, reg, _ = env
    first, second, cid = _approved(client, reg)
    other = [c["cred_id"] for c in reg.list_credentials("worker") if c["cred_id"] != cid][0]
    for _ in range(3):
        reg.touch("worker", "read", cid)       # 限频 60s ⇒ 手动绕开节流直接打三次
        reg._last_touch.pop(f"worker#{cid}", None)     # noqa: SLF001（测试要看到计数在动）
    mine = [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]
    theirs = [c for c in reg.list_credentials("worker") if c["cred_id"] == other][0]
    assert mine["reads"] >= 3 and mine["used_at"]
    assert theirs["reads"] == 0, "另一把不许跟着涨（不然'哪台在跑'就白监管了）"


def test_标签可改且不碰鉴权(env):
    client, reg, _ = env
    first, second, cid = _approved(client, reg)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/rename",
                    json={"label": "win 笔记本"}, headers=AUTH)
    assert r.status_code == 200, r.text
    got = [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]
    assert got["label"] == "win 笔记本"
    assert reg.verify_any(second)["ok"] is True      # 改个标签不该把钥匙弄坏


# ------------------------------------------------------------------ 不泄密

def test_读面与审计都不含凭据材料(env):
    client, reg, tmp_path = env
    _first, second, cid = _approved(client, reg)
    body = client.get("/api/v1/agents", headers=AUTH).text
    raw = client.get("/api/v1/agents/worker/credentials", headers=AUTH).text
    assert "credential_hash" not in raw and "gAAAA" not in raw
    assert second not in raw and second not in body
    assert ENC_COLUMN not in raw
    items = client.get("/api/v1/agents/worker/credentials", headers=AUTH).json()["items"]
    # 只许出现 hint（8 位）这一档，且必须与账上真列的前 8 位一致
    assert len(items) == 2
    for row, item in zip(reg._cred.list("worker"), items):     # noqa: SLF001
        assert item["credential_hint"] == row["credential_hint"][:8]
        assert len(item["credential_hint"]) == 8
    assert all("credential_hash" not in i for i in items)
    client.post("/api/v1/agents/worker/credentials", json={"label": "再一把", "confirm": True},
                headers=AUTH)
    client.post(f"/api/v1/agents/worker/credentials/{cid}/revoke",
                json={"confirm": True}, headers=AUTH)
    dumped = json.dumps(_audit_lines(tmp_path)["all"], ensure_ascii=False)
    assert second not in dumped and "ksk_" not in dumped, "审计里不许出现凭据（连前缀都不必）"
