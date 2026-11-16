"""记忆班车管理端点测试（v2.2.0 方案 C：APScheduler 内核，契约不变）。

🔴 **绝不碰真实 PG / 真实班车**：调度器用 tmp_path 里的 sqlite jobstore；
BUS_JOBS 被换成 echo/false 假命令；状态表写入用假 connect 捕获。
上一代测试的 FakeCLI / argv 纪律系列随 subprocess 管理通道一起退役 ——
但 argv 注入的担心并没有消失，只是搬进 core/bus.py 的执行体：那里同样
有静态红线（shell=True 不许出现），本文件一并把关。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import crons as cron_router
from keepsake.core import bus

TOKEN = "bus-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _now():
    return datetime.now(timezone.utc)


class _Cur:
    """假游标（只认 fetchall —— 本模块的查询只用它）。"""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class _FakePg:
    """**内存版 PG**：只认三张表（墓碑 / 最近状态 / 执行历史）。

    🔴 **绝不碰真 PG**：本机没有 ~/.config/keepsake/config.json，
    `_conninfo()` 直接读不出来（这正是上一单误报 10 failed 的环境债），
    所以这里把 `_conninfo()` 按成假串 + 把 `psycopg.connect` 换成这张表。
    """

    def __init__(self):
        self.tombstones: set = set()
        self.state: Dict[str, Dict[str, Any]] = {}
        self.runs: List[Dict[str, Any]] = []

    def connect(self, *a, **k):
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pg: _FakePg):
        self.pg = pg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        pg = self.pg
        if s.startswith("SELECT job_id FROM ks_bus_tombstone"):
            return _Cur([(j,) for j in sorted(pg.tombstones)])
        if s.startswith("INSERT INTO ks_bus_tombstone"):
            pg.tombstones.add(params[0])
            return _Cur()
        if s.startswith("DELETE FROM ks_bus_tombstone"):
            pg.tombstones.discard(params[0])
            return _Cur()
        if s.startswith("INSERT INTO ks_bus_state"):
            pg.state[params[0]] = {"ran_at": _now(), "ok": params[1], "rc": params[2],
                                   "detail": params[3]}
            return _Cur()
        if s.startswith("INSERT INTO ks_bus_run"):
            pg.runs.append({"job_id": params[0], "ran_at": _now(), "ok": params[1],
                            "rc": params[2], "detail": params[3]})
            return _Cur()
        if s.startswith("SELECT job_id, ran_at, ok, rc, detail FROM ks_bus_state"):
            ids = list(params[0]) if params and params[0] else []
            return _Cur([(j, pg.state[j]["ran_at"], pg.state[j]["ok"], pg.state[j]["rc"],
                          pg.state[j]["detail"]) for j in ids if j in pg.state])
        if s.startswith("SELECT ran_at, ok, rc, detail FROM ks_bus_run"):
            rows = [r for r in pg.runs if r["job_id"] == params[0]][-params[1]:]
            return _Cur([(r["ran_at"], r["ok"], r["rc"], r["detail"]) for r in reversed(rows)])
        if s.startswith("SELECT ran_at, ok, rc, detail FROM ks_bus_state"):
            r = pg.state.get(params[0])
            return _Cur([(r["ran_at"], r["ok"], r["rc"], r["detail"])] if r else [])
        return _Cur()          # DDL 之类：不认就当空结果


@pytest.fixture()
def pg_stub(monkeypatch):
    """PG 连接串按成假串（本机没有 config.json）—— 真连库的调用由各用例自己接管。"""
    monkeypatch.setattr(bus, "_conninfo", lambda: "host=nowhere dbname=keepsake")


@pytest.fixture()
def fake_pg(pg_stub, monkeypatch):
    """内存版 PG（墓碑 / 状态 / 历史三张表）。"""
    pg = _FakePg()
    monkeypatch.setattr(bus.psycopg, "connect", lambda *a, **k: pg.connect())
    return pg


def _fake_jobs(tmp_path) -> List[dict]:
    return [
        {"id": "ks-distill", "name": "记忆提炼", "schedule": "15 * * * *",
         "cmd": ["/bin/echo", "distilled"], "timeout": 10},
        {"id": "ks-maintenance", "name": "memory-maintenance",
         "schedule": "0 */2 * * *", "cmd": ["/bin/false"], "timeout": 10},
    ]


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    d = tmp_path / "console_audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def live_sched(tmp_path, monkeypatch, fake_pg):
    """真 APScheduler + sqlite jobstore：调度语义（pause/resume/reschedule）全真跑。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    monkeypatch.setattr(bus, "BUS_JOBS", _fake_jobs(tmp_path))
    monkeypatch.setattr(bus, "_broken", "")
    sch = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{tmp_path}/sched.db")},
        job_defaults=dict(bus.SCHED_DEFAULTS),
        timezone="Asia/Shanghai")
    sch.start(paused=True)          # 注册任务但不真发车（测试不睡等 cron）
    bus._sync_jobs(sch)
    monkeypatch.setattr(bus, "_sched", sch)
    yield sch
    try:
        sch.shutdown(wait=False)   # lifespan 的 bus.shutdown 可能已关过（幂等收口）
    except Exception:              # noqa: BLE001
        pass
    monkeypatch.setattr(bus, "_sched", None)


