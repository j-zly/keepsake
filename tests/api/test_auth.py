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
from pathlib import Path
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


# ---------------------------------------------------------------- change_password

#: 新口令；同样是测试夹具，不是凭据
NEW_PWD = "brand new secret"


@pytest.fixture()
def audit_dirs(tmp_path, monkeypatch):
    """审计 / 备份目录指到 tmp —— 否则这几例会去写 /root/.hermes/backup/。

    两条都指（审计与备份），别只清一条。
    """
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "bak"))


@pytest.fixture()
def cp(client, audit_dirs):
    """改密用例的公共起点：**已初始化 + 一个已登录会话**。

    单独一个 fixture 而不是每例自己 `do_setup`：漏掉 setup 的改密用例会安静地
    401（签不出会话），看着像「改密功能坏了」，实则是用例自己没搭好现场。
    返回 `(client, 当前会话 token)`。
    """
    do_setup(client)
    return client, do_login(client).json()["token"]


def do_change(c, token: str, old: str = PWD, new: str = NEW_PWD, confirm: bool = True):
    return c.post("/api/v1/auth/change_password",
                  json={"old_password": old, "new_password": new, "confirm": confirm},
                  headers=bearer(token))


def audit_lines(tmp_path) -> list:
    """把当日审计文件读成 dict 列表（按天分区，所以直接找唯一的那个文件）。"""
    files = sorted((tmp_path / "audit").glob("audit-*.jsonl"))
    assert files, "审计文件没落盘"
    return [json.loads(l) for l in files[-1].read_text("utf-8").splitlines() if l.strip()]


def test_改口令_正确旧口令_改成功且当前会话仍活(cp):
    c, token = cp
    r = do_change(c, token)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["username"] == USER
    # 🔴 改完密不能把自己踢回登录页（用户还得靠当前会话继续用）
    assert c.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200


def test_改口令_其余会话全部作废(cp):
    c, keep = cp
    others = [do_login(c).json()["token"] for _ in range(3)]
    r = do_change(c, keep)
    # 4 = setup 自动登录那条 + 上面 3 条（setup 那条也是别人的会话，一样得死）
    assert r.json()["revoked_sessions"] == 4
    for t in others:
        assert c.get("/api/v1/auth/me", headers=bearer(t)).status_code == 401
    assert c.get("/api/v1/auth/me", headers=bearer(keep)).status_code == 200


def test_改口令_会话文件里只剩当前那条(cp):
    from keepsake.api import config as cfg

    c, keep = cp
    do_login(c)
    do_change(c, keep)
    left = json.loads(cfg.session_file().read_text("utf-8"))["sessions"]
    assert [s["token_hash"] for s in left] == [auth_mod.token_hash(keep)]


def test_改口令_凭据文件权限仍600(cp):
    from keepsake.api import config as cfg

    c, token = cp
    do_change(c, token)
    mode = stat.S_IMODE(os.stat(cfg.admin_file()).st_mode)
    assert mode == 0o600, oct(mode)


def test_改口令_写前备份存在且是旧记录(cp):
    from keepsake.api import config as cfg

    c, token = cp
    before = json.loads(cfg.admin_file().read_text("utf-8"))
    body = do_change(c, token).json()
    saved = json.loads(Path(body["backup"]).read_text("utf-8"))
    # 备份里是**改前**那份（salt/hash 还是旧的），人工回退就靠它
    assert saved["hash"] == before["hash"] and saved["salt"] == before["salt"]
    assert saved["username"] == USER


def test_改口令_审计留一行且不含口令明文(cp, tmp_path):
    c, token = cp
    do_change(c, token)
    lines = [x for x in audit_lines(tmp_path) if x["action"] == "auth.change_password"]
    assert len(lines) == 1
    assert lines[0]["ok"] is True and lines[0]["target"] == USER
    assert "revoked_others=" in lines[0]["reason"]
    # 🔴 明文口令不许出现在审计里（新旧都算）
    blob = json.dumps(lines[0], ensure_ascii=False)
    assert PWD not in blob and NEW_PWD not in blob


def test_改口令_回包不回显口令(cp):
    c, token = cp
    r = do_change(c, token)
    assert PWD not in r.text and NEW_PWD not in r.text and token not in r.text


def test_改口令_磁盘上仍只有哈希(cp):
    from keepsake.api import config as cfg

    c, token = cp
    do_change(c, token)
    raw = cfg.admin_file().read_text("utf-8")
    assert NEW_PWD not in raw and PWD not in raw
    assert json.loads(raw)["username"] == USER      # username / created 不被改掉


def test_改口令_错旧口令_403且文件没动(cp):
    from keepsake.api import config as cfg

    c, token = cp
    before = cfg.admin_file().read_text("utf-8")
    r = do_change(c, token, old="wrong password")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "wrong_password"
    assert cfg.admin_file().read_text("utf-8") == before
    assert do_login(c, password=PWD).status_code == 200


def test_改口令_错旧口令的响应不回显口令(cp):
    c, token = cp
    r = do_change(c, token, old="wrong password")
    assert "wrong password" not in r.text and PWD not in r.text


def test_改口令_弱新口令_400且旧口令仍有效(cp):
    """新口令没过强度 ⇒ 连旧口令都不必校验（不消耗 PBKDF2），也不写盘。"""
    c, token = cp
    r = do_change(c, token, new=SHORT_PWD)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "password_too_short"
    assert do_login(c, password=PWD).status_code == 200


def test_改口令_缺confirm_400且不动数据(cp):
    c, token = cp
    r = do_change(c, token, confirm=False)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert do_login(c, password=PWD).status_code == 200


