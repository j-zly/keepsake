"""常驻层自动分级班车（core tiering）—— 提名 / 自动升 / 压力降 三条规则。

调度**不在本文件**：`run_tiering()` 只跑一轮、纯同步、无线程无定时器；
每天的 cron 由 `scripts/tiering_cron.py` 发起（挂 cron 是运维接线的事，本模块不碰）。

## 🔴 默认关死

`tiering.enabled` **缺省 false**，缺这一节 / `enabled=false` ⇒ `run_tiering()`
第一行就返回 `{"skipped": "disabled"}`，**一条都不写**（连状态文件都不碰）。
上线 ≠ 机制启动：观察期用 `--dry`（`enabled=true` + dry）人肉跑几天，
看清 stats 再由人在设置弹框里开总开关。这是本单的第一条红线。

## 命中数据为什么对全库一视同仁（别误以为要按 agent 分域）

命中打点（`storage.record_hits`）在 **primary（主脑）检索**里同样产生 ——
主脑每轮注入前的那次 prefetch 检索，召回了什么就记什么。所以：
「升提名只统计非 primary 可检索域」这个问题**不成立**，本模块也不按 agent 分域。
提名对全库一视同仁，「别把我的手写常驻记忆降掉」这件事由 `is_explicit_core()`
豁免判据挡（见下），而不是靠分域。

## 三条规则

1. **升提名**：检索层条目（非 `core`、非已带 `core_candidate`）在
   `promote_nominate.window_days` 内命中 ≥ `promote_nominate.nominate_min_hits`
   （默认 3000）⇒ 打 `core_candidate` + `tier:auto-hot:<提名日>`（reason 写进
   tags，审核页一眼看出是哪来的申请）。**不新造表**：复用既有候选审核队列。
   🟡 阈值取 3000 是口语碎片凭高频检索打点也能进的现实压力 —— 现流量下基本
   等于关死本轨，但核心层手动 promote 端点不受影响（人批走另一条路）。
   🔴 **孤儿命中不进提名**（2026-10-23 案）：条目已删、命中事件还留着 ⇒
   静默跳过，**不计 failed**（否则次日班车无谓地红）。存在性判据复用
   `get_fragments_batch`（三后端语义一致：缺失的 key 不出现在结果里）。
2. **自动审核通过 / 驳回（替换旧 auto_promote）**：候选自提名日起满
   `promote_auto.review_window_days` 天（默认 7），评估**自提名日起累计命中**
   ≥ `promote_auto.promote_approve_hits`（默认 5000）⇒
   **自动打 core**（与手动 promote 同一存储路径 `_with_core`+`_apply`，加
   `tier:auto-promoted` 痕迹）；不足 ⇒ **自动驳回**：摘 `core_candidate`、
   保留 `tier:auto-hot:<提名日>` 历史戳、加 `tier:auto-rejected:<驳回日>`
   短戳，条目留检索层。未满窗的候选**不动**（人也可手动先批/驳）。
   🔴 **命中窗口的锚点是提名日**（`hits_stats(since=提名日)`），不是
   「现在减 7 天」的滚动窗 —— 见下「基数与复核口径」；阈值数字不动，
   动的只是**数哪一段**。
   **预算护栏**：自动批准的那一步 `used + len(content) > budget` ⇒ 本轮
   跳过、留在队列里等下一轮 —— 绝不硬塞；驳回不耗预算、不受这条护栏约束。
3.
3. **降级三轨**（全部只**提名** + 进审核队列；只有「强降」是自动动作）：
   * **信号轨**（`_rule_signal_demote`，**不看水位**，本单新增）：常驻条目的
     `feedback_score` 解析为负（人工点「没用」-2 / 已纠正 -1）⇒ 直接打
     `tier:demote-candidate`。**不受 `explicit_core` 豁免**（豁免只保护
     「自动降级」这个动作，见下）；已带建议标的跳过（幂等）。stats 键
     `signal_nominate`。
   * **压力轨**（`_rule_demote`）：`used/budget ≥ warn_ratio` ⇒ 按「最不活跃先走」提名退居候选
     （`tier:demote-candidate`），降到 `release_ratio` 为止或 `batch_cap` 到顶。
     排序分 = 近 90 天命中数**升序** → `last_hit` 旧 → `updated` 旧。
     🟡 **信号弱化注释**：常驻条目绝大多数命中为 0（它们是被**注入**的，不是靠
     检索召回的），所以「最不活跃」在多数时候退化成「最旧的那条」——
     这是有意的保守：宁可降旧不降新、也不要用一个根本不存在的热度差去排序。
   * **强降**：占比达 100% 且 `force_at_full` ⇒ 队列里**最尾部的一条**自动 demote。
     保守到只降 1 条：一次只让水位回到可谈判的区间，其余仍走人工审核。
   * **豁免**：`demote_exempt.explicit_core` 命中的条目**只进建议、永不自动降**
     （可被提名、可被人审核掉，但班车不许替主脑决定）；自动升进来的
     （`tier:auto-promoted`）**不豁免** —— 它是机器放进来的，机器也该能请出去。
     🔴 豁免挡的是**自动动作**（强降），不挡**提名**：信号轨对手写条目照样提名
     （见上），否则「这条误导」这句人话在常驻层里没人听得到。

## 2026-10 常驻层入口改革：core 不再直写 + 提名即带基数

用户拍板的两条新规矩（**阈值一律不动**：提名 3000 / 自动批 5000 / 观察窗 7 天）：

1. **任何写入都不直接进 core**：写 `core` 意图 ⇒ 落普通记忆条目 + 打候选标，
   进既有的待审核队列。常驻层从此只有两个入口：**审核页人工批准**、
   **候选满观察期自动通过**。
   写闸在 `adapters/hermes/provider.py::_apply_write_gate`（本模块只提供
   候选标与提名函数，**判据不在两处各写一份**）：
   * 非 primary 写 core ⇒ 也落 `core_candidate` **+ 提名日戳**（2026-10
     ks_perm_scope：**子代理自荐常驻 = 与主脑同流程**；此前「降级但不打戳」
     等于**死候选**，班车永远不审它）；
   * primary 写 core ⇒ 也落 `core_candidate` + 提名日戳（**不再直通**）；
   * 人工显式直写（控制台 promote / CoreView / MCP `memory_add` 显式 core）
     ⇒ 仍是直写 —— 那是「人在批」的等价动作。
     🔴 2026-10 ks_share_tighten：**只对 primary 生效**（MCP 侧按凭据反查的
     角色判）—— 外部代理带 core 标签进来不许绕审核进常驻。
2. **提名即带基数**（`tiering.nominate_backfill_hits`，默认 3000，0 = 关闭）：
   成为候选的**瞬间**给该 key 补写这么多条**命中事件**（批量 INSERT，
   分块，不逐条）。为什么是「打点」而不是「打个标」：规则二判的是
   **窗口内命中数**（`hits_stats` 数的是事件行数），基数必须是它能数到的东西。
   驳回摘候选标时**基数不回滚** —— 打点是历史事实，不是可撤销的额度。

🟢 **基数与复核口径**（2026-10 主脑裁定，**阈值数字一个没动**）：
复核数的是「**自提名日起累计命中**」，窗口锚点是**提名日**
（`hits_stats(since=提名日)`），不是「现在减 7 天」的滚动窗。
为什么必须这样：复核发生在「提名满 7 天」之后，若还数滚动 7 天窗，
提名那刻补的 3000 条基数**早已滑出窗口** ⇒ 基数白补、候选永远攒不到
批准门槛（5000）。改成「提名日→今」后，基数在复核那一刻**仍然计入**，
对历史命中**不封顶**。改的是**数哪一段**，不是「判多热」——
3000/5000/7 天三个数字原封不动。

（此前这里记的是「基数滑出窗口、自动批准仍看真实命中」那个边界；
主脑已裁定按上面这条口径执行，边界取消。）

## 为什么降级建议**不进** `retrieval.exclude_tags`（与晋升候选方向相反）

主脑检索出口默认排除 `core_candidate`（未审的晋升候选会污染注入、还会抢召回）。
`tier:demote-candidate` 恰恰**不排除**：降级建议存在的意义就是「提醒主脑这条
常驻在吃预算、建议退位」，藏进排除区它就永远没人看得见 —— 一个没人看得见的
提醒等于没有提醒。两个方向处理不同，是刻意的，不是漏配。

## 写路径纪律

升/降/打标**一律**经既有单条路径（`api/routers/ops.py` 的 `_with_core` /
`_apply`，隐含「写前备份 + 写后回读 + 审计」）：本模块**不自拼 UPDATE SQL**、
不自己开 PG 连接（G4 静态闸）。层次上 `core` 反向 `import api` 看着别扭，
但在这里让「打标签的语义只有一份」比层次洁癖重要 —— 那是防漂移的唯一办法。
（`api.routers.ops` 也不 import 本模块的写路径，只用它的**配置 schema**，
没有环。）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("keepsake.core.tiering")

# --------------------------------------------------------------------------
# 标签与阈值常量
# --------------------------------------------------------------------------

#: 常驻标签（与 `adapters/hermes/provider.py::_CORE_TAG`、`api/routers/core.py`
#: 同名字面量）。三处任一改动都必须三处一起改，否则「打上了但注入排不掉」。
CORE_TAG = "core"

#: 晋升候选标（与 `provider.CORE_CANDIDATE_TAG` 同名）：子代理写 core 被降级后的
#: 待审层，**复用既有候选审核队列**，本模块不新造表。
CANDIDATE_TAG = "core_candidate"

#: 退居建议标（压力轨提名）。**刻意不是 `core_candidate`** —— 那是晋升方向的
#: 徽标，混进同一个队列会让审核页分不清「批准=升常驻」还是「批准=降常驻」。
DEMOTE_CANDIDATE_TAG = "tier:demote-candidate"

#: 晋升提名标前缀，**带提名日**：`tier:auto-hot:2026-10-16`。
#: 为什么要日期：规则二判「自提名日满 review_window_days」，而打标签**不动**条目时间戳
#: （`update_fragment_fields` 不刷 `updated`；PG 侧压根没有 `updated_at` 列）。
#: 没有一个跨后端可读的「打标时刻」，就没有跨后端可比的「挂多久了」。
#: 审核页显示成「auto-hot · 2026-10-16」，顺带回答「这条是谁、什么时候提的」。
AUTO_HOT_PREFIX = "tier:auto-hot"

#: 自动升进来的痕迹（压力轨**不豁免**它们，见模块 docstring 规则 3）。
AUTO_PROMOTED_TAG = "tier:auto-promoted"

#: 自动驳回痕迹，带驳回日：`tier:auto-rejected:2026-10-23`。
#: 驳回**不动** `tier:auto-hot:<提名日>` 历史戳（那是哪条提的、什么时候提的）；
#: 这一段只回答「这条被班车驳了、什么时候驳的」，给回看用。
AUTO_REJECTED_PREFIX = "tier:auto-rejected"

#: 冲突证据标前缀，**带 key**：`tier:evid:memory:frag:xxxx`。
#: 谁打的：写侧管线（`core/pipeline.py`）判 UPDATE/DELETE 且目标是常驻条目时 ——
#: 那正是「这条常驻与更新的事实冲突」的一手证据，写侧不该自己封常驻（那是人的
#: 拍板），改把它连同证据 key 一起提名进本队列，由人在审核页裁决。
#: 前缀的**唯一定义在这里**，pipeline 反向 import 本常量（G5 单点闸）。
EVID_PREFIX = "tier:evid"

#: `explicit_core` 豁免判据的三个信号（判据本体见 `is_explicit_core`）：
EXEMPT_TAG = "memory"        # Hermes 显式写 core 时 tags 恒含它（provider `_core_write`）
DISTILL_TAG = "auto-distill" # 提炼产物恒含它 ⇒ 非人手写
AGENT_PREFIX = "agent:"      # 第三方域（子代理）⇒ 非主脑手写

#: 阈值缺省（= config 里 `tiering` 节写不写都一样；见模块 docstring）
#: `promote_nominate.nominate_min_hits` 默认 3000 是用户在带流量验证后定的
#: 严口径 —— 口语碎片高频打点也能进的旧值（5）会污染候选队列。
#: `promote_auto` 现在只两个键（review_window_days=7、promote_approve_hits=5000），
#: 旧 age_days/recent_window_days/recent_min_hits 已不再使用 —— config.json 里
#: 残留的旧键会被 `resolve_tiering` 静默忽略、由 DEFAULTS 兜底（见 GATES G4）。
DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "promote_nominate": {"window_days": 7, "nominate_min_hits": 3000},
    "promote_auto": {"review_window_days": 7, "promote_approve_hits": 5000},
    "demote_pressure": {"warn_ratio": 0.90, "release_ratio": 0.80, "force_at_full": True},
    "demote_exempt": {"explicit_core": True},
    "batch_cap": 50,
    # 提名基数（2026-10 常驻层入口改革）：成为候选的瞬间给该 key 补写的
    # 命中事件条数。默认 3000（与提名阈值同量级）＝「这条至少这么热」；
    # 0 = 关闭（只提名，不补基数）。见模块 docstring「提名即带基数」。
    "nominate_backfill_hits": 3000,
}

#: 各值域（校验与回落用同一份，PUT 与读配置两条路径不许各写一套区间）。
#: 上限按口语复活语境评分时打点峰值（单条历史最高 83 次/7d 是历史峰值；
#: 提名阈值 3000/批 5000 给上限 100000，足够未来扩 20 倍仍不撞墙）。
BOUNDS = {
    "window_days": (1, 365, int),
    "nominate_min_hits": (1, 100000, int),
    "review_window_days": (1, 365, int),
    "promote_approve_hits": (1, 100000, int),
    "warn_ratio": (0.01, 1.0, float),
    "release_ratio": (0.0, 1.0, float),
    "batch_cap": (1, 10000, int),
    # 基数条数：下限 **0**（0 = 关闭补基数，是合法取值不是非法值）
    "nominate_backfill_hits": (0, 100000, int),
}

#: 压力轨的排序窗口（命中数 / last_hit 都看这么远）。90 天 = 打点事件的保留期，
#: 超过这个窗口连事件都被 prune 了，再往回看只会读到「更早但已删」的假冷。
PRESSURE_WINDOW_DAYS = 90

#: stats 明细里最多回带多少个 key（够人核账，又不至于把日志撑爆）。
DETAIL_KEYS = 20

#: 规则一要拉多宽的热榜（`hits_stats` 的 limit）。取 batch_cap 的 10 倍：候选层
#: 绝大多数会被「已是 core / 已带候选标」筛掉，10 倍余量够筛完又不至于全表扫。
HOT_SCAN_FACTOR = 10
HOT_SCAN_MIN = 500

#: `hits_stats(keys=…)` 一次最多塞多少个 key（SQLite 的参数上限 999，留一半余量；
#: 超过就分块问，见 `_hits_by_key`）。
HITS_KEY_CHUNK = 400


# --------------------------------------------------------------------------
# 配置：读（容错回落）/ 写（严格拒绝）
# --------------------------------------------------------------------------

def _parse_bool(value: Any, default: bool) -> bool:
    """布尔解析：只认真布尔与 `true/1/yes/on`（大小写不敏感）的字符串。

    🟡 **数字不认**（`enabled: 1` 到底是开还是关不该猜），其它一律回落缺省 ——
    对 `enabled` 来说回落就是 `false`，也就是「关」：把一个看不懂的值猜成
    「开」，等于让班车在用户没说要开的时候每天动常驻层。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return default


