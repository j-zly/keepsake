"""知识库共享开关（per-agent `share_kb`）—— 与记忆开关**解耦**的第二个旗。

锁死的是**语义**，不是某个实现细节：

1. **老库补列自愈**：没有 `share_kb` 列的老库 ⇒ 就绪后列在、存量行全 `'0'`
   （「新增字段必须兼容旧数据」）；再跑一次 ⇒ **一条 DDL 都不许发**（幂等）。
2. **规范化**：缺列 / 空值 ⇒ **关**；`'1'` / `'true'` ⇒ **开**。
3. **两个旗解耦**（2×2 矩阵）：`share_memory=1, share_kb=0` 的代理在**记忆面**
   可见、在**知识库面**不可见；`share_kb=1` 的代理反之 —— 一个旗**不许**串台。
4. **primary ⇒ 全量**（知识库面也是 `None`）；pending / revoked **不进**共享集。
5. **flag 缺省仍是记忆面**：记忆可见性**逐字零变化**（把缺省改成 `share_kb`
   ⇒ 本文件里那条用例必红）。
6. **控制台端点**（`POST /agents/{id}/share-kb`）：投影带上 `share_kb`、
   `confirm` 缺/假一律拒且**不改**、未知代理 404、审计留痕。

全程 hermetic：`tmp_path` 里的临时 SQLite + 真 `ks_agent` 表，不连
Redis / PG / 网络 / 生产库，也不碰主脑的真实 config。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from keepsake.govern.registry import (
    KS_AGENT_DDL,
    SHARE_COLUMN,
    SHARE_KB_COLUMN,
    SHARE_KB_COLUMN_DECL,
    STATUS_APPROVED,
    Registry,
    _norm,
    _share_is_on,
)
from keepsake.govern.visibility import shared_agent_ids, visible_agent_ids


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def reg(tmp_path):
    """一份真注册表（临时 SQLite）：primary + 三个子代理，都已批准。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "share_kb.db"))
    assert st.ensure_index() is True
    r = Registry(st)
    assert r.ensure_schema() is True
    for aid in ("primary", "agent-m", "agent-k", "agent-b"):
        r.register(aid, aid)
        r.approve(aid)
    r.set_role("primary", "primary")
    yield r
    st.close()


# ---------------------------------------------------------------------------
# ① 老库补列自愈（+ 幂等：结构齐备时一条 DDL 都不发）
# ---------------------------------------------------------------------------

def _legacy_db(path: str) -> None:
    """造一张**开关上线前**的 `ks_agent`（没有 `share_kb` 列）+ 一行存量数据。"""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ks_agent ("
        "agent_id text PRIMARY KEY, name text NOT NULL DEFAULT '', "
        "role text NOT NULL DEFAULT 'sub', status text NOT NULL DEFAULT 'approved', "
        "credential_hash text NOT NULL DEFAULT '', credential_hint text NOT NULL DEFAULT '', "
        "first_seen_at text NOT NULL DEFAULT '', last_seen_at text NOT NULL DEFAULT '', "
        "reads integer NOT NULL DEFAULT 0, writes integer NOT NULL DEFAULT 0, "
        "exclude_from_primary integer NOT NULL DEFAULT 0, "
        "meta text NOT NULL DEFAULT '{}', dm_enabled text NOT NULL DEFAULT '1', "
        "share_memory text NOT NULL DEFAULT '0')"
    )
    conn.execute("INSERT INTO ks_agent (agent_id, name) VALUES ('old-row', 'old-row')")
    conn.commit()
    conn.close()


def test_老库补列自愈_存量行全关(tmp_path):
    """🔴 老库（没有 `share_kb` 列）补列后，存量行一律当**关**。

    「新增字段必须兼容旧数据」：补列落 `DEFAULT '0'` ⇒ 谁也没被共享出去过，
    存量行的语义一个字没变。
    """
    from keepsake.core.sqlite import SqliteStorage

    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    st = SqliteStorage(path=path)
    try:
        r = Registry(st)
        added: List[str] = []
        orig = r._be.add_column                      # noqa: SLF001 —— 直读后端验纪律

        def spy(column: str, decl: str) -> None:
            added.append(column)
            return orig(column, decl)

        r._be.add_column = spy                     # noqa: SLF001
        assert r.ensure_schema() is True           # 补列
        assert SHARE_KB_COLUMN in added, added
        # 列在 + 存量行全 '0'
        assert r._be.load("old-row")[SHARE_KB_COLUMN] == "0"   # noqa: SLF001
        assert r.get("old-row")["share_kb"] is False
        # 🔴 幂等：结构齐备 ⇒ **一条 DDL 都不许发**
        added.clear()
        assert r.ensure_schema() is True
        assert added == [], added
    finally:
        st.close()


