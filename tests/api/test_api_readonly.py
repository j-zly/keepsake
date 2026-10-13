"""只读控制台后端测试（G7）。

覆盖：无 token 401 / 错 token 401 / 正确 token 200 / 只读路由断言 / 分页参数 /
数据源异常 500。存储层用假实现（`dependency_overrides` 注入），不连真实后端。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from keepsake.api import config as config_mod
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import kb as kb_router

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

CORE_N = 120
DEMO_N = 40

#: 认证 POST（P3）：setup / login 公开，logout 要会话（单独断言，别混进下面的白名单）
AUTH_POST_PATHS = {
    "/api/v1/auth/setup",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
}

#: 写面白名单（P2）：非 GET 路由**恰好**是这 6 条写操作 + 3 条认证，其余一律不许出现。
ALLOWED_POST_PATHS = {
    "/api/v1/core/{key}/promote",
    "/api/v1/core/{key}/demote",
    "/api/v1/fragments/{key}/update",
    "/api/v1/fragments/{key}/archive",
    "/api/v1/ops/merge",
    "/api/v1/ops/forget",
} | AUTH_POST_PATHS


class FakeStorage:
    """最小存储替身：只实现控制台用到的那几个只读原语。"""

    def __init__(self) -> None:
        self.rows = []
        for i in range(CORE_N):
            self.rows.append({
                "key": f"memory:frag:core{i:03d}",
                "content": f"常驻记忆第 {i} 条 " + "内容" * 20,
                "tags": "core,shared",
                "updated_at": f"2026-10-13T00:00:{i % 60:02d}+00:00",
            })
        for i in range(DEMO_N):
            self.rows.append({
                "key": f"memory:frag:demo{i:03d}",
                "content": f"检索碎片第 {i} 条",
                "tags": "demo,shared",
                "updated_at": f"2026-10-12T00:00:{i % 60:02d}+00:00",
            })

    def list_by_tag(self, tag):
        return [r for r in self.rows if tag in r["tags"].split(",")]

    def search(self, query, tag_filter="", agent_id="", is_primary=None):
        hits = [dict(r, _combined_score=1.0 - i / 100)
                for i, r in enumerate(self.rows) if query in r["content"]]
        if tag_filter:
            hits = [h for h in hits if tag_filter in h["tags"].split(",")]
        return hits[:15]                      # 真后端也是 final_limit 截断

    def get_fragment(self, key):
        for r in self.rows:
            if r["key"] == key:
                return dict(r, embed_bin=b"\x00\x01binary")
        return None

    def get_fragments_batch(self, keys):
        return {r["key"]: r for r in self.rows if r["key"] in set(keys)}

    def scan_fragment_keys(self, cursor="", limit=200, prefix="memory:frag:"):
        keys = [r["key"] for r in self.rows if r["key"].startswith(prefix)]
        page = keys[:limit]
        nxt = "" if len(keys) <= limit else page[-1]
        return nxt, page


class BoomStorage:
    """数据源异常替身 —— 用来验「不许吞异常装成功」。"""

    def list_by_tag(self, tag):
        raise RuntimeError("后端不可达")

    def scan_fragment_keys(self, *a, **kw):
        raise RuntimeError("后端不可达")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- 认证

def test_health_免认证(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_无token_401(client):
    r = client.get("/api/v1/core")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "missing_token"


def test_错token_401(client):
    r = client.get("/api/v1/core", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"
    # 🔴 401 响应体里不许回显 token（哪怕是错的那个）
    assert TOKEN not in r.text


def test_token未配置一律401(client, monkeypatch, tmp_path):
    """token 未配置 ⇒ 一律 401 `token_not_configured`，与宿主环境无关。

    🔴 隔离必须写全三处：token 环境变量、token 文件**、管理员/会话文件**。
    少隔离管理员文件时，生产上（管理员已初始化 ⇒ `admin_exists()=True`）
    会走成 `invalid_token`，本用例必挂 —— 那不是用例的错，是隔离漏了。
    这里全部指到 `tmp_path` 下**不存在**的路径（另见 G4：外层环境指向真实
    存在的管理员文件也得过）。
    """
    monkeypatch.setenv("CONSOLE_TOKEN", "")
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "不存在-admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "不存在-sessions.json"))
    r = client.get("/api/v1/core", headers=AUTH)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "token_not_configured"


def test_畸形Authorization头_401而不是500(client):
    """畸形头（Bearer 后空白 / 非 Bearer scheme）必须是 401，不许掉进 500。"""
    for header in ({"Authorization": "Bearer "},
                   {"Authorization": "Bearer  "},
                   {"Authorization": TOKEN},
                   {"Authorization": "Basic dXNlcjpwYXNz"}):
        r = client.get("/api/v1/core", headers=header)
        assert r.status_code == 401, header


# ---------------------------------------------------------------- 只读

def test_只读路由断言():
    """🔴 P3 后断言升级：非 GET 路由**恰好**是写面 6 条 + 认证 3 条 POST，不多不少。

    P1 时这里是「一条非 GET 都不能有」；P2 加了写端点，P3 又加了认证端点 ——
    多一条（比如漏加 require_session 依赖的临时端点）同样会被这里抓住。
    """
    app = create_app()
    write = {r.path for r in app.routes
             if getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}}
    assert write == ALLOWED_POST_PATHS, sorted(write)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
            "/api/v1/fragments/{key}", "/api/v1/kb/summary", "/api/v1/kb/search",
            "/api/v1/stats", "/api/v1/crons"} <= paths
    assert len([p for p in paths if p.startswith("/api/v1/")]) >= 8


def test_写路由全部需要token():
    """写面 6 条 POST 一条都不许免认证 —— 少挂一个 require_session 就红。

    认证的 3 条不在此列：setup / login 本来就公开（P3 硬要求），
    logout 的会话校验由 tests/api/test_auth.py 单独逐条打。
    """
    app = create_app()
    for r in app.routes:
        if getattr(r, "path", "") in (ALLOWED_POST_PATHS - AUTH_POST_PATHS):
            names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
            assert any("require_session" in n for n in names), r.path


# ---------------------------------------------------------------- core

def test_core_正确token_200(client):
    r = client.get("/api/v1/core", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == CORE_N
    assert len(body["items"]) == 20
    assert set(body["items"][0]) == {"key", "tags", "content", "updated_at"}


def test_core_stats与列表口径一致(client):
    stats = client.get("/api/v1/core/stats", headers=AUTH).json()
    listing = client.get("/api/v1/core?page_size=100", headers=AUTH).json()
    assert stats["count"] == listing["count"]
    assert stats["chars"] == sum(len(i["content"]) for i in listing["items"]) or stats["chars"] > 0
    assert stats["budget"] > 0 and stats["percent"] >= 0


def test_core_服务端分页(client):
    p1 = client.get("/api/v1/core?page=1&page_size=10", headers=AUTH).json()
    p2 = client.get("/api/v1/core?page=2&page_size=10", headers=AUTH).json()
    assert len(p1["items"]) == len(p2["items"]) == 10
    assert {i["key"] for i in p1["items"]} & {i["key"] for i in p2["items"]} == set()
    assert p1["count"] == p2["count"] == CORE_N


@pytest.mark.parametrize("qs", ["page=0", "page_size=0", "page_size=101", "page=abc"])
def test_core_非法分页参数_422(client, qs):
    assert client.get(f"/api/v1/core?{qs}", headers=AUTH).status_code == 422


def test_core_越界页_空列表但总数不变(client):
    body = client.get("/api/v1/core?page=999&page_size=10", headers=AUTH).json()
    assert body["items"] == []
    assert body["count"] == CORE_N


# ---------------------------------------------------------------- fragments

def test_fragments_检索与分页(client):
    r = client.get("/api/v1/fragments?q=检索&page=1&page_size=5", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert 0 < len(body["items"]) <= 5
    # count 是「检索返回的候选池条数」（真后端 search 也有 final_limit 截断），不是全库总数
    assert body["count"] == 15
    assert body["has_more"] is True
    assert body["items"][0]["score"] is not None


def test_fragments_按标签浏览(client):
    body = client.get("/api/v1/fragments?tag=core&page_size=7", headers=AUTH).json()
    assert body["count"] == CORE_N
    assert len(body["items"]) == 7
    assert all(i["tags"] == "core,shared" for i in body["items"])


def test_fragments_全库浏览_真实总数(client):
    body = client.get("/api/v1/fragments?page_size=5", headers=AUTH).json()
    assert len(body["items"]) == 5
    assert body["has_more"] is True
    # 🟡 P1R2：无过滤时 count 必须是真实总数（本库几千行，扫描可接受），不是 null
    assert body["count"] == CORE_N + DEMO_N
    assert body["count_reason"] == "scan_total"


def test_fragments_各分支都带count口径说明(client):
    for qs, reason in (("?q=检索", "search_candidates"),
                       ("?tag=core", "tag_exact"),
                       ("", "scan_total")):
        body = client.get(f"/api/v1/fragments{qs}", headers=AUTH).json()
        assert body["count_reason"] == reason, qs
        assert body["count"] is not None, qs


def test_fragments_扫描触顶_不给假总数(monkeypatch):
    """keyset 扫描触上限时 count=null，且必须用 count_reason 说明（不许默默给 null）。"""
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    # 上限调到 1 ⇒ 一扫就触顶（截断分支），逼出 scan_capped
    monkeypatch.setattr(config_mod, "RETRIEVAL_SCAN_CAP", 1)
    with TestClient(app) as c:
        body = c.get("/api/v1/fragments?page_size=5", headers=AUTH).json()
    app.dependency_overrides.clear()
    assert body["count"] is None
    assert body["count_reason"] == "scan_capped"
    assert len(body["items"]) == 5


def test_fragment_详情全文(client):
    key = FakeStorage().rows[0]["key"]
    r = client.get(f"/api/v1/fragments/{key}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == key
    assert body["content"].startswith("常驻记忆第 0 条")
    # BLOB 字段不许原样进 JSON
    assert body["raw"]["embed_bin"].startswith("<blob ")


def test_fragment_不存在_404(client):
    r = client.get("/api/v1/fragments/no-such-key", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


# ---------------------------------------------------------------- 失败语义

def test_数据源异常_500且结构化(monkeypatch):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: BoomStorage()
    # Starlette 的 ServerErrorMiddleware 发完 500 响应**仍会把异常抛给 ASGI 服务器**
    # （设计如此，好让服务端日志留痕），所以断言响应体必须关掉 raise_server_exceptions。
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/core", headers=AUTH)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["error"] == "internal_error"
    assert "RuntimeError" in detail["message"]      # 报错可见，不吞成空数组


def test_kb_未配置DSN_500(client, monkeypatch):
    monkeypatch.delenv("KEEPSAKE_PG_DSN", raising=False)
    r = client.get("/api/v1/kb/summary", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_unconfigured"


def test_crons_文件缺失_空列表带来源(client, monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(tmp_path / "jobs.json"))
    body = client.get("/api/v1/crons", headers=AUTH).json()
    assert body["jobs"] == [] and body["count"] == 0
    assert body["source"].endswith("jobs.json")


def test_crons_只报记忆类任务(client, monkeypatch, tmp_path):
    jobs = {"jobs": [
        {"name": "记忆提炼", "script": "memory_distill.py",
         "schedule": {"display": "0 * * * *"}, "last_run_at": "2026-10-13T01:00:00+00:00",
         "last_status": "ok", "enabled": True},
        {"name": "清理磁盘", "script": "clean-disk.sh", "last_status": "failed"},
    ]}
    f = tmp_path / "jobs.json"
    f.write_text(__import__("json").dumps(jobs, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(f))
    body = client.get("/api/v1/crons", headers=AUTH).json()
    assert [j["name"] for j in body["jobs"]] == ["记忆提炼"]
    assert body["jobs"][0]["last_status"] == "ok"
    assert body["jobs"][0]["schedule"] == "0 * * * *"


def test_crons_坏文件_500(client, monkeypatch, tmp_path):
    f = tmp_path / "jobs.json"
    f.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CRON_JOBS", str(f))
    r = client.get("/api/v1/crons", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "cron_read_failed"


# ---------------------------------------------------------------- KB 假驱动
# 88 上没有 KEEPSAKE_PG_DSN、也不许连 180，所以 KB 一律用假驱动覆盖。

FAKE_DSN = "postgresql://fake/fake"          # 假串，无口令；只为让 `_dsn()` 通过
KB_COLS = {"skill_docs": ["id", "title", "content"],
           "documents": ["id", "title", "content"],
           "chunks": ["id", "doc_id", "content"]}
KB_COUNTS = {"skill_docs": 11, "documents": 22, "chunks": 33}


def _fake_driver(counts=None, columns=None):
    """假 PG 驱动：只实现 kb.py 用到的那几个原语（connect/cursor/execute/fetch*）。"""
    counts = counts or KB_COUNTS
    columns = KB_COLS if columns is None else columns

    class Cur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            if "information_schema.columns" in flat:
                self._rows = [{"table_name": t, "column_name": c}
                              for t, cols in columns.items() for c in cols]
            elif "COUNT(*)" in flat:
                self._rows = [dict(counts)]
            else:                               # 检索：一条命中
                self._rows = [{"chunk_id": "c1", "doc_id": "d1", "title": "标题",
                               "snippet": "命中片段", "score": 0.5}]

        def fetchone(self):
            return self._rows[0]

        def fetchall(self):
            return list(self._rows)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return Cur()

    class Driver:
        connected_with = None

        @staticmethod
        def connect(dsn, cursor_factory=None, connect_timeout=5):
            assert dsn, "DSN 不能为空（未配置应先在 _dsn() 就被拦下）"
            Driver.connected_with = (dsn, cursor_factory)
            return Conn()

    return Driver


def _install_fake_pg2(monkeypatch, *, extras="ok", columns=None):
    """塞假 psycopg2 并让 psycopg3 不可用（逼驱动只能走 psycopg2 分支）。

    `extras`：`ok` 有 RealDictCursor / `missing` 整个子模块都没有 /
    `broken` 子模块在但缺 RealDictCursor（属性访问会 AttributeError）。
    """
    mod = _fake_driver(columns=columns)
    monkeypatch.setitem(sys.modules, "psycopg2", mod)
    if extras == "ok":
        mod.extras = SimpleNamespace(RealDictCursor=object())
        monkeypatch.setitem(sys.modules, "psycopg2.extras", mod.extras)
    else:
        monkeypatch.delitem(sys.modules, "psycopg2.extras", raising=False)
        if extras == "broken":
            monkeypatch.setitem(sys.modules, "psycopg2.extras", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "psycopg", None)
    return mod


@pytest.fixture()
def kb_env(monkeypatch):
    """装假驱动 + 清列名缓存（kb 的列名缓存是进程级的，测试间必须隔离）。"""
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    saved = dict(kb_router._COLS_CACHE)
    kb_router._COLS_CACHE.clear()
    yield monkeypatch
    kb_router._COLS_CACHE.clear()
    kb_router._COLS_CACHE.update(saved)


def test_kb_summary_假驱动_返回三表计数(kb_env, client):
    mod = _install_fake_pg2(kb_env)
    kb_env.setenv("KEEPSAKE_PG_DSN", FAKE_DSN)
    r = client.get("/api/v1/kb/summary", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == KB_COUNTS
    assert mod.connected_with[0] == FAKE_DSN
    assert mod.connected_with[1] is mod.extras.RealDictCursor


def test_kb_search_假驱动_返回命中(kb_env, client):
    _install_fake_pg2(kb_env)
    kb_env.setenv("KEEPSAKE_PG_DSN", FAKE_DSN)
    r = client.get("/api/v1/kb/search?q=命中", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0]["snippet"] == "命中片段"
    assert body["count"] == 1


@pytest.mark.parametrize("extras", ["missing", "broken"])
def test_kb_假驱动不完整_不裸炸(kb_env, client, extras):
    """🔴 P1R2 回归：psycopg2 在但 extras 用不了 ⇒ 500 kb_no_driver，不是裸 AttributeError。"""
    _install_fake_pg2(kb_env, extras=extras)
    kb_env.setenv("KEEPSAKE_PG_DSN", FAKE_DSN)
    r = client.get("/api/v1/kb/summary", headers=AUTH)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["error"] == "kb_no_driver"
    assert "Traceback" not in detail["message"]
    assert FAKE_DSN not in r.text          # 报错不许回显连接串


def test_kb_未配置DSN_优先于驱动缺失(kb_env, client):
    """判断顺序硬约束：先 `_dsn()` 判未配置，再 `_pg()` 载驱动。"""
    _install_fake_pg2(kb_env, extras="missing")
    kb_env.delenv("KEEPSAKE_PG_DSN", raising=False)
    r = client.get("/api/v1/kb/summary", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_unconfigured"


def test_kb_表不存在_结构化报错(kb_env, client):
    """列探不到（表不存在）⇒ kb_table_missing，不是空计数装成功。"""
    _install_fake_pg2(kb_env, columns={})
    kb_env.setenv("KEEPSAKE_PG_DSN", FAKE_DSN)
    r = client.get("/api/v1/kb/summary", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "kb_table_missing"


def test_stats_kb段_不可用时显式标(kb_env, client):
    """总览接口里 kb 不可用要显式标（可见报错），不是空数据装成功。"""
    kb_env.delenv("KEEPSAKE_PG_DSN", raising=False)
    kb = client.get("/api/v1/stats", headers=AUTH).json()["kb"]
    assert kb["available"] is False
    assert kb["error"] == "kb_unconfigured"
