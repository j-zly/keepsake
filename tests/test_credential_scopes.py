"""凭据功能授权（scope）—— 2026-11-14 用户拍板：
「根据凭证授权是能使用记忆功能和知识库功能，只授权记忆的话知识库是不能使用，
 只授权知识库的话记忆功能不能使用」。

钉住的纪律（每条都是一个真会出错的口子）：

1. **缺省全授权**：新签与迁移来的凭据都是 `memory,kb` ⇒ 升级不改变任何人在用什么
   （收紧必须是界面上的显式动作，不是升级的副作用）。
2. **逐把独立**：改 A 把的授权不动 B 把（多把的意义就在这）。
3. 🔴 **闸门前缀派生**：判据从 `SCOPES_ALL` 的名字派生，不列工具名单——
   列名单迟早漏一条，漏的那条是**静默放行**。新加 `memory_*` / `kb_*` 自动入闸。
4. 🔴 **缺键 ≠ 无权限**：旧进程/本机通道给的结论里没有 `scopes` 键 ⇒ 视为全授权；
   显式空列表 ⇒ 全拒。这两种情况混起来的后果是"升级一瞬全线断功能"。
5. **不在档内的工具不受影响**：私信 / 待办 / 治理工具本轮**不**被 scope 管，
   边界要钉住（顺手把 message_ 也纳入 = 未经用户同意的扩面）。
6. **打错的档位名直接拒**（`memmory` ⇒ ValueError/400）：宽容解析会把它变成
   "空集合 = 全禁"，界面显示成"只授了知识库"，静默少授权比报错难查十倍。
7. **跨代理的编号动不了**（改别人的授权 = 越权）。
8. **MCP 与 HTTP 同一个判据**：`gate.check` 与 KB 写入口都走 `has_scope`，
   界面上勾掉知识库 ⇒ 两条路**同时**拒绝。
9. **被拒的调用不打活动**：否则一次越权尝试给这把钥匙刷用量，监管面变噪声源。

存储用临时 SQLite（同 `test_agents_multi_credential.py`），不连 PG / Redis / 生产库。
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
from keepsake.govern.registry import (
    SCOPES_ALL,
    SCOPES_DEFAULT,
    SCOPE_KB,
    SCOPE_MEMORY,
    Registry,
    has_scope,
)
from keepsake.mcp_server.server import (  # noqa: I001
    AgentGate,
    ToolError,  # type: ignore  # 从 tools 转出，闸门用它拒客
    scope_of_tool,
)

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


def _seed(client, reg, aid="worker"):
    """登记 + 批准 + 再加一把，返回 (第一把明文, 第二把明文, 第二把编号)。"""
    reg.register(aid, aid)
    r = client.post(f"/api/v1/agents/{aid}/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    first = str(r.json()["credential"])
    r = client.post(f"/api/v1/agents/{aid}/credentials",
                    json={"label": "只查知识库", "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    return first, str(r.json()["credential"]), str(r.json()["cred_id"])


# ------------------------------------------------------------------ 缺省与逐把

def test_缺省是全授权(env):
    client, reg, _ = env
    first, _second, cid = _seed(client, reg)
    assert reg.verify_any(first)["scopes"] == ["memory", "kb"]
    assert reg.verify_any(_second)["scopes"] == ["memory", "kb"]
    assert SCOPES_DEFAULT == "memory,kb" and SCOPES_ALL == ("memory", "kb")
    rows = reg.list_credentials("worker")
    assert [r["scopes"] for r in rows] == [["memory", "kb"], ["memory", "kb"]]
    # 读面带出来给界面用（不是只存在库里）
    raw = client.get("/api/v1/agents/worker/credentials", headers=AUTH).json()
    assert raw["items"][0]["scopes"] == ["memory", "kb"]


def test_改一把不牵连另一把(env):
    client, reg, _ = env
    first, _second, cid = _seed(client, reg)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                    json={"scopes": ["kb"], "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["kb"]
    assert reg.verify_any(_second)["scopes"] == ["kb"]
    assert reg.verify_any(first)["scopes"] == ["memory", "kb"]


# ------------------------------------------------------------------ 前缀派生（不列名单）

@pytest.mark.parametrize("tool,scope", [
    ("memory_search", SCOPE_MEMORY), ("memory_add", SCOPE_MEMORY),
    ("memory_capabilities", SCOPE_MEMORY),
    ("kb_search", SCOPE_KB), ("kb_store", SCOPE_KB), ("kb_get", SCOPE_KB),
])
def test_在册工具都落进对应档位(tool, scope):
    assert scope_of_tool(tool) == scope


@pytest.mark.parametrize("tool", [
    "message_send", "message_inbox", "todo_list", "agent_approve", "unrelated_thing",
])
def test_不在档内的工具不受_scope_约束(tool):
    """本轮的边界（用户只点了记忆与知识库两档）。

    这条用例的作用是：**将来有人把 message_ 顺手纳进来，这里会红** ——
    扩面必须是有意识的改动，不是顺手的副作用。
    """
    assert scope_of_tool(tool) == ""


def test_新加的同族工具自动入闸():
    """判据是前缀派生，所以"漏一条工具"这种洞结构上不存在。"""
    assert scope_of_tool("memory_brand_new_tool") == SCOPE_MEMORY
    assert scope_of_tool("kb_brand_new_tool") == SCOPE_KB


# ------------------------------------------------------------------ has_scope 的两种"没有"

def test_kb闸的判据不许引用返回值才有的字段():
    """护栏：`writer_identity` 里那道 scope 闸曾经写成 `res.get("kind") == "agent"`。

    `kind` 是这个依赖**自己构造的返回值**字段，不在 `verify_any` 的结论里 ⇒ 条件恒假
    ⇒ 闸静默永真（真机测试才逮到）。这类"判据引用了还没造出来的东西"不报错、不崩溃，
    只是永远放行，所以留一条源码级断言：KB 的 scope 闸不许再出现 `kind`。
    """
    import pathlib

    src = pathlib.Path('src/keepsake/api/routers/kb.py').read_text(encoding='utf-8')
    at = src.index('def _require_kb_scope')
    raw = src[at:at + 1400]
    # 只看**代码行**：这段注释本身要解释"以前写错过什么"，把注释也扫进来会自己绊自己
    body = "\n".join(l for l in raw.split("\n") if not l.strip().startswith('#'))
    assert 'SCOPE_KB' in body and 'has_scope' in body
    assert 'res.get("kind")' not in body, 'scope 闸的判据不许引用返回值才有的字段（恒假=永真）''' 


def test_缺键与空集合必须分开判():
    assert has_scope({}, SCOPE_MEMORY) is True                  # 旧进程结论：兼容放行
    assert has_scope({"scopes": []}, SCOPE_MEMORY) is False    # 人为主观关档：真拒
    assert has_scope({"scopes": ["kb"]}, SCOPE_KB) is True
    assert has_scope({"scopes": "memory"}, SCOPE_KB) is False   # 库里存的是串
    assert has_scope(None, SCOPE_MEMORY) is True


# ------------------------------------------------------------------ 闸的真行为

def _gate_for(registry: Any, credential: str) -> AgentGate:
    """按 http 传输造一个闸门（头里只有 Bearer，身份靠凭据反查）。

    `_http_headers` 换成固定字典 = 模拟"请求头带来了这把钥匙"，其余链路（`identify`
    → `verify_any` → `check`）一字不动地走真代码。
    """
    gate = AgentGate(registry, None, agent_id="", credential="")
    gate._http_headers = lambda: {"authorization": f"Bearer {credential}"}  # noqa: SLF001
    return gate


def test_只授知识库的凭据调记忆工具被拒(env):
    client, reg, _ = env
    _first, second, cid = _seed(client, reg)
    assert client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                       json={"scopes": ["kb"], "confirm": True}, headers=AUTH).status_code == 200

    gate = _gate_for(reg, second)
    before = reg.list_credentials("worker")
    hit = [c for c in before if c["cred_id"] == cid][0]
    with pytest.raises(Exception) as boom:      # ToolError：人话拒绝，不静默
        gate.check("memory_search")
    text = str(boom.value)
    assert "记忆" in text and "知识库" in text, text
    # 🔴 被拒的那次调用**不打活动**（用量只算真用成的）
    after = [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]
    assert after["reads"] == hit["reads"] and after["used_at"] == hit["used_at"]
    # 同一把调知识库工具 ⇒ 必须放行（证明不是"整把被封"）
    res = gate.check("kb_search")
    assert res["ok"] is True and res["scopes"] == ["kb"]


def test_只授记忆的凭据走_kb_http_写入口也被拒(env):
    """MCP 与 HTTP 两条路必须同一个判据：勾掉知识库 = 两边同时不能用。

    🟡 反向对照的判据写成"不是 scope_denied"而不是 200：这个夹具是 SQLite，
       KB 写入在生产口径上要 PG（未配就 500 `kb_unconfigured`），
       拿 200 当判据等于把"过没过闸"和"这台机器有没有 KB"混成一件事。
    """
    client, reg, _ = env
    first, _second, _cid = _seed(client, reg)
    rows = reg.list_credentials("worker")
    first_cid = [c for c in rows if c["credential_hint"] == first[:8]][0]["cred_id"]
    r = client.post(f"/api/v1/agents/worker/credentials/{first_cid}/scopes",
                    json={"scopes": ["memory"], "confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    # 只授记忆的那把 ⇒ KB 写入口必须 403（在依赖里就拒掉，进不到 KB 服务）
    r = client.post("/api/v1/kb/documents",
                    headers={"Authorization": f"Bearer {first}"},
                    json={"title": "T", "content": "C", "confirm": True})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "scope_denied", r.text
    # 反证：全授权的那把**过了闸**（后面 500 是夹具没配 KB，不是被权限挡）
    other = client.post("/api/v1/agents/worker/credentials",
                        json={"label": "全授权", "confirm": True}, headers=AUTH).json()
    r2 = client.post("/api/v1/kb/documents",
                     headers={"Authorization": f"Bearer {other['credential']}"},
                     json={"title": "T2", "content": "C2", "confirm": True})
    assert r2.json().get("detail", {}).get("error") != "scope_denied", r2.text


# ------------------------------------------------------------------ 闸的形状

def test_不认识的档位名直接拒(env):
    client, reg, _ = env
    _first, _second, cid = _seed(client, reg)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                    json={"scopes": ["memmory"], "confirm": True}, headers=AUTH)
    assert r.status_code == 400 and "memmory" in r.json()["detail"]["message"], r.text
    # 没改成一个字节：打错字不该把授权悄悄清空
    assert [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]["scopes"] \
        == ["memory", "kb"]


def test_跨代理改授权与闸的旁路(env):
    client, reg, _ = env
    _f, second, cid = _seed(client, reg, "worker")
    reg.register("other", "o")
    r = client.post("/api/v1/agents/other/approve", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    rr = client.post(f"/api/v1/agents/other/credentials/{cid}/scopes",
                     json={"scopes": [], "confirm": True}, headers=AUTH)
    assert rr.status_code == 409 and rr.json()["detail"]["error"] == "credential_not_found"
    # 无 token 先 401（鉴权在确认闸之前），缺 confirm 才是 400
    assert client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                       json={"scopes": ["kb"]}).status_code == 401
    assert client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                       json={"scopes": ["kb"]}, headers=AUTH).status_code == 400


def test_两档都关是合法状态且如实显示(env):
    client, reg, _ = env
    _f, second, cid = _seed(client, reg)
    r = client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                    json={"scopes": [], "confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["scopes"] == [], r.text
    got = [c for c in reg.list_credentials("worker") if c["cred_id"] == cid][0]
    assert got["scopes"] == [] and got["status"] == "active"   # 在册但什么都不能用
    # 仍能取出明文（收紧授权 ≠ 作废钥匙；两者是分开的动作）
    assert reg.reveal_credential("worker", cid)["ok"] is True


def test_审计与读面不含凭据材料(env):
    client, reg, tmp_path = env
    _f, second, cid = _seed(client, reg)
    client.post(f"/api/v1/agents/worker/credentials/{cid}/scopes",
                json={"scopes": ["kb"], "confirm": True}, headers=AUTH)
    raw = client.get("/api/v1/agents/worker/credentials", headers=AUTH).text
    assert second not in raw and "credential_hash" not in raw and "gAAAA" not in raw
    import glob
    dumped = json.dumps([json.loads(line)
                         for f in sorted(glob.glob(str(tmp_path / "audit/audit-*.jsonl")))
                         for line in open(f, encoding="utf-8")], ensure_ascii=False)
    assert "ksk_" not in dumped and second not in dumped
