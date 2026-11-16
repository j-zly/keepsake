"""记忆班车调度器（v2.2.0，方案 C）：APScheduler + PG jobstore，嵌在 console 进程里。

背景（2026-11-16 复盘）：班车原来挂在 Hermes 网关的 cron 上，网关重启 / 安装树整树
替换 / worker 解释器漂移三次实锤把班车打断过（升级事故 + 维护班车两连红）。本模块
把**调度真值源**迁到 keepsake 自己的 PG：

- 任务定义与持久化状态（下次运行 / 暂停态）= PG 里的 APScheduler jobstore 表；
- 上次运行结果 = ``ks_bus_state`` 表（3.x 的 jobstore 不存「上次跑过没有」，自记一张）；
- jobs.json 从此与本系统无关 —— provider 侧的自动注册（``_ensure_cron_jobs``）同批下线，
  那正是「记忆去重」僵尸每次复活换 id 的出生地（``uuid4().hex[:12]``）。

三条铁律：
1. **执行体只调现成的 sh/py 包装，绝不内联重写脚本逻辑**——班车脚本是被反复打磨过的
   运行手册（tiering 的红报、distill 的窗口语义），调度器只管「何时跑、成没成」。
2. **解释器钉死树外绝对路径**（/opt/keepsake-venv、/bin/sh），Hermes 安装树升级永远
   够不着这里。
3. **调度器起不来只降级不抛穿**：PG 失联时 console 照常服务，crons 读面显示
   「调度器停止」，绝不为难页面（与 crons.py 的「读面不许 500」同源纪律）。

misfire 口径：``coalesce=True`` + 宽限期 1 小时 —— console 重启错过几个 tick 只补跑
一次（提炼 / 维护都是幂等窗口语义，多烧没有意义）。
"""

from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import psycopg

logger = logging.getLogger("keepsake.bus")

#: 与 Hermes 时代逐字对齐的班车表（schedule 用五段 cron，Asia/Shanghai 口径）。
#: id 是本包的稳定主键：改名 = 重建任务（暂停态会丢），只增不改。
BUS_JOBS: List[Dict[str, Any]] = [
    {
        "id": "ks-distill",
        "name": "记忆提炼",
        "schedule": "15 * * * *",
        "cmd": ["/bin/sh", "/root/.hermes/scripts/memory_distill_cron.sh"],
        "timeout": 1800,
    },
    {
        "id": "ks-maintenance",
        "name": "memory-maintenance",
        "schedule": "0 */2 * * *",
        "cmd": ["/bin/sh", "/root/.hermes/scripts/memory_maintenance_cron.sh"],
        "timeout": 1800,
    },
    {
        "id": "ks-tiering",
        "name": "记忆分级班车（实跑）",
        "schedule": "30 4 * * *",
        "cmd": ["/bin/sh", "/root/.hermes/scripts/tiering_cron.sh"],
        "timeout": 900,
    },
    {
        "id": "ks-synonyms",
        "name": "synonym-discovery-daily",
        "schedule": "0 */8 * * *",
        "cmd": ["/opt/keepsake-venv/bin/python",
                "/root/.hermes/scripts/discover_synonyms.py"],
        "timeout": 3600,
    },
]