def test_建表DDL与补列共用同一份声明():
    """🔴 建表 DDL 里的列声明 = 补列用的那份常量（不许漂）。"""
    assert SHARE_KB_COLUMN_DECL == "text NOT NULL DEFAULT '0'"
    decl_lines = [" ".join(l.split()) for l in KS_AGENT_DDL.splitlines()
                  if l.strip().startswith(SHARE_KB_COLUMN)]
    assert len(decl_lines) == 1, decl_lines
    assert decl_lines[0] == f"{SHARE_KB_COLUMN} {SHARE_KB_COLUMN_DECL},"


# ---------------------------------------------------------------------------
# ② 规范化：缺列 / 空值 ⇒ 关；'1' / 'true' ⇒ 开
# ---------------------------------------------------------------------------

def test_规范化_缺列与空值按关():
    assert _norm({"agent_id": "a"})[SHARE_KB_COLUMN] == "0"
    assert _norm({"agent_id": "a", "share_kb": ""})[SHARE_KB_COLUMN] == "0"
    assert _norm({"agent_id": "a", "share_kb": "   "})[SHARE_KB_COLUMN] == "0"
    assert _norm({"agent_id": "a", "share_kb": "1"})[SHARE_KB_COLUMN] == "1"
    assert _norm({"agent_id": "a", "share_kb": "true"})[SHARE_KB_COLUMN] == "true"


def test_判读口_开值集与记忆面同款():
    """🔴 两个旗共用 `SHARE_ENABLED_VALUES` 这一把尺子（不许各写一份）。"""
    for value in ("1", "true", "yes", "on"):
        assert _share_is_on({"share_kb": value}, SHARE_KB_COLUMN) is True, value
    for value in ("", "0", "false", "off", None):
        assert _share_is_on({"share_kb": value}, SHARE_KB_COLUMN) is False, value
    assert _share_is_on({}, SHARE_KB_COLUMN) is False      # 缺列 ⇒ 关
    assert _share_is_on(None, SHARE_KB_COLUMN) is False    # 查无此代理 ⇒ 关


# ---------------------------------------------------------------------------
# ③ 两个旗解耦（2×2 矩阵）
# ---------------------------------------------------------------------------

def test_两个旗解耦_记忆面与知识库面各看各的(reg):
    """🔴 本单的核心判据：`share_memory` 与 `share_kb` **解耦**。

    * `agent-m` 只开记忆共享 ⇒ **记忆面**看得见、**知识库面**看不见；
    * `agent-k` 只开知识库共享 ⇒ **知识库面**看得见、**记忆面**看不见。
    """
    reg.set_share_memory("agent-m", True)     # 只开记忆
    reg.set_share_kb("agent-k", True)         # 只开知识库
    # 记忆面（缺省旗）：只有开了记忆共享的 agent-m
    assert visible_agent_ids("agent-b", reg) == {"agent-b", "agent-m"}
    # 知识库面：只有开了知识库共享的 agent-k（记忆旗**不串台**）
    assert visible_agent_ids("agent-b", reg, flag=SHARE_KB_COLUMN) == {"agent-b", "agent-k"}


def test_共享集两个旗各算各的(reg):
    reg.set_share_memory("agent-m", True)
    reg.set_share_kb("agent-k", True)
    assert shared_agent_ids(reg) == ["agent-m"]
    assert shared_agent_ids(reg, flag=SHARE_KB_COLUMN) == ["agent-k"]


def test_开关幂等且不碰状态角色与凭据(reg):
    assert reg.set_share_kb("agent-b", True) is True
    assert reg.set_share_kb("agent-b", True) is False        # 没变
    assert reg.set_share_kb("agent-b", False) is True
    assert reg.set_share_kb("查无此代理", True) is False     # 行不存在
    reg.set_share_kb("agent-b", True)
    before = reg.get("agent-b")
    reg.set_share_kb("agent-b", False)
    after = reg.get("agent-b")
    assert after["status"] == before["status"] == STATUS_APPROVED
    assert after["role"] == before["role"]
    assert after["credential_hint"] == before["credential_hint"]
    assert after["share_kb"] is False


# ---------------------------------------------------------------------------
# ④ primary 全量 / pending·revoked 不进共享集
# ---------------------------------------------------------------------------

def test_primary在知识库面也是全量(reg):
    """🔴 primary 的可见性逐字不变：知识库面也是 `None`（不过滤，全量可见）。"""
    reg.set_share_kb("agent-k", True)
    assert visible_agent_ids("primary", reg, flag=SHARE_KB_COLUMN) is None
    assert visible_agent_ids("primary", reg) is None


def test_pending与revoked不进知识库共享集(reg):
    """🔴 共享集只认 `status=approved`：pending / revoked 的代理不进共享范围。"""
    reg.set_share_kb("agent-k", True)
    assert "agent-k" in shared_agent_ids(reg, flag=SHARE_KB_COLUMN)
    reg.revoke("agent-k")
    assert "agent-k" not in shared_agent_ids(reg, flag=SHARE_KB_COLUMN)
    reg.register("pending-kb", "pending-kb")
    reg.set_share_kb("pending-kb", True)
    assert "pending-kb" not in shared_agent_ids(reg, flag=SHARE_KB_COLUMN)


