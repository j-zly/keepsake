"""勾选批量端点测试（keepsake_bulk_ops）· 正负双向。

钉死四件最容易悄悄坏掉的事：

  1. **bulk 与单条端点同一存储路径** —— 升/降常驻打 core 标签、归档置
     `is_archived=1+valid_until`、真删逐条备份后 `delete_fragments_batch`。批量
     另写一份 SQL/标签拼接，语义迟早与单条漂移（列表看着变了、检索层没变）。
  2. **确认串 = 去重后的选中条数** —— 勾 37 条手输 38 个都不放行，这是批量删
     唯一的「按回车就删」防线。
  3. **单条失败不中断整批** —— 不存在 / 已是常驻 / 备份写不进，各自在 `failed`
     里带原因码；499 条成功不许被第 500 条拖成整批 0 成功。备份写不成的条目
     **宁删不成**（防数据丢失的硬要求，不打折）。
  4. **bulk 与 bulk-export 的鉴权分级** —— bulk 改数据要 `write_guard`（审计
     落不下账就不许写），bulk-export 是纯读、只要会话（否则「勾选导出」会因
     审计目录不可写而整个用不了，而它一个字节都没改）。

🔴 **配置隔离**：`KEEPSAKE_CONFIG` 与 `HOME` 一律指到 `tmp_path` 下。只清 env
不指文件是假绿 —— 读不到 env 就回落到 `~/.config/keepsake/config.json`，真机上
那份文件存在，测试会悄悄读到。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api import audit as audit_mod
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.sqlite import SqliteStorage

TOKEN = "bulk-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

BULK = "/api/v1/ops/bulk"
BULK_EXPORT = "/api/v1/ops/bulk-export"

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


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """配置 / 凭据 / 审计 / 备份根目录全部隔离到 tmp_path（逻辑与线上同一条路径）。"""
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "config.json"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))          # env 读不到时也别回落到真配置
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "console_audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "console_ops"))
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)


@pytest.fixture()
def backup_dir(tmp_path) -> Any:
    return tmp_path / "console_ops"


@pytest.fixture()
def audit_dir(tmp_path) -> Any:
    return tmp_path / "console_audit"


@pytest.fixture()
def storage(tmp_path):
    """真 SqliteStorage（临时文件）+ 种子数据：写路径的价值就在于真的落库。"""
    s = SqliteStorage(path=str(tmp_path / "bulk.db"), embedder=None, embed_dim=8)
    assert s.ensure_index() is not False
    assert s.write_fragments_batch([dict(r) for r in SEED]) == len(SEED)
    return s


@pytest.fixture()
def client(storage):
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def bulk(client, action, keys, confirm=None):
    """确认串默认按**去重后**的条数填（与后端 `_dedup_keys` 同一口径）。"""
    n = len({k.strip() for k in keys if k.strip()})
    return client.post(BULK, json={"action": action, "keys": keys,
                                   "confirm": str(n) if confirm is None else confirm},
                       headers=AUTH)


def audit_lines(audit_dir) -> List[dict]:
    out = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def backup_files(backup_dir) -> List[str]:
    return sorted(str(p.relative_to(backup_dir)) for p in backup_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------- 鉴权

@pytest.mark.parametrize("url,body", [
    (BULK, {"action": "archive", "keys": ["memory:frag:ccc"], "confirm": "1"}),
    (BULK_EXPORT, {"keys": ["memory:frag:ccc"], "format": "jsonl"}),
])
def test_批量端点都要会话(client, url, body):
    """少挂一个 require_session 就红 —— 逐条打。"""
    r = client.post(url, json=body)
    assert r.status_code == 401, url
    assert r.json()["detail"]["error"] == "missing_token"
    assert TOKEN not in r.text


def test_bulk挂写闸_bulk_export不挂():
    """分级：bulk 改数据要 fail-closed 的 write_guard；bulk-export 纯读不挂。

    判据直接看路由的依赖表 —— 这是「分级」这件事唯一的凭据，写注释没用。
    """
    app = create_app()
    deps = {r.path: {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
            for r in app.routes if getattr(r, "dependant", None)}
    assert "write_guard" in deps[BULK]
    assert "require_session" in deps[BULK]
    assert "write_guard" not in deps[BULK_EXPORT]
    assert "require_session" in deps[BULK_EXPORT]


# ---------------------------------------------------------------- promote / demote

def test_bulk_promote_demote_往返(client, storage):
    keys = ["memory:frag:ccc", "memory:frag:ddd"]
    r = bulk(client, "promote", keys)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["action"] == "promote"
    assert body["requested"] == 2 and body["succeeded"] == 2 and body["failed"] == []
    # 与 promote 端点同一存储路径：core 标签放最前、回读校验过
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "core,demo"
    assert storage.get_fragment("memory:frag:ddd")["tags"] == "core,demo,stale"

    r = bulk(client, "demote", keys)
    assert r.status_code == 200 and r.json()["succeeded"] == 2
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "demo"
    assert storage.get_fragment("memory:frag:ddd")["tags"] == "demo,stale"


def test_bulk_promote_与单条端点产出同款(client, storage, backup_dir):
    """批量升与单条升，落库结果与备份文件名必须完全一致（同一存储路径的证据）。"""
    assert bulk(client, "promote", ["memory:frag:ddd"]).json()["succeeded"] == 1
    client.post("/api/v1/core/memory:frag:ccc/promote", json={"confirm": True}, headers=AUTH)
    # 同一条走单条、另一条走批量：core 标签都放最前、原标签原序保留
    assert storage.get_fragment("memory:frag:ddd")["tags"] == "core,demo,stale"
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "core,demo"
    # 两条都落逐条备份（备份文件名口径也一致）
    names = backup_files(backup_dir)
    assert any("ddd.before.json" in n for n in names), names
    assert any("ccc.before.json" in n for n in names), names


def test_bulk_promote_已是常驻_计入failed不中断(client, storage):
    """一条已是常驻不许拖垮整批：其余照常升上去。"""
    r = bulk(client, "promote", ["memory:frag:aaa", "memory:frag:ccc"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 1 and body["requested"] == 2
    assert body["failed"] == [{"key": "memory:frag:aaa", "reason": "already_core"}]
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "core,demo"


def test_bulk_demote_不在常驻_计入failed(client, storage):
    r = bulk(client, "demote", ["memory:frag:ccc", "memory:frag:aaa"])
    body = r.json()
    assert body["succeeded"] == 1
    assert body["failed"] == [{"key": "memory:frag:ccc", "reason": "not_core"}]
    assert storage.get_fragment("memory:frag:aaa")["tags"] == "ops"


# ---------------------------------------------------------------- archive

def test_bulk_archive_置失效不物理删(client, storage, audit_dir):
    r = bulk(client, "archive", ["memory:frag:ccc", "memory:frag:ddd"])
    assert r.status_code == 200, r.text
    assert r.json()["succeeded"] == 2 and r.json()["failed"] == []
    for k in ("memory:frag:ccc", "memory:frag:ddd"):
        row = storage.get_fragment(k)
        assert row["is_archived"] == "1" and row["valid_until"]
        assert storage.fragment_exists(k)         # 还在，只是不再有效
    assert audit_lines(audit_dir)[-1]["action"] == "ops.bulk.archive"


def test_bulk_archive_幂等_与单条一致(client, storage):
    """单条归档重复做不报 409（归档 = 「确保这条已下线」），批量同款。"""
    assert bulk(client, "archive", ["memory:frag:ccc"]).json()["succeeded"] == 1
    second = bulk(client, "archive", ["memory:frag:ccc"])
    assert second.status_code == 200 and second.json()["succeeded"] == 1
    assert second.json()["failed"] == []
    assert storage.get_fragment("memory:frag:ccc")["is_archived"] == "1"


# ---------------------------------------------------------------- delete

def test_bulk_delete_备份落盘且行消失(client, storage, backup_dir):
    r = bulk(client, "delete", ["memory:frag:ccc", "memory:frag:ddd"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 2 and body["failed"] == []
    assert body["backup_dir"] == str(audit_mod.backup_dir())
    assert storage.get_fragment("memory:frag:ccc") is None
    assert storage.get_fragment("memory:frag:ddd") is None
    # 逐条备份：两条原文都在档（唯一的回退路径）
    names = backup_files(backup_dir)
    for k in ("ccc", "ddd"):
        assert any(f"{k}.before.json" in n for n in names), names
    bak = json.loads((audit_mod.backup_dir() / "memory:frag:ccc.before.json").read_text("utf-8"))
    assert bak["row"]["content"] == "检索记忆条目 C：临时笔记"


def test_bulk_delete_备份失败_该条跳过其余照删(client, storage, monkeypatch, backup_dir):
    """🔴 防数据丢失：这条的备份写不进 ⇒ 宁删不成，也不许「先删后补备份」。"""
    real = audit_mod.backup_fragment

    def flaky(key, row):
        if key == "memory:frag:ccc":
            raise OSError("磁盘满")
        return real(key, row)

    monkeypatch.setattr(audit_mod, "backup_fragment", flaky)
    r = bulk(client, "delete", ["memory:frag:ccc", "memory:frag:ddd"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 1
    assert body["failed"] == [{"key": "memory:frag:ccc", "reason": "backup_failed"}]
    assert storage.get_fragment("memory:frag:ccc") is not None      # 没删
    assert storage.get_fragment("memory:frag:ddd") is None          # 另一条照删


def test_bulk_delete_不存在的key_计入failed(client, storage):
    r = bulk(client, "delete", ["memory:frag:zzz"])
    body = r.json()
    assert body["succeeded"] == 0
    assert body["failed"] == [{"key": "memory:frag:zzz", "reason": "not_found"}]
    assert storage.scan_fragment_keys("", 500)[1] == [r["key"] for r in SEED]


# ---------------------------------------------------------------- 混合批 / 校验

def test_bulk_混合批_塞一个不存在的key_其余照常(client, storage):
    """用户眼里的「一次勾了 4 条」不该因为其中 1 条已被别处删掉而整批失败。"""
    r = bulk(client, "promote",
             ["memory:frag:zzz", "memory:frag:ccc", "memory:frag:ddd"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 3 and body["succeeded"] == 2
    assert body["failed"] == [{"key": "memory:frag:zzz", "reason": "not_found"}]
    assert storage.get_fragment("memory:frag:ddd")["tags"] == "core,demo,stale"


def test_bulk_keys去重后按去重数确认(client, storage):
    """勾两次同一条 = 勾一条：confirm 按去重后的条数算，操作也只做一遍。"""
    r = client.post(BULK, json={"action": "archive",
                                "keys": ["memory:frag:ccc", "memory:frag:ccc"],
                                "confirm": "2"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    r = bulk(client, "archive", ["memory:frag:ccc", "memory:frag:ccc"])
    assert r.status_code == 200 and r.json()["requested"] == 1


def test_bulk_confirm数不符_400且不写库(client, storage, audit_dir):
    r = bulk(client, "delete", ["memory:frag:ccc"], confirm="2")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    assert storage.get_fragment("memory:frag:ccc") is not None
    assert audit_lines(audit_dir)[-1]["reason"] == "confirm_mismatch"


def test_bulk_缺confirm_400且不写库(client, storage):
    r = client.post(BULK, json={"action": "archive", "keys": ["memory:frag:ccc"]},
                    headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert "is_archived" not in storage.get_fragment("memory:frag:ccc")


def test_bulk_空keys_400(client, audit_dir):
    r = client.post(BULK, json={"action": "archive", "keys": [], "confirm": "0"},
                    headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bulk_no_keys"
    assert audit_lines(audit_dir)[-1]["ok"] is False


def test_bulk_超500条_400不截断(client, storage, audit_dir):
    """超限报 400，绝不默默截成「前 500 条」——被截的批量删 = 用户以为都删了。"""
    keys = [f"memory:frag:k{i:04d}" for i in range(501)]
    r = client.post(BULK, json={"action": "archive", "keys": keys, "confirm": "501"},
                    headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bulk_too_large"
    assert audit_lines(audit_dir)[-1]["reason"] == "bulk_too_large"
    assert storage.scan_fragment_keys("", 500)[1] == [r["key"] for r in SEED]


def test_bulk_非法action_400(client, storage, audit_dir):
    r = client.post(BULK, json={"action": "drop_table", "keys": ["memory:frag:ccc"],
                                "confirm": "1"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bulk_bad_action"
    assert audit_lines(audit_dir)[-1]["action"] == "ops.bulk"


def test_bulk_审计一条批次汇总(client, audit_dir):
    r = bulk(client, "delete", ["memory:frag:ccc", "memory:frag:zzz"])
    assert r.status_code == 200
    lines = [ln for ln in audit_lines(audit_dir) if ln["action"] == "ops.bulk.delete"]
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == {"ts", "actor", "action", "target", "ok", "reason"}
    assert "requested=2" in line["reason"] and "succeeded=1" in line["reason"]
    assert "failed=1" in line["reason"] and "backup_dir=" in line["reason"]
    # 🔴 审计里不许出现 token / 口令
    assert TOKEN not in json.dumps(lines, ensure_ascii=False)


# ---------------------------------------------------------------- bulk-export

def test_bulk_export_jsonl_行数等于勾选数(client):
    keys = ["memory:frag:ccc", "memory:frag:aaa"]
    r = client.post(BULK_EXPORT, json={"keys": keys, "format": "jsonl"}, headers=AUTH)
    assert r.status_code == 200, r.text
    lines = [l for l in r.text.splitlines() if l.strip()]
    assert len(lines) == 2
    assert [json.loads(l)["key"] for l in lines] == keys        # 顺序 = 勾选顺序
    assert json.loads(lines[0])["content"] == "检索记忆条目 C：临时笔记"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"


def test_bulk_export_csv_带BOM与表头(client):
    r = client.post(BULK_EXPORT,
                    json={"keys": ["memory:frag:ccc", "memory:frag:ddd"], "format": "csv"},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.text.startswith("﻿")                    # Excel 直开不乱码
    rows = [l for l in r.text.lstrip("﻿").splitlines() if l.strip()]
    assert rows[0].startswith("key,content,tags")
    assert len(rows) == 3                            # 表头 + 两条


def test_bulk_export_按keys导出_不受全库影响(client):
    """只导勾选的那几条 —— 库里还有 aaa/bbbb 两条常驻，不许混进文件。"""
    r = client.post(BULK_EXPORT, json={"keys": ["memory:frag:bbb"]}, headers=AUTH)
    assert r.status_code == 200
    keys = [json.loads(l)["key"] for l in r.text.splitlines() if l.strip()]
    assert keys == ["memory:frag:bbb"]


def test_bulk_export_零写入(client, storage, audit_dir):
    """纯读端点：一次请求后数据与审计都不该多出一行。"""
    before = storage.scan_fragment_keys("", 500)[1]
    client.post(BULK_EXPORT, json={"keys": ["memory:frag:ccc"]}, headers=AUTH)
    assert storage.scan_fragment_keys("", 500)[1] == before
    assert not audit_dir.exists() or not list(audit_dir.glob("audit-*.jsonl"))


def test_bulk_export_空keys与超限_400(client):
    r = client.post(BULK_EXPORT, json={"keys": []}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bulk_no_keys"
    r = client.post(BULK_EXPORT, json={"keys": [f"k{i}" for i in range(501)]}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bulk_too_large"


def test_bulk_export_非法格式_422(client):
    r = client.post(BULK_EXPORT, json={"keys": ["memory:frag:ccc"], "format": "xlsx"},
                    headers=AUTH)
    assert r.status_code == 422


# ---------------------------------------------------------------- 不回退

def test_单条端点行为未被批量影响(client, storage):
    """红线：单条 promote/demote/archive/delete 的确认串口径与 409 一条都不许变。"""
    assert client.post("/api/v1/core/memory:frag:aaa/promote",
                       json={"confirm": True}, headers=AUTH).status_code == 409
    assert client.post("/api/v1/core/memory:frag:ccc/demote",
                       json={"confirm": True}, headers=AUTH).status_code == 409
    assert client.post("/api/v1/fragments/memory:frag:ccc/archive",
                       json={"confirm": "WRONG"}, headers=AUTH).status_code == 400
    assert storage.get_fragment("memory:frag:ccc")["tags"] == "demo"


def test_GET_export老参数零改动(client):
    """批量另开端点后，按筛选导出（tag/q/limit）必须仍是原样。"""
    r = client.get("/api/v1/export?tag=core&format=jsonl", headers=AUTH)
    assert r.status_code == 200
    keys = [json.loads(l)["key"] for l in r.text.splitlines() if l.strip()]
    assert keys == ["memory:frag:aaa", "memory:frag:bbb"]
