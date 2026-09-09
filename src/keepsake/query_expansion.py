"""LLM 动态查询扩展 — 治词汇鸿沟（热路径零延迟）。

# 为什么做（2026-09 ks_retr 任务）
`storage.search_bm25` 现有同义词扩展靠 `keepsake:synonyms` 共现词典：
  * 统计噪音大（jieba 切英文产生 eg→max 之类碎渣）
  * 新词对没共现统计就永远扩不出
  * 召回受限于历史数据

加 LLM 动态扩展层 —— 用 glm-4-flash 把 query 拆成 2-8 字检索词组
（不整句改写），用作 OR 扩展。但**热路径绝不拖慢**：
  * 缓存命中 → 同步并入查询（Redis hash 一次 HGET，<1ms）
  * 缓存未命中且 BM25 结果 < min_results → **先返回原结果**，
    后台线程异步调 glm 扩展 → 写缓存（供下次同类查询受益）
  * 缓存未命中且结果 ≥ min_results → 什么都不做（节约调用）

# 设计点
  * 配置：config.json `retrieval.query_expansion` {enabled, min_results, cache_ttl, max_terms}
  * 复用 `resolve_llm_channel_cached` + `_call_llm`（走已趟平的坑：智谱免费端点）
  * 5s socket 级超时；429/超时/解析失败 → 静默放弃（不重试，下次同查询再试）
  * 提示词要「拆词组」不「改写整句」+ 黑名单（首先/其次/然后/可以/需要/建议/如果）
  * 长度 4-12 字符过滤；剥离 <think>...</think> 段
  * 复用 `_expand_terms` 通道注入查询词（不另起查询构造）
  * 后台线程 daemon=True；不让程序退出时挂起
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------

# 缓存 key（Redis hash, field=归一化query, value=JSON 扩展词表）
QEXP_CACHE_KEY = "keepsake:qexp"

# 默认 TTL（24h）
DEFAULT_QEXP_TTL = 86400

# 默认最小结果数（BM25 命中 < 此值才触发后台扩展）
DEFAULT_QEXP_MIN_RESULTS = 3

# 默认最大扩展词数（LLM 给多了就截到上限）
DEFAULT_QEXP_MAX_TERMS = 6

# 扩展词长度过滤（4-12 字符；过短=虚词，过长=整句）
MIN_TERM_LEN = 4
MAX_TERM_LEN = 12

# LLM 调用超时（socket 级）—— 设 5s 防拖死后台线程
LLM_SOCKET_TIMEOUT = 5.0

# 黑名单：常见虚词/连词/语气词（不当作检索词）
# 中文虚词 + 英文常见前缀碎块（discover_synonyms 修过的同类）
_STOP_BLACKLIST = frozenset({
    # 中文
    "首先", "其次", "然后", "最后", "例如", "可以", "需要",
    "建议", "如果", "但是", "因为", "所以", "而且", "还是",
    "这个", "那个", "什么", "怎么", "为什么", "这样", "那样",
    "的", "了", "在", "是", "我", "有", "和", "就", "不",
    "也", "很", "到", "说", "要", "去", "你", "会", "着",
    # 英文（jieba 碎词 + 常见虚词）
    "the", "this", "that", "what", "why", "how", "and",
    "but", "for", "with", "not", "are", "was", "had",
    "its", "has", "all", "can", "use", "get", "set",
})

# 思考段剥离：glm-4-flash 等常在 answer 前先 <think>...</think>
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def normalize_query(query: str) -> str:
    """归一化 query（用作 qexp cache field key）。

    规则：strip + collapse 空白 + 转小写。空字符串返回空字符串（避免 Redis 空 field）。
    """
    if not query:
        return ""
    q = query.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q


# ---------------------------------------------------------------------------
# LLM 提示词（few-shot 内嵌）
# ---------------------------------------------------------------------------

# 设计点（写在注释里）：
#   1. 拆 3-4 个 2-8 字检索词组 —— 不要整句改写、不要同义改写
#   2. 不要常见虚词（首先/其次/然后/可以/需要/建议/如果）
#   3. 每个词 4-12 字符（不是单词数量，是字符长度）
#   4. 输出严格 JSON 数组（不是对象）
QEXPANSION_SYSTEM = "你是一个搜索关键词提取助手，擅长把一句话拆成可独立检索的关键词组。"

QEXPANSION_USER_TEMPLATE = """把以下查询拆成 {max_terms} 个可独立检索的关键词组（每个 4-12 字符）。