# ---------------------------------------------------------------------------
# ⑤ flag 缺省仍是记忆面（记忆可见性逐字零漂移）
# ---------------------------------------------------------------------------

def test_flag缺省就是记忆旗(reg):
    """🔴 红绿对照：把 `visible_agent_ids` / `shared_agent_ids` 的**缺省 flag**
    改成 `share_kb` ⇒ 本用例必红。

    记忆面的可见性**一个字都不许变**：缺省 flag 必须仍是 `share_memory`。
    """
    assert inspect.signature(visible_agent_ids).parameters["flag"].default == SHARE_COLUMN
    assert inspect.signature(shared_agent_ids).parameters["flag"].default == SHARE_COLUMN
    reg.set_share_memory("agent-m", True)
    reg.set_share_kb("agent-k", True)
    # 缺省调用 ≡ 显式传记忆旗（逐字一致）
    assert visible_agent_ids("agent-b", reg) == visible_agent_ids("agent-b", reg, flag=SHARE_COLUMN)
    assert visible_agent_ids("agent-b", reg) == {"agent-b", "agent-m"}
    assert shared_agent_ids(reg) == shared_agent_ids(reg, flag=SHARE_COLUMN)
    assert shared_agent_ids(reg) == ["agent-m"]


# ---------------------------------------------------------------------------
# ⑥ 控制台端点（投影 / confirm 闸 / 404 / 审计）
# ---------------------------------------------------------------------------

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def console(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite 存储 + 审计/备份目录 + 固定 token。"""
    from fastapi.testclient import TestClient

    from keepsake.api.app import create_app
    from keepsake.api.deps import get_storage
    from keepsake.core.sqlite import SqliteStorage

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sess.json"))
    storage = SqliteStorage(path=str(tmp_path / "console.db"))
    assert storage.ensure_index() is True
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        yield client, Registry(storage), tmp_path
    app.dependency_overrides.clear()
    storage.close()


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for f in sorted(Path(tmp_path, "audit").glob("audit-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            lines.append(json.loads(line))
    return lines


def test_列表投影带上share_kb(console):
    client, reg, _ = console
    reg.register("worker-1", "工人")
    item = client.get("/api/v1/agents", headers=AUTH).json()["items"][0]
    assert "share_kb" in item
    assert item["share_kb"] is False              # 缺省就是关


def test_share_kb端点_开关往返(console):
    client, reg, _ = console
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share-kb",
                       headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["share_kb"] is True and body["changed"] is True
    assert reg.share_kb("worker-1") is True
    item = client.get("/api/v1/agents", headers=AUTH).json()["items"][0]
    assert item["share_kb"] is True
    # 再来一次 ⇒ 幂等（报「没变」）
    resp = client.post("/api/v1/agents/worker-1/share-kb",
                       headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.json()["changed"] is False


def test_share_kb端点_不带confirm一律拒且不改(console):
    """🔴 与记忆共享（`share`）同一道闸：缺 `confirm` / `confirm=false` ⇒ 400 且不改。"""
    client, reg, _ = console
    reg.register("worker-1", "工人")
    for body in ({"enabled": True}, {"enabled": True, "confirm": False}):
        resp = client.post("/api/v1/agents/worker-1/share-kb", headers=AUTH, json=body)
        assert resp.status_code == 400, resp.text
        assert reg.share_kb("worker-1") is False


def test_share_kb端点_未知代理404与无token401(console):
    client, reg, _ = console
    resp = client.post("/api/v1/agents/查无此代理/share-kb",
                       headers=AUTH, json={"enabled": True, "confirm": True})
    assert resp.status_code == 404, resp.text
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share-kb",
                       json={"enabled": True, "confirm": True})
    assert resp.status_code == 401


def test_share_kb端点_审计留痕且改前有备份(console):
    client, reg, tmp = console
    reg.register("worker-1", "工人")
    resp = client.post("/api/v1/agents/worker-1/share-kb",
                       headers=AUTH, json={"enabled": True, "confirm": True})
    backup = resp.json()["backup"]
    saved = json.loads(Path(backup).read_text(encoding="utf-8"))
    assert saved["agent_id"] == "worker-1"
    lines = _audit_lines(tmp)
    assert any(l.get("action") == "agents.share_kb" and l.get("ok") for l in lines), lines
    # 被拒的写也留一行（ok=false）
    client.post("/api/v1/agents/worker-1/share-kb",
                headers=AUTH, json={"enabled": True})          # 缺 confirm ⇒ 400
    lines = _audit_lines(tmp)
    assert any(l.get("action") == "agents.share_kb" and not l.get("ok") for l in lines), lines
