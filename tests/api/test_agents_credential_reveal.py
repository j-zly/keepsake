"""凭据「可还原存档」（重复复制 / 回填）—— 2026-11-14 用户拍板：
「肯定是不能换发啊，是直接能复制」。

钉住的纪律（每条都对应一个真实会出错的口子）：

1. **签发即落密文**，且**校验面只认 sha256**：`verify()` 用的还是 hash，
   密文坏了不影响代理读写（这条是"改动不碰鉴权路径"的证据）。
2. **取出的明文真的能用**：reveal 出来的串再过 `verify` 必须 ok ——
   否则"能复制"就是句假话（复制一把不能用钥匙比不给钥匙更糟）。
3. **可重复**：连取两次内容逐字一致（用户的原始诉求就是这个）。
4. **闸的形状（2026-11-14 二次定档）**：取出与回填都只要显式意图位 `confirm=true`（用户原话
   「复制是直接能复制不用输入什么东西」）；缺位/`false` 仍 400（挡盲发请求），而防认错行
   在回填那条路上由 **sha256 对账**承担（粘错串整体拒绝），比手输更硬。
5. **没主密钥 ⇒ fail-closed**：签发照旧成功、`recoverable=false`、reveal 说清楚原因；
   🔴 绝不允许任何路径把**明文**写进库里。
6. **老行回填**：清掉密文（模拟机制前签发的行）⇒ reveal 报 `not_recoverable`；
   回填**必须比对 sha256** —— 粘错串 ⇒ 拒绝且**一个字节都不改**；
   粘对 ⇒ 之后可重复取出。
7. **作废要作干净**：`revoke` / `revoke_credential` 之后密文必须一起清掉，
   否则"钥匙已废"的行还能取出明文（这是本次改动最危险的反例）。
8. **换过密钥 ⇒ 明说**，不静默返回空串当成功。
9. **明文 / 密文都不许外泄**：审计行里不含串、读面 JSON 里不含密文列。

存储用临时 SQLite（同 `test_agents_api.py`），不连 PG / Redis / 生产库。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.sqlite import SqliteStorage
from keepsake.govern import credcrypto
from keepsake.govern.registry import ENC_COLUMN, Registry, hash_credential

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEY = Fernet.generate_key().decode("ascii")


# ------------------------------------------------------------------ 夹具

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """控制台环境：临时 SQLite + 审计目录 + **主密钥在场**（走 KEY_FILE 那条路）。"""
    key_file = tmp_path / "cred.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY", raising=False)
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
        yield client, Registry(storage), tmp_path
    app.dependency_overrides.clear()
    storage.close()


def _raw(reg: Registry, aid: str) -> Dict[str, Any]:
    """代理**原始行**（含已冻结的老三列）。

    🔴 2026-11-14 多凭据之后，密文与哈希的真列在 **`ks_credential`** 上 ——
    要验"库里到底存了什么"必须读 `_cred_raw()`；本函数留着正是为了钉
    「代理行上那几个老字段不再被写」（见 test_签发落密文且校验面仍只认hash）。
    """
    return reg._be.load(aid)        # noqa: SLF001（测试就是要读原始行）


def _cred_raw(reg: Registry, aid: str, nth: int = 0) -> Dict[str, Any]:
    """凭据账的**原始行**（含 hash 与密文）。`nth` 按签发序取第几把。"""
    rows = reg._cred.list(aid)      # noqa: SLF001（同上：验的是库里的真列）
    assert rows, f"{aid} 名下没有任何凭据账"
    return rows[nth]


def _seed_approved(client, reg: Registry, aid: str = "worker") -> str:
    """登记 + 批准，返回一次性凭据明文（测试内部持有，绝不写盘断言用）。"""
    reg.register(aid, aid)
    r = client.post(f"/api/v1/agents/{aid}/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    return str(r.json()["credential"])


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    import pathlib

    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files
            for line in f.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ 加密件本身

def test_加密件三档密钥来源(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY_FILE", raising=False)
    assert credcrypto.available() is False
    assert credcrypto.encrypt("ksk_x") is None                  # 没密钥 ⇒ 不存
    assert credcrypto.decrypt_with_reason("gAAA...") == (None, "key_missing")

    # 直接给环变量
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY", KEY)
    assert credcrypto.key_source() == credcrypto.SOURCE_ENV
    tok = credcrypto.encrypt("ksk_abc123")
    assert tok and credcrypto.decrypt(tok) == "ksk_abc123"

    # EnvironmentFile 形状（KEY=value 也能读，同一个文件两边通用）
    f = tmp_path / "k.env"
    f.write_text("KEEPSAKE_CREDENTIAL_KEY=" + KEY + "\n", encoding="utf-8")
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(f))
    assert credcrypto.key_source() == credcrypto.SOURCE_FILE
    assert credcrypto.decrypt(tok) == "ksk_abc123"

    # 坏文件 ⇒ 按「没有密钥」处理，不抛（不能把签这条路打死）
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(tmp_path / "不存在"))
    assert credcrypto.available() is False


def test_裸key文件里的等号填充不许被当成分隔符(tmp_path, monkeypatch):
    """2026-11-14 实踩的 bug：Fernet key 是 urlsafe-base64、**结尾带 `=`**。
    早先按"行里含 `=` 就切一刀"解析 EnvironmentFile ⇒ 裸 key 被从第一个 `=` 截断成空串，
    症状是 `key_source()` 报 file 却 `available()` 报 False、密文列永远是空。
    两种文件形状都必须可用。"""
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY", raising=False)
    bare = tmp_path / "bare.key"
    bare.write_text(KEY + "\n", encoding="utf-8")          # 裸 key（含 = 填充）
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(bare))
    assert credcrypto.available() is True, "裸 key 整行必须当值用"
    assert "=" in KEY, "这条测试的前提：key 本身含等号填充"
    tok = credcrypto.encrypt("ksk_padding_matters")
    assert tok and credcrypto.decrypt(tok) == "ksk_padding_matters"

    shaped = tmp_path / "shaped.env"
    shaped.write_text("KEEPSAKE_CREDENTIAL_KEY=" + KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(shaped))
    assert credcrypto.decrypt(tok) == "ksk_padding_matters", "KEY=value 形状同样可用"


def test_换密钥后解不开要报得出来(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY", KEY)
    tok = credcrypto.encrypt("ksk_secret")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))
    assert credcrypto.decrypt_with_reason(tok) == (None, "key_mismatch")
    assert credcrypto.decrypt_with_reason("") == (None, "no_ciphertext")


# ------------------------------------------------------------------ 签发 / 取出

def test_签发落密文且校验面仍只认hash(env):
    client, reg, _ = env
    cred = _seed_approved(client, reg, "worker")
    row = _cred_raw(reg, "worker")
    assert row[ENC_COLUMN], "签发后必须落密文"
    assert cred not in row[ENC_COLUMN]            # 库里存的是密文不是明文
    assert row["credential_hash"] == hash_credential(cred)
    # 校验面走的还是 hash：密文列清空也不影响 verify
    assert reg.verify("worker", cred)["ok"] is True
    # 🔴 多凭据的单一事实源：签发动**只**落凭据账，代理行老三列一个字都不写。
    #（老三列只是迁移前的快照 —— 还在写它就有第二张鉴权面，两边迟早漂。）
    legacy = _raw(reg, "worker")
    assert not legacy["credential_hash"] and not legacy[ENC_COLUMN]


def test_取出的明文真能用且可重复(env):
    client, reg, tmp_path = env
    cred = _seed_approved(client, reg, "worker")
    got = None
    for _ in range(2):
        r = client.post("/api/v1/agents/worker/reveal_credential",
                        json={"confirm": True}, headers=AUTH)      # 意图位：不需要人打字
        assert r.status_code == 200, r.text
        assert r.json()["credential"] == cred, "连取两次必须逐字一致（重复复制的本义）"
        got = r.json()["credential"]
    # 🔴 取出来的串必须真能过校验 —— 否则"能复制"是假话
    assert got == cred and reg.verify("worker", got)["ok"] is True
    lines = _audit_lines(tmp_path)
    takes = [x for x in lines if x["action"] == "agents.reveal_credential"]
    assert len(takes) == 2 and all(x["ok"] for x in takes)
    # 审计里不许有凭据（明文与密文都不许）
    assert all(cred not in json.dumps(x, ensure_ascii=False) for x in takes)


def test_一键取出只要意图位_不再要求手输(env):
    """2026-11-14 用户二次定档：「复制是直接能复制不用输入什么东西」。

    钉三件事，缺一就算口径漂移：
      ① `confirm: true` 就能取出（人不需要打任何字）；
      ② 缺位 / `false` / 拿旧形状"手输一串 id"来当值，仍然 400 —— 意图位只认真正的
         `true`，这一位是**防盲发请求**的，不许连它一起删掉；
      ③ 未认证仍然 401、不在册仍然 404（会话闸与存在性闸没被这次改动带松）。
    """
    client, reg, _ = env
    cred = _seed_approved(client, reg, "worker")
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["credential"] == cred, "点一下就得出整条串"
    for body in ({}, {"confirm": False}, {"confirm": "worker"}):
        rr = client.post("/api/v1/agents/worker/reveal_credential", json=body, headers=AUTH)
        assert rr.status_code == 400, f"{body} 不该成功"
    assert client.post("/api/v1/agents/nope/reveal_credential",
                       json={"confirm": True}, headers=AUTH).status_code == 404
    assert client.post("/api/v1/agents/worker/reveal_credential",
                       json={"confirm": True}).status_code == 401


def test_回填的防认错行靠对账不靠手输(env):
    """回填也不需要打字：粘错串由 sha256 对账拒绝（`credential_mismatch`），
    缺意图位仍然 400。这两条各钉一次，别把它们混成"同一个闸"。"""
    client, reg, _ = env
    cred = _seed_approved(client, reg, "worker")
    _wipe_enc(reg, "worker")
    r = client.post("/api/v1/agents/worker/backfill_credential",
                    json={"credential": cred, "confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["recoverable"] is True
    r = client.post("/api/v1/agents/worker/backfill_credential",
                    json={"credential": cred}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


def test_读面不含密文只给布尔(env):
    client, reg, _ = env
    _seed_approved(client, reg, "worker")
    body = client.get("/api/v1/agents", headers=AUTH).json()
    item = [x for x in body["items"] if x["agent_id"] == "worker"][0]
    assert item["recoverable"] is True
    raw = json.dumps(body, ensure_ascii=False)
    assert "credential_enc" not in raw and "gAAAA" not in raw
    assert client.get("/api/v1/agents", headers=AUTH).status_code == 200


# ------------------------------------------------------------------ 没有主密钥

def test_没主密钥时签发照旧但不可还原(env, monkeypatch):
    client, reg, _ = env
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("KEEPSAKE_CREDENTIAL_KEY", raising=False)
    cred = _seed_approved(client, reg, "worker")
    row = _cred_raw(reg, "worker")
    assert row["credential_hash"] == hash_credential(cred)
    assert not row[ENC_COLUMN], "没密钥 ⇒ 密文列为空（不许把明文塞进去）"
    # 两个账都扫一遍：明文不许落在任何一处（凭据账是主战场，代理行也别漏）
    dumped = json.dumps([{k: str(v) for k, v in r.items()}
                         for r in (row, _raw(reg, "worker"))], ensure_ascii=False)
    assert cred not in dumped
    item = [x for x in client.get("/api/v1/agents", headers=AUTH).json()["items"]
            if x["agent_id"] == "worker"][0]
    assert item["recoverable"] is False
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_recoverable"
    # 回填这时也做不了（没密钥写不了密文），且必须明说而不是"成功"
    r = client.post("/api/v1/agents/worker/backfill_credential",
                    json={"credential": cred, "confirm": True}, headers=AUTH)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "key_missing"
    assert not _raw(reg, "worker")[ENC_COLUMN]


# ------------------------------------------------------------------ 老行回填

def _wipe_enc(reg: Registry, aid: str) -> None:
    """清空密文 = 模拟「可还原机制上线前签发的老凭据」（账上只有 sha256）。"""
    for row in reg._cred.list(aid):                   # noqa: SLF001（同上）
        reg._cred.set_fields(row["cred_id"], {ENC_COLUMN: ""})


def test_老行reveal报not_recoverable(env):
    client, reg, _ = env
    _seed_approved(client, reg, "worker")
    _wipe_enc(reg, "worker")
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_recoverable"
    assert "回填" in r.json()["detail"]["message"]


def test_回填粘错串拒绝且不改一个字节(env):
    client, reg, tmp_path = env
    cred = _seed_approved(client, reg, "worker")
    _wipe_enc(reg, "worker")
    before = dict(_raw(reg, "worker"))
    r = client.post("/api/v1/agents/worker/backfill_credential",
                    json={"credential": "ksk_" + "0" * 32, "confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "credential_mismatch"
    after = dict(_raw(reg, "worker"))
    assert after["credential_hash"] == before["credential_hash"]
    assert not after[ENC_COLUMN], "对不上账 ⇒ 密文列必须还是空的（不写脏账）"
    # 被拒的回填也要留痕（不含明文）
    bad = [x for x in _audit_lines(tmp_path)
           if x["action"] == "agents.backfill_credential" and not x["ok"]]
    assert bad and cred not in json.dumps(bad, ensure_ascii=False)


def test_回填粘对串之后可重复复制(env):
    client, reg, tmp_path = env
    cred = _seed_approved(client, reg, "worker")
    _wipe_enc(reg, "worker")
    r = client.post("/api/v1/agents/worker/backfill_credential",
                    json={"credential": cred, "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["recoverable"] is True
    # 换发没有发生：hash 与回填前一致（= 线上代理用的还是原来那把钥匙）
    assert _cred_raw(reg, "worker")["credential_hash"] == hash_credential(cred)
    assert len(reg.list_credentials("worker")) == 1, "回填不许顺手多签一把"
    for _ in range(2):
        got = client.post("/api/v1/agents/worker/reveal_credential",
                          json={"confirm": True}, headers=AUTH)
        assert got.status_code == 200 and got.json()["credential"] == cred
    ok = [x for x in _audit_lines(tmp_path)
          if x["action"] == "agents.backfill_credential" and x["ok"]]
    assert ok and cred not in json.dumps(ok, ensure_ascii=False)


def test_回填的空串与没凭据的行(env):
    client, reg, _ = env
    reg.register("fresh", "fresh")                    # 没批准过 ⇒ 没有账
    r = client.post("/api/v1/agents/fresh/backfill_credential",
                    json={"credential": "", "confirm": True}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "credential_required"
    r = client.post("/api/v1/agents/fresh/backfill_credential",
                    json={"credential": "ksk_" + "1" * 32, "confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "no_credential"
    # 🔴 鉴权在最外层：无 token 时**先 401**，轮不到 body 的确认闸 —— 这条钉住
    #    "新端点没有绕过会话闸的旁路"（与 test_agents_api 的逐条 401 同一意图）。
    assert client.post("/api/v1/agents/fresh/backfill_credential",
                       json={"credential": "ksk_" + "1" * 32,
                             "confirm": True}).status_code == 401
    # 带 token 但缺 confirm ⇒ 才是确认闸的 400
    assert client.post("/api/v1/agents/fresh/backfill_credential",
                       json={"credential": "ksk_" + "1" * 32},
                       headers=AUTH).status_code == 400


# ------------------------------------------------------------------ 作废必须干净

# 两档确认闸本来就不同：revoke_credential 要 confirm=true，revoke 要手输前 8 位。
# 这里按各自原语的正确形状喂 —— 测的是"作废后密文清没清"，不是确认闸（闸另有测试）。
_STOP_BODY = {"revoke_credential": {"confirm": True}, "revoke": {"confirm": "worker"}}


@pytest.mark.parametrize("endpoint", ["revoke_credential", "revoke"])
def test_停钥匙后密文一起清掉(env, endpoint):
    """🔴 本次改动最危险的反例：钥匙废了但密文还在 ⇒ 随时能再取出来用。"""
    client, reg, _ = env
    _seed_approved(client, reg, "worker")
    r = client.post(f"/api/v1/agents/worker/{endpoint}",
                    json=_STOP_BODY[endpoint], headers=AUTH)
    assert r.status_code == 200, r.text
    row = _raw(reg, "worker")
    assert not row["credential_hash"] and not row[ENC_COLUMN]
    assert [x for x in client.get("/api/v1/agents", headers=AUTH).json()["items"]
            if x["agent_id"] == "worker"][0]["recoverable"] is False
    got = client.post("/api/v1/agents/worker/reveal_credential",
                      json={"confirm": True}, headers=AUTH)
    assert got.status_code == 409 and got.json()["detail"]["error"] == "no_credential"


def test_换过主密钥后取出要报得清(env, tmp_path, monkeypatch):
    client, reg, _ = env
    cred = _seed_approved(client, reg, "worker")
    # 运行中换密钥（泄露止血场景）：旧密文解不开，但不能当成"成功取出空串"
    other = tmp_path / "other.key"
    other.write_text(Fernet.generate_key().decode("ascii") + "\n", encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE", str(other))
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "key_mismatch"
    # 原密钥回来 ⇒ 照旧能取出（证明问题在密钥不在数据）
    monkeypatch.setenv("KEEPSAKE_CREDENTIAL_KEY_FILE",
                       str(tmp_path / "cred.key"))
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["credential"] == cred


def test_密文与账对不上时拒绝返回(env, monkeypatch):
    """两列任一处被手工改过 ⇒ 不把对不上账的串当凭据发出去。"""
    client, reg, _ = env
    cred = _seed_approved(client, reg, "worker")
    reg.register("other", "other")
    cred2 = _seed_approved(client, reg, "other")      # 别的代理的密文
    assert cred2 != cred
    enc_other = _cred_raw(reg, "other")[ENC_COLUMN]
    # 把别人的密文挂到 worker 的那把上（= 手工改坏一列的最小仿真）
    reg._cred.set_fields(_cred_raw(reg, "worker")["cred_id"], {ENC_COLUMN: enc_other})
    r = client.post("/api/v1/agents/worker/reveal_credential",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "hash_mismatch"
    assert cred2 not in r.text