@pytest.fixture()
def client(monkeypatch, tmp_path, audit_dir, live_sched):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    with TestClient(app) as c:      # 进 lifespan：get_scheduler() 见 _sched 已在 ⇒ 不碰真 PG
        yield c
    app.dependency_overrides.clear()


def audit_lines(audit_dir) -> List[dict]:
    out: List[dict] = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


# ---------------------------------------------------------------- 认证 / 闸门

def test_三个管理端点都要token(client):
    for act, extra in [("pause", {}), ("resume", {}), ("edit", {"schedule": "30 4 * * *"})]:
        r = client.post(f"/api/v1/crons/ks-distill/{act}", json={"confirm": True, **extra})
        assert r.status_code == 401, (act, r.status_code)
        assert r.json()["detail"]["error"] == "missing_token"
        assert TOKEN not in r.text


def test_缺confirm_400且调度器原样(client, live_sched):
    r = client.post("/api/v1/crons/ks-distill/pause", json={}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_required"
    job = live_sched.get_job("ks-distill")
    assert job.next_run_time is not None      # paused=True 下未发车，但任务仍是「在排」态


def test_旧Hermes_id形状被拒_400(client, live_sched, audit_dir):
    """12 位十六进制是上一代 Hermes 任务的形状 —— 新闸门必须拒（旧书签不串门）。"""
    for bad in ("a1b2c3d4e5f6", "ks-", "ks-distill;rm", "distill"):
        r = client.post(f"/api/v1/crons/{bad}/pause", json={"confirm": True}, headers=AUTH)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"] == "bad_job_id"
    # 大写归一后形状合法但表里没有 ⇒ 404（不是 400：闸门只管形状）
    r = client.post("/api/v1/crons/ks-UPPER/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "unknown_job"
    assert audit_lines(audit_dir)


def test_形状合法但表里没有_404(client, live_sched):
    r = client.post("/api/v1/crons/ks-nonexist/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_job"


# ---------------------------------------------------------------- 管理语义（真调度器）

def test_pause_后读面enabled假_next_run空(client, live_sched):
    r = client.post("/api/v1/crons/ks-distill/pause", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["action"] == "pause"
    body = client.get("/api/v1/crons", headers=AUTH).json()
    row = [j for j in body["jobs"] if j["job_id"] == "ks-distill"][0]
    assert row["enabled"] is False and row["next_run"] == ""


def test_pause再resume_调度恢复且audit两笔(client, live_sched, audit_dir):
    client.post("/api/v1/crons/ks-distill/pause", json={"confirm": True}, headers=AUTH)
    assert live_sched.get_job("ks-distill").next_run_time is None
    r = client.post("/api/v1/crons/ks-distill/resume", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200
    assert live_sched.get_job("ks-distill").next_run_time is not None
    acts = [(l["action"], l["ok"]) for l in audit_lines(audit_dir) if l["action"].startswith("cron.")]
    assert ("cron.pause", True) in acts and ("cron.resume", True) in acts


def test_edit_改表达式_调度器与定义面同账(client, live_sched, audit_dir):
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "schedule": "0 5 * * 1"}, headers=AUTH)
    assert r.status_code == 200
    job = live_sched.get_job("ks-distill")
    assert job.next_run_time.minute == 0 and job.next_run_time.hour == 5
    assert bus.BUS_JOBS[0]["schedule"] == "0 5 * * 1"     # 定义面同步（重启不冲回）
    line = [l for l in audit_lines(audit_dir) if l["action"] == "cron.edit"][-1]
    # 审计理由记的是**改后的整份规格**（不止 schedule）—— 逐字相等太脆，取包含
    assert line["ok"] is True and "schedule=0 5 * * 1" in line["reason"]


@pytest.mark.parametrize("expr", [
    "", "30 4 * *", "30 4 * * * *", "60 4 * * *", "30 24 * * *", "30 4 32 * *",
    "30 4 * 13 *", "30 4 * * 8", "abc", "* * * * * ; rm -rf /", "0 0 * * */0",
])
def test_非法schedule_400带example(client, live_sched, expr):
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "schedule": expr}, headers=AUTH)
    assert r.status_code == 400, expr
    d = r.json()["detail"]
    assert d["error"] == "bad_schedule" and "30 4 * * *" in d["message"]


@pytest.mark.parametrize("expr", ["30 4 * * *", "*/15 * * * *", "0 0 1,15 * *",
                                  "0 9-18 * * 1-5", "@daily", "0 0 * * 7"])
def test_合法schedule过闸(client, live_sched, expr):
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "schedule": expr}, headers=AUTH)
    assert r.status_code == 200, expr