规则：
1. 每个词组 4-12 字符（汉字算 1 字符、字母算 1 字符）
2. 只输出关键词本身，不要解释、不要举例、不要对话
3. 不要包含这些常见虚词：首先/其次/然后/最后/例如/可以/需要/建议/如果/但是/因为/所以
4. 不要整句改写、不要同义改写 —— 拆出能各自独立搜的词
5. 输出严格 JSON 数组：`["关键词1", "关键词2", ...]`

查询：{query}

关键词："""


def build_expansion_messages(query: str, max_terms: int) -> List[Dict[str, str]]:
    """构造 LLM 调用的 messages 列表。"""
    return [
        {"role": "system", "content": QEXPANSION_SYSTEM},
        {"role": "user", "content": QEXPANSION_USER_TEMPLATE.format(
            max_terms=max_terms,
            query=query.strip(),
        )},
    ]


# ---------------------------------------------------------------------------
# LLM 输出解析
# ---------------------------------------------------------------------------

def _strip_think(raw: str) -> str:
    """剥离 <think>...</think> 段（glm-4-flash 等会先思考再回答）。"""
    if not raw:
        return ""
    # 先匹配完整 think 段
    s = _THINK_TAG_RE.sub("", raw)
    # 若没有完整闭合但存在开口 → 截掉开口后的所有内容（视为思考段未完）
    if "<think>" in s and "</think>" not in s:
        s = _THINK_OPEN_RE.sub("", s)
    return s.strip()


def _extract_json_array(raw: str) -> Optional[List[Any]]:
    """从 LLM 输出中抽出 JSON 数组。

    兼容外层 ```json ... ``` 包裹、前后废话。失败返回 None。
    """
    if not raw:
        return None
    s = raw.strip()
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return None
    candidate = s[start:end + 1]
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, list):
        return None
    return obj


def parse_expansion_response(raw: str) -> List[str]:
    """LLM 输出 → 干净的扩展词列表。

    步骤：
      1. 剥离 <think>...</think>
      2. 抽出 JSON 数组
      3. 字符串化、去重、过滤长度 4-12、过滤黑名单
      4. 截到 max_terms 上限
    """
    if not raw:
        return []
    s = _strip_think(raw)
    arr = _extract_json_array(s)
    if not arr:
        return []
    out: List[str] = []
    seen: set = set()
    for item in arr:
        if not isinstance(item, str):
            continue
        t = item.strip()
        if not t:
            continue
        # 长度过滤
        if len(t) < MIN_TERM_LEN or len(t) > MAX_TERM_LEN:
            continue
        # 黑名单过滤（大小写不敏感）
        if t.lower() in _STOP_BLACKLIST:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# 缓存读写（Redis hash，field=归一化 query）
# ---------------------------------------------------------------------------

def _read_cache(client, normalized: str) -> Optional[List[str]]:
    """读 qexp 缓存。命中返回 list（可能空），未命中返回 None。

    设计：缓存空 list 也算命中（避免对「无法扩展」的 query 反复打 LLM）。
    """
    if client is None or not normalized:
        return None
    try:
        raw = client.hget(QEXP_CACHE_KEY, normalized)
    except Exception as e:
        logger.debug("qexp: cache HGET failed: %s", e)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(arr, list):
        return None
    # 过滤无效项（防缓存被脏写污染）
    return [t for t in arr if isinstance(t, str) and t]


def _write_cache(client, normalized: str, terms: List[str], ttl: int) -> bool:
    """写 qexp 缓存（HSET + EXPIRE）。失败静默。"""
    if client is None or not normalized:
        return False
    try:
        pipe = client.pipeline()
        pipe.hset(QEXP_CACHE_KEY, normalized, json.dumps(terms, ensure_ascii=False))
        pipe.expire(QEXP_CACHE_KEY, ttl)
        pipe.execute()
        return True
    except Exception as e:
        logger.debug("qexp: cache write failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# LLM 调用（复用 consolidator._call_llm + resolve_llm_channel_cached）
# ---------------------------------------------------------------------------

def _expand_via_llm(query: str, *, max_terms: int, llm_call_fn) -> List[str]:
    """调 LLM 拿扩展词。失败/超时/解析失败 → 静默返回 []。

    llm_call_fn: (messages, model) -> Optional[str] —— 可注入（测试用）。
    """
    if not query or not query.strip():
        return []
    try:
        messages = build_expansion_messages(query, max_terms=max_terms)
    except Exception as e:
        logger.debug("qexp: build messages failed: %s", e)
        return []
    try:
        raw = llm_call_fn(messages, "")  # model 由 channel 提供
    except Exception as e:
        logger.debug("qexp: LLM call raised: %s", e)
        return []
    if not raw:
        return []
    return parse_expansion_response(raw)


# ---------------------------------------------------------------------------
# 后台线程（缓存未命中时启动）
# ---------------------------------------------------------------------------

def _background_expand(
    client,
    normalized: str,
    raw_query: str,
    *,
    max_terms: int,
    ttl: int,
    llm_call_fn,
) -> None:
    """后台线程：调 LLM → 写 qexp 缓存。失败静默。"""
    try:
        terms = _expand_via_llm(raw_query, max_terms=max_terms, llm_call_fn=llm_call_fn)
        # 即使空也写（避免重复打 LLM）—— 写空 list
        _write_cache(client, normalized, terms, ttl)
    except Exception as e:
        logger.debug("qexp: background expand failed: %s", e)


# ---------------------------------------------------------------------------
# 同步入口（供 storage.search_bm25 调用）
# ---------------------------------------------------------------------------

def lookup_terms_for_search(
    client,
    normalized_query: str,
    raw_terms: List[str],
    *,
    enabled: bool,
) -> Tuple[List[str], bool]:
    """按 qexp 缓存决定本次 BM25 检索用什么 terms。

    分支：
      * enabled=False 或 client 不可用或 query 为空 → 原样返回 (raw_terms, False)
      * 缓存命中 → 把扩展词并入 (raw_terms, True)
      * 缓存未命中 → 原样返回 (raw_terms, False)；后续由 caller 视结果数决定是否后台扩展

    返回:
        (terms_for_search, cache_was_hit)
    """
    if not enabled or client is None or not normalized_query:
        return raw_terms, False

    cached = _read_cache(client, normalized_query)
    if cached is None:
        return raw_terms, False

    merged = list(raw_terms)
    for t in cached:
        if t and t not in merged:
            merged.append(t)
    return merged, True


def schedule_background_expansion(
    client,
    normalized_query: str,
    raw_query: str,
    *,
    enabled: bool,
    fragments_count: int,
    min_results: int,
    llm_call_fn,
    max_terms: int = DEFAULT_QEXP_MAX_TERMS,
    ttl: int = DEFAULT_QEXP_TTL,
) -> bool:
    """条件成立时启动后台扩展线程。

    启动条件：enabled AND client 可用 AND 缓存未命中（normalized_query 非空）
             AND fragments_count < min_results。

    返回:
        True 表示线程已启动；False 表示未启动（无论原因）。
    """
    if not enabled or client is None or not normalized_query:
        return False
    if fragments_count >= min_results:
        return False
    try:
        t = threading.Thread(
            target=_background_expand,
            args=(client, normalized_query, raw_query),
            kwargs={
                "max_terms": max_terms,
                "ttl": ttl,
                "llm_call_fn": llm_call_fn,
            },
            name="keepsake-qexp",
            daemon=True,
        )
        t.start()
        return True
    except Exception as e:
        logger.debug("qexp: failed to start background thread: %s", e)
        return False


# ---------------------------------------------------------------------------
# 公共导出
# ---------------------------------------------------------------------------

__all__ = [
    "QEXP_CACHE_KEY",
    "DEFAULT_QEXP_TTL",
    "DEFAULT_QEXP_MIN_RESULTS",
    "DEFAULT_QEXP_MAX_TERMS",
    "MIN_TERM_LEN",
    "MAX_TERM_LEN",
    "LLM_SOCKET_TIMEOUT",
    "normalize_query",
    "build_expansion_messages",
    "parse_expansion_response",
    "lookup_terms_for_search",
    "schedule_background_expansion",
]