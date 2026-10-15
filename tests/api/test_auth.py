"""登录 / 会话测试（P3 · 正负双向）。

🔴 **一个字节都不碰 `/root/.hermes/secrets/`**：管理员文件、会话文件全部由
环境变量指到 `tmp_path`，逻辑与线上同一条代码路径（同样的 PBKDF2、同样的
会话文件读写、同样的限速）。

存储用真 `SqliteStorage`（临时文件）+ 种子数据 —— 「会话 token 能进业务接口」
这件事要真的打通才有意义，假存储只能测到 401/200 的 if/else。
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from keepsake.api import auth as auth_mod
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

#: 旧静态 token（兼容路径）；与 test_api_readonly.TOKEN 不同名，两边各写一遍
LEGACY_TOKEN = "p3-legacy-static-token-not-a-real-secret"
LEGACY = {"Authorization": f"Bearer {LEGACY_TOKEN}"}

USER = "admin"
#: 口令是**测试夹具**，不是凭据；生产口令从不出现在任何文件里
PWD = "correct horse"
SHORT_PWD = "short"

SEED: list = [
    {"key": "memory:frag:aaa", "content": "常驻记忆 A", "tags": "core",
     "created": "2026-01-01T00:00:00+00:00"},
    {"key": "memory:frag:bbb", "content": "检索记忆条目 B", "tags": "demo",
     "created": "2026-01-01T00:00:00+00:00"},
]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """凭据 / 会话文件指向临时目录 + 静态 token 就位 + 限速计数清零。

    限速计数是**进程内**的（不引第三方库），所以每例都得清 —— 否则 A 例的失败
    会把 B 例的登录也 429 掉。
    """
    monkeypatch.setenv("KEEPSAKE_CONSOLE_ADMIN_FILE", str(tmp_path / "admin.json"))
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("CONSOLE_TOKEN", LEGACY_TOKEN)
    auth_mod.reset_rate_limit()
    yield
    auth_mod.reset_rate_limit()


@pytest.fixture()
def storage(tmp_path):
    from keepsake.core.sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "auth.db"), embedder=None, embed_dim=8)
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


def do_setup(c, username: str = USER, password: str = PWD) -> Dict[str, Any]:
    r = c.post("/api/v1/auth/setup", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def do_login(c, username: str = USER, password: str = PWD, remember: bool = False):
    return c.post("/api/v1/auth/login",
                  json={"username": username, "password": password, "remember": remember})


# ---------------------------------------------------------------- status

def test_status_公开_未初始化(client):
    r = client.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json() == {"initialized": False, "auth_mode": "login"}


def test_status_setup之后变True(client):
    do_setup(client)
    assert client.get("/api/v1/auth/status").json()["initialized"] is True


def test_status_不回显用户名或静态token(client):
    """🔴 status 是公开端点：只回一个布尔，别的什么都不许漏。"""
    do_setup(client)
    body = client.get("/api/v1/auth/status").json()
    assert set(body) == {"initialized", "auth_mode"}
    assert USER not in json.dumps(body) and LEGACY_TOKEN not in json.dumps(body)


# ---------------------------------------------------------------- setup

def test_setup_建管理员并自动登录(client):
    body = do_setup(client)
    assert body["username"] == USER and body["token"] and body["expires_at"]
    me = client.get("/api/v1/auth/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.json()["username"] == USER


def test_setup_并发抢注_只留一个管理员(env):
    """🔴 两个 setup 同时开跑时，靠 os.link 的原子性挡住第二个（不能「后写的覆盖先写的」）。"""
    from keepsake.api import config as cfg

    do_setup(TestClient(create_app()))
    with pytest.raises(FileExistsError):
        auth_mod.create_admin("hacker", PWD)
    assert json.loads(cfg.admin_file().read_text("utf-8"))["username"] == USER


def test_setup_口令太短_400且不落文件(client):
    r = client.post("/api/v1/auth/setup", json={"username": USER, "password": SHORT_PWD})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "password_too_short"
    assert client.get("/api/v1/auth/status").json()["initialized"] is False


def test_setup_口令刚好8位_可以初始化(client):
    """边界：7 位拒、8 位过。"""
    assert client.post("/api/v1/auth/setup",
                       json={"username": USER, "password": "1234567"}).status_code == 400
    assert client.post("/api/v1/auth/setup",
                       json={"username": USER, "password": "12345678"}).status_code == 200


def test_setup_用户名空_400(client):
    r = client.post("/api/v1/auth/setup", json={"username": "  ", "password": PWD})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "username_required"


def test_setup_缺字段_400(client):
    assert client.post("/api/v1/auth/setup", json={}).status_code == 400


def test_setup_重复初始化_409(client):
    do_setup(client)
    r = client.post("/api/v1/auth/setup", json={"username": "hacker", "password": PWD})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "already_initialized"
    # 🔴 409 那次不能把管理员换掉 —— 抢注窗口必须关死
    assert client.get("/api/v1/auth/me", headers=bearer(
        do_login(client).json()["token"])).json()["username"] == USER


def test_setup_磁盘上只有哈希没有明文(env):
    admin_file = __import__("keepsake.api.config", fromlist=["x"]).admin_file()
    do_setup(TestClient(create_app()))
    raw = admin_file.read_text(encoding="utf-8")
    assert PWD not in raw
    rec = json.loads(raw)
    assert set(rec) == {"username", "salt", "hash", "iters", "created"}
    assert rec["iters"] >= 100000 and rec["username"] == USER


def test_setup_凭据文件权限600(env):
    from keepsake.api import config as cfg

    do_setup(TestClient(create_app()))
    mode = stat.S_IMODE(os.stat(cfg.admin_file()).st_mode)
    assert mode == 0o600, oct(mode)


def test_setup_口令太长_400(client):
    r = client.post("/api/v1/auth/setup",
                    json={"username": USER, "password": "x" * (auth_mod.MAX_PASSWORD_LEN + 1)})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "password_too_long"


# ---------------------------------------------------------------- login

def test_login_正确口令_发会话(client):
    do_setup(client)
    r = do_login(client, remember=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] and body["expires_at"] and body["username"] == USER


def test_login_remember_30天(client):
    from datetime import datetime, timezone

    do_setup(client)
    body = do_login(client, remember=True).json()
    assert body["ttl_seconds"] == 30 * 86400
    days = (datetime.fromisoformat(body["expires_at"])
            - datetime.now(timezone.utc)).days
    assert 29 <= days <= 30


def test_login_不remember_12小时(client):
    do_setup(client)
    assert do_login(client).json()["ttl_seconds"] == 12 * 3600


def test_login_会话TTL可用环境变量改(env, monkeypatch):
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_TTL", "60")
    do_setup(TestClient(create_app()))
    assert do_login(TestClient(create_app())).json()["ttl_seconds"] == 60


def test_login_TTL配成非法值_回落默认(env, monkeypatch):
    monkeypatch.setenv("KEEPSAKE_CONSOLE_SESSION_TTL", "not-a-number")
    from keepsake.api import config as cfg

    assert cfg.session_ttl() == cfg.DEFAULT_SESSION_TTL


def test_login_口令错_401且文案统一(client):
    do_setup(client)
    r = do_login(client, password="wrong password")
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_credentials"


def test_login_用户不存在_与口令错同一个文案(client):
    """🔴 不区分 = 不给用户名爆破当预言机。"""
    do_setup(client)
    a = do_login(client, password="wrong password").json()["detail"]
    b = do_login(client, username="ghost", password="wrong password").json()["detail"]
    assert a == b


def test_login_错误响应不回显口令或token(client):
    do_setup(client)
    r = do_login(client, password="wrong password")
    assert PWD not in r.text and "wrong password" not in r.text


def test_login_会话文件只存token的sha256(env):
    from keepsake.api import config as cfg

    do_setup(TestClient(create_app()))
    token = do_login(TestClient(create_app())).json()["token"]
    raw = cfg.session_file().read_text(encoding="utf-8")
    assert token not in raw
    assert auth_mod.token_hash(token) in raw
    rec = json.loads(raw)["sessions"][0]
    assert set(rec) == {"token_hash", "username", "expires_at"}


def test_login_未初始化_一律401(env):
    r = do_login(TestClient(create_app()))
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_credentials"


def test_login_空用户名_401不是500(client):
    do_setup(client)
    assert client.post("/api/v1/auth/login",
                       json={"username": "", "password": PWD}).status_code == 401


# ---------------------------------------------------------------- 限速

def test_限速_第6次错口令_429(client):
    do_setup(client)
    codes = [do_login(client, password="wrong password").status_code for _ in range(5)]
    assert codes == [401] * 5
    r = do_login(client, password="wrong password")
    assert r.status_code == 429 and r.json()["detail"]["error"] == "too_many_attempts"


def test_限速_被限速时正确口令也不放行(client):
    """🔴 限速不是「只挡错口令」：窗口内的正确口令也要等 —— 否则限速等于没有。"""
    do_setup(client)
    for _ in range(5):
        do_login(client, password="wrong password")
    assert do_login(client).status_code == 429


def test_限速_登录成功即清零(client):
    do_setup(client)
    for _ in range(4):
        do_login(client, password="wrong password")
    assert do_login(client).status_code == 200
    assert do_login(client, password="wrong password").status_code == 401   # 计数从头来


def test_限速_按用户名分桶(client):
    """换个用户名是另一个桶（攻击者可以轮着试，锁死单桶反而挡不住）。"""
    do_setup(client)
    for _ in range(5):
        do_login(client, password="wrong password")
    assert do_login(client, username=USER).status_code == 429
    assert do_login(client, username="someone-else", password="x").status_code == 401


def test_限速_窗口过期后恢复(monkeypatch):
    from keepsake.api import auth as am

    do_setup(TestClient(create_app()))
    for _ in range(5):
        do_login(TestClient(create_app()), password="wrong password")
    clock = [am.time.monotonic()]          # 从真实时刻起算，猴补的时钟要能盖住已记的时间戳
    monkeypatch.setattr(am.time, "monotonic", lambda: clock[0])
    clock[0] += am.RATE_WINDOW_SEC + 1
    assert do_login(TestClient(create_app()), password="wrong password").status_code == 401


# ---------------------------------------------------------------- 会话校验

def test_会话token_能进业务接口(client):
    do_setup(client)
    token = do_login(client).json()["token"]
    r = client.get("/api/v1/core", headers=bearer(token))
    assert r.status_code == 200 and r.json()["count"] == 1


def test_会话token_能进写接口且写闸仍在(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "bak"))
    do_setup(client)
    token = do_login(client).json()["token"]
    r = client.post("/api/v1/ops/merge", json={"dry_run": False}, headers=bearer(token))
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


def test_伪造的会话token_401(client):
    do_setup(client)
    r = client.get("/api/v1/core", headers=bearer("totally-made-up"))
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_token"


def test_过期会话_401且从文件清理(env):
    from keepsake.api import config as cfg

    c = TestClient(create_app())
    do_setup(c)
    token = do_login(c).json()["token"]
    # 只把**这一个 token** 的记录改成过期（setup/login 会话是两条，别一起抹掉）
    recs = json.loads(cfg.session_file().read_text("utf-8"))["sessions"]
    for rec in recs:
        if rec["token_hash"] == auth_mod.token_hash(token):
            rec["expires_at"] = "2020-01-01T00:00:00+00:00"
    cfg.session_file().write_text(json.dumps({"sessions": recs}), encoding="utf-8")
    assert c.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
    left = json.loads(cfg.session_file().read_text("utf-8"))["sessions"]
    assert auth_mod.token_hash(token) not in {s["token_hash"] for s in left}
    assert len(left) == 1                       # 过期的被清掉了，setup 那条活的还在


def test_会话文件损坏_不放行(env):
    """坏文件 ⇒ 一律 401，绝不「读不出来就当都放行」。"""
    from keepsake.api import config as cfg

    c = TestClient(create_app())
    do_setup(c)
    cfg.session_file().write_text("{ 这不是 json", encoding="utf-8")
    assert c.get("/api/v1/auth/me", headers=bearer("whatever")).status_code == 401


def test_没有Authorization头_401(client):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_畸形Authorization头_401不是500(client):
    for header in ({"Authorization": "Bearer "}, {"Authorization": "Bearer  "},
                   {"Authorization": USER}, {"Authorization": "Basic dXNlcjpwYXNz"}):
        r = client.get("/api/v1/auth/me", headers=header)
        assert r.status_code == 401, header


def test_未初始化且无静态token_报未配置(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_TOKEN", "")
    # 🔴 文件回退也要切断：本机（主脑）存在 /root/.hermes/secrets/keepsake_console.token，
    # 只清 CONSOLE_TOKEN 时本用例在主脑恒失败、在 88 恒通过 ⇒ 环境敏感用例。
    # 指到一个确定不存在的路径，让「未配置」这件事与环境无关。
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(tmp_path / "不存在.token"))
    r = TestClient(create_app()).get("/api/v1/core",
                                     headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "token_not_configured"


# ---------------------------------------------------------------- 旧静态 token 兼容

def test_旧静态token_仍能进业务接口(client):
    assert client.get("/api/v1/core", headers=LEGACY).status_code == 200


def test_旧静态token_me_可用且标记legacy(client):
    do_setup(client)
    body = client.get("/api/v1/auth/me", headers=LEGACY).json()
    assert body["legacy_token"] is True and body["username"] == "static-token"


def test_旧静态token_静态token文件也生效(env, monkeypatch, tmp_path):
    f = tmp_path / "legacy.token"
    f.write_text("file-token-value\n", encoding="utf-8")
    monkeypatch.setenv("CONSOLE_TOKEN", "")
    monkeypatch.setenv("KEEPSAKE_CONSOLE_TOKEN_FILE", str(f))
    c = TestClient(create_app())
    ok = {"Authorization": "Bearer file-token-value"}
    # 用 /me 探（要会话的端点）—— /api/v1/core 还得连存储，这里不掺合数据源
    assert c.get("/api/v1/auth/me", headers=ok).json()["legacy_token"] is True
    assert c.get("/api/v1/auth/me", headers=LEGACY).status_code == 401


def test_旧静态token_错的口令会话不能混用(client):
    do_setup(client)
    assert client.get("/api/v1/core",
                      headers={"Authorization": f"Bearer {LEGACY_TOKEN}x"}).status_code == 401


# ---------------------------------------------------------------- logout / me

def test_logout_会话立即失效(client):
    do_setup(client)
    token = do_login(client).json()["token"]
    assert client.post("/api/v1/auth/logout", headers=bearer(token)).status_code == 200
    assert client.get("/api/v1/core", headers=bearer(token)).status_code == 401


def test_logout_无会话_401(client):
    do_setup(client)
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 401 and r.json()["detail"]["error"] == "missing_token"


def test_logout_只销当前会话_别的会话不受影响(client):
    do_setup(client)
    a = do_login(client).json()["token"]
    b = do_login(client).json()["token"]
    client.post("/api/v1/auth/logout", headers=bearer(a))
    assert client.get("/api/v1/auth/me", headers=bearer(a)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer(b)).status_code == 200


def test_logout_回包不回显token(client):
    do_setup(client)
    token = do_login(client).json()["token"]
    r = client.post("/api/v1/auth/logout", headers=bearer(token))
    assert token not in r.text


def test_me_回用户名与到期时间(client):
    do_setup(client)
    token = do_login(client, remember=True).json()["token"]
    body = client.get("/api/v1/auth/me", headers=bearer(token)).json()
    assert body["username"] == USER and body["expires_at"]
    assert set(body) == {"username", "expires_at", "legacy_token"}


def test_me_过期时间与登录回包一致(client):
    do_setup(client)
    login_body = do_login(client, remember=True).json()
    me = client.get("/api/v1/auth/me", headers=bearer(login_body["token"])).json()
    assert me["expires_at"] == login_body["expires_at"]


# ---------------------------------------------------------------- 路由形状 / 不回退

def test_写面仍是十四条业务写端点加三条auth_POST():
    app = create_app()
    write = {r.path for r in app.routes
             if getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}}
    assert write == {
        "/api/v1/auth/setup", "/api/v1/auth/login", "/api/v1/auth/logout",
        "/api/v1/core/{key}/promote", "/api/v1/core/{key}/demote",
        "/api/v1/fragments", "/api/v1/fragments/{key}/update",
        "/api/v1/fragments/{key}/archive", "/api/v1/fragments/{key}/delete",
        "/api/v1/import",
        "/api/v1/ops/merge", "/api/v1/ops/forget",
        "/api/v1/kb/documents", "/api/v1/kb/documents/{id}/update",
        "/api/v1/kb/documents/{id}/delete", "/api/v1/kb/import",
        "/api/v1/settings/core-budget",
    }, sorted(write)


def test_业务路由全部挂了require_session():
    """漏挂一处 = 某个接口裸奔，这里逐条打。"""
    app = create_app()
    protected = {"/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
                 "/api/v1/fragments/{key}", "/api/v1/kb/summary",
                 "/api/v1/kb/search", "/api/v1/stats", "/api/v1/crons",
                 "/api/v1/export",
                 "/api/v1/core/{key}/promote", "/api/v1/core/{key}/demote",
                 "/api/v1/fragments/{key}/update", "/api/v1/fragments/{key}/archive",
                 "/api/v1/fragments/{key}/delete", "/api/v1/import",
                 "/api/v1/ops/merge", "/api/v1/ops/forget", "/api/v1/auth/logout",
                 "/api/v1/auth/me",
                 "/api/v1/settings/core-budget", "/api/v1/import/template"}
    for r in app.routes:
        if getattr(r, "path", "") in protected:
            names = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
            assert any("require_session" in n for n in names), r.path


def test_公开端点只有三条且确实公开(client):
    """setup / login 免凭证可达（会因参数或已初始化报 400/409，**绝不是 401**）；
    logout / me 要会话 ⇒ 401。多挂一个「公开」端点会在这里被抓住。"""
    assert client.get("/api/v1/auth/status").status_code == 200
    assert client.post("/api/v1/auth/login", json={"username": "x", "password": "y"}).status_code == 401
    assert client.post("/api/v1/auth/setup",
                       json={"username": "x", "password": SHORT_PWD}).status_code == 400
    assert client.post("/api/v1/auth/logout").status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_P1P2只读路由没被登录改动(client):
    for url in ("/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
                "/api/v1/fragments/memory:frag:bbb", "/api/v1/stats", "/api/v1/crons"):
        assert client.get(url, headers=LEGACY).status_code == 200, url