_lock = threading.Lock()
_sched = None            # BackgroundScheduler 或 None（起不来时）
_broken = ""             # 起不来的原因（人话），给读面回显

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS ks_bus_state (
    job_id   text PRIMARY KEY,
    ran_at   timestamptz NOT NULL,
    ok       boolean NOT NULL,
    rc       integer,
    detail   text
)
"""


def _pg_conf() -> Dict[str, Any]:
    """config.json 的 storage.postgres 节（与主存储同源，不设第二套账）。"""
    import json
    from pathlib import Path
    cfg = json.loads(Path("~/.config/keepsake/config.json").expanduser().read_text())
    return cfg["storage"]["postgres"]


def _jobstore_url() -> str:
    pg = _pg_conf()
    ssl = "" if not pg.get("sslmode") or pg.get("sslmode") == "disable" \
        else f"?sslmode={pg['sslmode']}"
    return ("postgresql+psycopg://%s:%s@%s:%s/%s%s"
            % (pg["user"], quote_plus(str(pg.get("password", ""))),
               pg["host"], pg.get("port", 5432), pg["dbname"], ssl))


def _conninfo() -> str:
    pg = _pg_conf()
    return ("host=%s port=%s dbname=%s user=%s password=%s connect_timeout=8"
            % (pg["host"], pg.get("port", 5432), pg["dbname"],
               pg["user"], pg.get("password", "")))


# --------------------------------------------------------------------------
# 执行体（被 jobstore 序列化引用：必须是模块级函数，args 必须可持久化）
# --------------------------------------------------------------------------

def _execute(job_def: Dict[str, Any]) -> None:
    """跑一条班车（argv 列表、shell=False、绝对解释器），结果落 ks_bus_state。

    失败不抛（APScheduler 会把异常吞进 logger，值守看不见）；统一转成
    ok=False + 末行摘要，页面与排查看一张表就够。
    """
    rc: Optional[int] = None
    ok = False
    detail = ""
    try:
        p = subprocess.run(job_def["cmd"], capture_output=True, text=True,
                           timeout=job_def.get("timeout", 1800), shell=False,
                           cwd="/root")
        rc = p.returncode
        ok = (rc == 0)
        blob = (p.stdout if ok else (p.stderr or p.stdout)) or ""
        lines = [ln for ln in blob.splitlines() if ln.strip()]
        detail = (lines[-1] if lines else "")[:500]
    except subprocess.TimeoutExpired:
        detail = f"超时被杀（>{job_def.get('timeout', 1800)}s）"
    except OSError as e:
        detail = f"起进程失败：{e.__class__.__name__}: {e}"[:500]
    if not ok:
        logger.error("bus: %s 跑红 rc=%s detail=%s", job_def.get("id"), rc, detail[:200])
    try:
        with psycopg.connect(_conninfo()) as conn:
            conn.execute(_STATE_DDL)
            conn.execute(
                "INSERT INTO ks_bus_state(job_id, ran_at, ok, rc, detail) "
                "VALUES (%s, now(), %s, %s, %s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "ran_at = now(), ok = EXCLUDED.ok, rc = EXCLUDED.rc, "
                "detail = EXCLUDED.detail",
                (job_def.get("id", "?"), ok, rc, detail))
    except Exception as e:            # noqa: BLE001 —— 状态写不进也不能把班车再炸一次
        logger.warning("bus: ks_bus_state 写入失败: %s", e)


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

def get_scheduler():
    """幂等取调度器：没起则起（PG jobstore + 同步任务表），起不来返回 None。

    console 启动时由 app.py 的 lifespan 调一次；crons 路由只读它，不再各自造。
    """
    global _sched, _broken
    if _sched is not None or _broken:
        return _sched
    with _lock:
        if _sched is not None or _broken:
            return _sched
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            sch = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(url=_jobstore_url())},
                job_defaults={"coalesce": True, "misfire_grace_time": 3600},
                timezone="Asia/Shanghai",
            )
            sch.start()
            _sync_jobs(sch)
            _sched = sch
            logger.info("bus: 调度器已启动（%d 条班车，PG jobstore）", len(BUS_JOBS))
        except Exception as e:        # noqa: BLE001 —— 降级铁律 3
            _broken = f"{e.__class__.__name__}: {e}"
            logger.error("bus: 调度器启动失败（console 继续服务）: %s", _broken)
            _sched = None
    return _sched


def shutdown() -> None:
    global _sched
    if _sched is not None:
        try:
            _sched.shutdown(wait=False)
        except Exception:             # noqa: BLE001
            pass
        _sched = None


def _sync_jobs(sch) -> None:
    """BUS_JOBS 里缺的补建；已存在的一律不覆盖（保住页面上的暂停态与改过的调度）。"""
    from apscheduler.triggers.cron import CronTrigger
    for jd in BUS_JOBS:
        if sch.get_job(jd["id"]) is None:
            sch.add_job(_execute, CronTrigger.from_crontab(jd["schedule"]),
                        args=[dict(jd)], id=jd["id"], name=jd["name"],
                        replace_existing=False, max_instances=1)


def broken_reason() -> str:
    return _broken


def recent_state(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """读 ks_bus_state 的每任务最近一行（crons 读面用）。读不到 ⇒ 空 dict，不抛。"""
    if not ids:
        return {}
    try:
        with psycopg.connect(_conninfo()) as conn:
            conn.execute(_STATE_DDL)
            rows = conn.execute(
                "SELECT job_id, ran_at, ok, rc, detail FROM ks_bus_state "
                "WHERE job_id = ANY(%s)", (ids,)).fetchall()
        return {r[0]: {"ran_at": r[1].isoformat() if r[1] else None,
                       "ok": r[2], "rc": r[3], "detail": r[4]} for r in rows}
    except Exception as e:            # noqa: BLE001
        logger.warning("bus: 状态表读取失败: %s", e)
        return {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
