"""检索结果 LLM 筛选（recall screening）—— 检索后、进上下文前的最后一道筛子。

## 口径（2026-10-24 主脑拍板，**不要再选型**）

影子回测（34 条真实探针、双臂对比）结论：M3.1-Flash 做「检索后、进上下文前」的
候选筛选**零错杀**、注入字符 **-66%**，但单次 ~10s ⇒ **只允许挂在显式检索调用上**，
严禁进任何自动 / 预取热路径。本模块**只**被 `mcp_server.tools` 的
`memory_search` / `kb_search` 在**显式 `screen=true`** 时调用；
常驻注入路径（prefetch / provider）**一条口子都不留**。
常驻层**不参与**筛选（prompt cache 前缀纪律 —— 常驻块在每次请求里逐字不变）。

## 🔴 默认关死 + 唯一配置源

`screening.enabled` **缺省 false**：缺这一节 / `enabled=false` ⇒
**一切行为与接入前逐字一致**（连 config.json 都不必写）。
**没有配置 = 显式关闭**：不许环境变量开关、不许默认付费兜底
（不像 `consolidator` 早期那种「没配就用某家付费模型」的兜底）。
配置唯一源是 config.json 的顶层 `screening` 节，**mtime 热生效**（改完下一轮即生效）。

## 🔴 fail-open：任何失败都原样放行

429 / 超时 / 坏 JSON / 无 key / 关着 ⇒ 一律返回 `([], "skipped:<原因>")`，
调用方照旧把**原始检索结果**返回给代理。筛选是附加值，
它坏了不许让检索瘫掉（与 `dm_hint` 的未读计数同一把尺子）。

**决策位不重试**：筛选是一次「要不要」的判断，重试只会把一次 10s 拖成 30s，
限流时还会把配额烧光 —— 所以这里**刻意不走 `core.http.retrying`**。

## 🔴 越界 id 一律当坏答案

LLM 回的编号只要**越界 / 重复 / 空数组**，整份回答作废
（`skipped:bad_answer` ⇒ 原结果原样放行）：宁可多给几条原文，
也不许因为模型编号乱了就静默丢掉可能有用的一条。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from .http import get_client
# 配置路径与文件指纹**复用 consolidator 的同一份**（`KEEPSAKE_CONFIG` > 默认路径）：
# 同一份 config.json、同一套 mtime 口径 —— 再写一份路径规则迟早漂成「读的不是同一个文件」。
from .ops.consolidator import _config_path, _stat_fingerprint

logger = logging.getLogger("keepsake.core.screening")

# --------------------------------------------------------------------------
# 配置：唯一真值源 = config.json 顶层 `screening` 节
# --------------------------------------------------------------------------

#: 缺省值 —— 缺这一节 ⇒ `enabled=False`（关死）；下面的 model/base_url 只在
#: **已经显式开了**的情况下才会被用到（关着的时候连 key 文件都不碰）。
DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "model": "MiniMax-M3.1-Flash",
    "base_url": "https://api.minimax.cn/v1",
    "api_key_file": "/root/.config/keepsake/screen_key",
    "max_pick": 10,
    "timeout_s": 90,
    # 🔴 4096 是**实测教训值**：M3.1-Flash 的自适应思考吃 token 预算，
    # 给小了模型会「想不完就交白卷」（JSON 不完整 ⇒ 整轮作废）。
    "max_tokens": 4096,
}

#: 各值域（校验与回落用同一份）。上界按「够用且不至于把上下文撑爆」取。
BOUNDS: Dict[str, Tuple[int, int]] = {
    "max_pick": (1, 100),
    "timeout_s": (1, 600),
    "max_tokens": (64, 65536),
}

#: 候选预览长度（送进 LLM 的每条正文截断长度）—— 与回测版一致。
PREVIEW_CHARS = 200


def _parse_bool(value: Any, default: bool) -> bool:
    """布尔解析：只认真布尔与 `true/1/yes/on`（大小写不敏感）的字符串。

    数字不认（`enabled: 1` 到底是开还是关不该猜）—— 与 `core.tiering` 同款口径。
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
    """数值解析 + 值域校验；非法 ⇒ warning + 回落缺省（风格同 `core.tiering`）。

    键**缺省**不走这条路（静默回落缺省）：「没配」是正常状态，
    为它 warning 一次会让每次读配置都刷一屏噪音。
    """
    lo, hi = BOUNDS[name]
    if value is None:
        return default
    if isinstance(value, bool):
        logger.warning("keepsake: screening.%s=%r 非法，回落 %r", name, value, default)
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        logger.warning("keepsake: screening.%s=%r 非法，回落 %r", name, value, default)
        return default
    if not (lo <= n <= hi):
        logger.warning("keepsake: screening.%s=%r 越界(%s~%s)，回落 %r",
                       name, value, lo, hi, default)
        return default
    return n


def _parse_str(value: Any, name: str, default: str) -> str:
    """字符串解析：非字符串 ⇒ warning + 回落缺省；空串也回落缺省。"""
    if value is None:
        return default
    if not isinstance(value, str):
        logger.warning("keepsake: screening.%s=%r 非法，回落 %r", name, value, default)
        return default
    text = value.strip()
    return text or default


