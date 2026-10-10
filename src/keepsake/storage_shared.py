"""
与后端无关的检索共用逻辑 —— Redis / PostgreSQL 两个后端**共用同一份实现**。

🔴 **这是批 2 的架构重点**：检索后处理（RRF 融合 / v2 过滤 / 综合重排 / 批量载入）
与「排序权重公式」都是纯函数逻辑，只跟候选 dict 的形状有关，与后端怎么把候选取出来
无关。两个后端各自抄一份 = 两边迟早漂移，且上层 `_rrf_fuse` / `_rerank_with_decay`
按同样的键取值，一漂移就**静默错排**（不报错，只是排出来的东西不对）。

因此这里以**普通模块函数**（不是 mixin / 不是基类）存在，两个后端在类体里直接
绑定同一个函数对象：

    class RedisStorage(StorageBase):
        _rrf_fuse = rrf_fuse            # ← 不是 def，是别名
    class PgStorage(StorageBase):
        _rrf_fuse = rrf_fuse            # ← 同一个对象

好处：`RedisStorage._rrf_fuse is PgStorage._rrf_fuse` 恒为 True（测试直接断言对象
身份），且 AST 里全仓只有一处 `def`（不���能出现「同名两份实现」）。

每个函数以 `storage`（self）为第一参，因为它们要读 `self._final_limit` /
`self._decay_half_days` 等实例权重。真正跟后端有关的部分收敛成两个**必须由每个后端
提供的小钩子**（都很薄，各 5~10 行）：

  * `storage._fetch_superseded_by(keys) -> {key: superseded_by}`   读封边标记
  * `storage._fetch_fragments(keys) -> {key: fragment_dict}`       批量读碎片

其余全部共用。`storage.py` / `storage_pg.py` 都 `import keepsake.storage_shared`，
两者之间**不互相 import**（storage_pg 只在文档里说明它对齐 storage 的语义）。

历史上这些函数是 `RedisStorage` 的私有方法（2026-09 引入），本模块只是把它们搬到
一起，函数体逐行未改 —— Redis 侧行为零变化由 tests/test_v2_search.py 全绿兜底。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 排序权重常量（原本写在 storage.py 模块顶部，两个后端共用同一份定义）
# ---------------------------------------------------------------------------

DECAY_HALF_DAYS = 60                     # 时间衰减半衰期（天）
FEEDBACK_POSITIVE_BOOST = 1.3           # 正反馈 ×1.3
FEEDBACK_NEGATIVE_PENALTY = 0.5         # 负反馈 ×0.5
HOT_TOPIC_BOOST = 1.2                   # 命中热门话题 ×1.2
HOT_TOPIC_DECAY_HALF_DAYS = 30          # 热门话题时间衰减半衰期（天）

# 检索产出的 fragment 里固定会出现的字段（Redis / PG 共用的结果形状）。
# 🔴 上层 `_rrf_fuse` / `_apply_v2_filters` 按同样的键取值，增删字段必须同步改这里。
FRAGMENT_FIELDS = (
    "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "invalid_at", "valid_until", "entities", "fragment_type",
)

# 🔴 **检索结果**的字段集（比 FRAGMENT_FIELDS 少 valid_until）。
# 为什么少：Redis 侧 search_bm25 / search_knn 读 valid_until 只是为了「跳过已封边
# 的历史版本」，并不把它放进结果；_load_fragments_by_keys 才把它带出去。
# 两后端的结果必须逐字同形，所以把「哪个字段进结果」也钉死在这里。
SEARCH_FIELDS = (
    "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "invalid_at", "entities", "fragment_type",
)

# 「新版继承旧版名次」按 key 载入的 fragment 缓存（进程级 60s TTL）
# 这些条目是稳定内容、量小，缓存避免重复往返。
_FRAG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_FRAG_CACHE_TTL = 60.0


# ---------------------------------------------------------------------------
# 纯计算：tag 过滤的 SQL 片段（PG / SQLite 共用同一份；Redis 侧靠索引的 TAG 字段）
# ---------------------------------------------------------------------------

#: `tags` 列的**真实落库形态**是**逗号串**（`a,b` / `x,agent:worker`；PG 与 SQLite
#: 实测 `SELECT DISTINCT tags` 见 /tmp/ks_tag_pre.txt），Redis 侧索引也是
#: `tags TAG SEPARATOR ,`（RediSearch 按逗号切 token 后精确匹配）。
#: 所以 SQL 侧的命中判据必须是「**逗号**边界上的完整标签相等」。
#:
#: 🔴 历史上这里写的是「**竖线**包裹 + 子串」（`instr('|'||tags||'|', '|tag|')`），
#:    竖线在库里根本不存在 ⇒ 只要该行有第二个标签就永远匹配不上（`tag_filter`
#:    非空时召回恒 0，`agent:` 隔离同病）。现在改成逗号边界，语义对齐 Redis。
#: 用 `instr`/`replace` 而不是 `LIKE`：tag 里的 `%` `_` 不当通配符；
#: `replace(x, a, b)` 两边都有；**取位置的函数名两边不同**：PG 只有 4 参版的
#: `instr(string, substring, start, occurrence)`（PG11+），两参形式不存在
#: （实测 PG 18.6 报 `function instr(text, unknown) does not exist`）⇒ PG 侧必须用
#: `strpos`，SQLite 侧才是两参的 `instr`。
#:
#: ponytail: 空格容错只做「逗号两侧各一个空格」这一轮 replace（迁移进来的原样行
#:            就是 `c, d` 这种形态）。连续多个空格（`a,  b`）不归一，若真出现
#:            再把这两层 replace 套两遍即可 —— 现在不套，避免每行多两次字符串扫描。
_TAG_COL_NORM = "replace(replace(',' || {col} || ',', ' ,', ','), ', ', ',')"


def tag_match_sql(col: str, tags: List[str], placeholder: str,
                  pos_fn: str = "instr") -> Tuple[str, List[str]]:
    """「tags 里含其中任一标签」的 SQL 片段 + 参数（多标签是**并集/OR**）。

    `tags` 必须已按 `_clean_tag` 清理（分隔符/引号/空格都剔掉了，否则会自己造边界）。
    `placeholder` 是驱动占位符：PG 是 `%s`，SQLite 是 `?`；
    `pos_fn` 是取子串位置的函数名：PG 传 `strpos`（PG 没有两参 `instr`），
    SQLite 传 `instr`；两者都返回首个匹配位置，0 = 不匹配。
    """
    if not tags:
        return "", []
    hay = _TAG_COL_NORM.format(col=col)
    sql = "(" + " OR ".join([f"{pos_fn}({hay}, {placeholder}) > 0"] * len(tags)) + ")"
    return sql, [f",{t}," for t in tags]


# ---------------------------------------------------------------------------
# 纯计算：注意力 / 热词加权（两个后端共用同一份公式）
# ---------------------------------------------------------------------------

def attention_boost_from_topics(
    rows: List[tuple],
    content: str,
    boost_max: float = 1.5,
) -> float:
    """注意力命中加权（1.0 ~ boost_max）——**纯计算，不碰任何后端**。

    参数:
        rows:    [(topic, score), ...]，已按 score 降序且未过期
        content: 碎片内容
    公式（与 2026-09 起线上一直跑的那份逐字相同）：
        命中话题的 score 之和 / 全部话题 score 之和 = ratio（封顶 1.0）
        → 1.0 + (boost_max - 1.0) * ratio
    两者都调用它：`attention.match_attention_boost`（Redis）与 `PgStorage.match_attention`。
    """
    if not rows or not content:
        return 1.0
    content_lower = content.lower()
    total_score = 0.0
    max_score = 0.0
    for topic, score in rows:
        sc = float(score)
        if len(topic) >= 2 and topic in content_lower:
            total_score += sc
        max_score += sc
    if max_score <= 0:
        return 1.0
    ratio = min(total_score / max_score, 1.0)
    return 1.0 + (float(boost_max) - 1.0) * ratio


def hot_topic_weighted_hits(
    topics: List[str],
    last_seen: Dict[str, float],
    text: str,
    now_ts: float,
    decay_half_days: float = HOT_TOPIC_DECAY_HALF_DAYS,
) -> float:
    """热词时间衰减加权命中数 —— **纯计算，不碰任何后端**。

    参数:
        topics:    热门话题词（已按热度降序、未过期）
        last_seen: {话题: 最后提及时间戳}；缺项按 0.5 折半
    公式：命中且有 last_seen → 2^(-天数/半衰期)；命中但无 last_seen → 0.5
    RedisStorage.match_hot_topics 与 PgStorage.match_hot_topics 都调它。
    """
    if not topics or not text:
        return 0.0
    decay_half = float(decay_half_days)
    text_lower = text.lower()
    weighted_hits = 0.0
    for topic in topics:
        if len(topic) < 2 or topic not in text_lower:
            continue
        seen_ts = last_seen.get(topic)
        if seen_ts and seen_ts > 0:
            days_ago = max(0, (now_ts - seen_ts) / 86400.0)
            weighted_hits += 2.0 ** (-days_ago / decay_half)
        else:
            weighted_hits += 0.5  # 无时间戳的折半
    return weighted_hits


# ---------------------------------------------------------------------------
# 共用：按 key 批量载入碎片（供「新版继承旧版名次」注入新版候选）
# ---------------------------------------------------------------------------

def load_fragments_by_keys(storage: Any, keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """按 key 一次性载入多条 fragment（字段与 search_bm25 产出一致）。

    ⚠️ 必须由后端的 `_fetch_fragments(keys)` 批量取 —— 逐条读就是 N+1 往返
    （Redis 侧实测 0.17s/次，一次查询能白加 1 秒延迟）。

    本函数只做「缓存 + 字段筛选 + _key 标注」，怎么取由后端决定。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not keys:
        return out
    now = time.time()
    if now - _FRAG_CACHE["ts"] > _FRAG_CACHE_TTL:
        _FRAG_CACHE["data"].clear()
        _FRAG_CACHE["ts"] = now
    miss = [k for k in keys if k not in _FRAG_CACHE["data"]]
    if not miss:
        return {k: _FRAG_CACHE["data"][k] for k in keys if k in _FRAG_CACHE["data"]}
    try:
        fetched = storage._fetch_fragments(miss) or {}   # noqa: SLF001 — 后端钩子
    except Exception as e:
        logger.debug("storage_shared: batch load fragments failed: %s", e)
        return {k: _FRAG_CACHE["data"][k] for k in keys if k in _FRAG_CACHE["data"]}
    for k, frag in fetched.items():
        if not frag.get("content"):
            continue
        frag["_key"] = k
        out[k] = frag
        _FRAG_CACHE["data"][k] = frag
    for k in keys:
        if k not in out and k in _FRAG_CACHE["data"]:
            out[k] = _FRAG_CACHE["data"][k]
    return out


