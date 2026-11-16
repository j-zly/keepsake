"""记忆班车管理端点测试（v2.2.0 方案 C：APScheduler 内核，契约不变）。

🔴 **绝不碰真实 PG / 真实班车**：调度器用 tmp_path 里的 sqlite jobstore；
BUS_JOBS 被换成 echo/false 假命令；状态表写入用假 connect 捕获。
上一代测试的 FakeCLI / argv 纪律系列随 subprocess 管理通道一起退役 ——
但 argv 注入的担心并没有消失，只是搬进 core/bus.py 的执行体：那里同样
有静态红线（shell=True 不许出现），本文件一并把关。
"""

from __future__ import annotations

import json
from typing import List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.routers import crons as cron_router
from keepsake.core import bus

TOKEN = "bus-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
def live_sched(tmp_path, monkeypatch):
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
    assert line["ok"] is True and line["reason"] == "schedule=0 5 * * 1"


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


def test_读面带上次运行状态_成败都露(client, live_sched, monkeypatch):
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

def test_execute_echo落ok_false落failed(monkeypatch):
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
