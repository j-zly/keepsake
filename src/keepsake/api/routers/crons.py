"""记忆类定时任务接口：读 Hermes 的 `~/.hermes/cron/jobs.json`，管理走 `hermes cron` CLI。

判据沿用 provider 自动注册的三条记忆任务（`KeepsakeProvider.CRON_JOBS`
的 name + script）—— 同一份「什么叫记忆类任务」口径，不在这里另起一套。

🔴 **不直接写 jobs.json**：暂停 / 恢复 / 改调度一律 subprocess 调官方
`hermes cron {pause,resume,edit}`（网关支持的外部变更通道，调度器自会跟上）。
argv 永远以**列表**传给 `subprocess.run`，**绝不拼 shell 字符串**：
job_id 只从 12 位十六进制里放行，schedule 另过一遍 cron 校验，两道闸门之外
不进子进程。本期不做 create / remove（删任务风险大，留给对话里人工）。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import audit, config
from ..deps import require_session
from ..schemas import CronActionBody, CronJob, CronList
from .ops import _need_flag, _reject, write_guard

router = APIRouter(tags=["crons"])

#: 兜底的「记忆类」关键词（provider 不可导入时用；与 CRON_JOBS 同源口径）
#: 「班车」/「tiering」覆盖分级班车：主脑手挂的「分级班车 dry 观察」不在
#: CRON_JOBS 里（**故意不登记**——provider 的 _ensure_cron_jobs 会在启动时把
#: CRON_JOBS 里不存在的任务**写进** jobs.json，而本包没有 src/keepsake/cron/
#: 脚本目录，登记只会在别的节点凭空注册一个指向空脚本的死任务），但它确实是
#: 记忆体系的一环，面板必须看得见。见 tasks: keepsake_cron_panel。
_FALLBACK_KEYS = ("memory", "记忆", "distill", "dedup", "synonym", "班车", "tiering")


def _memory_keys() -> Tuple[Set[str], Set[str]]:
    """记忆类任务的 (name 集合, script 集合)。"""
    names: Set[str] = set()
    scripts: Set[str] = set()
    try:
        from keepsake.adapters.hermes.provider import KeepsakeProvider

        for jdef in KeepsakeProvider.CRON_JOBS:
            names.add(str(jdef.get("name", "")))
            scripts.add(str(jdef.get("script", "")))
    except Exception:            # noqa: BLE001 —— provider 不可导入不致命，退关键词
        pass
    if not names:
        names = {"记忆提炼", "记忆维护", "记忆去重"}
        scripts = {"memory_distill.py", "memory-maintenance.py", "dedup-memory.sh"}
    return names, scripts


def _is_memory_job(job: Dict[str, Any], names: Set[str], scripts: Set[str]) -> bool:
    name = str(job.get("name", ""))
    script = str(job.get("script", "") or "")
    if name in names or script in scripts:
        return True
    hay = f"{name} {script}".lower()
    return any(k.lower() in hay for k in _FALLBACK_KEYS)


def _schedule_text(job: Dict[str, Any]) -> str:
    sch = job.get("schedule")
    if isinstance(sch, dict):
        return str(sch.get("display") or sch.get("expr") or "")
    return str(sch or job.get("schedule_display") or "")


# --------------------------------------------------------------------------
# hermes cron CLI（唯一的管理通道）
# --------------------------------------------------------------------------

#: job_id 只放行 12 位十六进制 —— jobs.json 里的任务 id 形状，也挡住路径 / 引号
#: 一类的手滑与注入（argv 是列表，但子进程仍可能把 id 当选项解析）。
_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_CLI = "hermes"
_CLI_TIMEOUT = 20          # 秒；CLI 卡住不许把控制台请求一起吊住
_CLI_OUT_MAX = 400         # 回给前端的 stdout 截断长度

#: 五段 cron 的字段取值范围（分 时 日 月 周；周日 0 与 7 都合法）
_CRON_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_ALIASES = {"@hourly", "@daily", "@midnight", "@weekly", "@monthly",
                 "@yearly", "@annually", "@reboot"}
_SCHEDULE_EXAMPLE = "30 4 * * *"


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
    """五段 cron 表达式（或 `@daily` 这类别名）校验。

    仓库里没有现成的 cron 校验器，也不值得为它引一个依赖：只认数字 / `*` /
    区间 / 步长 / 逗号列表，`0-7` 之外一律 400 回显 example。**宁可拒掉一个
    冷门写法，也不放行一个 CLI 会当成选项的东西**。
    """
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


def _last_line(text: str) -> str:
    """CLI 报错的人话来源：取最后一行非空输出（hermes 的错误都在末行）。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def run_cli(args: List[str]) -> Tuple[int, str, str]:
    """跑 `hermes <args...>`，返回 (rc, stdout, stderr)。

    🔴 **argv 一律列表、`shell=False`**：job_id / schedule 是外部输入，拼成
    字符串过一次 shell 就等于给了对方 `;`、`$()`、`--flag` 三条路。
    找不到 CLI ⇒ 503（人话：控制台这台机器管不了定时任务），超时 ⇒ 504。
    """
    try:
        p = subprocess.run([_CLI] + list(args), capture_output=True, text=True,
                           timeout=_CLI_TIMEOUT, shell=False)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail={
            "error": "hermes_cli_missing",
            "message": "本机无 hermes CLI，控制台无法管理定时任务"}) from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=504, detail={
            "error": "cron_cli_timeout",
            "message": f"hermes cron 超时（>{_CLI_TIMEOUT}s），没敢替它决定成功与否"}) from e
    except OSError as e:
        raise HTTPException(status_code=503, detail={
            "error": "hermes_cli_failed",
            "message": f"起 hermes CLI 失败：{getattr(e, 'strerror', None) or e.__class__.__name__}"}) from e
    return p.returncode, p.stdout or "", p.stderr or ""