def test_改口令_限速_第6次错旧口令_429(cp):
    """与 login 共用同一个 (IP, 用户名) 桶 —— 爆破旧口令的天花板和爆破登录一样。"""
    c, token = cp
    codes = [do_change(c, token, old="wrong password").status_code for _ in range(5)]
    assert codes == [403] * 5
    assert do_change(c, token, old="wrong password").status_code == 429


def test_改口令_限速_被限速时正确旧口令也不放行(cp):
    c, token = cp
    for _ in range(5):
        do_change(c, token, old="wrong password")
    assert do_change(c, token).status_code == 429


def test_改口令_限速_改密成功清零后仍能再试(cp):
    c, token = cp
    for _ in range(4):
        do_change(c, token, old="wrong password")
    assert do_change(c, token).status_code == 200
    assert do_change(c, token, old="wrong password").status_code == 403


def test_改口令_改后新口令能登录旧口令不能(cp):
    c, token = cp
    do_change(c, token)
    assert do_login(c, password=NEW_PWD).status_code == 200
    assert do_login(c, password=PWD).status_code == 401


def test_改口令_无会话_401(client, audit_dirs):
    r = client.post("/api/v1/auth/change_password",
                    json={"old_password": PWD, "new_password": NEW_PWD, "confirm": True})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "missing_token"


def test_改口令_静态token改不了口令(cp):
    """🔴 脚本/MCP 通道那条静态 token 不具备换管理员口令的能力。"""
    c, _ = cp
    r = do_change(c, LEGACY_TOKEN)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "wrong_password"
    assert do_login(c, password=PWD).status_code == 200


def test_改口令_旧静态token改密后仍可用(cp):
    """改的是管理员口令，不是脚本令牌 —— 静态 token 不受影响。"""
    c, token = cp
    do_change(c, token)
    assert c.get("/api/v1/auth/me", headers=LEGACY).status_code == 200


def test_改口令_未初始化_签不出会话所以401(env, audit_dirs):
    """没有管理员 ⇒ 签不出会话 ⇒ 要会话的改密端点 401（而不是 500）。"""
    assert TestClient(create_app()).post(
        "/api/v1/auth/change_password",
        json={"old_password": PWD, "new_password": NEW_PWD, "confirm": True},
    ).status_code == 401


# ---------------------------------------------------------------- 路由形状 / 不回退

def test_写面仍是二十九条业务写端点加四条auth_POST():
    """写面清单不许悄悄长/短：多一个端点就是多一条能改数据的路（少一个 = 有人删了功能）。

    🆕 批2 审核队列加了 approve / reject 两条（23 → 25）。它们只在控制台会话下
    可用（不接代理身份），接线口径见 `routers/kb.py` 的 `kb_approve_document`。
    🆕 代理治理再加改 id 一条（28 → 29）：换主键 + 记忆归属级联。
    """
    app = create_app()
    write = {r.path for r in app.routes
             if getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}}
    assert write == {
        "/api/v1/auth/setup", "/api/v1/auth/login", "/api/v1/auth/logout",
        "/api/v1/auth/change_password",
        "/api/v1/core/{key}/promote", "/api/v1/core/{key}/demote",
        "/api/v1/fragments", "/api/v1/fragments/{key}/update",
        "/api/v1/fragments/{key}/archive", "/api/v1/fragments/{key}/delete",
        "/api/v1/import",
        "/api/v1/ops/merge", "/api/v1/ops/forget",
        "/api/v1/ops/bulk", "/api/v1/ops/bulk-export",
        "/api/v1/kb/documents", "/api/v1/kb/documents/{id}/update",
        "/api/v1/kb/documents/{id}/delete", "/api/v1/kb/import",
        "/api/v1/kb/documents/{id}/approve", "/api/v1/kb/documents/{id}/reject",
        "/api/v1/settings/core-budget", "/api/v1/settings/tiering",
        "/api/v1/crons/{job_id}/pause", "/api/v1/crons/{job_id}/resume",
        "/api/v1/crons/{job_id}/edit",
        "/api/v1/agents", "/api/v1/agents/{agent_id}/approve",
        "/api/v1/agents/{agent_id}/rename", "/api/v1/agents/{agent_id}/revoke",
        "/api/v1/agents/{agent_id}/role", "/api/v1/agents/{agent_id}/exclude",
        "/api/v1/agents/{agent_id}/set_id",
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
                 "/api/v1/settings/core-budget", "/api/v1/settings/tiering",
                 "/api/v1/tiering/status", "/api/v1/import/template",
                 "/api/v1/ops/bulk", "/api/v1/ops/bulk-export",
                 "/api/v1/auth/change_password",
                 "/api/v1/crons/{job_id}/pause", "/api/v1/crons/{job_id}/resume",
                 "/api/v1/crons/{job_id}/edit",
                 "/api/v1/agents/{agent_id}/approve", "/api/v1/agents/{agent_id}/revoke",
                 "/api/v1/agents/{agent_id}/role", "/api/v1/agents/{agent_id}/exclude",
                 "/api/v1/agents/{agent_id}/set_id"}
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
    assert client.post("/api/v1/auth/change_password",
                       json={"old_password": "x", "new_password": "12345678",
                             "confirm": True}).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_P1P2只读路由没被登录改动(client):
    for url in ("/api/v1/core", "/api/v1/core/stats", "/api/v1/fragments",
                "/api/v1/fragments/memory:frag:bbb", "/api/v1/stats", "/api/v1/crons"):
        assert client.get(url, headers=LEGACY).status_code == 200, url