# ---------------------------------------------------------------------------
# 共用：RRF 融合
# ---------------------------------------------------------------------------

def rrf_fuse(
    storage: Any,
    bm25_results: List[Dict[str, Any]],
    knn_results: List[Dict[str, Any]],
    k: int = 60,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion：两路检索按排名位置融合，返回 `storage._final_limit` 条。

    原理：score(content) = 1/(k+BM25排名) + 1/(k+KNN排名)，k 取 60（业界常用）。
    - 两路都命中 → 分数叠加 → 排最前（交叉验证）
    - 单路命中 → 得该路排名分
    - 按 content 去重合并（同一记忆条目只保留一份，字段取先出现者）
    """
    scores: dict = {}
    items: dict = {}
    for rank, frag in enumerate(bm25_results, 1):
        c = frag.get("content")
        if not c:
            continue
        scores[c] = scores.get(c, 0.0) + 1.0 / (k + rank)
        items[c] = frag
    for rank, frag in enumerate(knn_results, 1):
        c = frag.get("content")
        if not c:
            continue
        scores[c] = scores.get(c, 0.0) + 1.0 / (k + rank)
        if c not in items:
            items[c] = frag

    fused = sorted(items.values(), key=lambda f: -scores[f.get("content")])
    for frag in fused:
        frag["_combined_score"] = scores.get(frag.get("content"), 0.0)
    return fused[: storage._final_limit]   # noqa: SLF001 — 实例权重，两个后端都有


# ---------------------------------------------------------------------------
# 共用：v2 后置过滤 + 新版继承旧版名次
# ---------------------------------------------------------------------------

def apply_v2_filters(storage: Any, fragments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """v2 后置过滤：剔除 consumed + superseded；可选 min_score 地板。

    设计点：
      1. fragment_type == "consumed" 由 search_bm25 直接 skip；这里再兜一次（防止
         pipeline 后续路径绕过 search_bm25）
      2. superseded_by 非空 → 封边的旧事实，剔除（封边 ≠ 物理删）
      3. min_score 地板：sim 阈值 = storage._v2_min_score（默认 0.05）。

    读 superseded_by 走后端钩子 `storage._fetch_superseded_by(keys)`；载入新版候选
    走共用 `load_fragments_by_keys`。本函数不含任何 Redis/PG 专属语句。
    """
    if not fragments:
        return fragments

    # 第一道：fragment_type（已有）
    after_type: List[Dict[str, Any]] = []
    for f in fragments:
        if f.get("fragment_type") == "consumed":
            continue
        after_type.append(f)

    # 第二道：批量查 superseded_by（pipeline 写入但 schema 未建索引）
    keys = [f.get("_key") for f in after_type if f.get("_key")]
    superseded_map: Dict[str, str] = {}
    if keys:
        try:
            superseded_map = storage._fetch_superseded_by(keys) or {}   # noqa: SLF001
        except Exception as e:
            logger.debug("storage_shared: v2 superseded_by batch read failed: %s", e)

    # 第三道：新版继承旧版名次
    # 背景：旧版被 superseded 剔除时，其"同一知识的新摘要"（superseded_by 指向的条目）
    # 往往检索名次更靠后甚至没被召回 ⇒ 相关内容凭空消失。
    # 做法：把旧版的名次分转移给新版；新版不在候选里就按 key 载入后注入同等分数。
    if superseded_map:
        by_key = {f.get("_key"): f for f in after_type if f.get("_key")}
        # 只在「旧版本来就在前列（前 5）」时才补新版：低位的旧版被取代不值得再花一次往返
        _scored = sorted((float(f.get("_combined_score", 0.0) or 0.0)
                          for f in after_type), reverse=True)
        _cut = _scored[min(4, len(_scored) - 1)] if _scored else 0.0
        # 一遍：收集可继承的名次（不触发任何后端调用）
        pending = []
        for old_key, new_key in superseded_map.items():
            if not new_key or new_key == "__void__":   # DELETE 路径：无后继
                continue
            old_frag = by_key.get(old_key)
            if not old_frag:
                continue
            sc = float(old_frag.get("_combined_score", 0.0) or 0.0)
            if sc <= 0 or sc < _cut:
                continue
            pending.append((old_key, new_key, sc, old_frag.get("_sim")))
        # 两遍：候选里没有的新版一次性批量载入（避免 N+1）
        to_load = [n for _, n, _, _ in pending if n not in by_key]
        if to_load and len(set(to_load)) != len(to_load):
            to_load = list(dict.fromkeys(to_load))
        loaded = load_fragments_by_keys(storage, to_load) if to_load else {}
        inherited = 0
        for old_key, new_key, sc, old_sim in pending:
            tgt = by_key.get(new_key)
            if tgt is None:
                tgt = loaded.get(new_key)
                if tgt is None:
                    continue
                tgt["_combined_score"] = sc
                if old_sim is not None:
                    tgt["_sim"] = old_sim   # 同一知识：沿用旧版语义分，别被地板误杀
                after_type.append(tgt)
                by_key[new_key] = tgt
            else:
                tgt["_combined_score"] = float(tgt.get("_combined_score", 0.0) or 0.0) + sc
            tgt["_inherited_from"] = old_key
            inherited += 1
        if inherited:
            logger.info("storage: %d 个被取代的旧版把名次转移给新版", inherited)
            if all("_combined_score" in f for f in after_type):
                after_type.sort(key=lambda f: -float(f.get("_combined_score", 0.0) or 0.0))

    after_super: List[Dict[str, Any]] = []
    for f in after_type:
        k = f.get("_key")
        if k and k in superseded_map:
            continue
        after_super.append(f)

    # 第四道：min_score 地板
    floor = getattr(storage, "_v2_min_score", 0.05)   # noqa: SLF001
    if floor and floor > 0:
        kept: List[Dict[str, Any]] = []
        for f in after_super:
            sim = float(f.get("_sim", 0.0) or 0.0)
            if sim < floor:
                continue
            kept.append(f)
        return kept
    return after_super


# ---------------------------------------------------------------------------
# 共用：综合得分重排序（BM25 / KNN 两路都用）
# ---------------------------------------------------------------------------

def rerank_with_decay(
    storage: Any,
    fragments: List[Dict[str, Any]],
    score_key: str = "_bm25_score",
    is_knn: bool = False,
) -> List[Dict[str, Any]]:
    """综合得分重排序。

    BM25 模式: combined = BM25归一化得分 × 时间衰减 × 情绪权重 × 反馈权重 × 热门权重 × 注意力权重
    KNN 模式:   combined = (1 - 余弦距离/2) × 时间衰减 × 情绪权重 × 反馈权重 × 热门权重 × 注意力权重

    🔴 KNN 的 `score_key` 语义 = **余弦距离**（越小越近），与 Redis
    `DISTANCE_METRIC COSINE` 返回的 doc.score 一致；PG 侧用 `<=>` 算余弦距离，
    量纲对齐后才可共用本函数。BM25 的原始分先做 min-max 归一化，
    故 PG 侧 `ts_rank_cd` 的具体刻度不影响最终排序（只影响 _sim 的相对比例）。
    """
    if not fragments:
        return fragments

    now = datetime.now(timezone.utc)

    # ---- Step 1: 计算语义相似度 sim ----
    for frag in fragments:
        raw = float(frag.get(score_key, 0.0))
        if is_knn:
            # KNN: score 是余弦距离（0~2），越小越近
            frag["_sim"] = 1.0 - max(0.0, min(1.0, raw / 2.0))
        else:
            frag["_sim"] = raw  # 暂存原始 BM25 分数，后面归一化

    # ---- Step 2: BM25 模式用 min-max 动态归一化 ----
    if not is_knn and fragments:
        scores_raw = [float(f.get(score_key, 0.0)) for f in fragments]
        max_raw = max(scores_raw) if scores_raw else 1.0
        if max_raw < 0.001:
            max_raw = 1.0
        for frag in fragments:
            raw_val = float(frag.get(score_key, 0.0))
            frag["_sim"] = raw_val / max_raw

    # ---- Step 3: 六维权重综合 ----
    for frag in fragments:
        sim = float(frag.get("_sim", 0.0))

        # 0: 已纠正碎片直接压到最低，永远不出现在 Top-K
        tags = frag.get("tags", "")
        if "corrected" in (tags if isinstance(tags, str) else ""):
            frag["_combined_score"] = -1.0
            continue

        # 3a: 时间衰减
        created_str = frag.get("created", "")
        if not created_str:
            decay = 0.01
        else:
            try:
                created = datetime.fromisoformat(created_str)
                age_days = (now - created).total_seconds() / 86400.0
                if age_days < 0:
                    age_days = 0
            except (ValueError, TypeError):
                decay = 0.01
                age_days = 0
            else:
                decay = 2.0 ** (-age_days / storage._decay_half_days)   # noqa: SLF001

        # 3b: 情绪权重（基于烈度，不再分正负）
        try:
            intensity = float(frag.get("sentiment_score", 0))
        except (ValueError, TypeError):
            intensity = 0.0
        # intensity 0.0~2.0 → 权重 1.0~1.0+2.0*factor
        emotion_factor = getattr(storage, "_emotion_intensity_factor", 0.4)  # noqa: SLF001
        emotion_w = 1.0 + min(intensity, 2.0) * emotion_factor

        # 3c: 反馈权重
        try:
            fb = float(frag.get("feedback_score", 0))
        except (ValueError, TypeError):
            fb = 0.0
        if fb > 0:
            feedback_w = 1.0 + (storage._feedback_positive_boost - 1.0) * min(fb / 3.0, 1.0)  # noqa: SLF001
        elif fb < 0:
            feedback_w = 1.0 - (1.0 - storage._feedback_negative_penalty) * min(abs(fb) / 3.0, 1.0)  # noqa: SLF001
        else:
            feedback_w = 1.0

        # 3d: 热门话题加权
        # 🔴 读**实例**上的配置（与情绪/反馈/注意力同一口径），模块常量仅作兜底。
        # 这里原本直接读 HOT_TOPIC_BOOST ⇒ `hot_topic_boost` 配置项从头到尾是死的：
        # 传什么都不影响重排。现 getattr 兜底，老对象/桩对象行为不变（默认仍是 1.2）。
        hot_boost = getattr(storage, "_hot_topic_boost", HOT_TOPIC_BOOST)
        hot_w = 1.0
        content = frag.get("content", "")
        if content and hasattr(storage, "match_hot_topics"):
            try:
                hits = storage.match_hot_topics(content, limit=10)
                if hits >= 3:
                    hot_w = hot_boost
                elif hits >= 1:
                    hot_w = 1.0 + (hot_boost - 1.0) * (hits / 3.0)
            except Exception:
                pass

        # 3e: 注意力加权
        attn_w = 1.0
        if content and hasattr(storage, "match_attention"):
            try:
                attn_w = storage.match_attention(content)
            except Exception:
                pass

        frag["_combined_score"] = sim * decay * emotion_w * feedback_w * hot_w * attn_w
        frag["_weights"] = {
            "sim": round(sim, 4),
            "decay": round(decay, 4),
            "emotion": round(emotion_w, 4),
            "feedback": round(feedback_w, 4),
            "hot_topic": round(hot_w, 4),
            "attention": round(attn_w, 4),
        }

    fragments.sort(key=lambda x: x.get("_combined_score", 0), reverse=True)
    return fragments


# ---------------------------------------------------------------------------
# 语料维护的纯计算：同义词发现 + jieba 用户词典（SQLite / PostgreSQL 共用）
# ---------------------------------------------------------------------------
#
# 🔴 **为什么这一段也共用**（与上面的检索后处理同一个理由）：`discover_synonyms`
#    的判定链（降噪筛 → 词频门槛 → Jaccard/共现 → 每词条截断 → 与手工项合并）
#    逐条都在两个后端的 `discover_synonyms` 里各写一遍的话，两边迟早漂移 ——
#    而同义词表是**检索查询式扩展**的输入（`_expand_terms`），漂移的表现是
#    「同一条查询在两个后端召回到不同结果」且**不报错**，对照评测直接失真。
#
# 后端相关的只剩「怎么把正文取出来」与「怎么把结果写回去」两件，各 ~10 行，
# 留在各自的存储类里；这里的函数**不碰任何后端、不碰 self**。
#
# Redis 侧**没有**并进来：它的截断排序（只按 Jaccard 分、不定字典序 tiebreak）
# 与合并循环（一趟 + `if word in merged: continue`，结果依赖 set 迭代序 ⇒
# 同一语料两次运行结果不同）是**已知的历史行为**，改成与另两侧一致属于
# 改 Redis 既有行为，任务书明确禁止（见本次 verdict）。

#: 同义词发现的降噪黑名单（与 `RedisStorage.discover_synonyms` 的局部常量逐条同源：
#: 2 字母高频虚词 + jieba 切英文常见碎块）。
DENOISE_STOPWORDS = frozenset({
    "eg", "us", "ok", "no", "of", "to", "in", "an", "be", "by",
    "it", "is", "as", "at", "or", "so", "if", "do", "on", "up",
    "he", "we", "me", "my", "am", "go",
    "em", "be", "dd", "ce", "ng", "st", "th", "nt", "ab", "cd",
    "ef", "gh", "ij", "kl", "mn", "op", "qr", "uv", "wx", "yz",
})

#: 每词条同义表上限（防 hub 式泛连）—— 同 Redis 侧 `_MAX_SYNS_PER_WORD`
MAX_SYNONYMS_PER_WORD = 8


def is_pure_ascii_short(w: str) -> bool:
    """纯 ASCII 词长度 < 3 → 视为 jieba 切碎渣（同 Redis `discover_synonyms` 口径）。"""
    try:
        return w.isascii() and len(w) < 3
    except Exception:  # noqa: BLE001 — 判据本身不该让维护入口崩
        return False


def has_chinese(w: str) -> bool:
    """是否含 CJK 基本区汉字（中文对长度门槛用）。"""
    return any(0x4E00 <= ord(c) <= 0x9FFF for c in w)


def synonym_words(content: str) -> List[str]:
    """正文 → 同义词发现的候选词（jieba 切词 + 降噪筛）。

    判据与 Redis 侧逐条同源：`len>=2`、不在 `splitter._STOP_WORDS`、
    不在 `DENOISE_STOPWORDS`、非纯数字、非纯 ASCII 短词（<3 字）。
    """
    import jieba                                        # noqa: PLC0415 — 首调建词典
    from .splitter import _STOP_WORDS                   # noqa: PLC0415

    return [w for w in jieba.lcut(content)
            if len(w) >= 2
            and w not in _STOP_WORDS
            and w not in DENOISE_STOPWORDS
            and not w.isdigit()
            and not is_pure_ascii_short(w)]


def jieba_dict_words(content: str) -> List[str]:
    """正文 → jieba 用户词典的候选词（比 `synonym_words` 宽一档）。

    🔴 两条口径**故意不同**（与 Redis/SQLite 侧逐条一致）：用户词典要把
    「短英文碎片」也收进去（用户自己可能就拿它当词条），所以这里**不**套
    `DENOISE_STOPWORDS` 与 ASCII 短词判据，只保留长度/停用词/纯数字三条。
    改成一致 = 两个后端的词典文件对不齐，不是收敛是回归。
    """
    import jieba                                        # noqa: PLC0415
    from .splitter import _STOP_WORDS                   # noqa: PLC0415

    return [w for w in jieba.lcut(content)
            if len(w) >= 2 and w not in _STOP_WORDS and not w.isdigit()]


def accumulate_word_freq(words: List[str], word_freq: Dict[str, int]) -> None:
    """一条正文的词频累计（原地写回）。`set` 去重 ⇒ 同一碎片内不重复计数。"""
    for w in set(words):
        word_freq[w] = word_freq.get(w, 0) + 1


def accumulate_co_occurrence(words: List[str], co_occur: Dict[Tuple[str, str], int]) -> None:
    """一条正文的共现累计（原地写回）。

    `sorted(set(words))` 去重 ⇒ 同一碎片内重复出现的词只算一次（不去重会让共现
    数被词频放大、Jaccard 虚高）；排序是为了让 `(a, b)` 这个 pair 键与词序无关 ——
    否则同一对词会以两种次序入表，Jaccard 查不到。
    """
    uniq = sorted(set(words))
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            pair = (uniq[i], uniq[j])
            co_occur[pair] = co_occur.get(pair, 0) + 1


def discover_synonym_pairs(
    word_freq: Dict[str, int],
    co_occur: Dict[Tuple[str, str], int],
    min_word_freq: int,
    jaccard_threshold: float,
    min_co_occurrence: int,
) -> Tuple[Dict[str, set], int]:
    """候选词两两配对 → `{词: {同义词}}`（每词条截断到 8）+ 配对数。

    成对判据（与 Redis/SQLite 侧逐条相同）：Jaccard >= `jaccard_threshold`
    **或** 共现次数 >= `min_co_occurrence`；中文对额外要求至少一方是 ≥2 字汉字。
    """
    # ponytail: O(候选数²)。候选由词频门槛压住（默认 10 次），
    # 语料再大一个数量级时该换 MinHash/SIMHash，改动点只在这一个循环里。
    candidates = {w for w, f in word_freq.items() if f >= min_word_freq}
    new_map: Dict[str, set] = {}
    pair_score: Dict[Tuple[str, str], float] = {}
    discovered_groups = 0
    for word_a in sorted(candidates):
        for word_b in sorted(candidates):
            if word_a >= word_b:
                continue
            if (has_chinese(word_a) or has_chinese(word_b)) and not any(
                    has_chinese(w) and len(w) >= 2 for w in (word_a, word_b)):
                continue                        # 中文单字对：靠 _STOP_WORDS 之外再兜一层
            c = co_occur.get((word_a, word_b), 0)
            union = word_freq[word_a] + word_freq[word_b] - c
            jaccard = (c / union) if union > 0 else 0.0
            if jaccard >= jaccard_threshold or c >= min_co_occurrence:
                new_map.setdefault(word_a, set()).add(word_b)
                new_map.setdefault(word_b, set()).add(word_a)
                discovered_groups += 1
                pair_score[(word_a, word_b)] = max(
                    pair_score.get((word_a, word_b), 0.0), jaccard)

    # 每词条上限 8：按 Jaccard 降序截断（hub 式泛连被剪掉）。
    # 🔴 平手时**再按词字典序**定序：只按分数排的话，同分项的先后取决于
    #    `set` 的迭代序（PYTHONHASHSEED 随机化）⇒ 同一份语料重跑得到不同的表，
    #    「机械对照」根本无法成立。
    capped: Dict[str, set] = {}
    for word, syns in new_map.items():
        if len(syns) <= MAX_SYNONYMS_PER_WORD:
            capped[word] = syns
            continue
        capped[word] = set(sorted(
            syns,
            key=lambda s: (-pair_score.get((word, s) if word < s else (s, word), 0.0), s),
        )[:MAX_SYNONYMS_PER_WORD])
    return capped, discovered_groups


def merge_synonym_maps(existing: Dict[str, set], discovered: Dict[str, set]) -> Dict[str, set]:
    """已有（手工/历史）项 + 自动发现项 → 最终 `{词: {同义词}}`。

    已有项优先、**不被自动发现覆盖**（手工添加的词条不该被一次 rebuild 洗掉）。
    🔴 **两趟 + 排序遍历**：若一趟遍历且 `if word in merged: continue`，某词若先
    作为别人的反向映射被建出来，它自己的同义表就被这一句跳过 ⇒ 结果取决于
    「谁先被遍历」，而遍历序来自 `set` 迭代序（PYTHONHASHSEED 随机化）⇒
    同一份语料两次运行结果不同，逐 term 对照无从谈起。先按 term 排序、先铺已有
    项、再统一补反向 ⇒ 结果与遍历序完全无关。
    """
    merged: Dict[str, set] = {t: set(s) for t, s in existing.items()}
    for word in sorted(discovered):
        if word in existing:
            continue
        merged.setdefault(word, set()).update(discovered[word])
    for word in sorted(discovered):
        if word in existing:
            continue
        for syn in discovered[word]:
            merged.setdefault(syn, set()).add(word)
    return merged


def synonym_rows(merged: Dict[str, set]) -> List[Tuple[str, str]]:
    """`{词: {同义词}}` → `[(term, JSON 串)]`（**不带引号**，参数化写入用）。

    同义词表逐行小（几十~几百），一个事务写完；JSON 串排序输出 ⇒ 同语料重跑
    得到**逐字节相同**的表（Redis 侧从 set 直出、顺序不定，两边对不齐的正是这里）。
    """
    import json                                           # noqa: PLC0415

    return [(w, json.dumps(sorted(s), ensure_ascii=False)) for w, s in merged.items()]


def jieba_dict_entries(
    word_freq: Dict[str, int],
    synonym_terms: List[str],
    min_freq: int = 2,
) -> List[Tuple[str, int]]:
    """词典词条 `[(词, 词频)]`，按 `(-词频, 词)` 排序（确定性 ⇒ 重跑逐字节相同）。

    同义词表里的 term 至少算 3 次：手工加的词不该因为语料里没出现过就被筛掉。
    排序键固定成字典序（Redis 侧只按 `-词频` + dict 插入序，同频词的行序跨后端
    对不齐）—— 见 `generate_jieba_dict` 的 docstring。
    """
    freq = dict(word_freq)
    for term in synonym_terms:
        freq[term] = max(freq.get(term, 0), 3)
    return sorted(((w, f) for w, f in freq.items() if f >= min_freq),
                  key=lambda x: (-x[1], x[0]))


def maintenance_client(storage: Any) -> Any:
    """【已退役 2026-10 ks_pmn】取「全库维护」用的 Redis 连接。

    合并（consolidator）与遗忘（forgetter）现在**只通过 StorageBase 的维护原语**
    （`scan_fragment_keys` / `get_fragments_batch` / `write_fragments_batch` /
    `update_fragment_fields` / `delete_fragments_batch`）访问存储，两个后端语义等价，
    本函数**已无任何调用方**。保留它只为不打断可能存在的外部 import；
    新代码请勿再用（它返回 None 就等于「PG 上整条维护链路不可用」的老形态）。
    """
    getter = getattr(storage, "_get_client", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None
