"""记忆分级班车（in-process）：提名 / 自动升 / 压力降。

收编自仓内手跑入口 ``scripts/tiering_cron.py`` 的核心逻辑 —— **脚本保留为手跑
入口，它 import 本模块复用同一份实现**（不复制：两处各养一套阈值口径，迟早
分叉；本模块是那份「一份」）。

* ``tiering.enabled=false``（缺省）⇒ 本轮跳过、**零写**（连存储都不构造）——
  与手跑入口逐字一致（cron 不报红）。
* ``dry_run=True`` ⇒ 只算不写（观察期人肉跑几天）。

本模块的「核心逻辑」= 读整份 config → ``resolve_tiering`` 判开关 → 造存储 →
``run_tiering`` 跑一轮；调度器（``keepsake.core.bus``）按
``keepsake.buses.tiering:run`` **同进程**调它。

班车唯一入口是 ``run()``；禁止模块级副作用（import 只定义函数）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("keepsake.bus.tiering")

__all__ = ["run", "run_round", "load_config", "ConfigError"]


class ConfigError(RuntimeError):
    """配置文件读不了 / 坏了 —— 一行人话错误（手跑入口转成退出码 2）。"""


def load_config() -> Dict[str, Any]:
    """读**整份** config.json（唯一配置源：``core.factory.config_path()``）。

    * 文件不存在 ⇒ 空 dict（缺省配置）；
    * 文件坏了 ⇒ 抛 ``ConfigError``（**不静默按缺省跑**：阈值全错着跑，
      比报错危险得多）。
    """
    from keepsake.core.factory import config_path, load_config as _load

    try:
        return _load(str(config_path()))
    except ValueError as e:
        raise ConfigError(str(e)) from e


def run_round(dry_run: bool = False, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """跑一轮分级 → stats dict。

    ``enabled=false`` ⇒ 返回 ``{"skipped": "disabled", ...}``，**零写**。
    """
    from keepsake.core.factory import storage_from_config
    from keepsake.core.tiering import resolve_tiering, run_tiering

    if cfg is None:
        cfg = load_config()
    if not resolve_tiering(cfg)["enabled"]:
        return {"skipped": "disabled", "dry_run": dry_run,
                "reason": "tiering.enabled=false"}
    storage = storage_from_config(config=cfg)
    try:
        return run_tiering(storage, cfg, dry_run=dry_run)
    finally:
        # 只关**本实例私有**的池 / 线程（PG 共享池不随实例 close 而关闭 ——
        # 见 core.pg.PgStorage.close 的注释）。
        try:
            storage.close()
        except Exception:     # noqa: BLE001 —— 关不掉不牵连班车结论
            pass


def _summary(stats: Dict[str, Any]) -> str:
    """stats → 人话摘要（落 ks_bus_state.detail 的一行）。"""
    nominate = (stats.get("nominate") or {}).get("count", 0)
    review = stats.get("auto_review") or {}
    demote = stats.get("demote") or {}
    demoted = ((demote.get("nominated") or {}).get("count", 0)
               + (demote.get("forced") or {}).get("count", 0))
    failed = len(stats.get("failed") or [])
    parts = [
        f"提名 {nominate} 条",
        f"自动通过 {review.get('approved_count', 0)} 条",
        f"驳回 {review.get('rejected_count', 0)} 条",
        f"降级 {demoted} 条",
        f"常驻 {stats.get('core_count', 0)} 条",
    ]
    if failed:
        parts.append(f"失败 {failed} 条")
    return "；".join(parts)


def run(*, dry_run: bool = False) -> Tuple[bool, str]:
    """跑一轮记忆分级 → ``(ok, 人读摘要)``。

    **班车唯一入口**（调度器按 ``keepsake.buses.tiering:run`` 调用它）。
    """
    stats = run_round(dry_run=dry_run)
    if stats.get("skipped") == "disabled":
        return True, "分级班车已关闭（tiering.enabled=false），本轮跳过"
    if dry_run:
        return True, "（预演）" + _summary(stats)
    return True, _summary(stats)
