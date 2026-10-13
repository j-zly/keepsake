"""记忆类定时任务只读接口：读 Hermes 的 `~/.hermes/cron/jobs.json`。

判据沿用 provider 自动注册的三条记忆任务（`KeepsakeProvider.CRON_JOBS`
的 name + script）—— 同一份「什么叫记忆类任务」口径，不在这里另起一套。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import config
from ..deps import require_token
from ..schemas import CronJob, CronList

router = APIRouter(tags=["crons"])

#: 兜底的「记忆类」关键词（provider 不可导入时用；与 CRON_JOBS 同源口径）
_FALLBACK_KEYS = ("memory", "记忆", "distill", "dedup", "synonym")


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


@router.get("/api/v1/crons", response_model=CronList, dependencies=[Depends(require_token)])
def list_crons(
    all_jobs: bool = Query(False, description="true=不过滤，返回 jobs.json 里的全部任务"),
) -> CronList:
    """记忆类定时任务（name / last_run_at / last_status）。

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
        )
        for j in picked
    ]
    return CronList(jobs=jobs, count=len(jobs), source=str(path))
