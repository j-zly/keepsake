"""写操作的**自动备份**与**审计** —— 纯文件，不建表、不改 DB schema。

任务书硬约束 3/4 的落地。两个根目录都可用环境变量改（测试与非 root 环境走
临时目录；线上默认落在 `/root/.hermes/backup/` 下）：

* 备份 `CONSOLE_BACKUP_DIR` → `<root>/<YYYYMMDD>/`
    - 单条操作：`<key>.before.json`（改前整条 fragment 原文）
    - 合并/遗忘实跑：`merge.<ts>.dryrun.json` + `affected.<ts>.before.jsonl`
* 审计 `CONSOLE_AUDIT_DIR` → `audit-YYYYMMDD.jsonl`，每次写追加一行

🔴 **审计里不许出现 token / 连接串 / 口令**：`record()` 只接受动作名、
目标 key、布尔结果与一段**由调用方自己写死的**简短理由，绝不转发异常消息
（异常消息里可能有 DSN）。

🔴 **审计失败 = fail-closed**：`record()` 的 I/O 异常直接往上抛，
由全局异常处理器转 500 —— 写操作宁可不许做，也不许在没留痕的情况下做。
调用方拿到的 `HTTPException`（400/404/409）不会因为审计写失败而变 500：
那些分支先落审计再抛，审计本身抛出的异常由全局处理器兜住。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("keepsake.api.audit")

#: 备份根目录（默认任务书给的口径）
BACKUP_ROOT_ENV = "CONSOLE_BACKUP_DIR"
DEFAULT_BACKUP_ROOT = "/root/.hermes/backup/console_ops"

#: 审计根目录
AUDIT_ROOT_ENV = "CONSOLE_AUDIT_DIR"
DEFAULT_AUDIT_ROOT = "/root/.hermes/backup/console_audit"

#: 审计里的操作者标识（控制台是唯一入口，token 本身绝不落盘）
ACTOR = "console"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def _root(env_name: str, default: str) -> Path:
    return Path((os.environ.get(env_name) or "").strip() or default)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def day() -> str:
    """日期分区（UTC），`YYYYMMDD`。"""
    return _now().strftime("%Y%m%d")


def stamp() -> str:
    """备份文件名用的时间戳，`YYYYMMDDTHHMMSS`（UTC）。"""
    return _now().strftime("%Y%m%dT%H%M%S")


def backup_dir() -> Path:
    """当日备份目录 `<backup_root>/<YYYYMMDD>/`（不保证已存在）。"""
    return _root(BACKUP_ROOT_ENV, DEFAULT_BACKUP_ROOT) / day()


def audit_file() -> Path:
    """当日审计文件 `audit-YYYYMMDD.jsonl`（不保证已存在）。"""
    return _root(AUDIT_ROOT_ENV, DEFAULT_AUDIT_ROOT) / f"audit-{day()}.jsonl"


def precheck() -> None:
    """写操作前的**落盘自检**：确保这次操作留得下痕（建目录 + 以追加模式打开）。

    🔴 这是 fail-closed 的落点：审计写不进去时**宁可不许写**，也不许先改完
    数据、再发现记不上账（那样调用方拿到 500、数据却已经变了 —— 「报失败」
    与「真没改」必须一致）。

    只开不写，所以不产生空行；`record()` 照旧追加完整的一行 JSON。
    """
    path = audit_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8"):
        pass


def _safe(name: str) -> str:
    """文件名安全化：只挡路径穿越（`/`、NUL、`..` 段），其余字符原样保留
    （key 里的 `:` 在 Linux 上是合法文件名字符，不必转义掉可读性）。"""
    out = str(name).replace("/", "%2F").replace("\0", "")
    return out if out and out not in (".", "..") else "_"


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------

def record(action: str, target: Any, ok: bool, reason: str = "") -> None:
    """追加一行审计 JSON：`{ts, actor, action, target, ok, reason}`。

    **被拒绝的写操作也要调用**（`ok=false`，`reason` 记 `confirm_required`
    这类错误码）—— 「谁在什么时候试图改什么但没改成」本身就是审计信息。
    """
    line = {
        "ts": _now().isoformat(),
        "actor": ACTOR,
        "action": str(action),
        "target": str(target),
        "ok": bool(ok),
        "reason": str(reason or ""),
    }
    path = audit_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------

def _dumps(payload: Any, indent: Optional[int] = 2) -> str:
    """BLOB（向量字节）不进 JSON —— 与只读详情接口同一口径。"""
    return json.dumps(payload, ensure_ascii=False, indent=indent, default=_fallback)


def _fallback(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<blob {len(value)} bytes>"
    return str(value)


def backup_fragment(key: str, row: Optional[Dict[str, Any]]) -> str:
    """落一条碎片的**改前原文**：`<backup_root>/<YYYYMMDD>/<key>.before.json`。

    必须在写存储层**之前**调用；抛异常 ⇒ 整个写操作中止（数据还没动）。
    返回落盘路径，供回包与审计引用。
    """
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe(key)}.before.json"
    path.write_text(_dumps({"key": key, "backed_up_at": _now().isoformat(),
                             "row": row or {}}), encoding="utf-8")
    return str(path)


def backup_json(name: str, payload: Any) -> str:
    """落一份 JSON 报告（如 `merge.<ts>.dryrun.json`）。返回落盘路径。"""
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _safe(name)
    path.write_text(_dumps(payload), encoding="utf-8")
    return str(path)


def backup_jsonl(name: str, rows: Iterable[Dict[str, Any]]) -> str:
    """落一份 JSONL 全量导出（如 `affected.<ts>.before.jsonl`）。返回落盘路径。"""
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _safe(name)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=_fallback) + "\n")
    return str(path)