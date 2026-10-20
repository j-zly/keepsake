"""代理注册表控制台端点测试（`keepsake_agent_registry` 第 1 张单）。

覆盖：读面形状 / 一次性凭据响应 / revoke 与 role 的确认串口径 / exclude 开关 /
未认证 401 / 未知代理 404 / 审计留痕（含被拒绝的写）/ 写面枚举 /
手工登记（pending / 重复 409 / 立即批准发一次性凭据）、改名（只动 name）
与改 id（换主键 + 归属级联 + 凭据不换用 / 唯一性 409 / primary 拒改 400）。

存储用 `dependency_overrides` 注入**临时 SQLite**（真 `ks_agent` 表，走
`ensure_index` 建表那一路）—— 不连 Redis / PG / 生产库，也不碰主脑的 config。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.govern.registry import CREDENTIAL_PREFIX, Registry
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 七条写端点（写面枚举的另一半在 tests/api/test_api_readonly.py）
WRITE_PATHS = (
    "/api/v1/agents/{agent_id}/approve",
    "/api/v1/agents/{agent_id}/revoke",
    "/api/v1/agents/{agent_id}/role",
    "/api/v1/agents/{agent_id}/exclude",
    "/api/v1/agents",
    "/api/v1/agents/{agent_id}/rename",
    "/api/v1/agents/{agent_id}/set_id",
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """控制台进程环境：临时 sqlite 存储 + 审计/备份目录 + 固定 token。"""
    from keepsake.core.sqlite import SqliteStorage

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
        yield client, Registry(storage)
    app.dependency_overrides.clear()
    storage.close()


def _seed(reg: Registry, *agent_ids: str) -> None:
    for aid in agent_ids:
        reg.register(aid, aid)


def _audit_lines(tmp_path) -> List[Dict[str, Any]]:
    import pathlib

    files = sorted(pathlib.Path(tmp_path / "audit").glob("audit-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 读面

def test_无token_401(env):
    client, _ = env
    assert client.get("/api/v1/agents").status_code == 401


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_写端点无token_逐条401(env, path):
    client, _ = env
    assert client.post(path.format(agent_id="a"), json={"confirm": True}).status_code == 401


def test_列表形状与pending计数(env):
    client, reg = env
    _seed(reg, "main", "worker-1")
    body = client.get("/api/v1/agents", headers=AUTH).json()
    assert body["count"] == 2 and body["pending_count"] == 2
    item = body["items"][0]
    # 🆕 多了 dm_enabled（私信开关）：控制台的「私信开关」读的就是这个字段
    assert set(item) == {"agent_id", "name", "role", "status", "first_seen_at",
                         "last_seen_at", "reads", "writes", "exclude_from_primary",
                         "credential_hint", "meta", "dm_enabled"}
    assert item["role"] == "sub" and item["status"] == "pending"
    # 🔴 读面永远不回显凭据哈希 / 明文
    assert "credential_hash" not in json.dumps(body)


def test_空注册表返回空列表不是报错(env):
    client, _ = env
    body = client.get("/api/v1/agents", headers=AUTH).json()
    assert body["items"] == [] and body["pending_count"] == 0


# ---------------------------------------------------------------- 批准

def test_approve返回一次性凭据(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/approve", headers=AUTH, json={"confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "approved"
    assert body["credential"].startswith(CREDENTIAL_PREFIX)
    assert body["credential_hint"] == body["credential"][:8]
    assert "只此一次" in body["note"]
    # 注册表里那条凭据真的能用
    assert reg.verify("worker-1", body["credential"])["ok"] is True
    assert reg.get("worker-1")["status"] == "approved"


def test_approve两次换凭据旧的即失效(env):
    client, reg = env
    _seed(reg, "worker-1")
    first = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                        json={"confirm": True}).json()["credential"]
    second = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                         json={"confirm": True}).json()["credential"]
    assert first != second
    assert reg.verify("worker-1", first)["ok"] is False
    assert reg.verify("worker-1", second)["ok"] is True


def test_approve不带confirm_400且不改状态(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/approve", headers=AUTH, json={})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert reg.get("worker-1")["status"] == "pending"


def test_未知代理_404(env):
    client, _ = env
    r = client.post("/api/v1/agents/查无此代理/approve", headers=AUTH, json={"confirm": True})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "agent_not_found"


# ---------------------------------------------------------------- 吊销

def test_revoke确认串是agent前8位(env):
    """🔴 确认串 = agent_id **前 8 位**（不是整串、不是凭据提示位）——
    漏掉这个闸 = 顺手点一下就吊销掉一个正在服务的代理。"""
    client, reg = env
    _seed(reg, "worker-alpha-01")
    credential = reg.approve("worker-alpha-01")
    r = client.post("/api/v1/agents/worker-alpha-01/revoke", headers=AUTH,
                    json={"confirm": "worker-alpha-01"})      # 整串不行
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    r = client.post("/api/v1/agents/worker-alpha-01/revoke", headers=AUTH,
                    json={"confirm": "worker-alpha-01"[:8]})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert reg.verify("worker-alpha-01", credential)["ok"] is False


def test_revoke确认串不匹配_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "写错了"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"


# ---------------------------------------------------------------- 角色

def test_置primary回显旧主被降级(env):
    client, reg = env
    _seed(reg, "main", "worker-1")
    client.post("/api/v1/agents/main/role", headers=AUTH,
                json={"role": "primary", "confirm": "main"})
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    body = r.json()
    assert body["role"] == "primary" and body["demoted"] == ["main"]
    assert "旧主已降" in body["note"] or "降为 sub" in body["note"]
    roles = {a["agent_id"]: a["role"]
             for a in client.get("/api/v1/agents", headers=AUTH).json()["items"]}
    assert roles == {"main": "sub", "worker-1": "primary"}


def test_角色非法_400(env):
    client, reg = env
    _seed(reg, "main")
    r = client.post("/api/v1/agents/main/role", headers=AUTH,
                    json={"role": "boss", "confirm": "main"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_role"


def test_角色确认串必须是完整agent_id(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"


def test_置sub不留降级说明(env):
    client, reg = env
    _seed(reg, "main")
    reg.set_role("main", "primary")
    body = client.post("/api/v1/agents/main/role", headers=AUTH,
                       json={"role": "sub", "confirm": "main"}).json()
    assert body["role"] == "sub" and body["demoted"] == [] and body["note"] == ""


# ---------------------------------------------------------------- 上下文开关

def test_exclude开关(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                    json={"exclude": True, "confirm": True})
    assert r.status_code == 200 and r.json()["exclude_from_primary"] is True
    assert reg.get("worker-1")["exclude_from_primary"] is True
    # 再点一次同值 = 无变化（幂等，面板可据此知道「其实没改」）
    again = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                        json={"exclude": True, "confirm": True}).json()
    assert again["changed"] is False
    off = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH,
                      json={"exclude": False, "confirm": True}).json()
    assert off["changed"] is True


def test_exclude不带confirm_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/exclude", headers=AUTH, json={"exclude": True})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


# ---------------------------------------------------------------- 登记 / 改名

def test_登记成功_落一行pending(env):
    """登记 = 建一行 pending（**不批准就读写全拒**）：先有名字，再等批准。"""
    client, reg = env
    r = client.post("/api/v1/agents", headers=AUTH,
                    json={"agent_id": "worker-2", "name": "第二台机器", "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["agent_id"] == "worker-2"
    assert body["status"] == "pending"
    row = reg.get("worker-2")
    assert row["status"] == "pending" and row["name"] == "第二台机器"
    assert row["role"] == "sub" and row["credential_hint"] == ""
    # 列表里能看见它（前端登记完就是靠列表刷新看到的）
    ids = [a["agent_id"] for a in client.get("/api/v1/agents", headers=AUTH).json()["items"]]
    assert "worker-2" in ids


def test_登记非法agent_id_400且不建行(env):
    """🔴 agent_id 形态是硬闸：它进审计目录文件名与 Redis key，放开就是开口子。"""
    client, reg = env
    for bad in ["Bad_ID", "-lead", "a", "有中文", "w" * 41, "with space"]:
        r = client.post("/api/v1/agents", headers=AUTH,
                        json={"agent_id": bad, "confirm": True})
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"] == "bad_agent_id", bad
        assert reg.get(bad) is None
    # 合规的就该过（2 位与 40 位都合法）
    assert client.post("/api/v1/agents", headers=AUTH,
                        json={"agent_id": "ab", "confirm": True}).status_code == 200
    assert client.post("/api/v1/agents", headers=AUTH,
                        json={"agent_id": "w" * 40, "confirm": True}).status_code == 200


def test_登记重复_409且不覆盖已有行(env):
    """🔴 已存在（任何状态，含 revoked）⇒ 409，绝不静默覆盖。

    覆盖会把管理员的吊销决定洗白（改了名看着像新代理，实际还是那条吊销的行）。
    """
    client, reg = env
    _seed(reg, "worker-1")
    reg.approve("worker-1")
    reg.set_role("worker-1", "primary")
    r = client.post("/api/v1/agents", headers=AUTH,
                    json={"agent_id": "worker-1", "name": "顶替者", "confirm": True})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "agent_exists"
    row = reg.get("worker-1")
    assert row["name"] == "worker-1" and row["status"] == "approved"
    assert row["role"] == "primary" and row["credential_hint"] != ""


def test_登记已吊销的代理_同样409(env):
    """revoked 也算「已存在」—— 吊销是「停到人重新批准为止」，不是「注销」。"""
    client, reg = env
    _seed(reg, "worker-1")
    reg.approve("worker-1")
    reg.revoke("worker-1")
    r = client.post("/api/v1/agents", headers=AUTH,
                    json={"agent_id": "worker-1", "confirm": True})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "agent_exists"
    assert reg.get("worker-1")["status"] == "revoked"


def test_登记不带confirm_400且不建行(env):
    client, reg = env
    r = client.post("/api/v1/agents", headers=AUTH, json={"agent_id": "worker-2"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert reg.get("worker-2") is None


def test_登记并立即批准_一次性凭据且二次调用换发(env):
    """`approve_now=true` ⇒ 登记 + 批准一次做完，明文只在这条响应里出现一次。"""
    client, reg = env
    r = client.post("/api/v1/agents", headers=AUTH,
                    json={"agent_id": "worker-2", "name": "急用", "confirm": True,
                          "approve_now": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["credential"].startswith(CREDENTIAL_PREFIX)
    assert body["credential_hint"] == body["credential"][:8]
    assert "只此一次" in body["note"]
    assert reg.verify("worker-2", body["credential"])["ok"] is True
    # 二次调用 = 再登记一次同名 ⇒ 409；重新发凭据走 approve（换一条新的）
    again = client.post("/api/v1/agents", headers=AUTH,
                        json={"agent_id": "worker-2", "confirm": True, "approve_now": True})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "agent_exists"
    reissued = client.post("/api/v1/agents/worker-2/approve", headers=AUTH,
                            json={"confirm": True}).json()["credential"]
    assert reissued != body["credential"]
    assert reg.verify("worker-2", body["credential"])["ok"] is False
    assert reg.verify("worker-2", reissued)["ok"] is True


def test_改名只动name_状态与凭据hint不变(env):
    """🔴 改名是给人看的标签：status / role / 凭据 / meta 一律不碰。"""
    client, reg = env
    _seed(reg, "worker-1")
    credential = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                             json={"confirm": True}).json()["credential"]
    before = reg.get("worker-1")
    r = client.post("/api/v1/agents/worker-1/rename", headers=AUTH,
                    json={"name": "打包机一号", "confirm": True})
    assert r.status_code == 200 and r.json()["name"] == "打包机一号"
    after = reg.get("worker-1")
    assert after["name"] == "打包机一号"
    assert after["status"] == before["status"] == "approved"
    assert after["role"] == before["role"]
    assert after["credential_hint"] == before["credential_hint"]
    assert after["meta"] == before["meta"]
    # 凭据没被作废（改个名不该把正在服务的代理踢下线）
    assert reg.verify("worker-1", credential)["ok"] is True


def test_改名未知代理_404(env):
    client, _ = env
    r = client.post("/api/v1/agents/查无此代理/rename", headers=AUTH,
                    json={"name": "新名字", "confirm": True})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "agent_not_found"


def test_改名不带confirm_400且不改名(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/rename", headers=AUTH, json={"name": "新名字"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"
    assert reg.get("worker-1")["name"] == "worker-1"


def test_改名留空名_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/rename", headers=AUTH,
                    json={"name": "   ", "confirm": True})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_name"
    assert reg.get("worker-1")["name"] == "worker-1"


def test_登记与改名都留审计且不含凭据明文(env, tmp_path):
    """🔴 凭据纪律：登记 + 立即批准这一路，审计里也不许出现明文。"""
    client, reg = env
    credential = client.post("/api/v1/agents", headers=AUTH,
                             json={"agent_id": "worker-2", "confirm": True,
                                   "approve_now": True}).json()["credential"]
    client.post("/api/v1/agents/worker-2/rename", headers=AUTH,
                json={"name": "改名了", "confirm": True})
    client.post("/api/v1/agents", headers=AUTH, json={"agent_id": "worker-2"})
    lines = _audit_lines(tmp_path)
    actions = {(l["action"], l["ok"]) for l in lines}
    assert ("agents.register", True) in actions
    assert ("agents.rename", True) in actions
    assert ("agents.register", False) in actions        # 被拒绝的写也要留痕
    assert credential not in json.dumps(lines, ensure_ascii=False)

# ------------------------------------------------- 现役 primary 的三道闸（409）

def _primary(reg: Registry, agent_id: str = "main") -> str:
    """造一个**现役** primary：已批准 + 已上位（= 真正在放行读写的那一位）。"""
    reg.register(agent_id, agent_id)
    reg.approve(agent_id)
    reg.set_role(agent_id, "primary")
    return agent_id


@pytest.mark.parametrize("path,body", [
    ("revoke", {"confirm": "main"}),
    ("role", {"role": "sub", "confirm": "main"}),
    ("exclude", {"confirm": True, "exclude": True}),
])
def test_动现役primary_409(env, path, body):
    """🔴 现役 primary 的 revoke / role / exclude 一律 409。

    前端不给那三个钮只是「不让人看见」；绕开 UI 直接打 API 一样能点进来，
    而吊销/降级主脑 = 写闸 fail-closed 把主脑降成 sub ⇒ 常驻写入全降级、
    候选审批链瘫痪。**必须先设别人为主**（那条路径会自动把旧主降级）。
    """
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    r = client.post(f"/api/v1/agents/main/{path}", headers=AUTH, json=body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "primary_protected"
    row = reg.get("main")
    assert row["status"] == "approved" and row["role"] == "primary"


def test_先设别人为主再动旧主_路径通(env):
    """409 拦的是「直接动它」，不是「永远不能动它」：换完主之后旧主就是普通 sub。"""
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    # 换主：把 worker-1 设为 primary（旧主由 registry 自动降级，不走被拦的那条路）
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    assert r.status_code == 200 and r.json()["demoted"] == ["main"]
    # 现在 main 已是普通 sub，三条路全通
    assert client.post("/api/v1/agents/main/exclude", headers=AUTH,
                       json={"confirm": True, "exclude": True}).status_code == 200


def test_被拦的写也要留审计(env, tmp_path):
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    client.post("/api/v1/agents/main/revoke", headers=AUTH, json={"confirm": "main"})
    actions = {(l["action"], l["ok"]) for l in _audit_lines(tmp_path)}
    assert ("agents.revoke", False) in actions


# ---------------------------------------------------------------- 搜索 / 分页

def test_q按agent_id与name大小写不敏感过滤(env):
    client, reg = env
    reg.register("Main-Brain", "主脑进程")
    reg.register("worker-Alpha", "Alpha 子代理")
    reg.register("worker-beta", "Beta 子代理")
    body = client.get("/api/v1/agents?q=ALPHA", headers=AUTH).json()
    assert [a["agent_id"] for a in body["items"]] == ["worker-Alpha"]
    assert body["count"] == 1
    # name 侧同样命中（子串、忽略大小写）
    body = client.get("/api/v1/agents?q=主脑", headers=AUTH).json()
    assert [a["agent_id"] for a in body["items"]] == ["Main-Brain"]
    body = client.get("/api/v1/agents?q=worker", headers=AUTH).json()
    assert body["count"] == 2


def test_q为空即全量且count是过滤后总数(env):
    client, reg = env
    _seed(reg, "worker-1", "worker-2", "worker-3")
    assert client.get("/api/v1/agents", headers=AUTH).json()["count"] == 3
    assert client.get("/api/v1/agents?q=", headers=AUTH).json()["count"] == 3
    assert client.get("/api/v1/agents?q=worker-2", headers=AUTH).json()["count"] == 1


def test_服务端分页_items是切片count是全量(env):
    """🔴 分页只切 items，**count 恒是过滤后的总数**（PagerBar 靠它算页数）。"""
    client, reg = env
    _seed(reg, *[f"worker-{i:02d}" for i in range(25)])
    p1 = client.get("/api/v1/agents?page=1&page_size=10", headers=AUTH).json()
    assert p1["count"] == 25 and len(p1["items"]) == 10
    p3 = client.get("/api/v1/agents?page=3&page_size=10", headers=AUTH).json()
    assert len(p3["items"]) == 5
    assert p1["items"][0]["agent_id"] == "worker-00"
    assert p3["items"][-1]["agent_id"] == "worker-24"


def test_分页越界返回空items不是报错(env):
    client, reg = env
    _seed(reg, "worker-1")
    body = client.get("/api/v1/agents?page=99&page_size=20", headers=AUTH).json()
    assert body["items"] == [] and body["count"] == 1


def test_pending_count是全量口径不受q影响(env):
    """🔴 红线：红条上的待审数必须永远等于**全库**待审数，不能被搜索词吞掉。

    被 q 过滤掉的待审代理此刻正在闸门外被拒 —— 若它从红条上消失，
    面板上唯一那个「有人正在被拒」的信号就被搜索框关掉了。
    """
    client, reg = env
    _seed(reg, "worker-1", "worker-2")
    reg.approve("worker-2")                      # 只剩 worker-1 是 pending
    body = client.get("/api/v1/agents?q=worker-2", headers=AUTH).json()
    assert body["count"] == 1 and len(body["items"]) == 1
    assert body["pending_count"] == 1, "q 命中的是已批准的那条，待审数仍应是全库的 1"


def test_page_size上限100(env):
    client, _ = env
    assert client.get("/api/v1/agents?page_size=101", headers=AUTH).status_code == 422
    assert client.get("/api/v1/agents?page_size=100", headers=AUTH).status_code == 200


# ---------------------------------------------------------------- 审计

def test_审计留痕且不含凭据明文(env, tmp_path):
    client, reg = env
    _seed(reg, "worker-1")
    credential = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                             json={"confirm": True}).json()["credential"]
    client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "worker-1"})
    client.post("/api/v1/agents/worker-1/revoke", headers=AUTH, json={"confirm": "写错了"})
    lines = _audit_lines(tmp_path)
    actions = {(l["action"], l["ok"]) for l in lines}
    assert ("agents.approve", True) in actions
    assert ("agents.revoke", True) in actions
    assert ("agents.revoke", False) in actions          # 被拒绝的写也要留痕
    assert credential not in json.dumps(lines, ensure_ascii=False)


def test_写前备份落盘(env, tmp_path):
    """改角色会连带降级别人 ⇒ 改前必须有一份注册表快照可人工复原。"""
    client, reg = env
    _seed(reg, "main", "worker-1")
    r = client.post("/api/v1/agents/worker-1/role", headers=AUTH,
                    json={"role": "primary", "confirm": "worker-1"})
    backup = r.json()["backup"]
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert {a["agent_id"] for a in saved} == {"main", "worker-1"}


# ---------------------------------------------------------------- 写面枚举

def test_写面枚举_七条agents写端点都在(env):
    """与 tests/api/test_api_readonly.py 的白名单互为反面：少挂一条依赖就红。

    （代理治理原有四条 approve / revoke / role / exclude，登记与改名两条，
    本次再加改 id 一条 —— 换主键 + 记忆归属级联。）
    """
    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    assert set(WRITE_PATHS) <= paths
    for p in WRITE_PATHS:
        # 🔴 按 **方法** 挑路由：`/api/v1/agents` 同时挂着 GET（列表）与 POST（登记），
        # 只按 path 挑会挑到先注册的那条 GET，判据就落在错误的路由上了。
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == p
                     and "POST" in getattr(r, "methods", set()))
        names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        assert any("require_session" in n for n in names), p
        assert any("write_guard" in n for n in names), p

# ---------------------------------------------------------------- 改 id（换主键 + 归属级联）

def _storage(client) -> Any:
    """从 dependency_overrides 拿回存储实例（用例要直接种记忆）。"""
    return client.app.dependency_overrides[get_storage]()


def _hits(storage: Any, query: str, tag: str = "") -> List[str]:
    """BM25 召回的 key 列表（`is_primary=True` = 主代理视角，不带 agent 隔离）。"""
    return [f["_key"] for f in storage.search_bm25(query, tag_filter=tag, is_primary=True)]


def test_set_id成功_凭据不换用且反查是新id(env):
    """🔴 改 id **不作废凭据**：客户端身份由凭据反查（`verify_any`），凭据不动 ⇒
    已发出去的凭据继续有效，而且反查出来的必须是**新 id**（否则客户端拿到旧身份）。"""
    client, reg = env
    _seed(reg, "worker-1")
    credential = client.post("/api/v1/agents/worker-1/approve", headers=AUTH,
                             json={"confirm": True}).json()["credential"]
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": "worker-2", "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["agent_id"] == "worker-2"
    assert body["moved_memories"] == 0
    # 旧 id 没了、新 id 在（整行搬过去：状态 / 角色 / 计数都还在）
    assert reg.get("worker-1") is None
    after = reg.get("worker-2")
    assert after["status"] == "approved" and after["role"] == "sub"
    # 凭据不换：旧凭据还能用，且 verify_any 反查出来的是新 id
    res = reg.verify_any(credential)
    assert res["ok"] is True and res["agent_id"] == "worker-2"
    assert reg.verify("worker-2", credential)["ok"] is True
    assert reg.verify("worker-1", credential)["ok"] is False
    # 列表里新 id 出现、旧 id 消失
    ids = [a["agent_id"] for a in client.get("/api/v1/agents", headers=AUTH).json()["items"]]
    assert "worker-2" in ids and "worker-1" not in ids


def test_set_id新id已存在_409且不覆盖(env):
    """🔴 唯一性：新 id 已被占用 ⇒ 409，**绝不覆盖**（覆盖会洗白别人的行）。"""
    client, reg = env
    _seed(reg, "worker-1", "worker-2")
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": "worker-2", "confirm": True})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "agent_exists"
    # 人话文案要点名冲突 + 给下一步（唯一性反馈要看得见）
    assert "worker-2" in detail["message"]
    # 两个行都还在，谁也没被动
    assert reg.get("worker-1") is not None and reg.get("worker-2") is not None


def test_set_id现役primary拒改_400(env):
    """🔴 primary 的 id 不许改：主脑的引导配置绑在这个 id 上，改了等于换了个主代理。"""
    client, reg = env
    _seed(reg, "worker-1")
    _primary(reg)
    r = client.post("/api/v1/agents/main/set_id", headers=AUTH,
                    json={"new_id": "main-2", "confirm": True})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "primary_id_locked"
    assert "主代理" in r.json()["detail"]["message"]
    assert reg.get("main") is not None and reg.get("main-2") is None


@pytest.mark.parametrize("bad", ["Bad_ID", "-lead", "a", "有中文", "w" * 41, "with space"])
def test_set_id非法新id_400(env, bad):
    """🔴 新 id 与登记同一套形态闸（它会进审计目录文件名与 Redis key）。"""
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": bad, "confirm": True})
    assert r.status_code == 400, bad
    assert r.json()["detail"]["error"] == "bad_agent_id", bad
    assert reg.get("worker-1") is not None


def test_set_id不带confirm_400且不改(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH, json={"new_id": "worker-2"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"
    assert reg.get("worker-1") is not None and reg.get("worker-2") is None


def test_set_id未知代理_404(env):
    client, _ = env
    r = client.post("/api/v1/agents/查无此代理/set_id", headers=AUTH,
                    json={"new_id": "worker-9", "confirm": True})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "agent_not_found"


def test_set_id新旧同串_400(env):
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": "worker-1", "confirm": True})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "same_agent_id"
    assert reg.get("worker-1") is not None


def test_set_id级联搬归属与索引(env):
    """🔴 归属级联 + **索引必须跟着重算**（本仓库已知坑）。

    种一条 `tags=agent:old-agent` 的记忆 → 改 id 后：
      ① 归属标签换成 `agent:new-agent`；
      ② 按**新 tag** 召得回、按**旧 tag** 召不回；
      ③ 🔴 按**旧 id 的词**召不回、按新 id 的词召得回 ——
         这条钉的是 content_tsv 重算：只改 tags 不重算索引，库里写着新归属、
         BM25 还按旧标签召回（不报错、不空召回，只是召回旧归属）。
    """
    client, reg = env
    storage = _storage(client)
    _seed(reg, "old-agent")
    storage.store("部署流程要点：证书续签", tags="agent:old-agent", category="ops")
    key = storage.scan_fragment_keys()[1][0]
    assert storage.get_fragment(key)["tags"] == "agent:old-agent"
    assert _hits(storage, "old-agent", "agent:old-agent") == [key]

    r = client.post("/api/v1/agents/old-agent/set_id", headers=AUTH,
                    json={"new_id": "new-agent", "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["moved_memories"] == 1
    assert storage.get_fragment(key)["tags"] == "agent:new-agent"
    # 归属召回：新 tag 命中、旧 tag 落空
    assert _hits(storage, "new-agent", "agent:new-agent") == [key]
    assert _hits(storage, "new-agent", "agent:old-agent") == []
    # 🔴 索引重算：旧词召不回、新词召得回
    assert _hits(storage, "old") == []
    assert _hits(storage, "new") == [key]


def test_set_id级联_不误伤别的代理(env):
    """🔴 `agent:win` 与 `agent:win-desktop` 是两个代理：改前者不许动后者。"""
    client, reg = env
    storage = _storage(client)
    _seed(reg, "win", "win-desktop")
    storage.store("打包机甲的部署要点", tags="agent:win", category="ops")
    storage.store("打包机乙的部署要点", tags="agent:win-desktop", category="ops")
    keys = storage.scan_fragment_keys()[1]
    r = client.post("/api/v1/agents/win/set_id", headers=AUTH,
                    json={"new_id": "win-new", "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["moved_memories"] == 1
    tags = sorted(storage.get_fragment(k)["tags"] for k in keys)
    assert tags == ["agent:win-desktop", "agent:win-new"]


def test_set_id级联_历史行consumed也搬(env):
    """🔴 consumed / superseded 的历史行**一起搬**（归属是事实，不是活跃状态）。"""
    client, reg = env
    storage = _storage(client)
    _seed(reg, "old-agent")
    storage.store("一条已被合并吞掉的记忆", tags="agent:old-agent",
                  fragment_type="consumed")
    key = storage.scan_fragment_keys()[1][0]
    r = client.post("/api/v1/agents/old-agent/set_id", headers=AUTH,
                    json={"new_id": "new-agent", "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["moved_memories"] == 1
    frag = storage.get_fragment(key)
    assert frag["tags"] == "agent:new-agent"
    assert frag["fragment_type"] == "consumed"      # 历史属性不动，只搬归属


def test_set_id留审计且写前有备份(env, tmp_path):
    """改 id 不可逆到「回滚靠记忆」 ⇒ 改前必须有整份注册表快照 + 审计一行。"""
    client, reg = env
    _seed(reg, "worker-1")
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": "worker-2", "confirm": True})
    assert r.status_code == 200
    backup = r.json()["backup"]
    saved = json.loads(open(backup, encoding="utf-8").read())
    assert {a["agent_id"] for a in saved} == {"worker-1"}
    lines = _audit_lines(tmp_path)
    assert ("agents.set_id", True) in {(l["action"], l["ok"]) for l in lines}


def test_改名同名_回包点名但不拦(env):
    """备注名只是给人看的标签：撞名**不拦**（不改治理语义），但回包要点名。"""
    client, reg = env
    _seed(reg, "worker-1", "worker-2")
    client.post("/api/v1/agents/worker-1/rename", headers=AUTH,
                json={"name": "打包机一号", "confirm": True})
    body = client.post("/api/v1/agents/worker-2/rename", headers=AUTH,
                       json={"name": "打包机一号", "confirm": True}).json()
    assert body["name"] == "打包机一号"
    assert "worker-1" in body["note"]          # 点名是谁也占了这个名字
    assert reg.get("worker-2")["name"] == "打包机一号"


def test_set_id_级联失败仍报改成功但说清楚(env, monkeypatch):
    """🔴 换 id 已生效（主键已换、不可回滚）⇒ 级联失败**不能报 500**。

    报 500 会让操作者以为没改、于是重试（重试只会拿到「代理不存在」，更迷惑）。
    正确姿态：换 id 成功就 200，把没搬动的那部分**说清楚**。
    """
    client, reg = env
    storage = _storage(client)
    _seed(reg, "worker-1")

    def _boom(old_id, new_id):
        raise RuntimeError("存储层炸了")

    monkeypatch.setattr(storage, "reassign_agent", _boom)
    r = client.post("/api/v1/agents/worker-1/set_id", headers=AUTH,
                    json={"new_id": "worker-2", "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["agent_id"] == "worker-2"
    assert body["moved_memories"] == 0
    assert "归属" in body["note"]              # 说清楚哪一步没成
    assert reg.get("worker-2") is not None     # 换 id 确实生效了