# ---------------------------------------------------------------- 调度器宕机的降级面

def test_调度器起不来_读面200带原因_写面503(monkeypatch, tmp_path, audit_dir):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setattr(bus, "_sched", None)
    monkeypatch.setattr(bus, "_broken", "OperationalError: PG 失联（测试剧本）")
    # 拦死真启动：get_scheduler 见 _broken 直接返回 None（降级铁律 3 的既有路径）
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/v1/crons", headers=AUTH)
        assert r.status_code == 200
        st = r.json()["scheduler_status"]
        assert "调度器停止" in st["text"] and "PG 失联" in st["text"]
        assert all(j["next_run"] == "" and j["enabled"] is False for j in r.json()["jobs"])
        w = c.post("/api/v1/crons/ks-distill/pause", json={"confirm": True}, headers=AUTH)
        assert w.status_code == 503
        assert w.json()["detail"]["error"] == "bus_scheduler_down"


# ---------------------------------------------------------------- 读面形状（契约）

def test_读面四字段契约与计数(client):
    body = client.get("/api/v1/crons", headers=AUTH).json()
    assert body["count"] == len(bus.BUS_JOBS) == len(body["jobs"])
    assert body["source"].startswith("keepsake.core.bus")
    for j in body["jobs"]:
        assert set(j) >= {"name", "script", "schedule", "enabled",
                          "last_run_at", "last_status", "next_run", "job_id"}
        assert j["job_id"].startswith("ks-")


def test_读面带上次运行状态_成败都露(client, live_sched, monkeypatch, pg_stub):
    """_execute 落的 ks_bus_state 是读面 last_* 的唯一来源：rc≠0 必须渲成 failed。"""
    captured = []

    class _FakeCur:
        def fetchall(self):
            return [("ks-distill", _dt(), True, 0, "distilled"),
                    ("ks-maintenance", _dt(), False, 1, "boom")]

    def _dt():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append(sql)
            return _FakeCur()

    monkeypatch.setattr(bus.psycopg, "connect", lambda *a, **k: _FakeConn())
    body = client.get("/api/v1/crons", headers=AUTH).json()
    rows = {j["job_id"]: j for j in body["jobs"]}
    assert rows["ks-distill"]["last_status"] == "ok"
    assert rows["ks-maintenance"]["last_status"] == "failed(rc=1)"
    assert rows["ks-distill"]["last_run_at"]


# ---------------------------------------------------------------- 执行体语义

def test_execute_echo落ok_false落failed(monkeypatch, pg_stub):
    rows = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if params:
                rows.append(params)
            return type("C", (), {})()

    monkeypatch.setattr(bus.psycopg, "connect", lambda *a, **k: _Conn())
    bus._execute({"id": "ks-a", "cmd": ["/bin/echo", "hi"], "timeout": 5})
    bus._execute({"id": "ks-b", "cmd": ["/bin/false"], "timeout": 5})
    bus._execute({"id": "ks-c", "cmd": ["/definitely/not/here"], "timeout": 5})
    ok_a = [r for r in rows if r[0] == "ks-a"][0]
    ok_b = [r for r in rows if r[0] == "ks-b"][0]
    ok_c = [r for r in rows if r[0] == "ks-c"][0]
    assert ok_a[1] is True and ok_a[2] == 0 and ok_a[3] == "hi"
    assert ok_b[1] is False and ok_b[2] == 1
    assert ok_c[1] is False and "失败" in (ok_c[3] or "")   # 起进程异常也入账，不抛穿


# ---------------------------------------------------------------- 静态红线