def _need_job_id(job_id: str, action: str) -> str:
    """job_id 过 12 位十六进制闸；不合规 ⇒ 400 + 留痕（审计里截断，防伪造行）。"""
    got = (job_id or "").strip().lower()
    if not _JOB_ID_RE.match(got):
        _reject(400, "bad_job_id",
                "job_id 应是 12 位十六进制（例：a1b2c3d4e5f6）", action, got[:64])
    return got


def _job_id_of(job: Dict[str, Any]) -> str:
    """读面：从 jobs.json 里取任务 id。认不出 / 不合规 ⇒ 空串（该行保持只读）。"""
    for key in ("id", "job_id"):
        v = job.get(key)
        if isinstance(v, str) and _JOB_ID_RE.match(v.strip().lower()):
            return v.strip().lower()
    return ""


def _scheduler_status() -> Optional[str]:
    """面板顶上一行的调度器状态。CLI 不可用 / 超时 ⇒ **None**，不许把读面拖成 500。"""
    try:
        rc, out, err = run_cli(["cron", "status"])
    except HTTPException:
        return None
    text = out if rc == 0 else (err or out)
    return (_last_line(text)[:200] or None)


def _cron_action(job_id: str, body: CronActionBody, action: str,
                 schedule: str = "") -> Dict[str, Any]:
    """pause / resume / edit 的共同出口：闸门 → CLI → 审计 → 回包。

    `hermes cron edit` 的调度参数名按 `--schedule <expr>` 写死（任务书口径）。
    🟡 **88 上没有 hermes CLI，这三条命令的真实行为未经实测** —— 参数名不符时
    CLI 会 rc≠0，错误原文透传在 message 里，便于主脑照着改这一个常量。
    """
    act = f"cron.{action}"
    jid = _need_job_id(job_id, act)
    _need_flag(body.confirm, act, jid, f'必须显式传 {{"confirm": true}} 才{action}')

    argv = ["cron", action, jid]
    if action == "edit":
        argv += ["--schedule", schedule]
    try:
        rc, out, err = run_cli(argv)
    except HTTPException as e:
        # CLI 起不来也是「试图改了但没改成」，按 ops 的纪律留一行 ok=false
        audit.record(act, jid, ok=False, reason=str(e.detail.get("error", "")))
        raise

    if rc != 0:
        message = _last_line(err) or _last_line(out) or f"hermes cron {action} 退出码 {rc}"
        audit.record(act, jid, ok=False, reason=f"rc={rc}")
        raise HTTPException(status_code=500,
                            detail={"error": "cron_cli_failed", "message": message})
    # 改的是网关那份 jobs.json，本端点拿不到备份也改不了它 —— 只留操作账
    audit.record(act, jid, ok=True, reason=f"schedule={schedule}" if schedule else "")
    return {"ok": True, "job_id": jid, "action": action,
            "cli_output": out[:_CLI_OUT_MAX]}