def resolve_screening(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 config dict（整份 config.json）解析出**完整**的 `screening` 节。

    任何缺失 / 非法值都回落缺省并 warning（风格照 `core.tiering.resolve_tiering`）：
    配置坏了的代价是「按缺省跑」，不是「检索崩掉」。
    `enabled` 是总开关，**非法值回落 false**（回落成 true 才是事故）。
    """
    raw: Dict[str, Any] = {}
    if isinstance(config, dict):
        node = config.get("screening")
        if isinstance(node, dict):
            raw = node
    return {
        "enabled": _parse_bool(raw.get("enabled"), DEFAULTS["enabled"]),
        "model": _parse_str(raw.get("model"), "model", DEFAULTS["model"]),
        "base_url": _parse_str(raw.get("base_url"), "base_url",
                               DEFAULTS["base_url"]).rstrip("/"),
        "api_key_file": _parse_str(raw.get("api_key_file"), "api_key_file",
                                   DEFAULTS["api_key_file"]),
        "max_pick": _parse_num(raw.get("max_pick"), "max_pick",
                               DEFAULTS["max_pick"]),
        "timeout_s": _parse_num(raw.get("timeout_s"), "timeout_s",
                                DEFAULTS["timeout_s"]),
        "max_tokens": _parse_num(raw.get("max_tokens"), "max_tokens",
                                 DEFAULTS["max_tokens"]),
    }


def screen_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """这一节是不是**开着**。

    `cfg` 传 `resolve_screening()` 的产物（或任何带 `enabled` 的 dict）；
    缺键 / 非布尔 ⇒ **False**（关）。判不出身份就当没开 ——
    反过来（把看不懂的值猜成「开」）才是事故。
    """
    if not isinstance(cfg, dict):
        return False
    return _parse_bool(cfg.get("enabled"), False)


# --------------------------------------------------------------------------
# 配置热读（mtime 指纹）：改完 config.json 下一轮就生效
# --------------------------------------------------------------------------

#: `{配置路径: (指纹, 解析结果)}` —— 指纹口径与 `core.ops.consolidator` 同款。
_CFG_CACHE: Dict[str, Tuple[Optional[Tuple[int, int]], Dict[str, Any]]] = {}


def load_cfg() -> Dict[str, Any]:
    """读 config.json 的 `screening` 节（**mtime 热生效**）。

    * 路径：`KEEPSAKE_CONFIG` > `~/.config/keepsake/config.json`
      （与 consolidator 同一份路径规则，见本模块 import 处的说明）；
    * 缓存：按 (mtime_ns, size) 指纹命中 ⇒ 不重读不重解析；
      **同尺寸改值指纹可能不变** ⇒ 写配置的一侧应调 `invalidate_cfg_cache()`；
    * 文件不存在 / 坏 JSON ⇒ 按**缺省**（= 关死）走，**不抛穿**。
    """
    path = os.path.expanduser(_config_path())
    key = str(path)
    fp = _stat_fingerprint(path)
    hit = _CFG_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]
    cfg: Dict[str, Any] = resolve_screening({})
    if fp is not None:
        try:
            with open(path) as f:
                raw = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("keepsake: config 读取失败（%s: %s），筛选按缺省（关）走",
                           path, e)
            raw = {}
        cfg = resolve_screening(raw if isinstance(raw, dict) else {})
    _CFG_CACHE[key] = (fp, cfg)
    return cfg


def invalidate_cfg_cache() -> None:
    """丢弃配置缓存（改完 config.json 的一侧应调；同尺寸改值指纹不变时靠它）。"""
    _CFG_CACHE.clear()


# --------------------------------------------------------------------------
# 筛选本体
# --------------------------------------------------------------------------

#: 问法（与回测版一致）：required=确信能帮回答才选、preferred=拿不准不选、
#: 宁缺毋滥、只输出 JSON。
SCREEN_PROMPT = """你是一个检索结果筛选器。用户给出一个问题，下面是若干候选条目（按检索相关度排序，编号从 1 开始，每条最多 {preview} 字）。

请挑出「确实能帮上回答这个问题」的条目编号：
- required：只有你确信该条目对回答有帮助时才选。
- preferred：拿不准的不要选；宁缺毋滥 —— 多留一条无关内容，比漏掉一条有用内容危害更大。
- 只输出 JSON，不要输出任何解释或多余文字：{{"keep": [编号, ...]}}

问题：{query}

候选条目：
{candidates}
"""


def build_prompt(query: str, candidates: Sequence[Dict[str, Any]]) -> str:
    """候选列表 → 提问文本（编号 1 起，正文截 `PREVIEW_CHARS` 字）。"""
    lines: List[str] = []
    for i, cand in enumerate(candidates, 1):
        text = "" if not isinstance(cand, dict) else str(cand.get("content") or "")
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        lines.append(f"[{i}] {text}")
    return SCREEN_PROMPT.format(preview=PREVIEW_CHARS,
                                query=str(query or "").strip(),
                                candidates="\n".join(lines))


def _safe_num(value: Any, default: Any, cast: Any) -> Any:
    """取值失败就回落缺省 —— `pick_relevant` **绝不抛穿**（配置坏了也不许掀翻检索）。"""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _read_api_key(cfg: Dict[str, Any]) -> str:
    """读 api_key 文件（**唯一来源**：配置里那个独立小文件）。

    缺文件 / 读不动 ⇒ 空串 ⇒ 上层降级 `skipped:no_key`。
    🔴 **不许环境变量兜底**：key 的来源只有一个，写死在 config.json 指的那条路径上。
    """
    path = str(cfg.get("api_key_file") or "").strip()
    if not path:
        return ""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError) as e:
        logger.warning("keepsake: screening key 文件读不到（%s: %s）—— 按无 key 降级",
                       path, type(e).__name__)
        return ""


def _strip_fences(text: str) -> str:
    """去掉模型偶尔加的 ```json 代码围栏（只去围栏，不动内容）。"""
    out = (text or "").strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[-1] if "\n" in out else out
        if out.endswith("```"):
            out = out[:-3]
    return out.strip()


def _parse_keep(raw_text: str) -> Tuple[Optional[List[int]], str]:
    """模型回复 → `(编号列表, 错误原因)`。

    * 错误原因 `""` ⇒ 解析成功（列表可能为空 —— 空数组由上层判 `bad_answer`）；
    * `"bad_json"` ⇒ **根本不是 JSON**（模型没按要求输出）；
    * `"bad_answer"` ⇒ JSON 合法但**答案形状不对**（没有 `keep` / 不是列表 /
      元素不是编号）—— 与「JSON 坏」分开，便于一眼看出是模型不听话还是传输坏了。
    """
    try:
        data = json.loads(_strip_fences(raw_text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "bad_json"
    if not isinstance(data, dict) or "keep" not in data:
        return None, "bad_answer"
    keep = data["keep"]
    if not isinstance(keep, list):
        return None, "bad_answer"
    ids: List[int] = []
    for item in keep:
        if isinstance(item, bool):
            return None, "bad_answer"
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            ids.append(int(item.strip()))
        else:
            return None, "bad_answer"
    return ids, ""


def pick_relevant(query: str, candidates: Sequence[Dict[str, Any]],
                   cfg: Optional[Dict[str, Any]]) -> Tuple[List[int], str]:
    """挑出「能帮回答这个问题」的候选编号 → `([编号...], "ok" | "skipped:<原因>")`。

    * `candidates`：候选 dict 列表，正文取 `content` 字段；
    * 编号**从 1 起**（与送进模型的编号一致）；
    * 🔴 **任何失败都不抛穿**：429 / 超时 / 坏 JSON / 无 key / 关着
      ⇒ `([], "skipped:<原因>")`，由调用方原样放行原始结果；
    * 🔴 **不重试**（决策位不重试，见模块 docstring）。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not screen_enabled(cfg):
        return [], "skipped:disabled"

    cands = list(candidates or [])
    if not cands:
        return [], "skipped:empty"

    api_key = _read_api_key(cfg)
    if not api_key:
        return [], "skipped:no_key"

    base_url = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "").strip()
    if not base_url or not model:
        return [], "skipped:unconfigured"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是检索结果筛选器，只输出 JSON。"},
            {"role": "user", "content": build_prompt(query, cands)},
        ],
        "temperature": 0,
        "max_tokens": _safe_num(cfg.get("max_tokens"), DEFAULTS["max_tokens"], int),
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        client = get_client(base_url, _safe_num(cfg.get("timeout_s"),
                                                DEFAULTS["timeout_s"], float))
        resp = client.post(f"{base_url}/chat/completions", json=body, headers=headers)
    except httpx.TimeoutException:
        return [], "skipped:timeout"
    except httpx.HTTPError as e:
        logger.warning("keepsake: screening 请求失败（%s: %s）", type(e).__name__, e)
        return [], "skipped:http_error"
    except Exception as e:      # noqa: BLE001 —— 筛选坏不许掀翻检索
        logger.warning("keepsake: screening 请求异常（%s: %s）", type(e).__name__, e)
        return [], "skipped:error"

    if resp.status_code == 429:
        return [], "skipped:rate_limited"
    if resp.status_code >= 400:
        return [], f"skipped:http_{resp.status_code}"

    try:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return [], "skipped:bad_json"

    ids, err = _parse_keep(str(content or ""))
    if err:
        return [], f"skipped:{err}"
    assert ids is not None      # err 为空 ⇒ ids 必非 None

    # 🔴 越界 / 重复 / 空数组 ⇒ **整份回答作废**（原样放行，不做半吊子过滤）。
    if not ids or len(set(ids)) != len(ids):
        return [], "skipped:bad_answer"
    total = len(cands)
    if any(i < 1 or i > total for i in ids):
        return [], "skipped:bad_answer"

    # `max_pick` 是上限：模型多挑了也只留前 N 个（保序）。
    cap = int(cfg.get("max_pick") or DEFAULTS["max_pick"])
    return ids[:cap], "ok"