def test_grace_形状钉死_注册后job真带3600(client, live_sched):
    """陈旧形状实锤（11-16 16:00 班被 dead socket 拖晚 10s、grace=1 直接丢班）：
    构造口径必须是单点常量，且注册出的 job 真带 grace=3600/coalesce=True。"""
    assert bus.SCHED_DEFAULTS["misfire_grace_time"] == 3600
    job = live_sched.get_job("ks-distill")
    assert job.misfire_grace_time == 3600 and job.coalesce is True


def test_静态判据_bus与crons不许shellTrue():
    """执行体 subprocess 仍是 argv 列表 + shell=False 的红线（两文件一起把关）。"""
    import inspect
    for mod in (cron_router, bus):
        src = inspect.getsource(mod)
        assert "shell=True" not in src, mod.__name__
        assert "shell" not in src or "shell=False" in src or mod is cron_router


# ---------------------------------------------------------------- 数据驱动（scheduler_page_v2）
#
# 班车模型从「代码 BUS_JOBS 单源」升级为「PG jobstore = 运行期正源，BUS_JOBS 只作
# 首次种子」：增 / 删 / 改 / 立即运行全部落在调度器上，删除靠**墓碑表**保证
# 「重启不复活」。下面这几条钉的就是这三件事。

def test_新建任务_列表可见(client, live_sched):
    """新建 ⇒ 落调度器 ⇒ 读面看得见（规格逐字回显）。"""
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-newbie", "name": "新任务",
        "schedule": "*/10 * * * *", "cmd": ["/bin/echo", "hi"], "timeout": 30},
        headers=AUTH)
    assert r.status_code == 200, r.text
    assert live_sched.get_job("ks-newbie") is not None
    body = client.get("/api/v1/crons", headers=AUTH).json()
    row = [j for j in body["jobs"] if j["job_id"] == "ks-newbie"][0]
    assert row["name"] == "新任务"
    assert row["schedule"] == "*/10 * * * *"
    assert row["cmd"] == ["/bin/echo", "hi"] and row["timeout"] == 30
    assert row["enabled"] is True


def test_新建_id撞车_409(client, live_sched):
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-distill", "name": "撞车",
        "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": 10}, headers=AUTH)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "job_exists"


def test_删除后_种子同步不复活(client, live_sched, fake_pg):
    """🔴 墓碑机制的核心：删掉的任务**重启不许复活**（_sync_jobs 重跑也不补）。"""
    r = client.request("DELETE", "/api/v1/crons/ks-distill", headers=AUTH)
    assert r.status_code == 200, r.text
    assert live_sched.get_job("ks-distill") is None
    assert "ks-distill" in fake_pg.tombstones
    bus._sync_jobs(live_sched)                 # 模拟重启后的种子同步
    assert live_sched.get_job("ks-distill") is None, "墓碑里的任务被种子复活了"


def test_删除_幂等_不存在也回ok(client, live_sched):
    r = client.request("DELETE", "/api/v1/crons/ks-ghost", headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_无墓碑_缺失种子照常补建(client, live_sched):
    """没有墓碑时，种子表里缺的任务**照常补建**（种子同步没被我改坏）。"""
    live_sched.remove_job("ks-distill")
    assert live_sched.get_job("ks-distill") is None
    bus._sync_jobs(live_sched)
    assert live_sched.get_job("ks-distill") is not None


def test_新建同id_撤墓碑_可重新添加(client, live_sched, fake_pg):
    """删除不是永禁：同一个 id 可以重新添加（墓碑只挡「自动复活」）。"""
    client.request("DELETE", "/api/v1/crons/ks-distill", headers=AUTH)
    assert "ks-distill" in fake_pg.tombstones
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-distill", "name": "再来一次",
        "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": 10}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "ks-distill" not in fake_pg.tombstones


def test_edit改cmd_下次触发用新命令(client, live_sched, tmp_path):
    """改完 cmd，下一次触发跑的是**新命令**（echo 探针写文件为证）。"""
    probe = tmp_path / "probe.txt"
    r = client.post("/api/v1/crons/ks-distill/edit", json={
        "confirm": True, "cmd": ["/bin/sh", "-c", f"echo probe > {probe}"]}, headers=AUTH)
    assert r.status_code == 200, r.text
    live_sched.resume()                       # 夹具默认 paused（不真发车）
    t = client.post("/api/v1/crons/ks-distill/trigger", json={"confirm": True}, headers=AUTH)
    assert t.status_code == 200, t.text
    deadline = time.time() + 20
    while time.time() < deadline and not probe.exists():
        time.sleep(0.2)
    assert probe.exists(), "触发后应跑新命令（探针文件没出现）"
    assert probe.read_text(encoding="utf-8").strip() == "probe"


