"""合并 · 遗忘（维护）班车（in-process）：同主题碎片合并提炼 + 低价值碎片清理。

收编自宿主 cron 的 `memory_maintenance_cron.sh` → `memory-maintenance.py`
（仓外 `/root/.hermes/scripts/`）：

* **dry-run / 实跑**：一轮维护要么**只算不写**（预演），要么**真跑**；
  缺省听 config 的 ``forget_dry_run``（缺省 true = 安全模式，与 provider
  构造 Forgetter 时的缺省同款 —— 班车不许自己另定一个开关）。
* **写前备份**（逐语义平移）：实跑前先落一份 **dry-run 报告**
  （``maintenance.<ts>.dryrun.json``），再把**受影响条目全量导出**
  （``affected.<ts>.before.jsonl``），**然后才真跑** —— 备份写不成功 ⇒
  这一轮中止（宁可不删，也不删出一件没法回滚的事）。
  备份与导出复用控制台同一套原语（``keepsake.api.audit``），不另写一份。
* **红报摘要同壳内现口径**：出错时抛出的消息以 ``[memory-maintenance] error:``
  开头（与原壳脚本打的那行逐字同款），由调度层捕获落 ok=False。

「谁会被合并 / 谁会被遗忘」的判据**直接借引擎自己的私有扫描原语**
（``Consolidator._scan_unconsolidated`` / ``Forgetter._find_forgettable``）——
与控制台 ``/api/v1/ops/merge`` / ``/api/v1/ops/forget`` 同一份判据：
两边判据漂移才是真危险（备份导出的集合和真跑的集合对不上，等于没备份）。

班车唯一入口是 ``run()``；禁止模块级副作用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("keepsake.bus.maintenance")

__all__ = ["run"]


def _affected_keys(consolidator: Any, forgetter: Any) -> List[str]:
    """本轮**受影响**的条目 key（合并候选 ∪ 遗忘候选）。

    借引擎自己的私有扫描原语（与 ``api/routers/ops.py`` 的同名原语同一份
    判据）：取不到（私有原语改名）就退成空表 —— 备份小一点，但**绝不漏**：
    漏了 = 真跑时删了没备份的东西。
    """
    out: List[str] = []

    def _keys_of(items: Any) -> None:
        for it in items or []:
            k = it.get("key") or it.get("_key") if isinstance(it, dict) else it
            if k:
                out.append(str(k))

    try:
        _keys_of(consolidator._scan_unconsolidated())
    except Exception as e:      # noqa: BLE001 —— 私有原语取不到就少备份，不炸
        logger.warning("maintenance: 合并候选取不到（备份可能偏小）: %s", type(e).__name__)
    try:
        _keys_of(forgetter._find_forgettable({"scanned": 0, "skipped_protected": 0}))
    except Exception as e:      # noqa: BLE001
        logger.warning("maintenance: 遗忘候选取不到（备份可能偏小）: %s", type(e).__name__)
    # 去重（保持顺序）
    seen = set()
    uniq = []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _export_before(storage: Any, keys: List[str], ts: str) -> str:
    """受影响条目全量导出 ``affected.<ts>.before.jsonl``（真跑前的最后一道兜底）。"""
    from keepsake.api import audit

    rows = storage.get_fragments_batch(keys) if keys else {}
    return audit.backup_jsonl(f"affected.{ts}.before.jsonl",
                              [{"key": k, "row": rows.get(k, {})} for k in keys])


def _summary(cons: Dict[str, Any], forg: Dict[str, Any], dry: bool) -> str:
    """两段 stats → 人话摘要（与原壳脚本的 watchdog 口径同款）。"""
    merged = int(cons.get("merged") or 0)
    forgotten = int(forg.get("deleted") or 0)
    if dry:
        # 预演口径：报「会」动多少，不报「动了」多少。
        merged = int(cons.get("would_merge") or 0)
        forgotten = int(forg.get("candidates") or 0)
    note = " (dry-run)" if dry else ""
    if merged == 0 and forgotten == 0:
        return f"无动作{note}"
    return f"合并 {merged} 条碎片，遗忘 {forgotten} 条碎片{note}"


def run(*, dry_run: Optional[bool] = None) -> Tuple[bool, str]:
    """跑一轮维护（合并 + 遗忘）→ ``(ok, 人读摘要)``。

    **班车唯一入口**（调度器按 ``keepsake.buses.maintenance:run`` 调用它）。

    ``dry_run=None`` ⇒ 听 config 的 ``forget_dry_run``（缺省 true = 只算不删）。
    """
    from keepsake.api import audit
    from keepsake.core.factory import load_config, storage_from_config
    from keepsake.core.ops.consolidator import Consolidator
    from keepsake.core.ops.forgetter import Forgetter

    cfg = load_config()
    dry = bool(cfg.get("forget_dry_run", True)) if dry_run is None else bool(dry_run)

    storage = storage_from_config(config=cfg)
    try:
        consolidator = Consolidator(storage)
        forgetter = Forgetter(storage)

        if dry:
            cons = consolidator.consolidate(dry_run=True)
            forg = forgetter.forget()
            return True, _summary(cons, forg, dry=True)

        # 实跑：先 dry-run 报告 + 受影响条目导出，**然后才真跑**。
        ts = audit.stamp()
        dry_report = {"consolidate": consolidator.consolidate(dry_run=True),
                      "forget": forgetter.forget()}
        dry_path = audit.backup_json(f"maintenance.{ts}.dryrun.json", dry_report)
        keys = _affected_keys(consolidator, forgetter)
        aff_path = _export_before(storage, keys, ts)
        logger.info("maintenance: 备份 dry_run=%s affected=%s（%d 条）",
                    dry_path, aff_path, len(keys))

        cons = consolidator.consolidate(dry_run=False)
        forg = forgetter.forget(force=True)
    except Exception as e:      # noqa: BLE001 —— 红报口径：与原壳脚本那行同款
        raise RuntimeError(f"[memory-maintenance] error: {type(e).__name__}: {e}") from e
    finally:
        try:
            storage.close()
        except Exception:       # noqa: BLE001 —— 关不掉不牵连班车结论
            pass

    summary = _summary(cons, forg, dry=False)
    errors = int(cons.get("errors") or 0)
    # 🔴 合并有失败 ⇒ 这一轮**报红**（ok=False ⇒ 调度层落 ks_bus_state.ok=false
    # + rc=1，页面取色成红）。旧口径「整体崩才红」是壳脚本遗留，合并失败无人知。
    if errors:
        summary += f"；合并失败 {errors} 条"
        return False, summary
    return True, summary
