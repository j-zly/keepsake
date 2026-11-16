"""记忆班车调度器（v2.2.0，方案 C）：APScheduler + PG jobstore，嵌在 console 进程里。

背景（2026-11-16 复盘）：班车原来挂在 Hermes 网关的 cron 上，网关重启 / 安装树整树
替换 / worker 解释器漂移三次实锤把班车打断过（升级事故 + 维护班车两连红）。本模块
把**调度真值源**迁到 keepsake 自己的 PG：

- 任务定义与持久化状态（下次运行 / 暂停态）= PG 里的 APScheduler jobstore 表；
- 上次运行结果 = ``ks_bus_state`` 表（3.x 的 jobstore 不存「上次跑过没有」，自记一张）；
- jobs.json 从此与本系统无关 —— provider 侧的自动注册（``_ensure_cron_jobs``）同批下线，
  那正是「记忆去重」僵尸每次复活换 id 的出生地（``uuid4().hex[:12]``）。

三条铁律：
1. **执行体只调现成的运行手册，绝不内联重写业务逻辑**——班车是被反复打磨过的
   运行手册（tiering 的红报、distill 的窗口语义），调度器只管「何时跑、成没成」。
   2026-11-16（bus_inprocess_pure_code）：运行手册的本体收编进仓
   （``keepsake.buses.*``），调度器**同进程**调它的 ``run()``；旧形态
   （``cmd`` = argv 列表 + 起子进程）**保留兼容**（编辑面兜底）。
2. **解释器钉死树外绝对路径**（/opt/keepsake-venv、/bin/sh），Hermes 安装树升级永远
   够不着这里 —— 这条只对**旧形态（cmd）**成立：新形态（func）跑在本进程里，
   压根不经过解释器。
3. **调度器起不来只降级不抛穿**：PG 失联时 console 照常服务，crons 读面显示
   「调度器停止」，绝不为难页面（与 crons.py 的「读面不许 500」同源纪律）。

misfire 口径：``coalesce=True`` + 宽限期 1 小时 —— console 重启错过几个 tick 只补跑
一次（提炼 / 维护都是幂等窗口语义，多烧没有意义）。

2026-11-16 追加（scheduler_page_v2）：班车模型从「代码 BUS_JOBS 单源」升级为
**数据驱动**（Quartz 式）：

- **PG jobstore 是运行期正源**（增 / 删 / 改都落它，重启后还在）；
  ``BUS_JOBS`` 降级为**首次种子**（只在 store 里没有这个 id 时补建）；
- **删除必须永久生效**：删掉的 id 写进 ``ks_bus_tombstone`` 墓碑表，
  种子同步时看见墓碑就不补（重启不复活）；
- 任务的**字段形状校验与落地**收口在 ``reconcile_register()`` 一处
  （API 层只管翻译错误码，不自己拼 job）。
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus

import psycopg

logger = logging.getLogger("keepsake.bus")

#: 与 Hermes 时代逐字对齐的班车表（schedule 用五段 cron，Asia/Shanghai 口径）。
#: id 是本包的稳定主键：改名 = 重建任务（暂停态会丢），只增不改。
#:
#: 2026-11-16 追加（scheduler_page_ui_fix）：四条班车的 **name** 统一成中文
#: （页面可见文案；旧名 memory-maintenance / synonym-discovery-daily 是英文代号，
#: 摆在定时任务页上认不出是哪条车）。
#: 🔴 **id / schedule / timeout 逐字不动** —— 动 id 等于换一条车（墓碑都拦不住）。
#:
#: 2026-11-16 追加（bus_inprocess_pure_code）：**执行方式从壳脚本换成函数注册**
#: （Quartz 的 JobType 式）：``cmd``（argv 列表 + 起子进程）换成 ``func``
#: （``"模块:可调用"``，**同进程**调用）。壳脚本是 Hermes cron 时代的补丁
#: （当时解释器环境不可控），现在调度器住 console 进程内，那个理由已消失。
#: 四条班车的业务本体因此收编进仓（``keepsake.buses.*``）。
#: 🔴 **cmd 形态仍然支持**（编辑面兜底：老任务 / 外部脚本继续能注册）。
#:
#: 迁移说明（**由主脑在生产侧执行，本包只负责种子**）：
#: `_sync_jobs` 只补「store 里没有的 id」，**不覆盖已存在的任务**（保住暂停态），
#: 所以现网 PG jobstore 里那四行还挂着旧 name / 旧 cmd —— 种子同步不会自己改。
#: 主脑收编后需：清掉 PG jobstore 里这四条常驻行（**不留墓碑**）→ 重启 console
#: → `_sync_jobs` 按新种子重建，name / func 即新值（不留墓碑 ⇒ 不影响
#: 「删除永久生效」）。种子侧的重建口径由 tests/api/test_cron_manage.py 钉死。
BUS_JOBS: List[Dict[str, Any]] = [
    {
        "id": "ks-distill",
        "name": "记忆提炼",
        "schedule": "15 * * * *",
        "func": "keepsake.buses.distill:run",
        "timeout": 1800,
    },
    {
        "id": "ks-maintenance",
        "name": "合并 · 遗忘（维护）",
        "schedule": "0 */2 * * *",
        "func": "keepsake.buses.maintenance:run",
        "timeout": 1800,
    },
    {
        "id": "ks-tiering",
        "name": "记忆分级班车",
        "schedule": "30 4 * * *",
        "func": "keepsake.buses.tiering:run",
        "timeout": 900,
    },
    {
        "id": "ks-synonyms",
        "name": "同义词总线",
        "schedule": "0 */8 * * *",
        "func": "keepsake.buses.synonyms:run",
        "timeout": 3600,
    },
]

#: 调度器构造的单点口径（生产 get_scheduler 与测试夹具共用同一份 —— 防止
#: 「store 里躺着 grace=1 的陈旧形状 job 而默认值写在生产代码里」再复发一次，
#: 2026-11-16 实踩：16:00 班被 dead socket 拖晚 10 秒、旧 grace=1 直接丢班）。
SCHED_DEFAULTS = {"coalesce": True, "misfire_grace_time": 3600}

#: timeout 的**单点默认值**（秒）：读面补默认（存量 job 没有 timeout 字段时）
#: 与执行路径缺省用的是同一个值 —— 补 0 会让编辑面拿到 0（编辑校验要求正整数）
#: ⇒ 存量 job 一编辑就 400；执行路径缺省又按 1800 跑，三处口径必须一致。
DEFAULT_TIMEOUT = 1800

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

#: 墓碑表：被删掉的任务 id（删除必须永久生效 —— 重启不复活）。
#: 建表路子沿用 ks_bus_state（同一张 PG、同一套 DDL 写法）。
_TOMBSTONE_DDL = """
CREATE TABLE IF NOT EXISTS ks_bus_tombstone (
    job_id     text PRIMARY KEY,
    removed_at timestamptz NOT NULL
)
"""

#: 执行历史（append-only）：``ks_bus_state`` 是「每任务最近一行」的 upsert 表
#: （读面要的是「上次跑成没有」），**近 10 次的流水**另存这张表（不覆盖）。
_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS ks_bus_run (
    id     bigserial PRIMARY KEY,
    job_id text NOT NULL,
    ran_at timestamptz NOT NULL,
    ok     boolean NOT NULL,
    rc     integer,
    detail text
)
"""

