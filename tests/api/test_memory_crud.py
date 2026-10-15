"""控制台记忆 CRUD 端点测试（增 / 改索引 / 真删 / 导出 / 导入）。

**用真后端**：临时目录上的 `SqliteStorage`（`is_primary=True`，与控制台主视角
同款 —— 非 primary 后端的检索只放行 core/shared，那是子代理的只读口径），
不走假存储：这一单的价值全在「真的经 `storage.store` /
`update_fragment_content` / `delete_fragments_batch` 落库」，假存储只能测到
控制台自己的 if/else。

本文件钉死四件最容易悄悄坏掉的事：
  1. 保留字治理 —— `core` 拒收、其余剥除，且判据与 `cron/memory_distill.py`
     **共用同一个函数**（漂一份 = 假申请进候选审核队列的入口）；
  2. **改正文后索引必须跟着改** —— 旧词搜不到、新词搜得到（双向断言，
     只断一头会漏掉「索引整个空了」这种假绿）；
  3. 真删 = 备份先行 + 确认串手输，删完库与备份文件对得上；
  4. 导出/导入的计数口径（超限 400 不截断、重复 skip、dry-run 零写入）。

PG 用例连 `~/.keepsake_pg_test.env` 里的 8.140.192.91/keepsake_test（隔离 schema，
用完 DROP），读不到凭据就 skip —— 与 `tests/test_list_by_tag_archived.py` 同一套接法。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import ops as ops_module

TOKEN = "crud-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

CREATE = "/api/v1/fragments"
EXPORT = "/api/v1/export"
IMPORT = "/api/v1/import"

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

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST, ALLOWED_DBNAME = "8.140.192.91", "keepsake_test"


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


DSN = _read_dsn()
_pg_ready = bool(DSN and ALLOWED_HOST in DSN and ALLOWED_DBNAME in DSN)


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def storage(tmp_path):
    """真 SqliteStorage（临时文件）+ 种子数据。"""
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "crud.db"), embedder=None, embed_dim=8,
                      is_primary=True)
    assert s.ensure_index() is not False
    assert s.write_fragments_batch([dict(r) for r in SEED]) == len(SEED)
    return s


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    d = tmp_path / "console_audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
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
    out: List[dict] = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def hits(storage, q: str, tag: str = "") -> List[str]:
    """检索命中的 key 列表（走真 `storage.search`，与检索层同一条路径）。"""
    return [str(r.get("key") or r.get("_key")) for r in storage.search(q, tag_filter=tag)]


def key_of(storage, content: str) -> str:
    """内容 → key（**用与 store() 同一个函数**，不自己拼 sha256）。"""
    from keepsake.core.shared import fragment_key_for

    return fragment_key_for(content)


# ---------------------------------------------------------------- 认证 / 守卫

def test_三个新写端点都要token(client):
    """漏挂 require_session 就是裸奔写面 —— 逐条打一遍（401，不是 400/500）。"""
    cases = [(CREATE, {"confirm": True, "content": "偷偷写一条"}),
             ("/api/v1/fragments/memory:frag:ccc/delete", {"confirm": "rag:ccc"}),
             (IMPORT, {"items": [{"content": "偷偷导一条"}]})]
    for url, body in cases:
        r = client.post(url, json=body)
        assert r.status_code == 401, (url, r.status_code)
        assert r.json()["detail"]["error"] == "missing_token"
        assert TOKEN not in r.text


def test_导出也要token(client):
    assert client.get(EXPORT).status_code == 401
    assert client.get(EXPORT, headers=AUTH).status_code == 200


def test_导出是只读端点_三写端点带write_guard():
    """🔴 静态判据：export 只挂 require_session；增/删/导入挂 write_guard。

    导出是纯读，挂了 write_guard 会在审计目录不可写时**白白拒绝一次读**；
    反过来三个写端点漏挂 = 审计落盘位置写不进时仍照改数据（fail-closed 破防）。
    """
    app = create_app()
    want = {EXPORT: ({"GET"}, False),
            CREATE: ({"POST"}, True),
            "/api/v1/fragments/{key}/delete": ({"POST"}, True),
            IMPORT: ({"POST"}, True)}
    seen = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if path not in want:
            continue
        methods, need_guard = want[path]
        if not (getattr(r, "methods", set()) & methods):
            continue          # 同路径的另一个方法（如 GET /fragments）不算
        seen.add(path)
        names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        assert any("require_session" in n for n in names), path
        assert any("write_guard" in n for n in names) is need_guard, (path, names)
    assert seen == set(want), f"有端点没注册：{set(want) - seen}"


# ---------------------------------------------------------------- 新增

def test_新增落库且可检索(client, storage, audit_dir):
    r = client.post(CREATE, json={"confirm": True, "content": "  新的记忆：值班用企业微信  ",
                                  "tags": "ops,oncall"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["score"] is None
    assert body["key"] == key_of(storage, "新的记忆：值班用企业微信")
    assert body["tags"] == "ops,oncall"
    # 真落库 + 检索得到（正文两端空白按没填处理，不入库）
    assert storage.get_fragment(body["key"])["content"] == "新的记忆：值班用企业微信"
    assert body["key"] in hits(storage, "企业微信")
    assert audit_lines(audit_dir)[-1]["action"] == "fragment.create"


def test_新增缺confirm_400(client, storage):
    r = client.post(CREATE, json={"content": "偷偷写一条"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
def test_新增空正文400(client, storage, content):
    r = client.post(CREATE, json={"confirm": True, "content": content}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "empty_content"
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)


def test_新增打core_400_且未落库(client, storage, audit_dir):
    """直打 core = 绕过 promote/候选审核 ⇒ 拒收（不是静默剥标签）。"""
    r = client.post(CREATE, json={"confirm": True, "content": "我想直打常驻",
                                  "tags": "core,ops"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "reserved_tag"
    assert "promote" in r.json()["detail"]["message"]
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)
    assert audit_lines(audit_dir)[-1]["ok"] is False


def test_新增其余保留字被剥除(client, storage):
    """`core_candidate` / `shared` / `agent:*` 剥掉，普通标签保序。"""
    r = client.post(CREATE, json={"confirm": True, "content": "带保留标签的记忆",
                                  "tags": "core_candidate,看板,shared,agent:x1,ops"},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == "看板,ops"
    assert storage.get_fragment(r.json()["key"])["tags"] == "看板,ops"


def test_保留字判据与提炼共用同一函数():
    """🔴 判据单点：控制台与 cron 的剥离结果必须来自同一个函数对象。"""
    import keepsake.core.shared as ks_shared

    assert ops_module.sanitize_reserved_tags is ks_shared.sanitize_reserved_tags
    assert "core_candidate" in ks_shared.RESERVED_TAG_EXACT
    assert ks_shared.RESERVED_TAG_PREFIX == "agent:"


def test_同内容再新增_返回版本化后的新key而不是失效旧行(client, storage):
    """🔴 `store()` 的去重语义 = 版本化（`base:epoch`），不是覆盖。

    若按内容 hash 直接把 base 返回给前端，它指向的是刚被标 `is_archived='1'`
    的旧行 —— 列表里看不见、导出里不出现、删它删的是历史版本。
    """
    first = client.post(CREATE, json={"confirm": True, "content": "同一条内容写两遍"},
                        headers=AUTH).json()
    second = client.post(CREATE, json={"confirm": True, "content": "同一条内容写两遍"},
                         headers=AUTH).json()
    assert first["key"] != second["key"], second
    assert second["key"].startswith(first["key"] + ":")
    # 活的那一份既在库里、也在列表里，且是第二条的 key
    assert storage.get_fragment(second["key"])["content"] == "同一条内容写两遍"
    assert second["key"] in [i["key"] for i in client.get("/api/v1/fragments?page_size=100",
                                                          headers=AUTH).json()["items"]]
    assert storage.get_fragment(first["key"])["is_archived"] == "1"
    # 删新key 删的是活的那份
    assert client.post(f"/api/v1/fragments/{second['key']}/delete",
                       json={"confirm": second["key"][-8:]}, headers=AUTH).status_code == 200
    assert storage.get_fragment(second["key"]) is None
    assert storage.get_fragment(first["key"]) is not None


# ---------------------------------------------------------------- 改正文 → 索引跟随

def test_改正文后旧词搜不到新词搜得到(client, storage):
    """🔴 双向断言：只断「新词能搜到」会放过「索引整个空了」的假绿。"""
    old, new = "回测框架用阿里云服务器", "熔断降级限流方案"
    assert client.post(CREATE, json={"confirm": True, "content": old},
                       headers=AUTH).status_code == 200
    key = key_of(storage, old)
    assert key in hits(storage, "阿里云"), "前置：改前旧词必须能搜到"

    r = client.post(f"/api/v1/fragments/{key}/update",
                    json={"confirm": True, "content": new}, headers=AUTH)
    assert r.status_code == 200, r.text

    assert key in hits(storage, "熔断"), "新词搜不到 = 索引没跟着改"
    assert key in hits(storage, "限流")
    assert key not in hits(storage, "阿里云"), "旧词还能搜到 = 索引留着旧正文"
    assert key not in hits(storage, "回测")
    assert storage.get_fragment(key)["content"] == new


def test_改正文同时改标签_标签仍在召回面(client, storage):
    key = "memory:frag:ccc"
    r = client.post(f"/api/v1/fragments/{key}/update",
                    json={"confirm": True, "content": "故障演练复盘记录", "tags": "demo,改稿"},
                    headers=AUTH)
    assert r.status_code == 200 and r.json()["updated"] == ["content", "tags"]
    assert key in hits(storage, "复盘")
    assert key in hits(storage, "改稿", tag="demo,改稿"), "改标签后标签仍在召回面"


@pytest.mark.skipif(not _pg_ready, reason="无 8.140.192.91/keepsake_test 凭据，跳过 PG 真库")
def test_pg_改正文后content_tsv同步():
    """PG 侧同款判据 —— 这是 PG 历史上真实存在的 bug：
    `update_fragment_fields` 只 UPDATE 列、不重算 `content_tsv`，
    于是「改成功了」而 BM25 仍按旧正文召回。
    """
    psycopg = pytest.importorskip("psycopg", reason="未装 psycopg")
    from keepsake.storage_pg import PgStorage

    schema = f"ks_crud_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    pg = PgStorage(dsn=DSN + f"?options=-csearch_path%3D{schema},public",
                   connect_timeout=30, embed_dim=4, is_primary=True)
    try:
        assert pg.ensure_index() is True
        old, new = "回测框架用阿里云服务器", "Kubernetes调度器用火山引擎"
        assert pg.store(text=old, tags="crudtest", category="manual",
                        source="console", fragment_type="memory") is True
        key = pg._fragment_key_for(old)  # noqa: SLF001 —— 与 store() 同一算法
        assert [r["_key"] for r in pg.search_bm25("阿里云", tag_filter="crudtest")] == [key], \
            "前置：改前旧词必须能搜到"

        assert pg.update_fragment_content(key, new, tags="crudtest") is True

        after_new = pg.search_bm25("火山引擎", tag_filter="crudtest")
        assert key in [r["_key"] for r in after_new], "新词搜不到 = content_tsv 没重算"
        assert pg.search_bm25("阿里云", tag_filter="crudtest") == [], \
            "旧词还能搜到 = content_tsv 留着旧正文"
        assert pg.get_fragment(key)["content"] == new
    finally:
        pg.close()
        with psycopg.connect(DSN, autocommit=True, connect_timeout=30) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# ---------------------------------------------------------------- 真删

def test_真删_行消失且备份含原文(client, storage, backup_dir, audit_dir):
    r = client.post("/api/v1/fragments/memory:frag:ddd/delete",
                    json={"confirm": "frag:ddd"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["deleted"] is True
    assert storage.get_fragment("memory:frag:ddd") is None
    assert "memory:frag:ddd" not in [r["key"] for r in storage.list_by_tag("demo")]
    # 备份是**删前**原文 —— 唯一的回退路径
    bak = json.loads(next(backup_dir.rglob("*.before.json")).read_text(encoding="utf-8"))
    assert bak["key"] == "memory:frag:ddd"
    assert bak["row"]["content"] == "检索记忆条目 D：旧版本排查"
    assert audit_lines(audit_dir)[-1]["action"] == "fragment.delete"


def test_真删_确认串错400且不删(client, storage, backup_dir):
    r = client.post("/api/v1/fragments/memory:frag:ddd/delete",
                    json={"confirm": "WRONG"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    assert storage.get_fragment("memory:frag:ddd") is not None
    assert not list(backup_dir.rglob("*"))       # 没过闸 ⇒ 连备份都不写


def test_真删_缺确认串400(client, storage):
    r = client.post("/api/v1/fragments/memory:frag:ddd/delete", json={}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert storage.get_fragment("memory:frag:ddd") is not None


def test_真删_不存在404(client, storage):
    r = client.post("/api/v1/fragments/memory:frag:zzz/delete",
                    json={"confirm": "frag:zzz"}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)


def test_真删_常驻条目可直接删(client, storage):
    """常驻层不豁免：删完 list_by_tag 自然少一条，与 promote/demote 无耦合。"""
    r = client.post("/api/v1/fragments/memory:frag:aaa/delete",
                    json={"confirm": "frag:aaa"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [i["key"] for i in storage.list_by_tag("core")] == ["memory:frag:bbb"]


def test_真删_备份写不进去_绝不删(client, monkeypatch, storage, backup_dir):
    """防数据丢失是硬要求：备份失败 ⇒ 行还在。"""
    from keepsake.api import audit as audit_mod

    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setattr(audit_mod, "backup_fragment",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("磁盘满")))
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/fragments/memory:frag:ddd/delete",
                   json={"confirm": "frag:ddd"}, headers=AUTH)
    app.dependency_overrides.clear()
    assert r.status_code == 500
    assert storage.get_fragment("memory:frag:ddd") is not None


# ---------------------------------------------------------------- 导出

def test_导出jsonl_行数等于库内条数(client, storage):
    r = client.get(EXPORT, headers=AUTH)
    assert r.status_code == 200, r.text
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == len(SEED)
    recs = [json.loads(ln) for ln in lines]
    assert {rec["key"] for rec in recs} == {s["key"] for s in SEED}
    assert set(recs[0]) == {"key", "content", "tags", "entities", "created", "updated_at"}
    assert "keepsake-export-" in r.headers["Content-Disposition"]


def test_导出_tag过滤子集正确(client, storage):
    r = client.get(f"{EXPORT}?tag=core", headers=AUTH)
    assert r.status_code == 200, r.text
    keys = {json.loads(ln)["key"] for ln in r.text.splitlines() if ln.strip()}
    assert keys == {"memory:frag:aaa", "memory:frag:bbb"}


def test_导出_q过滤走检索(client, storage):
    r = client.get(f"{EXPORT}?q=临时笔记", headers=AUTH)
    assert r.status_code == 200, r.text
    keys = {json.loads(ln)["key"] for ln in r.text.splitlines() if ln.strip()}
    assert keys == {"memory:frag:ccc"}


def test_导出时间列各后端都有值(client, storage):
    """sqlite/redis 的时间字段叫 `updated`（PG 没有该列，回落 `created`）
    —— 导出契约是**稳定并集**，不许某一后端整列空。"""
    recs = [json.loads(ln) for ln in
            client.get(EXPORT, headers=AUTH).text.splitlines() if ln.strip()]
    assert recs and all(r["updated_at"] for r in recs), [r["updated_at"] for r in recs]


def test_导出csv_有BOM与列头(client, storage):
    """BOM 的动机是 Excel 直开 CSV（中文按本地编码解会全乱码）。"""
    r = client.get(f"{EXPORT}?format=csv", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\xef\xbb\xbf"), "CSV 缺 UTF-8 BOM"
    text = r.content.decode("utf-8-sig")
    rows = [ln for ln in text.splitlines() if ln.strip()]
    assert rows[0] == "key,content,tags,entities,created,updated_at"
    assert len(rows) == len(SEED) + 1          # 列头 + 数据行
    assert r.headers["Content-Disposition"].endswith('.csv"')


def test_导出超限400_不截断(client, storage):
    """"看起来完整"的截断文件比报错危险得多 ⇒ 超限报 400 并给分批口径。"""
    r = client.get(f"{EXPORT}?limit=2", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "export_too_large"
    assert "tag=" in r.json()["detail"]["message"]


def test_导出limit封顶50000(client, storage):
    assert client.get(f"{EXPORT}?limit=50001", headers=AUTH).status_code == 422


# ---------------------------------------------------------------- 导入

ITEMS = [
    {"content": "导入的记忆一：机房钥匙在二楼抽屉", "tags": "ops"},
    {"content": "导入的记忆二：客户只走对公转账", "tags": "finance,ops"},
]


def test_导入dry_run零写入且计数对(client, storage, audit_dir):
    before = storage.scan_fragment_keys("", 500)[1]
    r = client.post(IMPORT, json={"items": ITEMS}, headers=AUTH)   # 不传 dry_run
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["total"] == 2 and body["would_add"] == 2
    assert body["skipped_duplicate"] == 0 and body["rejected_reserved"] == 0
    assert body["confirm_required"] == "2"
    assert storage.scan_fragment_keys("", 500)[1] == before, "dry-run 落库了"
    assert audit_lines(audit_dir)[-1]["ok"] is True


def test_导入重复内容skip(client, storage):
    first = client.post(IMPORT, json={"items": ITEMS, "dry_run": False, "confirm": "2"},
                        headers=AUTH)
    assert first.status_code == 200, first.text
    body = client.post(IMPORT, json={"items": [
        {"content": "导入的记忆一：机房钥匙在二楼抽屉", "tags": "ops"},
        {"content": "完全新的记忆三：周会改到周二", "tags": ""},
    ]}, headers=AUTH).json()
    assert body["total"] == 2 and body["skipped_duplicate"] == 1
    assert body["would_add"] == 1


def test_导入空正文与保留字逐条计数(client, storage):
    """批量场景不为一条打回整批 —— 逐条计入，计数必须对得上。"""
    body = client.post(IMPORT, json={"items": [
        {"content": "   ", "tags": "ops"},
        {"content": "想直打常驻的导入条目", "tags": "core,ops"},
        {"content": "普通的一条导入", "tags": "ops"},
    ]}, headers=AUTH).json()
    assert body["total"] == 3
    assert body["rejected_empty"] == 1 and body["rejected_reserved"] == 1
    assert body["would_add"] == 1 and body["confirm_required"] == "1"


@pytest.mark.parametrize("confirm", [None, "", "999", "1"])
def test_导入实跑confirm不符400且零写入(client, storage, confirm):
    body: Dict[str, Any] = {"items": ITEMS, "dry_run": False}
    if confirm is not None:
        body["confirm"] = confirm
    r = client.post(IMPORT, json=body, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] in ("confirm_required", "confirm_mismatch")
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)


def test_导入实跑落库可检索(client, storage, audit_dir):
    r = client.post(IMPORT, json={"items": ITEMS, "dry_run": False, "confirm": "2"},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False and body["would_add"] == 2
    assert set(body["keys"]) == {key_of(storage, i["content"]) for i in ITEMS}
    assert storage.get_fragment(body["keys"][0])["tags"] == "ops"
    assert body["keys"][0] in hits(storage, "机房钥匙")
    assert body["keys"][1] in hits(storage, "对公转账")
    # 批量写留痕（批次摘要，不含正文）
    lines = audit_lines(audit_dir)
    assert lines[-1]["action"] == "memory.import" and lines[-1]["ok"] is True
    assert "would_add=2" in lines[-1]["reason"]
    assert "机房钥匙" not in lines[-1]["reason"]


def test_导入超上限400(client, storage):
    r = client.post(IMPORT, json={"items": [{"content": f"条目{i}"} for i in range(5001)],
                                   "dry_run": False, "confirm": "5001"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "import_too_large"
    assert len(storage.scan_fragment_keys("", 500)[1]) == len(SEED)


# ---------------------------------------------------------------- 只读守卫

def test_P1只读路由未被写面影响(client):
    for url in ("/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
                "/api/v1/fragments/memory:frag:ccc", "/api/v1/stats",
                "/api/v1/crons"):
        assert client.get(url, headers=AUTH).status_code == 200, url
