"""记忆类定时任务接口（v2.2.0 起）：读本包 ``core.bus`` 的 APScheduler + PG 状态表。

历史：2026-11 之前这里是「读 Hermes jobs.json + subprocess 调 hermes cron CLI」，
班车的命挂在网关安装树上，升级事故实锤打断过（见 core/bus.py 头注）。方案 C 把
调度真值源迁进 keepsake 自己的 PG 后，**对外契约逐字不变**：``/api/v1/crons`` 的
字段形状、pause/resume/edit 的 confirm 纪律、非法输入的 400/503 口径全部保留，
前端零改动。

管理动作是对调度器对象的直接调用（``pause_job / resume_job / reschedule_job``），
不再有子进程 —— 原来的 argv 注入闸门随之退役，但 **job_id 形状闸保留**（挡住拿
别的系统 id 来戳的旧书签），edit 的表达式仍必须先过 ``valid_schedule``：宁可拒掉
冷门写法，不让错表达式进调度器。

本期不做 create / remove：班车表是代码里的 BUS_JOBS（评审过的 argv），页面上增删
任务 = 在服务器执行任意命令的入口，不开。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import audit
from ..deps import require_session
from ..schemas import CronActionBody, CronJob, CronList, SchedulerStatus
from .ops import _need_flag, _reject, write_guard
from keepsake.core import bus

router = APIRouter(tags=["crons"])

#: 本包班车 id 的形状（core/bus.py 的 BUS_JOBS 用同一形状）；同时挡掉
#: 「拿 Hermes 的 12 位十六进制 id 来戳新后端」的旧书签 / 脚本残留。
_JOB_ID_RE = re.compile(r"^ks-[a-z][a-z0-9-]{1,30}$")

#: 五段 cron 的字段取值范围（分 时 日 月 周；周日 0 与 7 都合法）
_CRON_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_ALIASES = {"@hourly", "@daily", "@midnight", "@weekly", "@monthly",
                 "@yearly", "@annually"}
_SCHEDULE_EXAMPLE = "30 4 * * *"

#: `@daily` 这类别名 → 五段正身（APScheduler 的 from_crontab 只吃五段，
#: 而 valid_schedule 沿用旧口径放行别名 —— 在进调度器前由我们翻译）。
_ALIAS_5F = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 1 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _to_aps_expr(s: str) -> str:
    """把过了 valid_schedule 的表达式归一成 APScheduler 3.x 能吃的形态。

    两条归一：① 别名展开成五段；② 周字段**单独一个 7**（周日）换成 0 ——
    3.x 的 parser 顶格 6，而 crontab 传统里 0 和 7 都是周日。`5-7` 这种
    含 7 的区间/列表它两边都不肯老实认，**保守 400 指路用 0-6 或 sun**，
    不猜语义（宁拒冷门写法，不放行一个会被调度器静默改写的）。
    """
    s = s.strip()
    if s.lower() in _ALIAS_5F:
        return _ALIAS_5F[s.lower()]
    parts = s.split()
    if len(parts) == 5 and "7" in parts[4]:
        if parts[4] == "7":
            parts[4] = "0"
        else:
            raise ValueError("周字段里的 7 只认单独一个（=周日）；"
                             "区间/列表请用 0-6 或 sun/mon 这类名字")
    return " ".join(parts)


def _valid_cron_field(tok: str, lo: int, hi: int) -> bool:
    """一个字段（含 `*` / `a-b` / `*/n` / 逗号列表）合不合法。"""
    tok = tok.strip()
    if not tok:
        return False
    if "/" in tok:                      # 步长：*/5、0-30/2 都行
        tok, _, step = tok.partition("/")
        if not step.isdigit() or int(step) < 1:
            return False
    if tok == "*":
        return True
    if "-" in tok:                      # 区间：1-5（月份/周日的越界一并挡住）
        a, _, b = tok.partition("-")
        if not (a.isdigit() and b.isdigit()):
            return False
        return lo <= int(a) <= hi and lo <= int(b) <= hi
    return tok.isdigit() and lo <= int(tok) <= hi


def valid_schedule(s: str) -> bool:
    """五段 cron 表达式（或 `@daily` 这类别名）校验，口径与旧版逐字一致。"""
    s = (s or "").strip()
    if not s:
        return False
    if s.lower() in _CRON_ALIASES:
        return True
    parts = s.split()
    if len(parts) != len(_CRON_FIELDS):
        return False
    for part, (lo, hi) in zip(parts, _CRON_FIELDS):
        if not all(_valid_cron_field(t, lo, hi) for t in part.split(",")):
            return False
    return True


def _need_job_id(job_id: str, action: str) -> str:
    """job_id 过形状闸；不合规 ⇒ 400 + 留痕（审计里截断，防伪造行）。"""
    got = (job_id or "").strip().lower()
    if not _JOB_ID_RE.match(got):
        _reject(400, "bad_job_id",
                "job_id 应是本包班车 id（形如 ks-distill；不是 Hermes 任务的 12 位十六进制）",
                action, got[:64])
    return got


def _live_scheduler(action: str):
    """管理动作必须有活调度器；起不来 ⇒ 503 带原因，不装成操作成功。"""
    sch = bus.get_scheduler()
    if sch is None:
        raise HTTPException(status_code=503, detail={
            "error": "bus_scheduler_down",
            "message": f"班车调度器不可用（{bus.broken_reason() or '未启动'}），"
                       f"没敢替你做 {action}"})
    return sch


def _job_def(job_id: str, action: str) -> Dict[str, Any]:
    """班车必须在 BUS_JOBS 表内；查不到 ⇒ 404（配合 _sync_jobs 语义：表外无任务）。"""
    for jd in bus.BUS_JOBS:
        if jd["id"] == job_id:
            return jd
    _reject(404, "unknown_job",
            "班车表里没有这个 id（可管：ks-distill / ks-maintenance / "
            "ks-tiering / ks-synonyms）", action, job_id[:64])
    raise AssertionError  # _reject 恒抛，此行不可达


@router.get("/api/v1/crons", response_model=CronList, dependencies=[Depends(require_session)])
def list_crons(
    all_jobs: bool = Query(False, description="已废弃的兼容参数（班车表本来就全量）"),
) -> CronList:
    """记忆类定时任务：定义（BUS_JOBS）× 调度态（APScheduler）× 上次结果（PG 状态表）。

    读面纪律不变：调度器 / 状态表读不到 ⇒ 相应字段留空并把「为什么」放进
    scheduler_status；**绝不把读失败装成「没有任务」**，也不许把读面拖成 500。
    """
    sch = bus.get_scheduler()
    states = bus.recent_state([jd["id"] for jd in bus.BUS_JOBS])

    jobs: List[CronJob] = []
    nexts: List[str] = []
    for jd in bus.BUS_JOBS:
        job = sch.get_job(jd["id"]) if sch is not None else None
        nxt = ""
        enabled = False
        if job is not None:
            enabled = getattr(job, "next_run_time", None) is not None
            if enabled:
                nxt = job.next_run_time.isoformat()
                nexts.append(nxt)
        st = states.get(jd["id"]) or {}
        jobs.append(CronJob(
            name=jd["name"],
            script=str(jd["cmd"][-1]),
            schedule=jd["schedule"],
            enabled=enabled,
            last_run_at=st.get("ran_at"),
            last_status=("ok" if st.get("ok") else f"failed(rc={st.get('rc')})")
                        if st else None,
            next_run=nxt,
            job_id=jd["id"],
        ))

    if sch is not None:
        sched_text = "APScheduler 运行中（console 进程内，PG jobstore）"
    else:
        sched_text = f"调度器停止：{bus.broken_reason() or '未启动'}"
    status = SchedulerStatus(text=sched_text, next_run=min(nexts) if nexts else "")

    return CronList(jobs=jobs, count=len(jobs),
                    source="keepsake.core.bus (PG jobstore)",
                    scheduler_status=status)


# --------------------------------------------------------------------------
# 管理（pause / resume / edit）—— 直接操作调度器对象
# --------------------------------------------------------------------------

@router.post("/api/v1/crons/{job_id}/pause",
             dependencies=[Depends(require_session), Depends(write_guard)])
def pause_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """暂停班车（``pause_job``）：不再按计划执行，jobstore 里保留、随时可恢复。"""
    act = "cron.pause"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才pause')
    _job_def(jid, act)
    sch = _live_scheduler(act)
    try:
        sch.pause_job(jid)
    except HTTPException:
        raise
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_pause_failed", "message": f"{e.__class__.__name__}: {e}"})
    audit.record(act, jid, ok=True)
    return {"ok": True, "job_id": jid, "action": "pause",
            "cli_output": f"班车 {jid} 已暂停（jobstore 保留，恢复即续）。"}


@router.post("/api/v1/crons/{job_id}/resume",
             dependencies=[Depends(require_session), Depends(write_guard)])
def resume_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """恢复班车（``resume_job``）：按当前调度继续；错过窗口由 coalesce 只补一次。"""
    act = "cron.resume"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才resume')
    _job_def(jid, act)
    sch = _live_scheduler(act)
    try:
        sch.resume_job(jid)
    except HTTPException:
        raise
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_resume_failed", "message": f"{e.__class__.__name__}: {e}"})
    audit.record(act, jid, ok=True)
    return {"ok": True, "job_id": jid, "action": "resume",
            "cli_output": f"班车 {jid} 已恢复调度。"}


@router.post("/api/v1/crons/{job_id}/edit",
             dependencies=[Depends(require_session), Depends(write_guard)])
def edit_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """改调度（``reschedule_job``）。

    表达式先过 `valid_schedule`，非法 400 并回显 example —— 在这儿拦能直接给例子，
    不用把调度器的报错原文透传给界面。改成功同步更新 BUS_JOBS 定义字典：读面立即
    反映新表达式，console 重启后 _sync_jobs 也不会把旧表达式冲回来。
    """
    act = "cron.edit"
    jid = _need_job_id(job_id, act)
    schedule = (body.schedule or "").strip()
    if not valid_schedule(schedule):
        _reject(400, "bad_schedule",
                f"调度表达式不合法，例：{_SCHEDULE_EXAMPLE}（五段 cron 或 @daily 这类别名）",
                act, jid)
    jd = _job_def(jid, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才edit')
    sch = _live_scheduler(act)
    try:
        from apscheduler.triggers.cron import CronTrigger
        aps_expr = _to_aps_expr(schedule)
    except ValueError as e:
        _reject(400, "bad_schedule", f"{e}（例：{_SCHEDULE_EXAMPLE}）", act, jid)
        return {}                      # _reject 恒抛，此行不可达
    try:
        sch.reschedule_job(jid, trigger=CronTrigger.from_crontab(aps_expr))
    except HTTPException:
        raise
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_edit_failed", "message": f"{e.__class__.__name__}: {e}"})
    jd["schedule"] = schedule          # 定义面与调度面同一本账（进程内）
    audit.record(act, jid, ok=True, reason=f"schedule={schedule}")
    return {"ok": True, "job_id": jid, "action": "edit",
            "cli_output": f"班车 {jid} 调度已改为：{schedule}"}
