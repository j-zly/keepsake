"""同义词总线（in-process）：扫碎片找同义组 → 写同义词表 + 重建 jieba 词典。

薄包装：本体在**存储层**（``discover_synonyms`` / ``generate_jieba_dict``，
PG / SQLite 两套后端语义等价），本模块只负责两件事：

1. 调存储层那两个原语（生产是 PG；后端由 config 决定，走工厂不写死）；
2. **把 JSON 结果翻成人话摘要**（原壳脚本打的是一坨 JSON dump，值班的人读
   不懂 —— 现在落进 ``ks_bus_state.detail`` 的是一句话）。

**静默纪律**：没发现新东西时摘要只有一句「无变化」，不刷日志、不发通知 ——
班车每 8 小时跑一次，噪音比信息重要。

班车唯一入口是 ``run()``；禁止模块级副作用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger("keepsake.bus.synonyms")

__all__ = ["run"]


def _summary(discover: Dict[str, Any], jieba: Dict[str, Any]) -> str:
    """两段 JSON → 人话摘要（第二返回值）。"""
    groups = int(discover.get("discovered_groups") or 0)
    terms = int(discover.get("total_terms") or 0)
    scanned = int(discover.get("scanned_fragments") or 0)
    written = int(jieba.get("written_terms") or 0)
    degraded = list(discover.get("degraded") or []) + list(jieba.get("degraded") or [])

    if not groups and not written and not degraded:
        return "无变化（无新同义词组，jieba 词典未变）"
    parts = [f"扫 {scanned} 条碎片，发现 {groups} 组同义词（词表 {terms} 条）",
             f"jieba 词典 {written} 条"]
    if degraded:
        # 🔴 降级不许静默（存储层铁律）：摘要里点明条数，值班的人一眼看得见。
        parts.append(f"降级 {len(degraded)} 条")
    return "；".join(parts)


def run(*, rebuild: bool = False) -> Tuple[bool, str]:
    """跑一轮同义词发现 + 词典重建 → ``(ok, 人读摘要)``。

    **班车唯一入口**（调度器按 ``keepsake.buses.synonyms:run`` 调用它）。

    ``rebuild=False``（缺省）= **增量**（手动加的同义词不覆盖）；
    ``rebuild=True`` = 清空同义词表后全量重建（洗历史累积碎渣，人工触发用）。
    """
    from keepsake.core.factory import load_config, storage_from_config

    cfg = load_config()
    storage = storage_from_config(config=cfg)
    try:
        discover = storage.discover_synonyms(rebuild=bool(rebuild))
        # 词典照旧每次都重建（哪怕没动同义词表）—— 与原壳脚本同款：
        # 词典内容还依赖碎片正文，不只依赖同义词表。
        jieba = storage.generate_jieba_dict()
    finally:
        try:
            storage.close()
        except Exception:     # noqa: BLE001 —— 关不掉不牵连班车结论
            pass
    return True, _summary(discover or {}, jieba or {})