@router.get("/api/v1/crons", response_model=CronList, dependencies=[Depends(require_session)])
def list_crons(
    all_jobs: bool = Query(False, description="true=不过滤，返回 jobs.json 里的全部任务"),
) -> CronList:
    """记忆类定时任务（name / last_run_at / next_run / last_status）。

    文件不存在 ⇒ 返回空列表（并回显 source 路径，便于定位「是没配还是没读到」）；
    文件存在但解析失败 / 读不了 ⇒ **500 + 结构化 detail**（不许装成「没有任务」）。
    """
    path = Path(config.cron_jobs_path())
    if not path.exists():
        return CronList(jobs=[], count=0, source=str(path))

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500,
                            detail={"error": "cron_read_failed",
                                    "message": f"读 {path} 失败：{e.__class__.__name__}"}) from e
    if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
        raise HTTPException(status_code=500,
                            detail={"error": "cron_bad_shape",
                                    "message": f"{path} 结构不是 {{jobs: [...]}}"})

    names, scripts = _memory_keys()
    raw: List[Dict[str, Any]] = [j for j in data.get("jobs", []) if isinstance(j, dict)]
    picked = raw if all_jobs else [j for j in raw if _is_memory_job(j, names, scripts)]
    jobs = [
        CronJob(
            name=str(j.get("name", "")),
            script=str(j.get("script", "") or ""),
            schedule=_schedule_text(j),
            enabled=bool(j.get("enabled", True)),
            last_run_at=(str(j["last_run_at"]) if j.get("last_run_at") else None),
            last_status=(str(j["last_status"]) if j.get("last_status") else None),
            # jobs.json 里有 next_run_at；缺键 / 非标量都退空串，前端 toLocal 出「—」
            next_run=(str(j["next_run_at"])
                      if isinstance(j.get("next_run_at"), (str, int, float)) else ""),
            job_id=_job_id_of(j),
        )
        for j in picked
    ]
    return CronList(jobs=jobs, count=len(jobs), source=str(path),
                    scheduler_status=_scheduler_status())


# --------------------------------------------------------------------------
# 管理（pause / resume / edit）—— 走 hermes cron CLI，不碰 jobs.json
# --------------------------------------------------------------------------

@router.post("/api/v1/crons/{job_id}/pause",
             dependencies=[Depends(require_session), Depends(write_guard)])
def pause_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """暂停任务（`hermes cron pause <id>`）：不再按计划执行，随时可恢复。"""
    return _cron_action(job_id, body, "pause")


@router.post("/api/v1/crons/{job_id}/resume",
             dependencies=[Depends(require_session), Depends(write_guard)])
def resume_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """恢复任务（`hermes cron resume <id>`）：按当前调度继续执行。"""
    return _cron_action(job_id, body, "resume")


@router.post("/api/v1/crons/{job_id}/edit",
             dependencies=[Depends(require_session), Depends(write_guard)])
def edit_cron(job_id: str, body: CronActionBody) -> Dict[str, Any]:
    """改调度（`hermes cron edit <id> --schedule <expr>`）。

    表达式先过 `valid_schedule`，非法 400 并回显 example —— 错表达式进 CLI
    只会被它自己拒，用户拿到的却是一句 CLI 错误原文；在这儿拦能直接给例子。
    """
    act = "cron.edit"
    jid = _need_job_id(job_id, act)
    schedule = (body.schedule or "").strip()
    if not valid_schedule(schedule):
        _reject(400, "bad_schedule",
                f"调度表达式不合法，例：{_SCHEDULE_EXAMPLE}（五段 cron 或 @daily 这类别名）",
                act, jid)
    return _cron_action(job_id, body, "edit", schedule=schedule)