def test_trigger_落状态且常驻任务数不增(client, live_sched, fake_pg):
    """立即运行 ⇒ 执行照常落状态表；**常驻任务数不增**（一次性 job 跑完自消）。"""
    before = sorted(j.id for j in live_sched.get_jobs())
    live_sched.resume()
    r = client.post("/api/v1/crons/ks-distill/trigger", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    deadline = time.time() + 20
    while time.time() < deadline and not fake_pg.runs:
        time.sleep(0.2)
    assert fake_pg.runs, "触发后应有执行记录"
    assert fake_pg.runs[-1]["job_id"] == "ks-distill"
    assert fake_pg.state["ks-distill"]["ok"] is True
    deadline = time.time() + 20
    while time.time() < deadline:
        if all("-run-" not in j.id for j in live_sched.get_jobs()):
            break
        time.sleep(0.2)
    after = sorted(j.id for j in live_sched.get_jobs())
    assert after == before, f"常驻任务数不该变：{after} vs {before}"


def test_trigger_调度器停着_4xx(client, live_sched, monkeypatch):
    """调度器停了 ⇒ 立即运行是**状态冲突**（4xx），不是 404 也不是 500。"""
    monkeypatch.setattr(bus, "_sched", None)
    monkeypatch.setattr(bus, "_broken", "OperationalError: PG 失联（测试剧本）")
    r = client.post("/api/v1/crons/ks-distill/trigger", json={"confirm": True}, headers=AUTH)
    assert 400 <= r.status_code < 500, r.status_code
    assert r.json()["detail"]["error"] == "bus_scheduler_down"


def test_执行记录_近10次只读(client, live_sched, fake_pg):
    fake_pg.runs.extend({"job_id": "ks-distill", "ran_at": _now(), "ok": i % 2 == 0,
                          "rc": 0 if i % 2 == 0 else 1, "detail": f"run{i}"} for i in range(12))
    r = client.get("/api/v1/crons/ks-distill/runs", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "ks-distill"
    assert len(body["runs"]) == 10                # 只回近 10 次
    assert body["runs"][0]["detail"] == "run11"   # 最近的在前


@pytest.mark.parametrize("payload", [
    {"id": "nope", "name": "x", "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": 10},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "cmd": "echo hi", "timeout": 10},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": 0},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": -5},
    {"id": "ks-bad", "name": "x", "schedule": "不是 cron", "cmd": ["/bin/echo"], "timeout": 10},
])
def test_非法形状_全4xx(client, live_sched, payload):
    """形状不合法（没 ks- 前缀 / cmd 非列表 / timeout 非正 / 表达式非法）⇒ 全 4xx。"""
    r = client.post("/api/v1/crons", json={"confirm": True, **payload}, headers=AUTH)
    assert 400 <= r.status_code < 500, (payload, r.status_code, r.text)
    assert live_sched.get_job("ks-bad") is None


def test_新建缺confirm_400(client, live_sched):
    r = client.post("/api/v1/crons", json={
        "id": "ks-nc", "name": "x", "schedule": "0 4 * * *",
        "cmd": ["/bin/echo"], "timeout": 10}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


# ---------------------------------------------------------------- 合并 / 遗忘实跑（confirm 口径改版）

@pytest.fixture()
def ops_client(monkeypatch, tmp_path, audit_dir, live_sched):
    """带真 SqliteStorage 的客户端（合并 / 遗忘实跑要过存储层）。"""
    from keepsake.core.sqlite import SqliteStorage
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(tmp_path / "console_ops"))
    storage = SqliteStorage(path=str(tmp_path / "ops.db"), embedder=None, embed_dim=8)
    assert storage.ensure_index() is not False
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_实跑_confirm_true_即过_缺confirm仍拒(ops_client):
    """实跑的键入计数环节废除：``confirm: true`` 即过；**缺 confirm 仍然拒**。"""
    for path in ("/api/v1/ops/merge", "/api/v1/ops/forget"):
        r = ops_client.post(path, json={"dry_run": False}, headers=AUTH)
        assert r.status_code == 400, path
        assert r.json()["detail"]["error"] == "confirm_required", path
        r = ops_client.post(path, json={"dry_run": False, "confirm": True}, headers=AUTH)
        assert r.status_code == 200, (path, r.text)
        assert r.json()["dry_run"] is False


def test_实跑_老口径手输计数仍认(ops_client):
    """老客户端手输的计数串仍认（对得上放行，对不上仍 400）。"""
    r = ops_client.post("/api/v1/ops/merge",
                        json={"dry_run": False, "confirm": "999999"}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
