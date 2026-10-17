"""写操作端点测试（P2 · 正负双向）。

**用真后端**：临时目录上的 `SqliteStorage`（keepsake 自己的第三后端），
不走假存储 —— 写路径的价值就在于「真的是经 `storage.update_fragment_fields`
/ `delete_fragments_batch` 落的库」，假存储只能测到控制台自己的 if/else。

备份与审计的根目录被环境变量指到 `tmp_path`（88 上没有 /root/.hermes），
逻辑与线上同一条代码路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "p2-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 写面白名单（与 test_api_readonly.ALLOWED_POST_PATHS 同一份口径；两处各写一遍
#: 是故意的 —— 一边改漏了，另一边的相等断言会红）。P3 起再加 3 条认证 POST
#: （setup / login 公开、logout 要会话 —— 那部分在 test_auth.py 里逐条打）。
#: keepsake_memory_crud_be 起再加 3 条业务 POST（增 / 真删 / 导入），
#: keepsake_kb_crud_be 起再加 4 条知识库写端点（新增 / 改 / 真删 / 导入），
#: keepsake_bulk_ops 起再加 2 条勾选批量端点（bulk 改数据 / bulk-export 纯读），
#: keepsake_cron_manage 起再加 3 条定时任务管理端点（暂停 / 恢复 / 改调度），
#: keepsake_agent_registry（agent_registry 治理中心化）起再加 4 条代理治理端点
#: （批准 / 吊销 / 改角色 / 上下文排除开关），keepsake_change_password 起再加
#: 1 条认证写端点（改口令）。
AUTH_POST_PATHS = {
    "/api/v1/auth/setup",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/change_password",
}

ALLOWED_POST_PATHS = {
    "/api/v1/core/{key}/promote",
    "/api/v1/core/{key}/demote",
    "/api/v1/fragments",
    "/api/v1/fragments/{key}/update",
    "/api/v1/fragments/{key}/archive",
    "/api/v1/fragments/{key}/delete",
    "/api/v1/import",
    "/api/v1/ops/bulk",
    "/api/v1/ops/bulk-export",
    "/api/v1/ops/merge",
    "/api/v1/ops/forget",
    "/api/v1/kb/documents",
    "/api/v1/kb/documents/{id}/update",
    "/api/v1/kb/documents/{id}/delete",
    "/api/v1/kb/import",
    "/api/v1/settings/core-budget",
    "/api/v1/settings/tiering",
    "/api/v1/crons/{job_id}/pause",
    "/api/v1/crons/{job_id}/resume",
    "/api/v1/crons/{job_id}/edit",
    "/api/v1/agents/{agent_id}/approve",
    "/api/v1/agents/{agent_id}/revoke",
    "/api/v1/agents/{agent_id}/role",
    "/api/v1/agents/{agent_id}/exclude",
} | AUTH_POST_PATHS

#: fixture 语料：一部分常驻、一部分普通、一条待归档
SEED: List[Dict[str, Any]] = [
    {"key": "memory:frag:aaa", "content": "常驻记忆 A：部署流程要点",
     "tags": "core,ops", "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:bbb", "content": "常驻记忆 B：值班联系人",
     "tags": "core,ops", "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:ccc", "content": "检索记忆条目 C：临时笔记",
     "tags": "demo", "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:ddd", "content": "检索记忆条目 D：旧版本排查",
     "tags": "demo,stale", "created": "2026-01-01T00:00:00+00:00"},
]

STORED_KEYS = [r["key"] for r in SEED]


@pytest.fixture()
def storage(tmp_path):
    """真 SqliteStorage（临时文件）+ 种子数据。"""
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "ops.db"), embedder=None, embed_dim=8)
    assert s.ensure_index() is not False
    assert s.write_fragments_batch([dict(r) for r in SEED]) == len(SEED)
    return s


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    """审计目录（任务书线上口径是 /root/.hermes/backup/console_audit）。"""
    d = tmp_path / "console_audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    """备份目录（任务书线上口径是 /root/.hermes/backup/console_ops/<日期>/）。"""
    d = tmp_path / "console_ops"
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(d))
    return d


@pytest.fixture()
def client(monkeypatch, storage, audit_dir, backup_dir):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def audit_lines(audit_dir) -> List[dict]:
    out = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def backup_files(backup_dir) -> List[str]:
    return sorted(str(p.relative_to(backup_dir)) for p in backup_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------- 认证

def test_所有写端点都要求token(client):
    """少挂一个 require_token 依赖就会红 —— 这里逐条打一遍。"""
    cases = [("/api/v1/core/memory%3Afrag%3Aaaa/promote", {"confirm": True}),
             ("/api/v1/core/memory%3Afrag%3Aaaa/demote", {"confirm": True}),
             ("/api/v1/fragments", {"confirm": True, "content": "x"}),
             ("/api/v1/fragments/memory%3Afrag%3Aaaa/update", {"confirm": True, "content": "x"}),
             ("/api/v1/fragments/memory%3Afrag%3Aaaa/archive", {"confirm": "frag:aaa"}),
             ("/api/v1/fragments/memory%3Afrag%3Aaaa/delete", {"confirm": "frag:aaa"}),
             ("/api/v1/import", {"items": [{"content": "x"}]}),
             ("/api/v1/ops/merge", {"dry_run": True}),
             ("/api/v1/ops/forget", {"dry_run": True}),
             ("/api/v1/agents/agent-aaa/approve", {"confirm": True}),
             ("/api/v1/agents/agent-aaa/revoke", {"confirm": "agent-a"}),
             ("/api/v1/agents/agent-aaa/role", {"role": "sub", "confirm": "agent-aaa"}),
             ("/api/v1/agents/agent-aaa/exclude", {"confirm": True, "exclude": True})]
    for url, body in cases:
        r = client.post(url, json=body)
        assert r.status_code == 401, (url, r.status_code)
        assert r.json()["detail"]["error"] == "missing_token"
        assert TOKEN not in r.text


# ---------------------------------------------------------------- promote / demote

def test_promote_加core标签并留痕(client, storage, audit_dir, backup_dir):
    r = client.post("/api/v1/core/memory:frag:ccc/promote", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["tags"] == "core,demo"
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "core,demo"
    # core 标签放最前 ⇒ /api/v1/core 立刻能看见
    assert "memory:frag:ccc" in [i["key"] for i in
                                 client.get("/api/v1/core?page_size=100", headers=AUTH).json()["items"]]
    assert any(".before.json" in p for p in backup_files(backup_dir))
    assert [ln["action"] for ln in audit_lines(audit_dir)] == ["core.promote"]


def test_promote_已是core_409(client, storage, audit_dir):
    r = client.post("/api/v1/core/memory:frag:aaa/promote", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "already_core"
    assert storage.get_fragment("memory:frag:aaa")["tags"] == "core,ops"   # 未被动
    # 🔴 被拒绝的写操作也必须审计
    assert audit_lines(audit_dir)[-1] == {
        "ts": pytest.approx(audit_lines(audit_dir)[-1]["ts"]), "actor": "console",
        "action": "core.promote", "target": "memory:frag:aaa", "ok": False,
        "reason": "already_core"}


def test_demote_去core标签(client, storage):
    r = client.post("/api/v1/core/memory:frag:aaa/demote", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == "ops"
    assert storage.get_fragment("memory:frag:aaa")["tags"] == "ops"


def test_demote_不在core_409(client, storage):
    r = client.post("/api/v1/core/memory:frag:ccc/demote", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_core"
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "demo"


@pytest.mark.parametrize("action", ["promote", "demote"])
def test_打标_缺confirm_400且不写库(client, storage, audit_dir, action):
    r = client.post(f"/api/v1/core/memory:frag:ccc/{action}", json={}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "demo"   # 一字未动
    assert audit_lines(audit_dir)[-1]["ok"] is False


@pytest.mark.parametrize("action", ["promote", "demote"])
def test_打标_不存在的key_404(client, audit_dir, action):
    r = client.post(f"/api/v1/core/memory:frag:zzz/{action}", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    assert audit_lines(audit_dir)[-1]["reason"] == "not_found"


# ---------------------------------------------------------------- update

def test_update_改正文与标签(client, storage, backup_dir):
    r = client.post("/api/v1/fragments/memory:frag:ccc/update",
                    json={"confirm": True, "content": "改过的正文", "tags": "demo,edited"},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == ["content", "tags"]
    row = storage.get_fragment("memory:frag:ccc")
    assert row["content"] == "改过的正文" and row["tags"] == "demo,edited"
    # 备份是**改前**原文
    bak = json.loads(next(backup_dir.rglob("*.before.json")).read_text(encoding="utf-8"))
    assert bak["row"]["content"] == "检索记忆条目 C：临时笔记"


def test_update_只改正文_标签不动(client, storage):
    r = client.post("/api/v1/fragments/memory:frag:ccc/update",
                    json={"confirm": True, "content": "只有正文变了"}, headers=AUTH)
    assert r.status_code == 200
    row = storage.get_fragment("memory:frag:ccc")
    assert row["content"] == "只有正文变了" and row["tags"] == "demo"


def test_update_正文tags都空_400(client, storage, audit_dir):
    r = client.post("/api/v1/fragments/memory:frag:ccc/update",
                    json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "nothing_to_update"
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"
    assert audit_lines(audit_dir)[-1]["reason"] == "nothing_to_update"


def test_update_空白正文按没填处理_不清空记忆(client, storage):
    """传空正文不许把记忆抹掉（不可逆、且分不清「想清空」与「忘了填」）。"""
    r = client.post("/api/v1/fragments/memory:frag:ccc/update",
                    json={"confirm": True, "content": "   ", "tags": "  "}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "nothing_to_update"
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"


def test_update_缺confirm_400(client, storage):
    r = client.post("/api/v1/fragments/memory:frag:ccc/update",
                    json={"content": "偷偷改"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"


def test_update_不存在的key_404(client):
    r = client.post("/api/v1/fragments/memory:frag:zzz/update",
                    json={"confirm": True, "content": "x"}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"


def test_update_存储层拒绝_500且明确报错(monkeypatch, storage, audit_dir, backup_dir):
    """后端不认这次写入 ⇒ 500 update_failed，不许报「成功」。"""
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    monkeypatch.setattr(storage, "update_fragment_fields", lambda k, f: False)
    with TestClient(app) as c:
        r = c.post("/api/v1/fragments/memory:frag:ccc/update",
                   json={"confirm": True, "content": "x"}, headers=AUTH)
    app.dependency_overrides.clear()
    assert r.status_code == 500 and r.json()["detail"]["error"] == "update_failed"
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"


# ---------------------------------------------------------------- archive

def test_archive_置失效不物理删(client, storage, backup_dir, audit_dir):
    r = client.post("/api/v1/fragments/memory:frag:ddd/archive",
                    json={"confirm": "frag:ddd"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["physical_delete"] is False and body["is_archived"] == "1"
    row = storage.get_fragment("memory:frag:ddd")
    assert row["is_archived"] == "1" and row["valid_until"]
    assert storage.fragment_exists("memory:frag:ddd")      # 还在，只是不再有效
    assert any("ddd.before.json" in p for p in backup_files(backup_dir))
    assert audit_lines(audit_dir)[-1]["action"] == "fragment.archive"


def test_archive_确认串错_400且不写库(client, storage, audit_dir):
    r = client.post("/api/v1/fragments/memory:frag:ddd/archive",
                    json={"confirm": "WRONG"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    assert "is_archived" not in storage.get_fragment("memory:frag:ddd")
    assert audit_lines(audit_dir)[-1]["reason"] == "confirm_mismatch"


def test_archive_缺确认串_400(client, storage):
    r = client.post("/api/v1/fragments/memory:frag:ddd/archive", json={}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert "is_archived" not in storage.get_fragment("memory:frag:ddd")


def test_archive_确认串对但key不存在_404(client, audit_dir):
    r = client.post("/api/v1/fragments/memory:frag:zzz/archive",
                    json={"confirm": "frag:zzz"}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    assert audit_lines(audit_dir)[-1]["reason"] == "not_found"


# ---------------------------------------------------------------- merge

def test_merge_dry_run_零写入(client, storage):
    before = storage.scan_fragment_keys("", 5000)[1]
    r = client.post("/api/v1/ops/merge", json={"dry_run": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["stats"]["dry_run"] is True
    assert body["confirm_required"] == str(len(storage.list_by_tag("core")))
    assert storage.scan_fragment_keys("", 5000)[1] == before      # 一条都没多


def test_merge_缺confirm_400(client, storage):
    r = client.post("/api/v1/ops/merge", json={"dry_run": False}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert storage.scan_fragment_keys("", 5000)[1] == STORED_KEYS


def test_merge_confirm对不上_400(client, storage):
    r = client.post("/api/v1/ops/merge",
                    json={"dry_run": False, "confirm": "999999"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    assert storage.scan_fragment_keys("", 5000)[1] == STORED_KEYS


def test_merge_实跑_先备份再动(client, storage, backup_dir, audit_dir):
    n_core = len(storage.list_by_tag("core"))
    r = client.post("/api/v1/ops/merge",
                    json={"dry_run": False, "confirm": str(n_core)}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    # 硬约束 3：先 dry-run 报告 + 受影响 keys 全量导出，**两个文件都得在**
    names = backup_files(backup_dir)
    assert any(".dryrun.json" in p for p in names), names
    assert any("affected." in p and p.endswith(".before.jsonl") for p in names), names
    aff = next(backup_dir.rglob("affected.*.before.jsonl"))
    exported = {json.loads(line)["key"] for line in aff.read_text(encoding="utf-8").splitlines()}
    assert exported <= set(STORED_KEYS) and exported
    assert audit_lines(audit_dir)[-1]["ok"] is True


def test_merge_confirm必须是当前core条数(client, storage):
    """防手滑：core 条数一变，旧确认串立刻失效。"""
    client.post("/api/v1/core/memory:frag:ccc/promote", json={"confirm": True}, headers=AUTH)
    r = client.post("/api/v1/ops/merge",
                    json={"dry_run": False, "confirm": "2"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"


# ---------------------------------------------------------------- forget

def test_forget_dry_run_零写入(client, storage):
    before = storage.scan_fragment_keys("", 5000)[1]
    r = client.post("/api/v1/ops/forget", json={"dry_run": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["stats"]["dry_run"] is True
    assert body["confirm_required"] == str(len(STORED_KEYS))
    assert storage.scan_fragment_keys("", 5000)[1] == before


def test_forget_缺confirm_400(client, storage):
    r = client.post("/api/v1/ops/forget", json={"dry_run": False}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert storage.scan_fragment_keys("", 5000)[1] == STORED_KEYS


def test_forget_confirm对不上_400(client, storage):
    r = client.post("/api/v1/ops/forget",
                    json={"dry_run": False, "confirm": "0"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    assert storage.scan_fragment_keys("", 5000)[1] == STORED_KEYS


def test_forget_实跑_走delete_fragments_batch(client, storage, backup_dir, audit_dir):
    r = client.post("/api/v1/ops/forget",
                    json={"dry_run": False, "confirm": str(len(STORED_KEYS))}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False and body["stats"]["dry_run"] is False
    names = backup_files(backup_dir)
    assert any("forget." in p and p.endswith(".dryrun.json") for p in names), names
    assert any("affected." in p for p in names), names
    # dry-run 阶段挑出来的候选（老 + 无保护）已被真删
    assert body["stats"]["deleted"] == body["stats"]["candidates"]
    assert storage.scan_fragment_keys("", 5000)[1] != STORED_KEYS
    assert audit_lines(audit_dir)[-1]["ok"] is True


# ---------------------------------------------------------------- 备份 / 审计

def test_审计行可被json逐行解析且字段齐全(client, audit_dir):
    client.post("/api/v1/ops/merge", json={"dry_run": True}, headers=AUTH)
    client.post("/api/v1/core/memory:frag:zzz/demote", json={"confirm": True}, headers=AUTH)
    lines = audit_lines(audit_dir)
    assert len(lines) == 2
    for ln in lines:
        assert set(ln) == {"ts", "actor", "action", "target", "ok", "reason"}
        assert ln["actor"] == "console" and isinstance(ln["ok"], bool)
    assert [ln["ok"] for ln in lines] == [True, False]


def test_审计里不许出现token(client, audit_dir):
    client.post("/api/v1/ops/forget", json={"dry_run": True}, headers=AUTH)
    raw = audit_dir.joinpath(sorted(p.name for p in audit_dir.iterdir())[0]).read_text("utf-8")
    assert TOKEN not in raw
    assert "Bearer" not in raw


def test_备份目录按日期分区(client, backup_dir):
    from keepsake.api import audit as audit_mod

    client.post("/api/v1/ops/merge", json={"dry_run": True}, headers=AUTH)
    client.post("/api/v1/fragments/memory:frag:ddd/archive",
                json={"confirm": "frag:ddd"}, headers=AUTH)
    day_dir = backup_dir / audit_mod.day()
    assert day_dir.is_dir()
    bak = json.loads((day_dir / "memory:frag:ddd.before.json").read_text("utf-8"))
    assert bak["key"] == "memory:frag:ddd"
    assert bak["row"]["content"] == "检索记忆条目 D：旧版本排查"
    assert bak["row"]["tags"] == "demo,stale"          # 归档**前**的标签


def test_备份失败_写操作中止(storage, audit_dir, backup_dir, monkeypatch):
    """备份写不进去 ⇒ 数据一秒都不许动（防数据丢失是硬要求，不是加分项）。"""
    from keepsake.api import audit as audit_mod

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setattr(audit_mod, "backup_fragment",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("磁盘满")))
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/fragments/memory:frag:ccc/update",
                   json={"confirm": True, "content": "改了"}, headers=AUTH)
    app.dependency_overrides.clear()
    assert r.status_code == 500
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"


def test_审计目录写不进去_写操作fail_closed(storage, audit_dir, backup_dir, monkeypatch):
    """审计写不进 ⇒ 宁可不做这次写（fail-closed），不许无声无息地改数据。"""
    from keepsake.api import audit as audit_mod

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setattr(audit_mod, "audit_file", lambda: Path("/proc/1/nope.jsonl"))
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/fragments/memory:frag:ccc/update",
                   json={"confirm": True, "content": "改了"}, headers=AUTH)
    app.dependency_overrides.clear()
    assert r.status_code == 500
    assert storage.get_fragment("memory:frag:ccc")["content"] == "检索记忆条目 C：临时笔记"


# ---------------------------------------------------------------- 只读不回退

def test_写面只有二十三条业务写端点加四条认证POST():
    app = create_app()
    write = {r.path for r in app.routes
             if getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}}
    assert write == ALLOWED_POST_PATHS, sorted(write)
    assert len(ALLOWED_POST_PATHS) == 28
    assert len(ALLOWED_POST_PATHS - AUTH_POST_PATHS) == 24


def test_P1只读路由未被写面影响(client):
    for url in ("/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
                "/api/v1/fragments/memory:frag:ccc", "/api/v1/stats",
                "/api/v1/crons"):
        assert client.get(url, headers=AUTH).status_code == 200, url


def test_写操作之后GET口径同步(client):
    """打 core 后，/api/v1/core 与 /api/v1/core/stats 的 count 必须一起变。"""
    before = client.get("/api/v1/core/stats", headers=AUTH).json()["count"]
    client.post("/api/v1/core/memory:frag:ccc/promote", json={"confirm": True}, headers=AUTH)
    after = client.get("/api/v1/core/stats", headers=AUTH).json()["count"]
    assert after == before + 1