"""记忆类定时任务接口（v2.2.0 起）：读本包 ``core.bus`` 的 APScheduler + PG 状态表。

历史：2026-11 之前这里是「读 Hermes jobs.json + subprocess 调 hermes cron CLI」，
班车的命挂在网关安装树上，升级事故实锤打断过（见 core/bus.py 头注）。方案 C 把
调度真值源迁进 keepsake 自己的 PG 后，**对外契约逐字不变**：``/api/v1/crons`` 的
字段形状、pause/resume/edit 的 confirm 纪律、非法输入的 400/503 口径全部保留。

2026-11-16（scheduler_page_v2）：班车模型从「代码 BUS_JOBS 单源」升级为
**数据驱动**（Quartz 式）—— PG jobstore 是运行期正源，``BUS_JOBS`` 退为首启种子。
管理面因此**全量开放**（页面归机主本人使用，授权边界由人管）：

* 增（POST /api/v1/crons）
* 删（DELETE /api/v1/crons/{id}，**写墓碑 ⇒ 重启不复活**）
* 改（POST /api/v1/crons/{id}/edit，全字段）
* 暂停 / 恢复（pause / resume）
* 立即运行一次（POST /api/v1/crons/{id}/trigger，一次性 DateTrigger，不建常驻 job）

字段形状的**校验与落地**收口在 ``core.bus.reconcile_register()`` 一处（API 层只
负责翻译错误码，不自己拼 job）；读面纪律不变：调度器 / 状态表读不到 ⇒ 相应字段
留空并把「为什么」放进 scheduler_status，**绝不把读失败装成「没有任务」**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit
from ..deps import require_session
from ..schemas import CronActionBody, CronJob, CronList, SchedulerStatus
from .ops import _need_flag, _reject, write_guard
from keepsake.core import bus

router = APIRouter(tags=["crons"])

#: 班车 id 的形状闸与 cron 表达式校验口径**都在 core.bus**（一把尺子：
#: API 层与调度落地共用，不许两处各写一份 —— 上一代两处各一份，改一处漏一处）。
_JOB_ID_RE = bus.JOB_ID_RE
valid_schedule = bus.valid_schedule      # 公开别名：这一把尺子的正主在 core.bus

#: 五段 cron 的示例（报错时回显，省得用户猜形状）
_SCHEDULE_EXAMPLE = "30 4 * * *"

#: job_id 形状不合时的话术（写端点与读端点同一句）
_JOB_ID_MSG = ("job_id 应是本包班车 id（形如 ks-distill；"
               "不是 Hermes 任务的 12 位十六进制）")


# --------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------

class CronRegisterBody(BaseModel):
    """``POST /api/v1/crons`` 的请求体（新建一条定时任务）。

    ``cmd`` 是**字符串列表**（每元素一条命令要素，如 ["/bin/sh", "/path/x.sh"]）——
    与执行体一直以来的 argv 口径一致（shell=False，不拼串）。
    """

    confirm: bool = False
    id: str = ""
    name: str = ""
    schedule: str = ""
    cmd: List[str] = Field(default_factory=list)
    timeout: int = 0


class CronEditBody(BaseModel):
    """``POST /api/v1/crons/{id}/edit`` 的请求体（全字段）。

    没传的字段**沿用现值**（不传的字段一律不改动）—— 改调度与改命令可以分开做。
    """

    confirm: bool = False
    name: Optional[str] = None
    schedule: Optional[str] = None
    cmd: Optional[List[str]] = None
    timeout: Optional[int] = None


class CronJobView(CronJob):
    """读面的一行：在既有字段之外补上**规格**（cmd / timeout）。

    编辑对话框要拿现值回填 —— 前端不许自己另存一份（那必然与调度器漂移）。
    """

    cmd: List[str] = Field(default_factory=list)
    timeout: int = 0


class CronListView(CronList):
    jobs: List[CronJobView]


# --------------------------------------------------------------------------
# 闸门（与旧版逐字同口径）
# --------------------------------------------------------------------------

def _need_job_id(job_id: str, action: str = "") -> str:
    """job_id 过形状闸；不合规 ⇒ 400（写端点还要留一行审计）。"""
    got = (job_id or "").strip().lower()
    if not _JOB_ID_RE.match(got):
        if action:
            _reject(400, "bad_job_id", _JOB_ID_MSG, action, got[:64])
        raise HTTPException(status_code=400,
                            detail={"error": "bad_job_id", "message": _JOB_ID_MSG})
    return got


def _live_scheduler(action: str, status: int = 503):
    """管理动作必须有活调度器；起不来 ⇒ 带原因报错，不装成操作成功。

    ``status`` 默认 503（调度器挂了 = 服务端不可用）；「立即运行」用 409
    （调度器停着 ≠ 任务不存在，是**状态冲突**，且产品要求这一条是 4xx）。
    """
    sch = bus.get_scheduler()
    if sch is None:
        raise HTTPException(status_code=status, detail={
            "error": "bus_scheduler_down",
            "message": f"班车调度器不可用（{bus.broken_reason() or '未启动'}），"
                       f"没敢替你做 {action}"})
    return sch


def _need_job(sch, job_id: str, action: str) -> Dict[str, Any]:
    """任务必须在**调度器 store 里**（PG jobstore 是运行期正源）；查不到 ⇒ 404。

    🔴 旧口径查的是 ``BUS_JOBS`` 代码表 —— 数据驱动之后那是「种子」，
    页面新建的任务不在表里，却实实在在在调度器里排着班。
    """
    job = sch.get_job(job_id)
    if job is None:
        _reject(404, "unknown_job",
                "调度器里没有这个任务（可能已经被删掉了）", action, job_id[:64])
    return bus.job_spec(job)


# --------------------------------------------------------------------------
# 读面
# --------------------------------------------------------------------------

@router.get("/api/v1/crons", response_model=CronListView,
            dependencies=[Depends(require_session)])
def list_crons(
    all_jobs: bool = Query(False, description="已废弃的兼容参数（班车表本来就全量）"),
) -> CronListView:
    """定时任务：定义（**调度器 store = 运行期正源**）× 调度态 × 上次结果（PG 状态表）。

    读面纪律不变：调度器 / 状态表读不到 ⇒ 相应字段留空并把「为什么」放进
    scheduler_status；**绝不把读失败装成「没有任务」**，也不许把读面拖成 500。
    """
    sch = bus.get_scheduler()
    jobs: List[CronJobView] = []
    nexts: List[str] = []

    if sch is not None:
        rows = []
        for job in sch.get_jobs():
            spec = bus.job_spec(job)
            nxt = getattr(job, "next_run_time", None)
            rows.append((spec, nxt))
        states = bus.recent_state([s["id"] for s, _ in rows])
        for spec, nxt in rows:
            enabled = nxt is not None
            if enabled:
                nexts.append(nxt.isoformat())
            st = states.get(spec["id"]) or {}
            jobs.append(_view_of(spec, enabled, nxt, st))
    else:
        # 调度器没起来 ⇒ 退回代码里的种子表（**不是**「没有任务」）：页面还能
        # 看见有哪些班车、只是全都跑不了（停摆红条在 scheduler_status 里说）。
        states = bus.recent_state([jd["id"] for jd in bus.BUS_JOBS])
        for jd in bus.BUS_JOBS:
            st = states.get(jd["id"]) or {}
            jobs.append(_view_of(jd, False, None, st))

    if sch is not None:
        sched_text = "APScheduler 运行中（console 进程内，PG jobstore）"
    else:
        sched_text = f"调度器停止：{bus.broken_reason() or '未启动'}"
    status = SchedulerStatus(text=sched_text, next_run=min(nexts) if nexts else "")

    return CronListView(jobs=jobs, count=len(jobs),
                        source="keepsake.core.bus (PG jobstore)",
                        scheduler_status=status)


def _view_of(spec: Dict[str, Any], enabled: bool, nxt, st: Dict[str, Any]) -> CronJobView:
    """一条任务 → 读面的一行（规格 + 调度态 + 上次结果）。"""
    cmd = list(spec.get("cmd") or [])
    return CronJobView(
        name=spec.get("name") or spec.get("id", ""),
        script=str(cmd[-1]) if cmd else "",
        schedule=spec.get("schedule", ""),
        cmd=cmd,
        timeout=int(spec.get("timeout") or 0),
        enabled=enabled,
        last_run_at=st.get("ran_at"),
        last_status=("ok" if st.get("ok") else f"failed(rc={st.get('rc')})")
                    if st else None,
        next_run=nxt.isoformat() if nxt else "",
        job_id=spec.get("id", ""),
    )


@router.get("/api/v1/crons/{job_id}/runs", dependencies=[Depends(require_session)])
def cron_runs(job_id: str) -> Dict[str, Any]:
    """某任务的**近 10 次执行记录**（只读）。

    口径：``ks_bus_run`` 流水（append-only）优先，一条都没有时退回
    ``ks_bus_state`` 的那一行（老任务只有「上次结果」）。读不到 ⇒ 空表，不抛。
    """
    jid = _need_job_id(job_id)
    runs = bus.recent_runs(jid, limit=10)
    return {"job_id": jid, "runs": runs, "count": len(runs)}


# --------------------------------------------------------------------------
# 管理（增 / 删 / 改 / 暂停 / 恢复 / 立即运行）
# --------------------------------------------------------------------------

@router.post("/api/v1/crons", dependencies=[Depends(require_session), Depends(write_guard)])
def create_cron(body: CronRegisterBody) -> Dict[str, Any]:
    """新建一条定时任务（落 PG jobstore ⇒ 重启后还在）。

    字段形状的校验与落地走 ``bus.reconcile_register()``（一处口径）；id 撞车
    ⇒ 409 明确说「已存在，去编辑」。
    """
    act = "cron.create"
    jid = _need_job_id(body.id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才新建')
    sch = _live_scheduler(act)
    if sch.get_job(jid) is not None:
        _reject(409, "job_exists",
                f"任务 {jid} 已存在（要改它请走「编辑」）", act, jid)
    spec = {"id": jid, "name": body.name, "schedule": body.schedule,
            "cmd": body.cmd, "timeout": body.timeout}
    job_def = _register_or_reject(sch, spec, act, jid)
    # 新建 = 撤销墓碑（删过的同一个 id 可以重新添加；墓碑挡的是「自动复活」）
    try:
        bus.drop_tombstone(jid)
    except Exception as e:            # noqa: BLE001 —— 撤墓碑失败只是提醒，不影响本次新建
        # 🔴 审计行只写异常**类型**（异常原文可能带连接串，审计不许落那种东西）
        audit.record(act, jid, ok=True,
                     reason=f"撤销墓碑失败（不影响本次新建）: {type(e).__name__}")
    audit.record(act, jid, ok=True,
                 reason=f"schedule={job_def['schedule']}; cmd={len(job_def['cmd'])}项")
    return {"ok": True, "job_id": jid, "action": "create",
            "cli_output": f"定时任务 {jid} 已创建（下次执行按新调度）。"}


@router.delete("/api/v1/crons/{job_id}",
               dependencies=[Depends(require_session), Depends(write_guard)])
def delete_cron(job_id: str) -> Dict[str, Any]:
    """删除一条定时任务（**永久生效**：写墓碑 ⇒ 重启不复活）。

    幂等：本来就不在（删过两遍 / 从没存在过）⇒ 照样回 ok。
    执行历史（``ks_bus_state`` / ``ks_bus_run``）**不删** —— 那是「跑过什么」的
    账，不是「现在有什么」的账。
    """
    act = "cron.delete"
    jid = _need_job_id(job_id, act)
    sch = _live_scheduler(act)
    # 🔴 **先写墓碑再删 job**：墓碑写不成功 ⇒ 任务留在原地（宁可不删，
    # 也不删出一件「重启就复活」的事）。
    try:
        bus.add_tombstone(jid)
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_tombstone_failed",
            "message": f"墓碑写不进去，没敢删（{e.__class__.__name__}: {e}）"})
    from apscheduler.jobstores.base import JobLookupError
    try:
        sch.remove_job(jid)
    except JobLookupError:
        pass                          # 幂等：本来就不在
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_delete_failed", "message": f"{e.__class__.__name__}: {e}"})
    audit.record(act, jid, ok=True, reason="已写墓碑（重启不复活）")
    return {"ok": True, "job_id": jid, "action": "delete",
            "cli_output": f"定时任务 {jid} 已删除（重启后不会复活）。"}


@router.post("/api/v1/crons/{job_id}/pause",
             dependencies=[Depends(require_session), Depends(write_guard)])
def pause_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """暂停班车（``pause_job``）：不再按计划执行，jobstore 里保留、随时可恢复。"""
    act = "cron.pause"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才pause')
    sch = _live_scheduler(act)
    _need_job(sch, jid, act)
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
    sch = _live_scheduler(act)
    _need_job(sch, jid, act)
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
def edit_cron(job_id: str, body: CronEditBody) -> Dict[str, Any]:
    """改一条定时任务的**全字段**（name / schedule / cmd / timeout）。

    没传的字段沿用现值；合并后的整份规格过 ``bus.reconcile_register()`` 落地
    （一处校验口径）。非法形状 ⇒ 400 并回显 example（在这儿拦能直接给例子，
    不用把调度器的报错原文透传给界面）。

    改完同步更新 ``BUS_JOBS`` 里同 id 的定义（进程内）：读面与调度面同一本账。
    """
    act = "cron.edit"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才edit')
    sch = _live_scheduler(act)
    cur = _need_job(sch, jid, act)
    spec = {
        "id": jid,
        "name": cur.get("name") if body.name is None else body.name,
        "schedule": cur.get("schedule") if body.schedule is None else body.schedule,
        "cmd": cur.get("cmd") if body.cmd is None else body.cmd,
        "timeout": cur.get("timeout") if body.timeout is None else body.timeout,
    }
    job_def = _register_or_reject(sch, spec, act, jid)
    for jd in bus.BUS_JOBS:           # 定义面与调度面同一本账（进程内）
        if jd["id"] == jid:
            jd.update({"name": job_def["name"], "schedule": job_def["schedule"],
                       "cmd": list(job_def["cmd"]), "timeout": job_def["timeout"]})
    audit.record(act, jid, ok=True,
                 reason=f"schedule={job_def['schedule']}; cmd={len(job_def['cmd'])}项; "
                        f"timeout={job_def['timeout']}; name={job_def['name']}")
    return {"ok": True, "job_id": jid, "action": "edit",
            "cli_output": f"定时任务 {jid} 已更新（调度：{job_def['schedule']}）。"}


@router.post("/api/v1/crons/{job_id}/trigger",
             dependencies=[Depends(require_session), Depends(write_guard)])
def trigger_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """**立即运行一次**（一次性 DateTrigger）：不新建常驻 job、跑完自动消失。

    执行照常落 ``ks_bus_state`` / ``ks_bus_run``（执行记录看得到）；不动墓碑
    （运行一次不是删除）。调度器停着 ⇒ 409（状态冲突，不是 404）。
    """
    act = "cron.trigger"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, '必须显式传 {"confirm": true} 才运行')
    sch = _live_scheduler(act, status=409)
    spec = _need_job(sch, jid, act)
    try:
        bus.trigger_once(sch, spec)
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_trigger_failed", "message": f"{e.__class__.__name__}: {e}"})
    audit.record(act, jid, ok=True, reason="一次性触发")
    return {"ok": True, "job_id": jid, "action": "trigger",
            "cli_output": f"定时任务 {jid} 已排入立即执行（跑完即消失）。"}


def _register_or_reject(sch, spec: Dict[str, Any], act: str, jid: str) -> Dict[str, Any]:
    """过 ``bus.reconcile_register()`` 落地；形状不合法 ⇒ 400（带该给的例子）。"""
    try:
        return bus.reconcile_register(sch, spec)
    except bus.JobSpecError as e:
        _reject(400, e.error, str(e), act, jid)
    except Exception as e:            # noqa: BLE001
        audit.record(act, jid, ok=False, reason=type(e).__name__)
        raise HTTPException(status_code=500, detail={
            "error": "cron_register_failed", "message": f"{e.__class__.__name__}: {e}"})
    raise AssertionError  # _reject 恒抛，此行不可达