# --------------------------------------------------------------------------
# 字段形状闸（**一把尺子**：API 层与调度落地共用，不许两处各写一份）
# --------------------------------------------------------------------------

#: 本包班车 id 的形状：ks- 前缀 + 小写字母开头的短横串（BUS_JOBS 同一形状）。
#: 同时挡掉「拿 Hermes 的 12 位十六进制 id 来戳新后端」的旧书签 / 脚本残留。
JOB_ID_RE = re.compile(r"^ks-[a-z][a-z0-9-]{1,30}$")

#: 五段 cron 的字段取值范围（分 时 日 月 周；周日 0 与 7 都合法）
_CRON_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_ALIASES = {"@hourly", "@daily", "@midnight", "@weekly", "@monthly",
                 "@yearly", "@annually"}

#: `@daily` 这类别名 → 五段正身（APScheduler 的 from_crontab 只吃五段）。
_ALIAS_5F = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 1 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _to_aps_expr(s: str) -> str:
    """把过了 valid_schedule 的表达式归一成 APScheduler 3.x 能吃的形态。

    两条归一：① 别名展开成五段；② 周字段**单独一个 7**（周日）换成 0 ——
    3.x 的 parser 顶格 6，而 crontab 传统里 0 和 7 都是周日。`5-7` 这种
    含 7 的区间/列表它两边都不肯老实认，**保守报错指路用 0-6 或 sun**，
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


class JobSpecError(ValueError):
    """任务定义不合法（字段形状没过）。

    ``error`` 是给 API 层的错误码（bad_schedule / bad_job_spec / bad_job_id），
    ``str(self)`` 是给人看的话（带例子）—— API 层只负责翻译，不自己判形状。
    """

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error


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

def _workdir() -> Optional[str]:
    """执行时的工作目录：生产恒为 /root（班车脚本都在那儿）。

    ponytail: 硬写死 ``cwd="/root"`` 在非 root 账号下会直接 PermissionError ——
    整条班车起不来（测试机 / 降级场景都踩过），而「脚本找不到」比「进程起不来」
    好诊断得多。/root 不可用时退回进程 cwd（None = 不改工作目录）。
    """
    if os.path.isdir("/root") and os.access("/root", os.W_OK):
        return "/root"
    return None


def _resolve_func(ref: str):
    """``"模块:可调用"`` → 可调用对象（importlib 解析）。

    解析不了（模块不存在 / 属性不存在 / 不可调用）⇒ 抛 —— 注册面据此回 4xx，
    执行面据此落 ok=False（**绝不静默当「跑成功但啥也没干」**）。
    不缓存解析结果：模块可能被热改，缓存会拿到旧函数。
    """
    mod_name, _, attr = str(ref or "").partition(":")
    if not mod_name or not attr:
        raise ValueError(f"func 形状不合法（须形如 模块:函数）：{ref!r}")
    module = importlib.import_module(mod_name)
    fn = getattr(module, attr, None)
    if fn is None:
        raise ValueError(f"func 指向的 {mod_name}:{attr} 不存在")
    if not callable(fn):
        raise ValueError(f"func 指向的 {mod_name}:{attr} 不可调用")
    return fn


def _run_func(job_def: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """in-process 跑一条班车 → ``(ok, 摘要, rc)``。

    契约：被调函数返回 ``(ok, 人读摘要)``（见 ``keepsake.buses`` 包头注）。
    **异常照抛**（这里接住并转成 ok=False —— 落库口径与 cmd 形态逐字同构：
    ok / rc / detail 三件套，一个字段都不多不少）。

    超时口径：函数跑在**独立线程**里、主线程 ``join(timeout)`` —— 超时记
    ok=False + rc=124（与 cmd 形态的 TimeoutExpired 同一口径）。
    ponytail: 线程**杀不掉**（Python 没有安全的强杀），超时后那个线程仍会
    跑完 ⇒ 班车本体必须幂等（提炼 / 分级 / 同义都是幂等窗口语义）。
    """
    timeout = int(job_def.get("timeout", DEFAULT_TIMEOUT))
    fn = _resolve_func(job_def.get("func", ""))
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:      # noqa: BLE001 —— 连 BaseException 都接住
            box["error"] = e

    started = time.monotonic()
    worker = threading.Thread(target=_target, daemon=True,
                              name=f"bus-func-{job_def.get('id', '?')}")
    worker.start()
    worker.join(timeout)
    elapsed = time.monotonic() - started
    if worker.is_alive():
        return False, f"超时被杀（>{timeout}s，实际 {elapsed:.1f}s）", 124
    err = box.get("error")
    if err is not None:
        return False, f"跑失败：{type(err).__name__}: {err}"[:500], 1
    result = box.get("result")
    if isinstance(result, tuple) and len(result) == 2:
        ok, summary = result
        return bool(ok), str(summary)[:500], (0 if ok else 1)
    # 契约外的返回形状：按「成功 + 原样摘要」兜底（不许把一个无害的返回
    # 判成红 —— 那会让值班的人白跑一趟）。
    return True, ("" if result is None else str(result))[:500], 0


def _execute(job_def: Dict[str, Any]) -> None:
    """跑一条班车，结果落 ks_bus_state。

    **双形态**（2026-11-16 bus_inprocess_pure_code）：

    * ``func`` 形态（**新形态**）：``"模块:可调用"``，**同进程**调用 ——
      调度器与业务同处 console 进程，不再起子进程；
    * ``cmd`` 形态（**旧形态，保留兼容**）：argv 列表 + shell=False + 绝对
      解释器（编辑面兜底：外部脚本、老任务继续能注册）。

    两种形态落库的字段**逐字同构**（ok / rc / detail），执行记录页与维护页
    读起来完全一样。

    失败不抛（APScheduler 会把异常吞进 logger，值守看不见）；统一转成
    ok=False + 末行摘要，页面与排查看一张表就够。

    两张表各写一行：``ks_bus_state`` 是「每任务最近一行」（upsert，读面要的是
    「上次跑成没有」），``ks_bus_run`` 是 append-only 的流水（近 10 次执行记录）。
    """
    rc: Optional[int] = None
    ok = False
    detail = ""
    try:
        if job_def.get("func"):
            ok, detail, rc = _run_func(job_def)
        else:
            p = subprocess.run(job_def["cmd"], capture_output=True, text=True,
                               timeout=job_def.get("timeout", DEFAULT_TIMEOUT),
                               shell=False, cwd=_workdir())
            rc = p.returncode
            ok = (rc == 0)
            blob = (p.stdout if ok else (p.stderr or p.stdout)) or ""
            lines = [ln for ln in blob.splitlines() if ln.strip()]
            detail = (lines[-1] if lines else "")[:500]
    except subprocess.TimeoutExpired:
        detail = f"超时被杀（>{job_def.get('timeout', DEFAULT_TIMEOUT)}s）"
    except OSError as e:
        detail = f"起进程失败：{e.__class__.__name__}: {e}"[:500]
    except Exception as e:            # noqa: BLE001 —— func 形态的 import 失败等
        detail = f"跑失败：{e.__class__.__name__}: {e}"[:500]
    if not ok:
        logger.error("bus: %s 跑红 rc=%s detail=%s", job_def.get("id"), rc, detail[:200])
    # 🔴 两张表**分开两个事务写**：history 建表/写入要是失败（权限不够、老库没这表），
    # 不许把「上次跑成没有」那一行也赔进去（PG 里一个事务出错会整笔回滚）。
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
    try:
        with psycopg.connect(_conninfo()) as conn:
            conn.execute(_HISTORY_DDL)
            conn.execute(
                "INSERT INTO ks_bus_run(job_id, ran_at, ok, rc, detail) "
                "VALUES (%s, now(), %s, %s, %s)",
                (job_def.get("id", "?"), ok, rc, detail))
    except Exception as e:            # noqa: BLE001
        logger.warning("bus: ks_bus_run 写入失败: %s", e)


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
            from sqlalchemy import create_engine
            # pool_pre_ping：PG 侧 keepsake 角色挂着 idle_session_timeout（11-14 泄漏止血），
            # jobstore 的长空闲连接会被服务端掐 —— 不预探测就会在发班时刻报 dead socket、
            # 拖到下一轮才成功（2026-11-16 实踩：16:00 班因此晚 10s，被旧 grace=1 判 misfire 丢班）。
            engine = create_engine(_jobstore_url(), pool_pre_ping=True)
            sch = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(engine=engine)},
                job_defaults=dict(SCHED_DEFAULTS),
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
    """BUS_JOBS（**首次种子**）里缺的补建；已存在的一律不覆盖（保住页面上的暂停态
    与改过的调度）；**墓碑里的绝不补**（删过的任务重启不许复活）。

    🔴 墓碑读不出来 ⇒ **本轮一个都不补**（fail-closed）：宁可少建（页面少一行），
    也不把用户明确删掉的任务复活 —— 「删除必须永久生效」是产品红线。
    """
    from apscheduler.triggers.cron import CronTrigger
    try:
        dead = tombstone_ids()
    except Exception as e:        # noqa: BLE001 —— 读不到墓碑 = 不敢补
        logger.error("bus: 墓碑表读不出来，本轮不补种子（宁可不建也不复活已删任务）: %s", e)
        return
    tz = getattr(sch, "timezone", None)
    for jd in BUS_JOBS:
        if jd["id"] in dead:
            continue
        if sch.get_job(jd["id"]) is None:
            sch.add_job(_execute,
                        CronTrigger.from_crontab(jd["schedule"], timezone=tz),
                        args=[dict(jd)], id=jd["id"], name=jd["name"],
                        replace_existing=False, max_instances=1,
                        misfire_grace_time=SCHED_DEFAULTS["misfire_grace_time"],
                        coalesce=SCHED_DEFAULTS["coalesce"])


def reconcile_register(sch, spec: Dict[str, Any]) -> Dict[str, Any]:
    """把一份任务定义校验后**落到调度器**（新增与修改共用这一处）。

    字段口径（越界即抛 JobSpecError，由调用方翻译成 4xx —— API 层不自己拼 job）：
      id       —— ``ks-`` 前缀形状（JOB_ID_RE，与 API 层同一把尺子）
      name     —— 非空（空则退回 id）
      schedule —— 五段 cron（valid_schedule 校验 + _to_aps_expr 归一）
      执行方式 —— **cmd / func 二选一必填其一**（一条班车只认一种执行方式）：
                   * ``func``：``"模块:可调用"`` 字符串，**必须可 import 且
                     callable**（注册时就验 —— 存一条跑不起来的任务等于骗人）；
                   * ``cmd``：字符串列表，逐元素非空（argv 列表，shell=False）。
      timeout  —— 正整数秒
    misfire_grace / coalesce 一律取 ``SCHED_DEFAULTS``（单点口径：构造口径不许分叉）。

    已存在的任务是**改**（modify_job）：改调度时**保住暂停态**（暂停着的任务改完
    仍是暂停的 —— 改表达式不该顺手把它复活）。
    """
    jid = str(spec.get("id") or "").strip().lower()
    if not JOB_ID_RE.match(jid):
        raise JobSpecError("bad_job_id", f"job_id 形状不合法（须形如 ks-xxx）：{jid[:64]}")
    name = str(spec.get("name") or "").strip() or jid
    schedule = str(spec.get("schedule") or "").strip()
    if not valid_schedule(schedule):
        raise JobSpecError("bad_schedule",
                           f"调度表达式不合法，例：30 4 * * *"
                           f"（五段 cron 或 @daily 这类别名）")
    try:
        aps_expr = _to_aps_expr(schedule)  # 周字段里的 7 只认单独一个 → 这里抛
    except ValueError as e:
        raise JobSpecError("bad_schedule", f"{e}（例：30 4 * * *）") from e
    func = spec.get("func")
    cmd = spec.get("cmd")
    func_ref = func.strip() if isinstance(func, str) else ""
    if func_ref and cmd:
        raise JobSpecError("bad_job_spec",
                           "cmd 与 func 二选一（一条班车只认一种执行方式）")
    if cmd is not None and not isinstance(cmd, list):
        raise JobSpecError("bad_job_spec", "cmd 必须是非空的字符串列表（每行一条命令要素）")
    if not func_ref and not cmd:
        raise JobSpecError("bad_job_spec",
                           "cmd 或 func 二选一必填其一（执行方式不能空）")
    timeout = spec.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise JobSpecError("bad_job_spec", "timeout 必须是正整数秒")
    if func_ref:
        # 🔴 func 必须**当场验**：能 import 且 callable —— 存一条跑不起来的
        # 任务等于骗人（页面上看着有班车，到点一声不吭）。
        try:
            _resolve_func(func_ref)
        except Exception as e:        # noqa: BLE001 —— 任何解析失败都回 4xx
            raise JobSpecError("bad_job_spec",
                               f"func 不可用（{type(e).__name__}: {e}）") from e
        job_def = {"id": jid, "name": name, "schedule": schedule,
                   "func": func_ref, "timeout": int(timeout)}
    else:
        cmd = [str(c) for c in cmd]
        if not all(c.strip() for c in cmd):
            raise JobSpecError("bad_job_spec", "cmd 里有空元素（每条命令要素都得非空）")
        job_def = {"id": jid, "name": name, "schedule": schedule,
                   "cmd": cmd, "timeout": int(timeout)}

    from apscheduler.triggers.cron import CronTrigger
    tz = getattr(sch, "timezone", None)
    trigger = CronTrigger.from_crontab(aps_expr, timezone=tz)
    old = sch.get_job(jid)
    if old is None:
        sch.add_job(_execute, trigger, args=[job_def], id=jid, name=name,
                    replace_existing=False, max_instances=1,
                    misfire_grace_time=SCHED_DEFAULTS["misfire_grace_time"],
                    coalesce=SCHED_DEFAULTS["coalesce"])
    else:
        paused = old.next_run_time is None
        nxt = None if paused else trigger.get_next_fire_time(None, datetime.now(tz))
        sch.modify_job(jid, name=name, trigger=trigger, args=[job_def],
                       misfire_grace_time=SCHED_DEFAULTS["misfire_grace_time"],
                       coalesce=SCHED_DEFAULTS["coalesce"], next_run_time=nxt)
    return job_def


def job_spec(job) -> Dict[str, Any]:
    """调度器里的一个 job → 规格 dict（id/name/schedule/cmd/func/timeout）。

    规格藏在 ``job.args[0]``（注册时就把整份定义塞进去了）—— jobstore 序列化
    的就是它，重启后读回来的还是它。取不到就退成残缺规格（只有 id/name），
    页面照样显示一行，不许整页炸掉。

    **双形态读回**：``cmd``（argv 列表）与 ``func``（"模块:可调用"）都认 ——
    缺哪个补空（不补 0、不报错），编辑面据此回填当前执行方式。
    """
    spec: Dict[str, Any] = {}
    args = getattr(job, "args", None) or []
    if args and isinstance(args[0], dict):
        spec = dict(args[0])
    spec.setdefault("id", getattr(job, "id", ""))
    spec.setdefault("name", getattr(job, "name", "") or spec.get("id", ""))
    spec.setdefault("schedule", str(getattr(job, "trigger", "")))
    if not isinstance(spec.get("cmd"), list):
        spec["cmd"] = []
    if not isinstance(spec.get("func"), str):
        spec["func"] = ""
    if not isinstance(spec.get("timeout"), int):
        # 🔴 存量 job（老 jobstore 里的形状）没有 timeout 字段 ⇒ 补**执行路径同款**
        # 默认值（DEFAULT_TIMEOUT），不是 0：编辑校验要求正整数，补 0 会让这类
        # job 一编辑就 400，编辑框也会显示 0。
        spec["timeout"] = DEFAULT_TIMEOUT
    return spec


def trigger_once(sch, spec: Dict[str, Any]) -> str:
    """投一次**立即执行**（一次性 DateTrigger）：不建常驻 job、跑完自动消失。

    走的是同一个执行包装（``_execute``）—— 「运行一次」与「按点跑」是同一份
    代码路径（同一份 argv、同一个落库口径），不是另写一条捷径。
    返回这一次性 job 的 id（调试用）。
    """
    from uuid import uuid4

    from apscheduler.triggers.date import DateTrigger
    one_id = f"{spec.get('id', 'ks-job')}-run-{uuid4().hex[:8]}"
    sch.add_job(_execute, DateTrigger(run_date=datetime.now(timezone.utc)),
                args=[dict(spec)], id=one_id,
                name=f"{spec.get('name') or spec.get('id')}（手动触发）",
                misfire_grace_time=SCHED_DEFAULTS["misfire_grace_time"],
                coalesce=SCHED_DEFAULTS["coalesce"])
    return one_id


def tombstone_ids() -> Set[str]:
    """墓碑表里的 job_id 集合（被删过的任务）。读不到 ⇒ 抛（调用方 fail-closed）。"""
    with psycopg.connect(_conninfo()) as conn:
        conn.execute(_TOMBSTONE_DDL)
        rows = conn.execute("SELECT job_id FROM ks_bus_tombstone").fetchall()
    return {str(r[0]) for r in rows}


def add_tombstone(job_id: str) -> None:
    """写墓碑（幂等）。写不成功 ⇒ 抛（调用方据此中止删除：不许「删了但会复活」）。"""
    with psycopg.connect(_conninfo()) as conn:
        conn.execute(_TOMBSTONE_DDL)
        conn.execute(
            "INSERT INTO ks_bus_tombstone(job_id, removed_at) VALUES (%s, now()) "
            "ON CONFLICT (job_id) DO UPDATE SET removed_at = EXCLUDED.removed_at",
            (job_id,))


def drop_tombstone(job_id: str) -> None:
    """撤销墓碑（重新添加同一个 id 时用 —— 删除不是永禁，是「别自动复活」）。"""
    with psycopg.connect(_conninfo()) as conn:
        conn.execute(_TOMBSTONE_DDL)
        conn.execute("DELETE FROM ks_bus_tombstone WHERE job_id = %s", (job_id,))


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


def recent_runs(job_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """某任务的近 N 次执行记录（读面用）。读不到 ⇒ 空表，不抛。

    口径：``ks_bus_run`` 是 append-only 流水（近 N 次）；一条都没有时退回
    ``ks_bus_state`` 的那一行（老库/老任务没流水时至少看得见上次结果）。
    """
    rows: List[Any] = []
    try:
        with psycopg.connect(_conninfo()) as conn:
            conn.execute(_HISTORY_DDL)
            rows = conn.execute(
                "SELECT ran_at, ok, rc, detail FROM ks_bus_run WHERE job_id = %s "
                "ORDER BY ran_at DESC LIMIT %s", (job_id, limit)).fetchall()
    except Exception as e:            # noqa: BLE001
        logger.warning("bus: 执行历史读取失败: %s", e)
        rows = []
    if not rows:
        # 流水一条都没有（老库 / 历史表还没建起来）⇒ 退回「最近一行」，
        # 页面至少看得见上次跑成没有（两个事务分开：历史读失败不许连累它）。
        try:
            with psycopg.connect(_conninfo()) as conn:
                rows = conn.execute(
                    "SELECT ran_at, ok, rc, detail FROM ks_bus_state WHERE job_id = %s",
                    (job_id,)).fetchall()
        except Exception as e:        # noqa: BLE001
            logger.warning("bus: 最近状态读取失败: %s", e)
            return []
    return [{"ran_at": r[0].isoformat() if r[0] else None,
             "ok": r[1], "rc": r[2], "detail": r[3]} for r in rows]
