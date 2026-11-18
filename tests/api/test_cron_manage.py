"""记忆班车管理端点测试（v2.2.0 方案 C：APScheduler 内核，契约不变）。

🔴 **绝不碰真实 PG / 真实班车**：调度器用 tmp_path 里的 sqlite jobstore；
BUS_JOBS 被换成假 func；状态表写入用假 connect 捕获。
2026-11-18（scheduler_cmd_mode_retire）：旧的 shell 执行形态**整体退役** ——
本文件里原先盖着「旧形态仍可注册」的那几条，**改成盖退役口径**：传了就
400 点名拒收、注册面拒注册、执行面 fail-closed 落 ok=False。
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


#: 真实种子（夹具会把 BUS_JOBS 换成假命令 —— 这里要验的是**真种子**的字段）。
#: 在模块导入时取一份（夹具的 monkeypatch 只在用例期内生效）。
REAL_BUS_JOBS = list(bus.BUS_JOBS)


# ---------------------------------------------------------------- func 形态的执行体
#
# func 形态 = ``"模块:可调用"``，调度器用 importlib 解析后**同进程**调用。
# 测试用的执行体就放在**本文件**里（模块级函数 ⇒ 可被 importlib 解析：
# ``__name__`` 就是 pytest 给本模块的模块名，sys.modules 里已经有了）。

def ok_run():
    """成功形态：返回 ``(ok, 人读摘要)``。"""
    return True, "提炼 2 条，已写入 2 条"


def boom_run():
    """失败形态：抛异常（由调度层捕获 ⇒ 落 ok=False）。"""
    raise RuntimeError("班车炸了（测试剧本）")


#: 探针路径（模块级）：测试写入目标文件路径，执行体据此写文件 ——
#: 证明「触发时跑的是**改完之后**的那条 func」。
PROBE_PATH = {"path": ""}


def probe_run():
    """把探针路径写进文件（成功形态 + 落盘副作用）。"""
    from pathlib import Path
    p = PROBE_PATH.get("path") or ""
    if p:
        Path(p).write_text("probe", encoding="utf-8")
    return True, "probe ok"


#: 三个测试执行体的引用（注册面用）。
FUNC_OK = f"{__name__}:ok_run"
FUNC_BOOM = f"{__name__}:boom_run"
FUNC_PROBE = f"{__name__}:probe_run"


def _fake_jobs(tmp_path) -> List[dict]:
    """夹具用的假班车表：**两条 func 形态**的种子。

    旧 shell 形态已退役（迭代决策）⇒ 种子侧只剩 func 一条路。
    """
    return [
        {"id": "ks-distill", "name": "记忆提炼", "schedule": "15 * * * *",
         "func": FUNC_OK, "timeout": 10},
        {"id": "ks-maintenance", "name": "合并 · 遗忘（维护）",
         "schedule": "0 */2 * * *", "func": FUNC_BOOM, "timeout": 10},
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
        # 旧的 shell 形态已退役 ⇒ 读面不再回显那个字段
        assert "cmd" not in j


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

def test_execute_旧形态_不执行且落ok_false_点名退役(monkeypatch, pg_stub):
    """🔴 旧的 shell 形态**已退役**：执行器不再起子进程 —— 规格里没有 func
    ⇒ fail-closed：落 ok=False + 末行摘要**点名退役**（不静默当「跑成功」）。"""
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
    row = [r for r in rows if r[0] == "ks-a"][0]
    assert row[1] is False and row[2] == 1
    assert "退役" in (row[3] or ""), row[3]


def test_execute_func形态_摘要与异常都入账(monkeypatch, pg_stub):
    """func 形态：成功落摘要、异常落 ok=False（都不抛穿）。"""
    rows = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if params:
                rows.append(params)
            return type("C", (), {})()

    monkeypatch.setattr(bus.psycopg, "connect", lambda *a, **k: _Conn())
    bus._execute({"id": "ks-a", "func": FUNC_OK, "timeout": 5})
    bus._execute({"id": "ks-b", "func": FUNC_BOOM, "timeout": 5})
    ok_a = [r for r in rows if r[0] == "ks-a"][0]
    ok_b = [r for r in rows if r[0] == "ks-b"][0]
    assert ok_a[1] is True and ok_a[2] == 0 and ok_a[3] == "提炼 2 条，已写入 2 条"
    assert ok_b[1] is False and ok_b[2] == 1 and "班车炸了" in (ok_b[3] or "")


# ---------------------------------------------------------------- 静态红线

def test_grace_形状钉死_注册后job真带3600(client, live_sched):
    """陈旧形状实锤（11-16 16:00 班被 dead socket 拖晚 10s、grace=1 直接丢班）：
    构造口径必须是单点常量，且注册出的 job 真带 grace=3600/coalesce=True。"""
    assert bus.SCHED_DEFAULTS["misfire_grace_time"] == 3600
    job = live_sched.get_job("ks-distill")
    assert job.misfire_grace_time == 3600 and job.coalesce is True


def test_静态判据_执行器不再起子进程():
    """🔴 旧的 shell 形态已退役 ⇒ 调度器**不再起子进程**（执行器里 subprocess 消失）。

    这条比「shell=True 不许出现」更强：连 argv 列表那条路都不留了，
    执行方式只剩 func（同进程调函数）。
    """
    import inspect
    for mod in (cron_router, bus):
        assert "shell=True" not in inspect.getsource(mod), mod.__name__
    assert "subprocess" not in inspect.getsource(bus), "执行器还在起子进程"


# ---------------------------------------------------------------- 数据驱动（scheduler_page_v2）
#
# 班车模型从「代码 BUS_JOBS 单源」升级为「PG jobstore = 运行期正源，BUS_JOBS 只作
# 首次种子」：增 / 删 / 改 / 立即运行全部落在调度器上，删除靠**墓碑表**保证
# 「重启不复活」。下面这几条钉的就是这三件事。

def test_新建任务_列表可见(client, live_sched):
    """新建 ⇒ 落调度器 ⇒ 读面看得见（规格逐字回显）。"""
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-newbie", "name": "新任务",
        "schedule": "*/10 * * * *", "func": FUNC_OK, "timeout": 30},
        headers=AUTH)
    assert r.status_code == 200, r.text
    assert live_sched.get_job("ks-newbie") is not None
    body = client.get("/api/v1/crons", headers=AUTH).json()
    row = [j for j in body["jobs"] if j["job_id"] == "ks-newbie"][0]
    assert row["name"] == "新任务"
    assert row["schedule"] == "*/10 * * * *"
    assert row["func"] == FUNC_OK and row["timeout"] == 30
    assert row["enabled"] is True


def test_新建_id撞车_409(client, live_sched):
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-distill", "name": "撞车",
        "schedule": "0 4 * * *", "func": FUNC_OK, "timeout": 10}, headers=AUTH)
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
        "schedule": "0 4 * * *", "func": FUNC_OK, "timeout": 10}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "ks-distill" not in fake_pg.tombstones


def test_edit改func_下次触发跑的是新func(client, live_sched, tmp_path):
    """改完 func 引用，下一次触发跑的是**新引用**（探针文件为证）。"""
    probe = tmp_path / "probe.txt"
    PROBE_PATH["path"] = str(probe)
    try:
        r = client.post("/api/v1/crons/ks-distill/edit", json={
            "confirm": True, "func": FUNC_PROBE}, headers=AUTH)
        assert r.status_code == 200, r.text
        live_sched.resume()                       # 夹具默认 paused（不真发车）
        t = client.post("/api/v1/crons/ks-distill/trigger", json={"confirm": True}, headers=AUTH)
        assert t.status_code == 200, t.text
        deadline = time.time() + 20
        while time.time() < deadline and not probe.exists():
            time.sleep(0.2)
        assert probe.exists(), "触发后应跑新 func（探针文件没出现）"
        assert probe.read_text(encoding="utf-8").strip() == "probe"
    finally:
        PROBE_PATH["path"] = ""


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
    {"id": "nope", "name": "x", "schedule": "0 4 * * *", "func": FUNC_OK, "timeout": 10},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "func": "", "timeout": 10},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "func": "no.such.module:run", "timeout": 10},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "func": FUNC_OK, "timeout": 0},
    {"id": "ks-bad", "name": "x", "schedule": "0 4 * * *", "func": FUNC_OK, "timeout": -5},
    {"id": "ks-bad", "name": "x", "schedule": "不是 cron", "func": FUNC_OK, "timeout": 10},
])
def test_非法形状_全4xx(client, live_sched, payload):
    """形状不合法（没 ks- 前缀 / func 缺失 / func 不可 import / timeout 非正 /
    表达式非法）⇒ 全 4xx。"""
    r = client.post("/api/v1/crons", json={"confirm": True, **payload}, headers=AUTH)
    assert 400 <= r.status_code < 500, (payload, r.status_code, r.text)
    assert live_sched.get_job("ks-bad") is None


def test_新建缺confirm_400(client, live_sched):
    r = client.post("/api/v1/crons", json={
        "id": "ks-nc", "name": "x", "schedule": "0 4 * * *",
        "func": FUNC_OK, "timeout": 10}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_required"


# ---------------------------------------------------------------- 种子更名（scheduler_page_ui_fix）
#
# 四条班车的 name 统一成中文（页面可见文案）；id / schedule / func / timeout 逐字不动。
# 🔴 种子同步**不覆盖已存在的任务**（保住暂停态）⇒ 现网 jobstore 里那四行还挂着旧名，
# 要等主脑清掉那四行（不留墓碑）再重启 console，_sync_jobs 才按新种子重建。
# 本节钉的是**种子侧**的口径：重建出来的 name 必须是新值。

def test_种子重建后_name是中文新值(live_sched, monkeypatch):
    """清掉 jobstore 里的常驻行 → _sync_jobs 重建 → name = 新种子的中文名。"""
    monkeypatch.setattr(bus, "BUS_JOBS", REAL_BUS_JOBS)
    for jd in REAL_BUS_JOBS:
        if live_sched.get_job(jd["id"]) is not None:
            live_sched.remove_job(jd["id"])
    bus._sync_jobs(live_sched)
    names = {}
    for jd in REAL_BUS_JOBS:
        job = live_sched.get_job(jd["id"])
        assert job is not None, jd["id"]
        names[jd["id"]] = job.name
    assert names == {
        "ks-distill": "记忆提炼",
        "ks-maintenance": "合并 · 遗忘（维护）",
        "ks-tiering": "记忆分级班车",
        "ks-synonyms": "同义词总线",
    }


def test_现役四条_重启重建后func与调度逐字不变(live_sched, monkeypatch):
    """🔴 红线：func 形态与**现役四条**（ks-distill / ks-maintenance /
    ks-tiering / ks-synonyms）行为零变化 —— 重启调度后仍照常注册，且
    func / schedule / timeout 逐字不动（动 id = 换一条车）。"""
    monkeypatch.setattr(bus, "BUS_JOBS", REAL_BUS_JOBS)
    for jd in REAL_BUS_JOBS:
        if live_sched.get_job(jd["id"]) is not None:
            live_sched.remove_job(jd["id"])
    bus._sync_jobs(live_sched)
    for jd in REAL_BUS_JOBS:
        job = live_sched.get_job(jd["id"])
        assert job is not None, jd["id"]
        assert job.next_run_time is not None, jd["id"]
        spec = bus.job_spec(job)
        assert spec["func"] == jd["func"], jd["id"]
        assert spec["schedule"] == jd["schedule"], jd["id"]
        assert spec["timeout"] == jd["timeout"], jd["id"]


def test_种子更名只动name_其余字段逐字不动():
    """🔴 只许改 name：id / schedule / timeout 一字都不能动
    （动 id = 换一条车，墓碑都拦不住）。

    执行方式（2026-11-16 bus_inprocess_pure_code）：四条种子**全部**是
    **func 形态**（``"模块:可调用"``），**不许再有 cmd**（壳脚本形态退役）。
    """
    expect = {
        "ks-distill": ("15 * * * *", 1800, "keepsake.buses.distill:run"),
        "ks-maintenance": ("0 */2 * * *", 1800, "keepsake.buses.maintenance:run"),
        "ks-tiering": ("30 4 * * *", 900, "keepsake.buses.tiering:run"),
        "ks-synonyms": ("0 */8 * * *", 3600, "keepsake.buses.synonyms:run"),
    }
    assert {j["id"] for j in REAL_BUS_JOBS} == set(expect)
    for j in REAL_BUS_JOBS:
        sched, timeout, func = expect[j["id"]]
        assert j["schedule"] == sched, j["id"]
        assert j["timeout"] == timeout, j["id"]
        assert j["func"] == func, j["id"]
        assert "cmd" not in j, j["id"]
    # 旧英文名不许回来
    for j in REAL_BUS_JOBS:
        assert j["name"] not in ("memory-maintenance", "synonym-discovery-daily")


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


# ---------------------------------------------------------------- timeout 三口径互斥（review fix）

def test_存量job无timeout_编辑面补1800不补0(client, live_sched):
    """🔴 存量 job（老 jobstore 形状：规格里**没有 timeout 字段**）的编辑面口径。

    读回补 0 ⇒ 编辑校验要求正整数 ⇒ 一编辑就 400、编辑框显示 0；
    执行路径缺省却是 1800 ⇒ 三处口径互斥。正确口径：读回缺失时补**执行路径同款**
    的 DEFAULT_TIMEOUT（1800）。"""
    from apscheduler.triggers.cron import CronTrigger

    # 往 jobstore 塞一条**没有 timeout 字段**的存量 job（老形状）
    legacy_spec = {"id": "ks-legacy", "name": "存量任务",
                   "schedule": "0 4 * * *", "func": FUNC_OK}
    live_sched.add_job(bus._execute, CronTrigger.from_crontab("0 4 * * *"),
                        args=[legacy_spec], id="ks-legacy", name="存量任务")

    # 读面（编辑框回填的来源）：不是 0，是 1800
    row = [j for j in client.get("/api/v1/crons", headers=AUTH).json()["jobs"]
           if j["job_id"] == "ks-legacy"][0]
    assert row["timeout"] == 1800, row

    # 编辑：只改合法 schedule ⇒ 成功，且落库的 timeout 是 1800（不是 0）
    r = client.post("/api/v1/crons/ks-legacy/edit",
                    json={"confirm": True, "schedule": "30 5 * * *"}, headers=AUTH)
    assert r.status_code == 200, r.text
    stored = bus.job_spec(live_sched.get_job("ks-legacy"))
    assert stored["timeout"] == 1800, stored
    assert live_sched.get_job("ks-legacy").args[0]["timeout"] == 1800


def test_timeout默认值单点定义():
    """timeout 的默认值只有一处定义（执行路径与读面补默认共用）。"""
    import inspect
    assert bus.DEFAULT_TIMEOUT == 1800
    src = inspect.getsource(bus)
    # 执行路径的缺省与读面的补默认都引用常量，不许各自写字面量
    assert 'job_def.get("timeout", 1800)' not in src
    assert "'timeout', 1800)" not in src
    assert 'spec["timeout"] = 0' not in src


# ---------------------------------------------------------------- func 形态（in-process）
#
# 2026-11-16 bus_inprocess_pure_code：执行方式从「argv 列表 + 起子进程」扩成
# **双形态**；2026-11-18 scheduler_cmd_mode_retire：旧的 shell 形态**整体退役**
# （迭代决策：迭代了就不要留着旧的）⇒ 执行方式只剩 func 一种。
# 本节钉的是 func 形态的四件事：**注册即验**（不可 import ⇒ 4xx）、**跑完落摘要**
# （detail = run() 的第二个返回值）、**抛异常落 ok=False**、**旧形态拒收**。

def test_func形态_注册即验importable_不可import即4xx(client, live_sched):
    """🔴 注册时就验：func 指向的模块 / 属性不存在 ⇒ 4xx（不许存一条跑不起来的任务）。"""
    for bad in ("no.such.module:run", "keepsake.core.bus:no_such_attr",
                "keepsake.core.bus:DEFAULT_TIMEOUT"):     # 最后一个不是可调用对象
        r = client.post("/api/v1/crons", json={
            "confirm": True, "id": "ks-badfunc", "name": "x",
            "schedule": "0 4 * * *", "func": bad, "timeout": 10}, headers=AUTH)
        assert 400 <= r.status_code < 500, (bad, r.status_code, r.text)
        assert r.json()["detail"]["error"] == "bad_job_spec", bad
    assert live_sched.get_job("ks-badfunc") is None


def test_func形态_注册成功且读面回显func(client, live_sched):
    """func 形态注册 ⇒ 读面回显 func（旧的 shell 字段已退役，不再回显）。"""
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-funcjob", "name": "函数班车",
        "schedule": "*/10 * * * *", "func": FUNC_OK, "timeout": 30}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert live_sched.get_job("ks-funcjob") is not None
    row = [j for j in client.get("/api/v1/crons", headers=AUTH).json()["jobs"]
           if j["job_id"] == "ks-funcjob"][0]
    assert row["func"] == FUNC_OK
    assert "cmd" not in row
    assert row["timeout"] == 30


def test_func形态_跑完落detail为摘要(client, live_sched, fake_pg):
    """run() 返回 (True, 摘要) ⇒ 落库 detail **就是那句摘要**（ok=True）。"""
    client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-funcjob", "name": "函数班车",
        "schedule": "*/10 * * * *", "func": FUNC_OK, "timeout": 30}, headers=AUTH)
    live_sched.resume()
    t = client.post("/api/v1/crons/ks-funcjob/trigger",
                    json={"confirm": True}, headers=AUTH)
    assert t.status_code == 200, t.text
    deadline = time.time() + 20
    while time.time() < deadline and "ks-funcjob" not in fake_pg.state:
        time.sleep(0.2)
    st = fake_pg.state.get("ks-funcjob")
    assert st, "触发后应有执行记录"
    assert st["ok"] is True
    assert st["detail"] == "提炼 2 条，已写入 2 条"


def test_func形态_抛异常落ok_false(client, live_sched, fake_pg):
    """run() 抛异常 ⇒ 落 ok=False（异常由调度层接住，**不抛穿**）。"""
    client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-boomjob", "name": "会炸的班车",
        "schedule": "*/10 * * * *", "func": FUNC_BOOM, "timeout": 30}, headers=AUTH)
    live_sched.resume()
    t = client.post("/api/v1/crons/ks-boomjob/trigger",
                    json={"confirm": True}, headers=AUTH)
    assert t.status_code == 200, t.text
    deadline = time.time() + 20
    while time.time() < deadline and "ks-boomjob" not in fake_pg.state:
        time.sleep(0.2)
    st = fake_pg.state.get("ks-boomjob")
    assert st, "触发后应有执行记录"
    assert st["ok"] is False
    assert "班车炸了" in (st["detail"] or "")


def test_旧形态_400拒收_不静默忽略(client, live_sched):
    """🔴 旧的 shell 形态**已退役**（迭代决策）：写面**显式拒收** —— 400 点名，
    不收下、也不静默忽略（对齐 AgentHub 代理节点参数退役的同口径）。"""
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-oldcmd", "name": "老壳班车",
        "schedule": "*/10 * * * *", "cmd": ["/bin/echo", "hi"], "timeout": 30},
        headers=AUTH)
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d["error"] == "cmd_retired"
    assert "退役" in d["message"] and "func" in d["message"]
    assert live_sched.get_job("ks-oldcmd") is None
    assert "ks-oldcmd" not in {j["job_id"] for j in
                                client.get("/api/v1/crons", headers=AUTH).json()["jobs"]}


def test_旧形态与func都给_同样400拒收(client, live_sched):
    """旧的 shell 形态 + func 一起给 ⇒ 同样 400 点名（先点名退役，不猜优先级）。"""
    r = client.post("/api/v1/crons", json={
        "confirm": True, "id": "ks-both", "name": "x", "schedule": "0 4 * * *",
        "cmd": ["/bin/echo"], "func": FUNC_OK, "timeout": 10}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "cmd_retired"
    assert live_sched.get_job("ks-both") is None


def test_编辑传旧形态_400拒收(client, live_sched):
    """编辑面同样拒收旧形态（改调度的人不该顺手把退休形态写回来）。"""
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "cmd": ["/bin/echo", "hi"]}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "cmd_retired"


def test_注册面_旧形态_拒注册(live_sched):
    """🔴 防复活闸（注册面）：规格里带旧形态 ⇒ 拒注册（点名退役），不落 job。"""
    from keepsake.core.bus import JobSpecError
    with pytest.raises(JobSpecError) as ei:
        bus.reconcile_register(live_sched, {
            "id": "ks-cmdjob", "name": "老壳班车", "schedule": "0 4 * * *",
            "cmd": ["/bin/echo"], "timeout": 10})
    assert ei.value.error == "cmd_retired"
    assert "退役" in str(ei.value)
    assert live_sched.get_job("ks-cmdjob") is None


def test_加载面_存量旧形态_拒执行并ERROR点名(live_sched, caplog):
    """🔴 防复活闸（加载面）：jobstore 里出现旧形态 ⇒ 停摆（不再发车）+ 日志点名。

    理论上不该存在（现役四条全是 func 形态）—— 出现即数据被外部写过：
    fail-closed，不猜、不迁移、不执行。
    """
    import logging
    from apscheduler.triggers.cron import CronTrigger
    legacy = {"id": "ks-legacycmd", "name": "老壳班车",
              "schedule": "0 4 * * *", "cmd": ["/bin/echo"], "timeout": 10}
    live_sched.add_job(bus._execute, CronTrigger.from_crontab("0 4 * * *"),
                        args=[legacy], id="ks-legacycmd", name="老壳班车")
    with caplog.at_level(logging.ERROR, logger="keepsake.bus"):
        bus._sync_jobs(live_sched)
    assert live_sched.get_job("ks-legacycmd").next_run_time is None, "旧形态仍在排班"
    assert any("ks-legacycmd" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_编辑可换func引用(client, live_sched):
    """编辑面：传 func ⇒ 换成新的函数引用；不传 ⇒ 沿用现值。"""
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "func": FUNC_PROBE}, headers=AUTH)
    assert r.status_code == 200, r.text
    stored = bus.job_spec(live_sched.get_job("ks-distill"))
    assert stored["func"] == FUNC_PROBE
    r = client.post("/api/v1/crons/ks-distill/edit",
                    json={"confirm": True, "schedule": "0 5 * * *"}, headers=AUTH)
    assert r.status_code == 200, r.text
    stored = bus.job_spec(live_sched.get_job("ks-distill"))
    assert stored["func"] == FUNC_PROBE and stored["schedule"] == "0 5 * * *"
