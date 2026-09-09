"""写闸门（Ingest Gate）— store 之前的纯函数裁决中间件。

# 为什么做（2026-09-08 事故复盘）

Keepsake 当前「零过滤直存」让几类内容污染召回：

  * CONTEXT COMPACTION 摘要（数千字、含 30+ 过期任务快照）每轮被 store，
    下次检索被注入后把 7 月早已完成的需求当待办复活。
  * builtin MEMORY.md 条目经 on_memory_write 同步进碎片库，与 Hermes 本身
    双份召回。
  * 纯确认短句（"可以的"×15）sim≈0 仍被注入。

# 设计

`decide(text, category, existing_meta=None) -> IngestDecision`

  纯函数，零副作用、零 Redis 依赖，单测可直跑。
  规则按序短路 R1-R7：

    R1 compaction 摘要     → reject / reason="compaction"
    R2 超长 (len > 2000)    → reject / reason="oversize"
    R3 纯确认 / 短状态问句   → reject / reason="low_signal_short"
    R4 纯粘贴 (无字母/汉字/数字) → reject / reason="paste"
    R5 builtin MEMORY.md 同步 → reject / reason="builtin_dup"
    R6 同 hash 已存在 + turn_memory/conversation → update_state / reason="state_only"
    R7 其余                 → store / reason=""

R6 关键闭环：只刷新 updated_at/count，绝不覆盖原 content。
防「[会话摘要] 完成:X」被后来 compaction 残句毁掉终态。

入库前的凭据打码（2026-09 新增，scrub_secrets）是独立的纯函数，
供调用方在 storage.store() 之前显式调用；不集成进 decide() 以保持
其纯函数性质并避免重复打码。

# 调用点

  * KeepsakeProvider.sync_turn()        — store 前 decide() 裁决 + scrub_secrets
  * KeepsakeProvider.on_memory_write()  — 直接 return（双份入库关闭）
  * Pipeline._do_add/_do_update/_fallback_to_v1 — storage.store() 前 scrub_secrets
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# 默认配置（与 KeepsakeProvider._resolve_config 对齐）
# ---------------------------------------------------------------------------

DEFAULT_GATE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "max_len": 2000,
}


# ---------------------------------------------------------------------------
# 决策值
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestDecision:
    """decide() 的返回值。

    action: "store"          — 落库（走原 storage.store 逻辑）
            "update_state"   — 仅刷新 updated_at/count，不覆盖 content
            "reject"         — 拒收，调用方直接 return
    reason: 空串（store）或 R1-R6 的 reason 标识字符串。
    """

    action: str
    reason: str = ""


# ---------------------------------------------------------------------------
# R1 拒收前缀表（case-sensitive, lstrip 后 startswith）：
#   - [CONTEXT COMPACTION   : 压缩摘要（9/8 事故源头）
#   - [System note          : 网关重启/中断等系统注入提示（9/10 实锤漏网）
# ---------------------------------------------------------------------------

_REJECT_PREFIXES = ("[CONTEXT COMPACTION", "[System note")

# ---------------------------------------------------------------------------
# R3 黑名单 + 状态问句正则（固定写全，禁止自作主张扩）
# ---------------------------------------------------------------------------

_CONFIRM_BLACKLIST = frozenset({
    "可以的", "可以", "好", "好的", "行", "行的", "嗯", "嗯嗯", "哦", "噢",
    "ok", "收到", "明白", "知道了", "懂了", "是的", "对", "对的",
    "没事了", "继续", "继续吧", "没有了", "不用", "不用了", "算了",
    "试试", "要的", "要", "加", "搞", "修", "部署", "提交", "合并",
    "看看", "停", "暂停", "恢复", "重试", "来吧", "行吧", "不用管",
    "先不管", "先不动", "随便", "都行",
})

# 状态问句：以这些前缀开头，允许尾部 0-1 个问号（全角或半角）
_STATUS_QUESTION_RE = re.compile(
    r"^(怎么样了|咋样了|目前咋样了|目前是什么情况|目前情况|现在呢|进展呢|结果呢|到哪了|弄好了吗|改好了吗|做完了吗|好了吗|完成了吗|还有吗|然后呢)[？?]?$"
)


# ---------------------------------------------------------------------------
# R7 — 凭据打码（scrub_secrets，2026-09 写侧源头闸门）
# ---------------------------------------------------------------------------
#
# 设计要点：
#   * 入库前最后一道防线，覆盖 5 类凭据（中文/英文 password、API key/token、
#     连接串 Password=、Bearer、高熵串兜底）。
#   * 打码格式：保留匹配前缀（label + separator）+ 值替换为 ***[REDACTED]。
#   * 幂等：第二次 scrub 不再产生新 mask（value 已含 REDACTED 直接跳过）。
#   * 兜底过滤：保守策略，仅当高熵 run 含 `+/=_-` 或大小写字母+数字混合
#     时才视为 secret，避免误伤 IPv4 / URL host / 文件路径 / 普通英文。
# ---------------------------------------------------------------------------

REDACTED = "***[REDACTED]"

# 5 类凭据正则（每条都带 ?P<prefix> + ?P<value> 命名组，方便后续重组）

# 1) 中文/英文 password/passwd 标签（值遇空白 / ; 即停；允许 " ' & 在内以便
#    处理 "Password=\"hunter2\"" / 'token="xxx"' 这类带引号凭据 —— 引号本身
#    与值一起被替换为 REDACTED）
_LABEL_PWD_RE = re.compile(
    r"(?P<prefix>(?:密码|口令|password|passwd))\s*[是为:：=]\s*(?P<value>[^\s;]{3,})",
    re.IGNORECASE,
)

# 2) api_key / token / secret / 密钥 标签（值同样遇空白 / ; 即停）
_LABEL_API_RE = re.compile(
    r"(?P<prefix>(?:api[_-]?key|token|secret|密钥))\s*[是为:：=]\s*(?P<value>[^\s;]{6,})",
    re.IGNORECASE,
)

# 3) Password=... 连接串风格（大小写不敏感；值排除 ; " ' & 终止符）
_LABEL_CONNSTR_RE = re.compile(
    r"(?P<prefix>Password)\s*=\s*(?P<value>[^\s;\"'&]{3,})",
    re.IGNORECASE,
)

# 4) Bearer xxx（OAuth / API token 风格；含 base64 的 `+/=` 字符）
_BEARER_RE = re.compile(
    r"(?P<prefix>Bearer)\s+(?P<value>[A-Za-z0-9+/=_\-\.]{16,})",
    re.IGNORECASE,
)

# 5) 兜底高熵串：12+ 连续 [A-Za-z0-9+=_\-\.]，两侧边界外必须不在该字符集。
#    字符集故意排除 `/`：URL path 段含 `/` 应被断成更短 run，从而避免
#    「https://api.example.com/v1/users」整段被误识别为 secret。
#    base64 中含 `/` 的 token 由 Bearer / ConnectionString 等标签正则捕获，
#    兜底只负责「无标签裸 secret」。
_HIGH_ENTROPY_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9+=_\-\.])(?P<run>[A-Za-z0-9+=_\-\.]{12,})(?![A-Za-z0-9+=_\-\.])"
)

# IPv4 显式过滤（4 段，每段 1-3 位数字）
_IPV4_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")


# ---------------------------------------------------------------------------
# 工具：剥离空白/标点/符号/控制字符，仅保留字母与汉字/数字
# ---------------------------------------------------------------------------

def _strip_text(text: str) -> str:
    """剥离空白/标点/符号/控制字符，仅保留字母（L*）、数字（N*）、汉字（Lo）。

    R3 用：剥离后再做黑名单与状态问句匹配；emoji（多数归 So）一并剥掉。
    """
    out = []
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            out.append(ch)
    return "".join(out).lower()


def _is_pure_paste(text: str) -> bool:
    """R4：纯粘贴判定 — 文本中无任何字母/汉字/数字字符。

    例：「==========」「------------」「////////////」整段 → reject。
    注：真正的 URL 多数含字母（https 等），不会命中 R4，会被 R3 长度阈值兜底。
    """
    for ch in text:
        # CJK 基本汉字 + Ext A
        if "一" <= ch <= "鿿":
            return False
        if "㐀" <= ch <= "䶿":
            return False
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            return False
    return True


# ---------------------------------------------------------------------------
# R7 高熵串判定（兜底过滤 — 保守策略，宁漏勿误）
# ---------------------------------------------------------------------------

# 预编译过滤子正则（避免 _is_high_entropy_secret 内每次现编）
# 注：故意排除 `-` `/` `=` —— 这三个在路径/URL/键值对中极常见，
#     不足以单独作为 secret 信号；dash-prefixed 串（sk-xxxx）由下面的
#     「小写字母 + 数字 + len≥16」兜底判断。
_HAS_SPECIAL_RE = re.compile(r"[+_]")
_HAS_UPPER_RE = re.compile(r"[A-Z]")
_HAS_DIGIT_RE = re.compile(r"[0-9]")
_HAS_LOWER_RE = re.compile(r"[a-z]")
_PURE_NUMDOT_RE = re.compile(r"[0-9.]+")


def _is_high_entropy_secret(s: str) -> bool:
    """判断一个 high-entropy run 是否更像 secret（保守策略）。

    排除：
      - 纯 [0-9.]（如 "154.219.96.202" 等 IPv4）
      - IPv4 显式格式（4 段，每段 1-3 位数字）

    接受（任一条件满足即视为 secret）：
      - 含 [+/=_] 任一字符（base64 / JWT 风格）
      - 大写字母 + 数字 混合（hex / base64 风格）
      - 全小写字母 + 数字 混合 且 len ≥ 16（base36 / 长 hash 风格）
    """
    # 纯数字+点 → 一律排除（IPv4 全段）
    if _PURE_NUMDOT_RE.fullmatch(s):
        return False
    # IPv4 显式格式
    if _IPV4_RE.fullmatch(s):
        return False
    # 含 `+/=_` 任一 → 是 secret
    if _HAS_SPECIAL_RE.search(s):
        return True
    # 大写字母 + 数字 混合
    if _HAS_UPPER_RE.search(s) and _HAS_DIGIT_RE.search(s):
        return True
    # 全小写字母 + 数字 混合 且 len ≥ 16（base36 / 长 hash）
    if len(s) >= 16 and _HAS_LOWER_RE.search(s) and _HAS_DIGIT_RE.search(s):
        return True
    # 其余（纯字母 / 普通英文）→ 不打码（保守）
    return False


# ---------------------------------------------------------------------------
# 入口：纯函数裁决
# ---------------------------------------------------------------------------

def scrub_secrets(text: str) -> Tuple[str, int]:
    r"""凭据文本入库前打码（R7 规则，纯函数）。

    覆盖 5 类凭据：
      1) 「密码/口令/password/passwd = xxx」类明文（值 ≥ 3 字符）
      2) 「api_key/token/secret/密钥 = xxx」类明文（值 ≥ 6 字符）
      3) 「Password=xxx」连接串风格（大小写不敏感；值遇 ; " ' & 即停）
      4) 「Bearer xxx」类 OAuth / API token（值 ≥ 16 字符）
      5) 兜底高熵串：12+ 连续 [A-Za-z0-9+=_.-] 且通过保守过滤
         （避免 IPv4 / URL host / 文件路径误伤）

    打码格式：保留匹配前缀（label + separator），值替换为 `***[REDACTED]`。
    幂等：第二次调用 n_masks=0（value 含 REDACTED 时跳过；REDACTED 本身不在
    高熵字符集内不会被兜底二次匹配）。

    参数:
        text: 待打码文本。

    返回:
        (cleaned_text, n_masks) 元组。n_masks 为本次 scrub 命中的 mask 数。
    """
    if not text:
        return text or "", 0

    state = {"masks": 0}

    def _rep_label(m: re.Match) -> str:
        """4 类标签正则的替换：保留 prefix + separator，值替换为 REDACTED。"""
        full = m.group(0)
        prefix = m.group("prefix")
        value = m.group("value")
        # 幂等保护：值已含 REDACTED → 不再打码
        if REDACTED in value:
            return full
        state["masks"] += 1
        # 重组：prefix + separator + REDACTED
        sep_start = len(prefix)
        sep_end = m.start("value") - m.start(0)
        separator = full[sep_start:sep_end]
        return f"{prefix}{separator}{REDACTED}"

    def _rep_high(m: re.Match) -> str:
        """兜底高熵串替换：先过 _is_high_entropy_secret 过滤。"""
        run = m.group("run")
        if REDACTED in run:
            return run
        if not _is_high_entropy_secret(run):
            return run
        state["masks"] += 1
        return REDACTED

    out = text
    # 先跑 4 类标签（更精确），再跑高熵兜底（避免 REDACTED 二次匹配）。
    # 顺序：连接串风格（Category 3，值遇 ; " ' & 即停）必须先于通用密码标签
    # （Category 1，\S{3,} 贪婪会吃掉分号等终止符）。
    for pattern in (_LABEL_CONNSTR_RE, _LABEL_PWD_RE, _LABEL_API_RE, _BEARER_RE):
        out = pattern.sub(_rep_label, out)
    out = _HIGH_ENTROPY_RUN_RE.sub(_rep_high, out)
    return out, state["masks"]


def decide(
    text: str,
    category: str,
    existing_meta: Optional[Dict[str, Any]] = None,
    gate_cfg: Optional[Dict[str, Any]] = None,
) -> IngestDecision:
    """写闸门裁决函数（纯函数，无副作用、无 Redis 依赖）。

    参数:
        text: 待写入的文本（建议调用方先 strip）。
        category: 调用方传入的 category（"turn_memory" / "memory_tool" / ...）。
        existing_meta: 若同内容 hash 已在碎片库，给出至少含 "key" 字段的元数据；
                       R6 用。None 或空 dict 表示无既有碎片。
        gate_cfg: 覆盖默认配置；None 表示用 DEFAULT_GATE_CONFIG。

    返回:
        IngestDecision（按 R1-R7 顺序短路）。凭据打码（scrub_secrets）是独立
        函数，由调用方在 store 路径显式调用，不在 decide() 内集成以保持
        其纯函数性质。
    """
    cfg = dict(DEFAULT_GATE_CONFIG)
    if gate_cfg:
        cfg.update(gate_cfg)

    if not cfg.get("enabled", True):
        return IngestDecision("store", "")

    # R1: compaction 摘要 / 网关系统注入（lstrip 后前缀匹配，case-sensitive）
    #   - [CONTEXT COMPACTION : 压缩摘要（9/8 事故源头）
    #   - [System note        : 网关重启/中断等系统注入提示（9/10 实锤漏网）
    if text.lstrip().startswith(_REJECT_PREFIXES):
        return IngestDecision("reject", "compaction")

    # R2: 超长
    max_len = int(cfg.get("max_len", 2000))
    if len(text) > max_len:
        return IngestDecision("reject", "oversize")

    # R3: 纯确认 / 状态问句 / 短指令
    stripped = _strip_text(text)
    low = stripped.lower()
    if low in _CONFIRM_BLACKLIST:
        return IngestDecision("reject", "low_signal_short")
    if _STATUS_QUESTION_RE.match(low):
        return IngestDecision("reject", "low_signal_short")
    if 0 < len(stripped) < 8:
        return IngestDecision("reject", "low_signal_short")

    # R4: 纯粘贴（无字母/汉字/数字）
    if _is_pure_paste(text):
        return IngestDecision("reject", "paste")

    # R5: builtin MEMORY.md 同步关闭
    if category == "memory_tool":
        return IngestDecision("reject", "builtin_dup")

    # R6: 同内容已存在 → 仅刷新状态（关键闭环，不覆盖原 content，不打码）
    if existing_meta and category in ("turn_memory", "conversation"):
        return IngestDecision("update_state", "state_only")

    # R7: 通过（入库前的凭据打码由调用方在拿到本决策后调用 scrub_secrets 自行处理；
    #     decide() 保持纯函数性质，不在此处打码以免与调用方重复 / 干扰既有测试）
    return IngestDecision("store", "")


# ---------------------------------------------------------------------------
# R6 执行函数：仅刷新 updated_at/count，绝不覆盖 content
# ---------------------------------------------------------------------------

def update_state_only(storage: Any, existing_meta: Dict[str, Any]) -> bool:
    """R6 命中后调用：仅刷新 updated_at/count，不覆盖 content。

    参数:
        storage: RedisStorage 实例（必须实现 _get_client()）。
        existing_meta: 至少含 "key" 字段（形如 "memory:frag:<hash>"）。

    返回:
        True = 刷新成功；False = 参数缺失或 storage 不可用或 Redis 异常。
    """
    from datetime import datetime, timezone

    if storage is None or not existing_meta:
        return False
    key = existing_meta.get("key")
    if not key:
        return False
    client = storage._get_client()  # noqa: SLF001 — 与 storage.store() 同级用法
    if not client:
        return False
    now = datetime.now(timezone.utc).isoformat()
    try:
        pipe = client.pipeline()
        pipe.hincrby(key, "touch_count", 1)
        pipe.hset(key, "updated_at", now)
        pipe.execute()
        return True
    except Exception:
        return False


__all__ = [
    "IngestDecision",
    "DEFAULT_GATE_CONFIG",
    "decide",
    "update_state_only",
    "scrub_secrets",
    "REDACTED",
]