def _parse_num(value: Any, name: str, default: Any) -> Any:
    """数值解析 + 值域校验；非法 ⇒ warning + 回落缺省（风格同 `exclude_tags`）。

    **键缺省不走这条路**（`value is None` 静默回落）：「没配」是正常状态，
    为它 warning 一次会让每次读配置都刷一屏噪音，噪音里真正的非法值反而看不见了。
    """
    lo, hi, kind = BOUNDS[name]
    if value is None:
        return default
    if isinstance(value, bool):
        logger.warning("keepsake: tiering.%s=%r 非法，回落 %r", name, value, default)
        return default
    try:
        n = kind(value)
    except (TypeError, ValueError):
        logger.warning("keepsake: tiering.%s=%r 非法，回落 %r", name, value, default)
        return default
    if not (lo <= n <= hi):
        logger.warning("keepsake: tiering.%s=%r 越界(%s~%s)，回落 %r",
                       name, value, lo, hi, default)
        return default
    return n


def _section(raw: Any, name: str) -> Dict[str, Any]:
    """取 `tiering` 的子节；不是 dict 一律当空节（配置坏了不该让班车崩）。"""
    if not isinstance(raw, dict):
        return {}
    sub = raw.get(name)
    return sub if isinstance(sub, dict) else {}


def resolve_tiering(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 config dict（整份 config.json）解析出**完整**的 `tiering` 节。

    任何缺失 / 非法值都回落缺省并 warning（风格照 `retrieval.exclude_tags`）：
    配置坏了的代价是「按缺省跑」，不是「班车崩掉」。
    `enabled` 是总开关，**非法值回落 false**（回落成 true 才是事故）。
    """
    raw = _section(config if isinstance(config, dict) else {}, "tiering")
    pn = _section(raw, "promote_nominate")
    pa = _section(raw, "promote_auto")
    dp = _section(raw, "demote_pressure")
    de = _section(raw, "demote_exempt")
    return {
        "enabled": _parse_bool(raw.get("enabled"), DEFAULTS["enabled"]),
        "promote_nominate": {
            "window_days": _parse_num(pn.get("window_days"), "window_days",
                                      DEFAULTS["promote_nominate"]["window_days"]),
            "nominate_min_hits": _parse_num(pn.get("nominate_min_hits"),
                                            "nominate_min_hits",
                                            DEFAULTS["promote_nominate"]["nominate_min_hits"]),
        },
        "promote_auto": {
            "review_window_days": _parse_num(pa.get("review_window_days"),
                                             "review_window_days",
                                             DEFAULTS["promote_auto"]["review_window_days"]),
            "promote_approve_hits": _parse_num(pa.get("promote_approve_hits"),
                                               "promote_approve_hits",
                                               DEFAULTS["promote_auto"]["promote_approve_hits"]),
        },
        "demote_pressure": {
            "warn_ratio": _parse_num(dp.get("warn_ratio"), "warn_ratio",
                                     DEFAULTS["demote_pressure"]["warn_ratio"]),
            "release_ratio": _parse_num(dp.get("release_ratio"), "release_ratio",
                                        DEFAULTS["demote_pressure"]["release_ratio"]),
            "force_at_full": _parse_bool(dp.get("force_at_full"),
                                         DEFAULTS["demote_pressure"]["force_at_full"]),
        },
        "demote_exempt": {
            "explicit_core": _parse_bool(de.get("explicit_core"),
                                         DEFAULTS["demote_exempt"]["explicit_core"]),
        },
        "batch_cap": _parse_num(raw.get("batch_cap"), "batch_cap",
                                DEFAULTS["batch_cap"]),
        "nominate_backfill_hits": _parse_num(raw.get("nominate_backfill_hits"),
                                             "nominate_backfill_hits",
                                             DEFAULTS["nominate_backfill_hits"]),
    }


def validate_tiering(payload: Any) -> Dict[str, Any]:
    """`PUT /api/v1/settings/tiering` 的**严格**校验：非法 ⇒ `ValueError`。

    与 `resolve_tiering` 的容错回落是两层、不是两套判据：
    * 手改 config.json（人肉写错）→ 回落缺省 + warning，班车照跑；
    * 走控制台端点（机器提交）→ **400 拒绝**，不许把半截阈值写进配置。
    未知键一律拒收：写进去就是一条没人读、还会在下回合被规范化悄悄吃掉的垃圾。
    """
    if not isinstance(payload, dict):
        raise ValueError("tiering 必须是 JSON 对象")
    unknown = set(payload) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"未知键：{sorted(unknown)}（允许：{sorted(DEFAULTS)}）")
    out = resolve_tiering({"tiering": payload})
    # 回落过的值就是非法值：逐键比一遍，命中即拒
    def _walk(flat: Dict[str, Any], given: Dict[str, Any]) -> None:
        for name, value in flat.items():
            raw = given.get(name, "<缺省>")
            if isinstance(value, dict):
                _walk(value, raw if isinstance(raw, dict) else {})
            elif raw != "<缺省>" and raw != value:
                raise ValueError(f"{name}={raw!r} 非法或越界")
    _walk(out, payload)
    for name in ("promote_nominate", "promote_auto", "demote_pressure", "demote_exempt"):
        if name in payload and not isinstance(payload[name], dict):
            raise ValueError(f"{name} 必须是 JSON 对象")
    if out["demote_pressure"]["release_ratio"] > out["demote_pressure"]["warn_ratio"]:
        raise ValueError("release_ratio 不得大于 warn_ratio（降级水位高于告警水位 ⇒ 永远降不动）")
    return out


# --------------------------------------------------------------------------
# 豁免判据（钉死的可判据）
# --------------------------------------------------------------------------

def tag_list(tags: Any) -> List[str]:
    """逗号串 → 标签数组（去空白、丢空项）。与 ops._tag_list / 前端 splitTags 同口径。"""
    return [t.strip() for t in str(tags or "").split(",") if t.strip()]


def is_explicit_core(tags: Any, *, exempt: bool = True) -> bool:
    """这条是不是**主脑显式写入**的常驻条目（`demote_exempt.explicit_core` 判据）。

    🔴 判据（三个条件全中才算，写死在这里 + 单测双向钉）：
      1. tags 里有**完整标签** `memory` —— Hermes `memory` 工具写 core 时 tags 恒为
         `core,{target}`，target 缺省就是 `memory`（provider `_core_write`）；
      2. **不含** `auto-distill` —— 自动提炼产物恒带这个标（有则说明是机器写的）；
      3. **不含** `agent:` 前缀标签 —— 那是子代理的第三方域声明。

    反面（MCP add、控制台新增/导入带 promote）**不豁免**：它们写的 tags 里没有
    `memory` 标 —— 机器写进来的东西就该能被机器请出去，只有「人自己写的」才享有
    豁免。`exempt=False` 时整条豁免关掉（配置项本身可关）。
    """
    if not exempt:
        return False
    ts = tag_list(tags)
    return EXEMPT_TAG in ts and DISTILL_TAG not in ts and not any(
        t.startswith(AGENT_PREFIX) for t in ts)


# --------------------------------------------------------------------------
# 小工具：时间 / 标签串 / 状态文件
# --------------------------------------------------------------------------

def _now_dt(now: Any = None) -> datetime:
    """本轮的「现在」。**所有 age / 窗口判据都吃这个参数**（别去 monkeypatch 时钟造 flaky）。"""
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO8601 时刻 → datetime（UTC）；解析不出来返回 None（不是 0 时刻）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_time(row: Dict[str, Any]) -> Optional[datetime]:
    """条目时间戳（`updated_at` → `updated` → `created`，各后端字段名不同）。"""
    for field in ("updated_at", "updated", "created"):
        dt = _parse_ts(row.get(field))
        if dt:
            return dt
    return None


def _drop_tag(tags: List[str], tag: str) -> List[str]:
    return [t for t in tags if t != tag]


def _add_tag(tags: List[str], tag: str) -> List[str]:
    return tags if tag in tags else tags + [tag]


def nominated_on(row: Dict[str, Any]) -> Optional[datetime]:
    """这条的**提名时刻**：`tier:auto-hot:<日期>` 标签上的日期；没有则 None。"""
    for t in tag_list(row.get("tags")):
        if t.startswith(AUTO_HOT_PREFIX + ":"):
            day = _parse_ts(t.rsplit(":", 1)[-1] + "T00:00:00+00:00")
            if day:
                return day
    return None


def feedback_of(row: Dict[str, Any]) -> float:
    """`feedback_score`（**文本列**）→ 数值；解析不出来一律当 **0**，不是负。

    文本列的脏形态是常态：空串 / NULL / `'-2'`（人工负反馈）/ `'-1'`（已纠正）/
    `'−2'`（U+2212 减号，人手输的）等。判据是「**确实解析出负数**」才算负反馈：
    把读不懂的值猜成负反馈会凭空提名一批常驻条目（误伤），漏判只少一个建议 ——
    这个方向上宁可漏、不误伤。
    """
    raw = str((row or {}).get("feedback_score") or "").strip()
    if not raw:
        return 0.0
    text = raw.replace("−", "-").replace("﹣", "-").replace("－", "-").replace(" ", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _age_days(row: Dict[str, Any], now: datetime) -> float:
    """挂了候选标的天数。

    有提名日标就用它；**没有的（老数据 / 收紧前子代理打的 `core_candidate`）回落到
    条目的 `updated_at`/`created`** —— 与条目同生共死，判据偏保守（老条目算挂得久），
    但绝不因此崩掉或当成刚挂上（那会让老候选第二天集体自动升）。
    """
    since = nominated_on(row) or _row_time(row)
    return (now - since).total_seconds() / 86400.0 if since else -1.0


def state_path() -> Path:
    """班车状态文件（`GET /api/v1/tiering/status` 的 `last_run` / `last_stats` 来源）。

    env `KEEPSAKE_TIERING_STATE` 可改路径；缺省 `~/.keepsake/tiering_last.json`。
    """
    env = (os.environ.get("KEEPSAKE_TIERING_STATE") or "").strip()
    return Path(env).expanduser() if env else Path.home() / ".keepsake" / "tiering_last.json"


def write_state(payload: Dict[str, Any]) -> str:
    """落本轮 stats 状态。**写不成不算失败**（记忆数据没动，只是一行遥测丢了）。"""
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as e:      # noqa: PERF203 —— 一条 warning，不许因遥测中止班车
        logger.warning("keepsake: tiering 状态文件写失败 %s: %s", path, e)
        return ""
    return str(path)


def read_state() -> Dict[str, Any]:
    """读状态文件；没有 / 坏 JSON ⇒ `{}`（看板显示「还没跑过」，不是报错）。"""
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _hits_since_nomination(storage: Any, rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """每条候选「**自提名日起**累计命中」→ `{key: hits}`。

    🔴 **窗口锚点是提名日，不是「现在减 review_window_days」**（2026-10 主脑裁定）：

    * 复核发生在「提名满 review_window_days」之后，若还数滚动 7 天窗，
      提名那刻补的基数（`tiering.nominate_backfill_hits`，默认 3000 条）
      早已滑出窗口 ⇒ 基数白补，候选永远攒不到批准门槛；
    * 改成「提名日→今」⇒ 基数在复核那一刻**仍然计入**，
      阈值数字（3000/5000/7 天）一个不动，改的只是**数哪一段**；
    * 「对历史命中不封顶」：窗口不设上限，名义上从提名日至今都算数。

    实现按**提名日分组**发查询（提名日戳是日期粒度，组数很少），
    每组一次 `hits_stats(since=提名日)` —— 锚点是**绝对时刻**，
    不受「挂钟 vs 注入的 now」的漂移影响（滚动窗做不到这点）。

    没有提名日戳的（老数据 / 收紧前子代理降级落的候选）⇒ 不在本函数里算数，
    调用方按「仍待审」处理（它们本来也不该被自动批）。
    """
    groups: Dict[datetime, List[str]] = {}
    for row in rows:
        key = str(row.get("key") or "")
        nominated = nominated_on(row)
        if not key or nominated is None:
            continue
        groups.setdefault(nominated, []).append(key)
    hits: Dict[str, int] = {}
    for nominated, group in groups.items():
        got, _ = _hits_by_key(storage, group, since=nominated)
        hits.update(got)
    return hits


def _hits_by_key(storage: Any, keys: List[str], window_days: Optional[int] = None,
                 since: Optional[datetime] = None) -> Tuple[Dict[str, int], Dict[str, str]]:
    """一批 key 的窗口命中数 / 最近命中时刻 → `({key: hits}, {key: last_hit})`。

    🔴 **分块问存储层**：三个后端的 `hits_stats(keys=...)` 都把 keys 摊成一条
    `IN (?,?,…)`，而 SQLite 的参数上限是 999（PG/Redis 同理有上限）。一次把上千个
    常驻条目的 key 丢过去 ⇒ 查询直接炸 ⇒ 班车一整个跑不起来。常驻层上百条时
    平时不炸，正是「平时没事、攒够就整轮挂掉」的那种坏，所以这里固定分块。

    `since`（绝对时刻）= 窗口锚点，给了就从那一刻起算（`window_days` 作废）；
    不给才是「现在减 window_days」的滚动窗（规则一提名、降级排序用滚动窗）。
    """
    hits: Dict[str, int] = {}
    last_hit: Dict[str, str] = {}
    uniq = [k for k in dict.fromkeys(keys) if k]
    for i in range(0, len(uniq), HITS_KEY_CHUNK):
        kwargs: Dict[str, Any] = {"keys": uniq[i:i + HITS_KEY_CHUNK],
                                  "limit": HITS_KEY_CHUNK}
        if since is not None:
            kwargs["since"] = since
        else:
            kwargs["window_days"] = int(window_days or 0)
        rows = storage.hits_stats(**kwargs) or []
        for row in rows:
            key = str(row.get("key") or "")
            if key:
                hits[key] = int(row.get("hits") or 0)
                last_hit[key] = str(row.get("last_hit") or "")
    return hits, last_hit


# --------------------------------------------------------------------------
# 候选标 / 提名基数（写闸与班车共用的两个原语）
#
# 为什么放这里而不是各写一份：候选标签的字面量（`core_candidate`）与
# `tier:auto-hot:<提名日>` 的格式**只有一份定义**，写闸（provider）与
# 班车（本模块）都必须用同一份 —— 两处各写一遍，迟早出现「一边打了标、
# 另一边按另一个名字找」的漂移（打上了但审核页看不见）。
# --------------------------------------------------------------------------

def candidate_tags(tags: Any, now: Optional[datetime] = None) -> str:
    """候选标签形态：`core` → `core_candidate`；给了 `now` 就补提名日戳。

    * 给了 `now` ⇒ 追加 `tier:auto-hot:<提名日>`（规则二判「挂多久了」与
      审核页显示「哪来的申请」都靠这个戳）—— **primary 与子代理都走这一支**
      （2026-10 ks_perm_scope：子代理自荐常驻 = 与主脑同一条审核/班车链）；
    * `now=None` ⇒ **只换标签不补戳**（不提名 ⇒ 班车规则二不去自动审它；
      保留这个口子给「只是打个候选标、没打算提名」的调用方）。

    其余标签原样保留、顺序不变（只把 `core` 那一项换掉）。
    """
    parts = [t.strip() for t in str(tags or "").split(",") if t.strip()]
    kept = [CANDIDATE_TAG if t == CORE_TAG else t for t in parts]
    if CANDIDATE_TAG not in kept:
        kept.append(CANDIDATE_TAG)
    if now is not None:
        stamp = f"{AUTO_HOT_PREFIX}:{now:%Y-%m-%d}"
        if stamp not in kept:
            kept.append(stamp)
    return ",".join(kept)


def backfill_nominate_hits(storage: Any, key: str, count: int) -> int:
    """提名基数：给 `key` 补 `count` 条命中事件，返回实际写入条数。

    🔴 **批量写，不逐条 insert**：`storage.record_hits_repeat` 一次落
    N 行（PG 走多行 INSERT 并按参数上限分块，SQLite 同款，Redis 一次
    HINCRBY）。逐条 insert 3000 次 = 3000 次往返，班车一轮 50 条提名
    就是 15 万次往返 —— 那不是「慢」，那是把定时任务变成事故。

    **fail-open**：后端不可达 / 未实现该原语 ⇒ warning + 返回 0，
    绝不把「补基数失败」升级成「提名失败」（提名已经落库了，基数是附属品）。
    """
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0 or not key:
        return 0
    try:
        return int(storage.record_hits_repeat(key, n) or 0)
    except Exception as e:      # noqa: BLE001 —— 附属品失败不许掀翻主流程
        logger.warning("keepsake: 提名基数回填失败（忽略，key=%s）: %s: %s",
                       key, type(e).__name__, e)
        return 0


# --------------------------------------------------------------------------
# 写路径：全部复用 api/routers/ops.py 的单条路径（隐含备份 + 回读 + 审计）
# --------------------------------------------------------------------------

def _write_tags(storage: Any, key: str, tags: str, action: str) -> None:
    """打标签（提名 / 摘标）：`ops._apply` —— 局部字段写 + 写后回读校验 + 审计。

    延迟 import：层次上是 `core` 反向引用 `api`（见模块 docstring 末段），
    延迟引用还顺带保证了 `api.routers.ops` ↔ 本模块之间**没有 import 环**。
    """
    from keepsake.api.routers.ops import _apply

    _apply(storage, key, {"tags": tags}, action)


def _promote(storage: Any, key: str, action: str, extra: List[str],
             drop: List[str]) -> None:
    """升常驻 = 单条 promote 的**存储路径**（`_with_core` + `_apply`）。

    与界面批准 `POST /api/v1/core/{key}/promote` 逐条同款：同标签口径
    （`core` 放最前）、同备份、同写后回读。`extra`/`drop` 是分级附带的痕迹
    （`tier:auto-promoted` / 摘 `core_candidate`）—— 合成一次写，不做两趟。
    """
    from keepsake.api import audit
    from keepsake.api.routers.ops import _apply, _with_core

    row = storage.get_fragment(key) or {}
    tags = tag_list(_with_core(row.get("tags"), True))
    for t in drop:
        tags = _drop_tag(tags, t)
    for t in extra:
        tags = _add_tag(tags, t)
    audit.backup_fragment(key, row)      # 备份写不成功 ⇒ 抛，整条不做
    _apply(storage, key, {"tags": ",".join(tags)}, action)


def _demote(storage: Any, key: str, action: str, extra: List[str],
            drop: List[str]) -> None:
    """降常驻 = 单条 demote 的存储路径（`_with_core(tags, False)` + `_apply`）。"""
    from keepsake.api import audit
    from keepsake.api.routers.ops import _apply, _with_core

    row = storage.get_fragment(key) or {}
    tags = tag_list(_with_core(row.get("tags"), False))
    for t in drop:
        tags = _drop_tag(tags, t)
    for t in extra:
        tags = _add_tag(tags, t)
    audit.backup_fragment(key, row)
    _apply(storage, key, {"tags": ",".join(tags)}, action)


# --------------------------------------------------------------------------
# 规则一：升提名
# --------------------------------------------------------------------------

def _rule_nominate(storage: Any, t: Dict[str, Any], now: datetime, *,
                   dry_run: bool, stats: Dict[str, Any]) -> None:
    """窗口内够热 ⇒ 打候选标（进既有审核队列，不直接升常驻）。

    🔴 **孤儿命中（条目已删、命中残留）静默跳过**：不进提名、不计 failed
    （三后端同一入口 ⇒ 一处改好；判据见 `get_fragments_batch` 的契约）。
    """
    cfg = t["promote_nominate"]
    cap = t["batch_cap"]
    backfill = int(t.get("nominate_backfill_hits") or 0)
    limit = max(HOT_SCAN_MIN, cap * HOT_SCAN_FACTOR)
    # 🔴 **窗口锚点必须吃注入的 `now`**（规则一同规则二；漏接 = 测试红 +
    # 未来可注入时钟做回填/对照实验时静默失真）：`since` 给了就从那一刻起算，
    # `window_days` 作废（见 StorageBase.hits_stats）。不传 `since` ⇒ 三后端
    # 内部退回「真挂钟减 window_days」，注入时钟形同虚设（生产语义不变：
    # 生产 now_dt 就是真挂钟，窗口起点与现状等价 —— 只差「挂钟读在调用前
    # 一点」那几微秒）。
    since = now - timedelta(days=int(cfg["window_days"]))
    hot = storage.hits_stats(window_days=cfg["window_days"],
                             min_hits=cfg["nominate_min_hits"],
                             keys=None, limit=limit, since=since) or []
    keys = [str(r.get("key") or "") for r in hot if r.get("key")]
    rows = storage.get_fragments_batch(keys) if keys else {}
    if keys and not rows:
        # 🔴 整批读回来一条都没有 ⇒ 更像**后端读失败**（Redis 侧
        # `get_fragments_batch` 失败只打 warning 并返回空 dict），不是
        # 「所有 key 都是孤儿」⇒ 留一条痕迹，免得班车安安静静一条不提名。
        logger.warning("keepsake: 提名候选批量读回空（%d 个 key）—— 可能后端读失败，"
                       "本轮按孤儿命中跳过", len(keys))
    picked: List[str] = []
    skipped_candidate = 0
    orphaned = 0
    backed_up = 0
    for key in keys:
        row = rows.get(key)
        if row is None:
            # 🔴 孤儿命中（2026-10-23 案）：条目已删、命中事件还留着 ⇒
            #   **静默跳过**：不进提名、**不计 failed**（否则次日班车无谓地红）。
            #   存在性判据用 `get_fragments_batch` 的同一份读（契约：缺失的 key
            #   不出现在结果里）⇒ PG / SQLite / Redis 三个后端一处改好。
            #   数据层已清过 3312 条孤儿命中，本条防的是**未来**「删条目 →
            #   命中残留 → 次日班车红」重现。
            orphaned += 1
            continue
        tags = tag_list(row.get("tags"))
        if CORE_TAG in tags or CANDIDATE_TAG in tags:
            skipped_candidate += 1
            continue
        if len(picked) >= cap:
            break
        picked.append(key)
        if not dry_run:
            new_tags = ",".join(_add_tag(tags, CANDIDATE_TAG) + [
                f"{AUTO_HOT_PREFIX}:{now:%Y-%m-%d}"])
            try:
                _write_tags(storage, key, new_tags, "tiering.nominate")
            except Exception as e:      # noqa: BLE001 —— 单条失败不中断整轮
                picked.pop()
                stats["failed"].append({"rule": "nominate", "key": key,
                                        "reason": type(e).__name__})
                continue
            # 提名即带基数（2026-10 常驻层入口改革）：打标成功后才补打点 ——
            # 没进候选队列的条目不该凭空多出 3000 次召回。
            if backfill > 0:
                backed_up += backfill_nominate_hits(storage, key, backfill)
    stats["nominate"] = {"count": len(picked), "keys": picked[:DETAIL_KEYS],
                         "already": skipped_candidate, "window_days": cfg["window_days"],
                         "nominate_min_hits": cfg["nominate_min_hits"],
                         "backfill_hits": backfill, "backfilled": backed_up,
                         "orphaned": orphaned,
                         "dry_run": dry_run}


# --------------------------------------------------------------------------
# 规则二：自动审核通过 / 驳回（替换旧 auto_promote）
# --------------------------------------------------------------------------

def _rule_auto_review(storage: Any, t: Dict[str, Any], now: datetime, *,
                      budget_total: int, used: int, dry_run: bool,
                      stats: Dict[str, Any]) -> int:
    """候选自提名日起满 review_window_days ⇒ **自动审核**（批 / 驳 二选一）。

    * **自提名日起累计命中** ≥ `promote_approve_hits` ⇒ 自动打 core
      （走手动 promote 同一条存储路径：`_with_core` + `_apply` + 加
      `tier:auto-promoted`），与规则三的「机器放进来的不豁免」天然兼容）；
      🔴 命中口径的窗口锚点是**提名日**（`_hits_since_nomination`），
      不是「现在减 review_window_days」—— 基数（提名瞬间补的那 3000 条）
      在复核那一刻**仍然计入**；
    * 不足 ⇒ 自动驳回：摘 `core_candidate`、保留 `tier:auto-hot:<提名日>`
      历史戳、加 `tier:auto-rejected:<驳回日>`，**条目留检索层**（不进降级队列、
      不打 `tier:demote-candidate`）—— 驳回与降级是两件事；
    * 未满窗的候选**不动**（人可手动先批/驳；本轮也不计入 approved/rejected，
      计入 still_pending）。

    返回本轮因自动批准产生的占用字符数（驳回不增占用，故不入 used）。
    """
    cfg = t["promote_auto"]
    review_days = int(cfg["review_window_days"])
    approve_hits = int(cfg["promote_approve_hits"])
    rows = [r for r in storage.list_by_tag(CANDIDATE_TAG) or [] if r.get("key")]
    recent = _hits_since_nomination(storage, rows)
    used_now = used
    approved: List[str] = []
    rejected: List[str] = []
    pending: List[str] = []
    held: List[str] = []
    for row in rows:
        key = str(row["key"])
        nominated_dt = nominated_on(row)
        if nominated_dt is None:
            pending.append(key)
            continue
        age = (now - nominated_dt).total_seconds() / 86400.0
        if age < review_days:
            pending.append(key)
            continue
        hits = int(recent.get(key, 0))
        if hits >= approve_hits:
            # 批准：预算护栏留给这一分支；驳回不耗预算、不受护栏约束。
            length = len(str(row.get("content") or ""))
            if budget_total > 0 and used_now + length > budget_total:
                held.append(key)        # 留队列，下一轮再试
                continue
            if not dry_run:
                try:
                    _promote(storage, key, "tiering.auto_review_approved",
                             extra=[AUTO_PROMOTED_TAG], drop=[CANDIDATE_TAG])
                except Exception as e:      # noqa: BLE001
                    stats["failed"].append({"rule": "auto_review_approved",
                                            "key": key,
                                            "reason": type(e).__name__})
                    continue
            approved.append(key)
            used_now += length
        else:
            # 驳回：摘 core_candidate + 加 tier:auto-rejected:<驳回日>；
            # tier:auto-hot:<提名日> 不动（哪条提的、什么时候提的历史）。
            current_tags = tag_list(row.get("tags"))
            current_tags = _drop_tag(current_tags, CANDIDATE_TAG)
            new_tags = ",".join(current_tags + [
                f"{AUTO_REJECTED_PREFIX}:{now:%Y-%m-%d}"])
            if not dry_run:
                try:
                    _write_tags(storage, key, new_tags, "tiering.auto_review_rejected")
                except Exception as e:      # noqa: BLE001
                    stats["failed"].append({"rule": "auto_review_rejected",
                                            "key": key,
                                            "reason": type(e).__name__})
                    continue
            rejected.append(key)
    stats["auto_review"] = {
        "approved": approved[:DETAIL_KEYS],
        "approved_count": len(approved),
        "rejected": rejected[:DETAIL_KEYS],
        "rejected_count": len(rejected),
        "still_pending": len(pending),
        "held_for_budget": held[:DETAIL_KEYS],
        "held_count": len(held),
        "review_window_days": review_days,
        "promote_approve_hits": approve_hits,
        "dry_run": dry_run,
    }
    # 旧解析兼容：auto_promote 段保留、停用、count 恒 0（旧看板若按字段读不会崩）。
    stats["auto_promote"] = {
        "count": 0,
        "keys": [],
        "note": "replaced by auto_review",
    }
    return used_now - used


# --------------------------------------------------------------------------
# 规则三：降级（压力轨提名 + 100% 强降 1 条 + 豁免）
# --------------------------------------------------------------------------

def _pressure_order(rows: List[Dict[str, Any]], hits: Dict[str, int],
                    last_hit: Dict[str, str]) -> List[Dict[str, Any]]:
    """「最不活跃先走」排序：近 90 天命中数升序 → last_hit 旧 → updated 旧。

    🟡 **命中数这个信号在常驻层是弱化的**（见模块 docstring 规则 3）：常驻条目是被
    **注入**的，不是靠检索召回的，绝大多数长期命中为 0 ⇒ 实际多数轮次由后两个键
    决定顺序。这是刻意的保守：没有真实热度差时，按「旧」总比按「新」安全。

    时间戳**升序**即「越旧越靠前」；解析不出来的记 `-inf`（当作最旧 ——
    没有时间戳的条目不该因为「排不进去」而被判成最新、永远留在常驻里）。
    """
    def _ts_key(value: Any) -> float:
        dt = _parse_ts(value)
        return dt.timestamp() if dt else float("-inf")

    return sorted(
        rows,
        key=lambda r: (hits.get(str(r.get("key")), 0),
                       _ts_key(last_hit.get(str(r.get("key")))),
                       _ts_key(_row_time(r)),
                       str(r.get("key"))),
    )


def _rule_signal_demote(storage: Any, t: Dict[str, Any], *, dry_run: bool,
                        stats: Dict[str, Any]) -> None:
    """信号轨（**不看水位**）：常驻条目吃了负反馈 ⇒ 直接提名退居。

    为什么排在压力轨前面：压力轨的排序分（命中 / 时间）在本层是弱信号，常驻条目
    绝大多数命中为 0 ⇒ 实际退化成「按年龄排序」，那是**保命阀**（水位真的顶住了
    才动），不是判据。而「人说过这条没用」是**真信号**：负反馈走的是
    `record_feedback(False)`（-2）/ `correct_fragments`（-1），都是人在点。

    🔴 **本轨不受 `demote_exempt.explicit_core` 豁免**（与压力轨/强降的处置刻意
    不同）：豁免保护的是「班车替主脑做主、自动降级」；本轨只**提名**一个建议，
    批不批仍然在人手里。若连提名都豁免，用户明确说过「这条误导」也会被班车装作
    没听见 —— 那条豁免就荒谬了。只有 100% 强降那条**自动动作**维持豁免。

    幂等：已带 `DEMOTE_CANDIDATE_TAG` 的跳过（一天只提一次，不刷标签）。
    """
    out: Dict[str, Any] = {"count": 0, "keys": [], "already": 0, "dry_run": dry_run}
    stats["signal_nominate"] = out
    rows = [r for r in storage.list_by_tag(CORE_TAG) or [] if r.get("key")]
    if not rows:
        return
    # `list_by_tag` 的列集里**没有** feedback_score（那是「列常驻条目的浏览面」，
    # 加列要动三个后端，本单不值得）⇒ 批量补一次全字段读（三后端都实现了）。
    try:
        full = storage.get_fragments_batch([str(r["key"]) for r in rows]) or {}
    except Exception as e:      # noqa: BLE001 —— 读不到就一条都不提（宁可漏）
        logger.warning("keepsake: tiering 信号轨读反馈失败 %s: %s", type(e).__name__, e)
        return
    picked: List[str] = []
    for row in rows:
        if len(picked) >= t["batch_cap"]:
            break
        key = str(row["key"])
        tags = tag_list(row.get("tags"))
        if DEMOTE_CANDIDATE_TAG in tags:
            out["already"] += 1
            continue
        if feedback_of(full.get(key) or {}) >= 0:
            continue
        picked.append(key)
        if not dry_run:
            try:
                _write_tags(storage, key,
                            ",".join(_add_tag(tags, DEMOTE_CANDIDATE_TAG)),
                            "tiering.signal_nominate")
            except Exception as e:      # noqa: BLE001 —— 单条失败不中断整轮
                picked.pop()
                stats["failed"].append({"rule": "signal_nominate", "key": key,
                                        "reason": type(e).__name__})
    out["count"] = len(picked)
    out["keys"] = picked[:DETAIL_KEYS]


def _rule_demote(storage: Any, t: Dict[str, Any], *, budget_total: int, used: int,
                 dry_run: bool, stats: Dict[str, Any]) -> None:
    """信号轨（前置，不看水位）+ 压力轨：水位 ≥ warn_ratio ⇒ 按最不活跃提名退居；
    100% 时强降**恰好 1 条**（强降仍享 `explicit_core` 豁免）。"""
    _rule_signal_demote(storage, t, dry_run=dry_run, stats=stats)
    cfg = t["demote_pressure"]
    exempt_on = t["demote_exempt"]["explicit_core"]
    ratio = (used / budget_total) if budget_total > 0 else 0.0
    out: Dict[str, Any] = {"warned": False, "ratio": round(ratio, 4),
                           "nominated": {"count": 0, "keys": []},
                           "forced": {"count": 0, "keys": []},
                           "exempt_held": 0, "dry_run": dry_run}
    stats["demote"] = out
    if budget_total <= 0 or ratio < cfg["warn_ratio"]:
        return

    rows = [r for r in storage.list_by_tag(CORE_TAG) or [] if r.get("key")]
    keys = [str(r["key"]) for r in rows]
    hits, last_hit = _hits_by_key(storage, keys, PRESSURE_WINDOW_DAYS)
    ordered = _pressure_order(rows, hits, last_hit)

    queued = {str(r["key"]): r for r in ordered
              if DEMOTE_CANDIDATE_TAG in tag_list(r.get("tags"))}
    # 待退位队列的预期释放量：已经在队列里的那些（等审核），本轮不再重复提名
    relief = sum(len(str(r.get("content") or "")) for r in queued.values())
    target = used - cfg["release_ratio"] * budget_total
    nominated: List[str] = []
    for row in ordered:
        if len(nominated) >= t["batch_cap"]:
            break
        if relief >= target:
            break
        key = str(row["key"])
        if key in queued or DEMOTE_CANDIDATE_TAG in tag_list(row.get("tags")):
            continue
        if is_explicit_core(row.get("tags"), exempt=exempt_on):
            out["exempt_held"] += 1     # 豁免：可以进建议，但班车不替主脑做主
        nominated.append(key)
        relief += len(str(row.get("content") or ""))
        if not dry_run:
            tags = _add_tag(tag_list(row.get("tags")), DEMOTE_CANDIDATE_TAG)
            try:
                _write_tags(storage, key, ",".join(tags), "tiering.demote_nominate")
            except Exception as e:      # noqa: BLE001
                nominated.pop()
                relief -= len(str(row.get("content") or ""))
                stats["failed"].append({"rule": "demote_nominate", "key": key,
                                        "reason": type(e).__name__})
    out["warned"] = True
    out["nominated"] = {"count": len(nominated), "keys": nominated[:DETAIL_KEYS],
                        "release_ratio": cfg["release_ratio"], "batch_cap": t["batch_cap"]}

    # 100% 才强降，且**只降 1 条**：从队列尾部往上找第一条不豁免的。
    # 「队列」= 本轮之前已在等的 + 本轮刚提名进去的（它们此刻同样在等人审）——
    # 队列一条都没有时不强降：那等于跳过审核直接动常驻，宁可多占一轮预算。
    queue_keys = set(queued) | set(nominated)
    if ratio >= 1.0 and cfg["force_at_full"]:
        victim = None
        for row in reversed(ordered):
            key = str(row["key"])
            if key not in queue_keys:
                continue
            if is_explicit_core(row.get("tags"), exempt=exempt_on):
                continue
            victim = row
            break
        if victim is not None and not dry_run:
            key = str(victim["key"])
            try:
                _demote(storage, key, "tiering.demote_force",
                        extra=[], drop=[DEMOTE_CANDIDATE_TAG])
                out["forced"] = {"count": 1, "keys": [key]}
            except Exception as e:      # noqa: BLE001
                stats["failed"].append({"rule": "demote_force", "key": key,
                                        "reason": type(e).__name__})


# --------------------------------------------------------------------------
# 班车本体
# --------------------------------------------------------------------------

def budget_total_of(config: Optional[Dict[str, Any]]) -> int:
    """注入预算字符数（`retrieval.core_char_budget`）。

    🔴 口径与 provider 的 `_core_budget()` **同一个键**：调用方
    （`scripts/tiering_cron.py`）把整份 config.json **读一次**传进来，
    预算与 provider 读的是同一个文件里的同一个值 —— 口径仍只有一份，
    不在这里另存一份常量。键缺失 / 非法 ⇒ 回落 60000（与 provider 同款兜底）。
    """
    raw = (config or {}).get("retrieval", {}) if isinstance(config, dict) else {}
    value = (raw or {}).get("core_char_budget") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 60000
    try:
        n = int(value)
    except ValueError:
        return 60000
    return n if n > 0 else 60000


def run_tiering(storage: Any, config: Optional[Dict[str, Any]] = None, *,
                dry_run: bool = False, now: Any = None) -> Dict[str, Any]:
    """跑一轮分级班车 → stats dict。**三条规则顺序执行，单条失败不中断整轮**。

    * `enabled=false` ⇒ 第一行返回 `{"skipped": "disabled"}`，**零写**；
    * `dry_run=True` ⇒ 只算不写（观察期人肉跑；`enabled=true` 但不落任何数据）；
    * `now` 注入本轮的「现在」，**所有 age / 窗口判据都吃它**（测试别去改时钟）；
    * 跑完（dry 也算）落一份状态文件给 `GET /api/v1/tiering/status` 读。
    """
    t = resolve_tiering(config)
    if not t["enabled"]:
        return {"skipped": "disabled", "enabled": False, "dry_run": dry_run,
                "reason": "tiering.enabled=false（缺省关死；观察期用 --dry 人肉跑，"
                          "看清 stats 再在设置里开总开关）"}
    now_dt = _now_dt(now)
    total = budget_total_of(config)
    core_rows = [r for r in storage.list_by_tag(CORE_TAG) or [] if r.get("key")]
    used = sum(len(str(r.get("content") or "")) for r in core_rows)

    stats: Dict[str, Any] = {
        "enabled": True,
        "dry_run": bool(dry_run),
        "now": now_dt.isoformat(),
        "budget": {"used": used, "total": total,
                   "ratio": round(used / total, 4) if total > 0 else 0.0},
        "core_count": len(core_rows),
        "failed": [],
    }
    _rule_nominate(storage, t, now_dt, dry_run=dry_run, stats=stats)
    used += _rule_auto_review(storage, t, now_dt, budget_total=total, used=used,
                              dry_run=dry_run, stats=stats)
    _rule_demote(storage, t, budget_total=total, used=used, dry_run=dry_run, stats=stats)
    stats["budget"]["used_after"] = used

    stats["last_run"] = now_dt.isoformat()
    stats["state_file"] = write_state(stats)
    return stats
