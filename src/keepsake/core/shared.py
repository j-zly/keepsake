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

import hashlib
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 日志名**钉在搬家前的模块名上**：logger 名是对外可观测行为（外部 log 配置、
# caplog 过滤都按名找），搬目录不改名，否则等于悄悄改行为。
logger = logging.getLogger("keepsake.storage_shared")

# ---------------------------------------------------------------------------
# 分词口径单源（2026-10 ks_recall_v1）
# ---------------------------------------------------------------------------
#
# 🔴 为什么要有这一节：改前**文档侧与查询侧走的是两套分词**，是「有索引却查不到」
#   的第一号成因（生产实测：`search_bm25("StrategyCompare")` 返 0 条，而同一条碎片
#   `content_tsv @@ plainto_tsquery('simple','回测')` = true）。
#
#   文档侧：`_tsv_tokens` / `_tok_string` = `jieba.lcut` 后交给 PG `to_tsvector('simple')`
#           或 SQLite `unicode61` —— 两边都会**再按 `\w+` 切一刀**（连字符/括号都被切掉）。
#   查询侧：`segment_query` 对**纯英文查询直接 `query.split()`、根本不过 jieba**，
#           且原样保留了 `2026-09-22`、`（` 这类带符号的整块 → 索引里压根没有这个 lexeme
#           ⇒ 该词恒 0 命中，而整条查询因为还有别的词命中而「看起来正常」。
#
# 本节把口径钉成**一份实现**：文档侧与查询侧都调 `tsv_lexemes` / `query_lexemes`，
# 查询词在建 tsquery / FTS MATCH 之前先归一成与索引**逐字同形**的词元。
# 三条约束：
#   1. 零新依赖 —— 只用已在依赖里的 jieba + 标准库 `re`；
#   2. 词典必须一致 —— 两侧都依赖 `splitter` 导入时加载的 jieba 自定义词典；
#      词典文件缺失时**显式 logger.warning**，绝不静默退回另一套分词（见 `tsv_lexemes`）；
#   3. 只增不减 —— 查询词归一**只把一个词拆成若干子词**（超集），
#      召回面只会变大，不会把原来能命中的词删掉。

#: 与 PG `to_tsvector('simple')` / SQLite `unicode61` 的词元口径逐字对齐：`\w+`。
LEXEME_RE = re.compile(r"\w+", re.UNICODE)

#: jieba 自定义领域词典路径（与 `splitter._DOMAIN_DICT` 同一份，见该模块注释）
_DOMAIN_DICT = Path.home() / ".config" / "keepsake" / "jieba_dict.txt"
_dict_warned = False


def warn_if_domain_dict_missing() -> bool:
    """jieba 自定义领域词典不存在 ⇒ **只告警一次**的 WARNING，返回是否缺失。

    🔴 为什么必须显式告警：文档侧与查询侧都靠这份词典把「回测」这类领域词切成
    一个整词。词典缺失时两侧**仍然用 jieba 默认词典**（不是另一套实现，但切法会漂），
    而漂了的直接后果是「入库时切成 A、查询时切成 B」⇒ 静默 0 召回。
    这类问题不报错、不打日志就完全不可见，所以这里明确说出来。
    """
    global _dict_warned
    if _DOMAIN_DICT.exists():
        return False
    if not _dict_warned:
        _dict_warned = True
        logger.warning(
            "keepsake: jieba 领域词典缺失（%s）—— 退回 jieba 默认词典。"
            "分词口径仍与索引侧同源（同一份 tsv_lexemes），但领域词切法可能与入库时不同，"
            "易表现为「有索引却查不到」。生成词典：PgStorage/SqliteStorage.generate_jieba_dict()",
            _DOMAIN_DICT,
        )
    return True


def tsv_lexemes(text: str) -> List[str]:
    r"""正文/查询 → tsvector 词元（**分词口径的唯一实现**，入库与查询共用）。

    流程与原 `_tsv_tokens` / `_tok_string` 逐字相同：jieba 切 → 按 `\w+` 取词元
    （PG `to_tsvector('simple')` 与 SQLite `unicode61` 本来就这么切，提前切只是为了
    让 Python 侧的 doclen/tf/df 计数与索引侧的词元逐字对齐）。
    """
    import jieba                                        # noqa: PLC0415 — 首调建词典
    warn_if_domain_dict_missing()
    return LEXEME_RE.findall(" ".join(w.strip() for w in jieba.lcut(text or "")))


def query_lexemes(terms: List[str]) -> List[str]:
    """查询词列表 → 与索引**逐字同形**的词元列表（保序去重）。

    改前查询词原样进 tsquery / FTS MATCH，`2026-09-22`、`回测（` 这类带符号的整块
    在索引里不存在对应 lexeme ⇒ 恒 0 命中。这里按 `LEXEME_RE` 拆成子词（超集），
    再丢掉纯符号词元（拆完可能只剩标点）与纯数字里长度 ≤ 2 的碎渣（`09`/`22` 这类
    日期残片，留着只会把候选池灌满噪声）。**只增不减**：原来能命中的词一个都没丢。
    """
    out: List[str] = []
    seen = set()
    for t in terms or []:
        for lex in LEXEME_RE.findall(str(t)):
            low = lex.lower()
            if not low or low in seen:
                continue
            # 拆完只剩标点的、以及 ≤2 位纯数字（日期残片）：索引侧词元存在但
            # 毫无区分度，命中它等于把候选池灌满噪声。
            if not any(ch.isalnum() for ch in lex):
                continue
            if low.isdigit() and len(low) <= 2:
                continue
            seen.add(low)
            out.append(lex)
    return out


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

# 🔴 **检索结果**的字段集（两后端必须逐字同形）。
# `valid_until` 进结果：2026-10 ks_retrieval_v3 意图惰性过期后，
# `apply_v2_filters` 的「第五道」要按 fragment 自身的 valid_until 当场判
# 「过期 ⇒ 降级 + 剔除」；不读它就无法判断。`_load_fragments_by_keys` 之前
# 只为「新版继承旧版名次」临时加载，**不动这条字段集合**。
SEARCH_FIELDS = (
    "content", "tags", "category", "source", "created",
    "sentiment_score", "sentiment_label", "feedback_score",
    "invalid_at", "valid_until", "entities", "fragment_type", "applicability",
)

# ---------------------------------------------------------------------------
# 契约 v2：演化链 + 适用条件（2026-10 keepsake_contract_v2，Hy-Memory 移植）
# ---------------------------------------------------------------------------
#
# 改的是什么：原来「顶掉」只有单向的 `superseded_by`，检索侧只能靠它**逐条剔除**，
# 既排不出链身也回溯不了「谁顶了谁」。这里补齐双向 + 链头 + 作废原因 + 适用条件。
#
# 四根列三后端同款（PG/SQLite 是列，Redis 是 hash 字段）：
#   is_latest         链头标记。'1'/1 = 链头（当前有效），'0' = 链身（已被顶掉）。
#                     **老行 NULL 视为链头**（迁移前写的行没人知道它是链头还是链身，
#                     猜成「链身」会把存量记忆整批从检索里抹掉 ⇒ 宁多不少）。
#   supersedes        反向链：JSON 数组，存「本条顶掉了哪些旧 key」。
#   supersede_reason  作废原因（调用方给的，没有就是空串）。
#   applicability     适用条件，自由文本多行 "key: value"（环境无关的记忆留空）。
CONTRACT_V2_COLUMNS = ("is_latest", "supersedes", "supersede_reason", "applicability")

#: 检索侧「只要链头」的 SQL 判据（PG / SQLite 共用一份拼法，逐字对齐）。
#: `COALESCE(..., '1')` 就是「老行 NULL 视为链头」这条纪律的落点。
#: 🔴 **只加在检索侧**（`_search_filter_sql`）：常驻层（`list_by_tag` 的 core 列表）
#:    **故意不加** —— 历史 core 条目照常可见，链身与否是「进不进检索」的事，
#:    不是「还算不算一条记忆」的事。加在这里会让老 core 列表凭空少条目。
CHAIN_HEAD_SQL = "COALESCE(is_latest, '1') <> '0'"

#: 老库未迁移这四列时的修复 SQL —— **只报给人看，代码不擅自 ALTER 生产表**。
#: 与 `api/routers/kb.py::_CTYPE_MIGRATION_HINT` 同款纪律；执行由主脑做
#: （脚本落在 `docs/sql/20261018_contract_v2.sql`）。
CONTRACT_V2_MIGRATION_HINT = (
    "ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS is_latest text NOT NULL DEFAULT '1'; "
    "ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS supersedes text NOT NULL DEFAULT ''; "
    "ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS supersede_reason text NOT NULL DEFAULT ''; "
    "ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS applicability text NOT NULL DEFAULT '';"
)

#: DELETE 路径的 sentinel（`superseded_by='__void__'`）：旧记忆被作废但**不写新事实**。
#: 原来这个字面量在 pipeline.py 与三个存储后端各写一遍 —— 四个地方必须永远相等，
#: 现在它是本模块的唯一定义处（`pipeline.VOID_KEY` 转调这里，旧导入路径不变）。
VOID_KEY = "__void__"


def append_supersedes(existing: Any, old_key: str) -> str:
    """反向链「追加」：把 `old_key` 并进已有的 `supersedes` 值，返回新的 JSON 数组串。

    为什么是追加而不是覆盖：`supersedes` 一条记忆可能顶掉**多条**旧条
    （pipeline 的 `_do_update` 先调 `set_supersedes` 再调 `supersede_fragment`，
    两步写同一根列）—— 覆盖式会把前一步写进去的 key 抹掉，链就断了。

    为什么容忍非 JSON 的历史值：契约 v2 之前 `supersedes` 存的是**裸 key 串**
    （不是数组）。老库里就是那个形态，解不开就当「单元素列表」读进来，
    写回时补成数组 —— 读-改-写一次就自愈，不为一行老数据跑迁移。
    """
    keys: List[str] = []
    raw = (existing or "").strip() if isinstance(existing, str) else ""
    if raw:
        try:
            parsed = json.loads(raw)
            keys = [str(k) for k in parsed] if isinstance(parsed, list) else [str(parsed)]
        except (ValueError, TypeError):
            keys = [raw]                       # 老形态：裸 key 串
    if old_key and old_key not in keys:
        keys.append(old_key)
    return json.dumps(keys, ensure_ascii=False)


def is_chain_head(value: Any) -> bool:
    """`is_latest` 是否表示**链头**（当前有效、应被召回/被去重基准）。

    字段缺失（老数据没有链概念）/ 空串 / `'1'` 一律算**链头** ⇒ 只有明确
    写成 `'0'` 才算链身。理由与 `CHAIN_HEAD_SQL` 的 `COALESCE` 同一个：
    判错方向要是「老数据当成链身」，那一次迁移就把存量记忆整批从检索里抹掉。
    bytes（decode_responses=False）一并认。

    单点化（2026-10 ks_retrieval_v3）：Redis 侧的 `_is_chain_body` 与 SQL 侧的
    `COALESCE(is_latest, '1') <> '0'` 现在都收口到这里 + `CHAIN_HEAD_SQL`，
    「链头 vs 链身」这条判据整个仓库只允许这两个出口说。
    """
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    return str(value or "1").strip() != "0"


#: 意图惰性过期后打上的「已过期事实」标签（Hy-Memory 移植语义）。
#: 检索读到「带 valid_until 且已过期」的条目时降级落此标签；下次当普通事实召回。
EXPIRED_FACT_TAG = "expired_fact"


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    """把 ISO 时间串解析为带 tz 的 datetime；解析失败返回 None（fail-open）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # fromisoformat 在 3.11 之前不认 'Z' 后缀；老行可能落成这种形态
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_intent_expired(fragment: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """`fragment["valid_until"]` 是否已过期（now >= valid_until）。

    没设 valid_until / 解析失败 ⇒ 视为未过期（普通事实，**不该**走降级路径）。
    """
    if not fragment:
        return False
    vu = fragment.get("valid_until") or ""
    if not vu:
        return False
    vu_dt = _parse_iso_dt(vu)
    if vu_dt is None:
        return False
    return vu_dt <= (now or datetime.now(timezone.utc))


def downgrade_expired_intent(
    storage: Any,
    fragment: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """惰性降级过期意图：valid_until 清空 + 打 `expired_fact` 标签（fail-open）。

    读路径顺手做（Hy-Memory `_retrieval/intention.py` 移植语义）：
      * 「带 valid_until 且已过期」⇒ 本次召回里**当场**降级（不依赖定时任务）
      * valid_until 清空 ⇒ 下次当普通事实召回
      * tags 加 `expired_fact` ⇒ 保留语义提示（不挡其他 consumer）

    返回 True = 成功降级；False = 失败（不抛异常，绝不冒到检索路径）。
    """
    if not fragment or not fragment.get("_key"):
        return False
    if not is_intent_expired(fragment, now=now):
        return False
    key = fragment["_key"]

    # 读现有 tags，避免覆盖（tags 是逗号串，见 storage_shared.tag_match_sql 的说明）。
    # 读失败时**也**写入 expired_fact（兜底单标签）—— valid_until 是核心动作，
    # tags 是语义提示，fail-open 不该把后者整个丢掉。
    new_tags: Optional[str] = None
    try:
        existing = storage.get_fragment(key)
        if existing is not None:
            tags_str = (existing.get("tags") or "") if isinstance(existing.get("tags"), str) else ""
            tag_list = [t.strip() for t in tags_str.split(",") if t.strip()]
            if EXPIRED_FACT_TAG not in tag_list:
                tag_list.append(EXPIRED_FACT_TAG)
            new_tags = ",".join(tag_list)
    except Exception as e:      # noqa: BLE001
        logger.debug("storage_shared: 读 tags 失败（fail-open，单标签兜底）: %s: %s",
                     type(e).__name__, e)
        new_tags = EXPIRED_FACT_TAG

    fields: Dict[str, Any] = {"valid_until": ""}
    if new_tags is not None:
        fields["tags"] = new_tags
    try:
        ok = storage.update_fragment_fields(key, fields)
    except Exception as e:      # noqa: BLE001
        logger.debug("storage_shared: 惰性降级写回失败（fail-open，检索不受影响）: "
                     "%s: %s", type(e).__name__, e)
        return False
    if not ok:
        # 列缺 / key 不存在等情况：debug 留痕，但绝不让检索冒错
        logger.debug("storage_shared: 惰性降级写回无更新（key=%s）", key)
    return True

# 「新版继承旧版名次」按 key 载入的 fragment 缓存（进程级 60s TTL）
# 这些条目是稳定内容、量小，缓存避免重复往返。
_FRAG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_FRAG_CACHE_TTL = 60.0


# ---------------------------------------------------------------------------
# 评分时钟（可冻结）—— 只管算分，不管写库
# ---------------------------------------------------------------------------
_SCORING_NOW: Optional[datetime] = None


def scoring_now() -> datetime:
    """评分路径统一读这里的时间。**默认 = 真实墙钟，与引入本函数之前逐字一致。**

    为什么要有（2026-10-12 实测）：`rerank_with_decay` 的时衰减、`match_hot_topics`
    的热词衰减都以「查询那一刻」求值 ⇒ 同一行两次查询分数就不同；两条 `bm25`/`sim`
    逐位相同、只差 1e-7 级衰减项的行，**1.5 秒内会互相穿过**（诊断实录：round0
    A 领先 5.2e-8、round1 B 反超 1.3e-8）⇒ 评测「同题连跑 3 轮 key 序列逐字相同」
    偶发失败。评测器用 `frozen_scoring_now()` 把时钟钉死即可复现；生产不冻结。

    🔴 写路径（`store()` 落 `created`、timeline/时间戳落库）**不许**读本函数 ——
    冻住写时间会污染数据。只有「按当前时间算权重」的读路径才走这里。
    """
    return _SCORING_NOW if _SCORING_NOW is not None else datetime.now(timezone.utc)


def scoring_now_ts() -> float:
    """`scoring_now()` 的 unix 秒 —— 供 `hot_topic_weighted_hits(now_ts=...)` 这类要秒的调用。"""
    return scoring_now().timestamp()


@contextmanager
def frozen_scoring_now(ts: Optional[datetime] = None):
    """把评分时钟钉在 `ts`（缺省 = 进入时刻），退出即恢复真实墙钟（线程内生效）。

    用法（评测器 / 确定性测试）::

        with frozen_scoring_now():
            ...   # 块内每次重排、热词衰减都按同一时刻算 ⇒ 连跑几轮结果逐字相同
    """
    global _SCORING_NOW
    prev = _SCORING_NOW
    # 不给 ts ⇒ 钉在「进入这一刻的评分时钟」上：外层已冻就沿用外层的值（嵌套冻结
    # 不该悄悄跳回真实墙钟），否则就是取真实墙钟。
    _SCORING_NOW = ts if ts is not None else scoring_now()
    try:
        yield _SCORING_NOW
    finally:
        _SCORING_NOW = prev


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
# 治理保留标签（单一判据：提炼 cron 与控制台写端点共用本节）
# ---------------------------------------------------------------------------
#
# 🔴 为什么必须单点（实测事故 + 复发路径）：
#   `core` / `core_candidate` / `shared` / `agent:*` 是**治理保留字** —— 谁
#   能声明它们，等于谁就能决定「这条记忆是否进常驻、是否进候选审核队列、
#   是否归属某个代理」。自动提炼的 LLM 无权声明（它聊到「候选审核」就会
#   自打 `core_candidate`，假申请混进控制台审核队列），控制台的**新增/导入**
#   端点同样无权直打 `core`（进常驻必须走既有条目的 promote 或候选审核）。
#   两处各写一份 set = 迟早漂一份，而漂的那份就是「假申请」的入口。

#: 常驻层标签（core）—— 检索可见性判据用（`RESERVED_TAG_EXACT` 里的同一个词）。
CORE_TAG = "core"

#: 共享层标签（shared）—— 共享层本来就是给人看的，检索侧一直放行。
SHARED_TAG = "shared"

#: 精确相等的保留标签（逗号边界上的完整标签，不做子串误伤：
#: `core_x` / `ycore` / `kind:fact` 都不是保留字）。
RESERVED_TAG_EXACT = frozenset({CORE_TAG, "core_candidate", SHARED_TAG})

#: 前缀即保留的标签族（代理归属由存储层按 `_agent_id` 注入，不是调用方能填的）。
RESERVED_TAG_PREFIX = "agent:"

#: **受控**元认知标签（2026-10 keepsake_reconcile_v2，Hy-Memory `models/memory.py:169-207`
#: 的薄版）—— 提炼管线对自己产出的**自评**，声明它们不改变任何人的权限。
#:
#: 🔴 为什么它们不是上面那个 `RESERVED_TAG_EXACT` 的同类：治理保留字（`core` /
#:    `core_candidate`）是「谁能声明它，决定一条记忆进不进常驻/审核队列」，谁
#:    声明都危险，所以一律剥；而 `low_confidence` / `context_dependent` 只描述
#:    「这条自己拿不准 / 只在特定语境下成立」—— 它不参与召回、排序、注入判据
#:    （检索渲染层原样透出），因此**受控放行**而不是剥除。
#:
#: 单点定义的理由同上：谁都能声明这两个标签本身就是无害的，但「哪两个是受控的」
#: 若有两份口径，迟早漂出一份「又多放行了一个自造标签」的清单。
META_COGNITIVE_TAGS = ("low_confidence", "context_dependent")


def sanitize_distill_tags(raw: Any) -> str:
    """提炼相（自动写入）标签净化：只放行 `META_COGNITIVE_TAGS`，其余自造标签照剥。

    与 `sanitize_reserved_tags` 的分工：那条判「是不是**治理**保留字」，剥掉 +
    记录；本条判「是不是**受控**元认知标签」，只留白名单里的那几个。两条同时需要
    时（控制台写入）调用方自己串起来 —— 提炼相当前只产生本条的输出。

    剥掉的自造标签**不报错也不记 touched**：自动提炼无权声明任何东西，剥掉继续
    写即可（与提炼侧调 `sanitize_reserved_tags(forbid=())` 同款取舍）。

    ponytail: 只做「白名单里有没有」+ 去重两件事，**不做大小写归一** ——
    `Low_Confidence` 与 `low_confidence` 是两个不同的标签，放行前者的判据只会有
    一处（这里），归一了就等于承认两种写法等价、而全库未必是那么写的。
    """
    allowed = set(META_COGNITIVE_TAGS)
    kept: List[str] = []
    for t in (x.strip() for x in str(raw or "").split(",")):
        if t and t in allowed and t not in kept:
            kept.append(t)
    return ",".join(kept)


def sanitize_reserved_tags(raw: Any, forbid: Tuple[str, ...] = ()) -> Tuple[str, List[Tuple[str, str]]]:
    """剥除治理保留标签。返回 `(清洗后的逗号串, [(被处理标签, 原因)])`。

    原因取 `"stripped"`（静默剥除）或 `"forbidden"`（调用方不许写、只剥不够用）。

    `forbid` 里的标签走 `forbidden` 分支：调用方（控制台新增/导入）拿到它就该
    报 400 而不是悄悄丢掉用户的标签 —— 「我打的常驻标签被系统吃掉了」是
    静默丢数据。自动提炼（`cron/memory_distill.py`）传空 `forbid`：它无权声明，
    剥掉继续写。

    ponytail: 不做去重与顺序重排 —— 判据只有「是不是保留字」，其余语义
    （重复标签要不要合并）由存储层的 tags 列自己兜，多做一层就多一处口径。
    """
    banned = set(forbid)
    kept: List[str] = []
    touched: List[Tuple[str, str]] = []
    for t in (x.strip() for x in str(raw or "").split(",")):
        if not t:
            continue
        if t in RESERVED_TAG_EXACT or t.startswith(RESERVED_TAG_PREFIX):
            reason = "forbidden" if t in banned else "stripped"
            touched.append((t, reason))
            continue
        kept.append(t)
    return ",".join(kept), touched


def agent_tag(agent_id: str) -> str:
    """代理归属标签（`agent:<id>`）—— 归属的**唯一落库形态**。

    🔴 碎片的归属**没有独立列**：`ks_fragment` 里没有 `agent_id` 列，归属就是
    `tags` 里的这一个词（`store()` 按 `_agent_id` 注入，见各后端实现）。
    所以「改 agent_id」在存储层的全部含义 = **把这个词换掉**。
    """
    return f"{RESERVED_TAG_PREFIX}{agent_id or ''}"


def replace_agent_tag(tags: str, old_id: str, new_id: str) -> str:
    """`tags` 里的 `agent:<old>` 精确换成 `agent:<new>`；没命中就原样返回。

    🔴 为什么必须按**标签边界**换、不能 `str.replace`：标签之间用逗号分隔，
    而 `agent:win` 是 `agent:win-desktop` 的前缀 ⇒ 子串替换会把**另一个代理**
    的归属也改掉（两个代理的记忆互相污染，事后从库里看不出来）。
    判据与 `tag_match_sql` 一致：**逗号边界上的完整标签相等**。

    ponytail: 只换**首个精确相等**的那个标签（归属标签本来就只该有一个），
    不做「把所有 agent: 前缀的标签都重写一遍」—— 那是另一个动作
    （把一条记忆改归别人），语义不同，不能混在「改 id」里。
    """
    old_tag = agent_tag(old_id)
    new_tag = agent_tag(new_id)
    if not old_id or not new_id or old_tag == new_tag:
        return tags or ""
    parts = [t.strip() for t in (tags or "").split(",")]
    if old_tag not in parts:
        return tags or ""
    parts[parts.index(old_tag)] = new_tag
    return ",".join(parts)


def stamp_owner_tag(tags: str, owner_agent_id: str) -> str:
    """按归属代理 id 盖 `agent:<id>` 标签（**旧的 `agent:` 全替换**）。

    🔴 归属**只认这一处注入**：调用方自报的 `agent:xxx` 一律剥掉 ——
    **不许自报归属**。远端代理的归属由**凭据反查**得出
    （MCP `memory_add` → `provider.add(caller_agent_id=…)` →
    存储层 `store(owner_agent_id=…)`）；本机直写/提炼管线路径不传这个参数
    ⇒ 落进程默认身份（`self._agent_id`）。

    `owner_agent_id` 为空 ⇒ **原样返回**（不盖任何归属标签）。
    """
    owner = str(owner_agent_id or "").strip()
    if not owner:
        return tags or ""
    parts = [t.strip() for t in str(tags or "").split(",") if t.strip()]
    kept = [t for t in parts if not t.startswith(RESERVED_TAG_PREFIX)]
    kept.append(agent_tag(owner))
    return ",".join(kept)


# ---------------------------------------------------------------------------
# 检索可见性（非 primary）—— 三后端共用一份口径
# ---------------------------------------------------------------------------
#
# 🔴 **口径**（2026-10 ks_search_core_filter，与 core_list 侧
#    `govern/visibility.py::visible_core_fragments` **同口径**）：
#
#     非 primary 看得见一行 ⇔
#         ① 行带 `agent:<X>` 标签，且 X ∈ {自己} ∪ {share_memory=1 的代理}
#         ② 行带 `shared` 标签（共享层字面量：共享层本来就是给人看的）
#         ③ 行带 `core` 标签**且没有归属**（老库里还没有 `agent:` 标签的常驻条目）
#
#     core（常驻层）**不再无条件放行** —— 旧口径「凡带 `core` 标签的条目人人可搜」
#     绕过了归属过滤：非 primary 能搜到**别人的**常驻记忆（实测：非 primary 搜一条
#     命中 10 条，其中 9 条是 `core,...,agent:main-brain`）。现在 core 与普通条目
#     用**同一把尺子**：按归属判（对称规则：谁都不许白拿别人的）。
#
#     ③ 那一档是「现状不变」硬回归线：core_list 侧也放行**无归属**的老数据
#     （老库写它的时候存储还没注入归属标签），两边必须一致 —— 检索侧与 core_list
#     侧对同一条老条目给同一个答案，否则「常驻层看得见、检索看不见」这种
#     半拉子状态最难查。
#
# 🔴 **为什么落在这里**：判据散在三个后端各写一份，第三份迟早漏掉前两份的收紧
#    （PG/SQLite 走 SQL 片段、Redis 的 RediSearch 查询语言表达不了 ③ 那一档 ⇒
#    只能取回后在 Python 侧过同一把尺子）。这里是**唯一**那份口径。

def visibility_filter_sql(col: str, visible_agent_tags: List[str], placeholder: str,
                          pos_fn: str = "instr") -> Tuple[str, List[Any]]:
    """非 primary 的**可见性** WHERE 片段（PG / SQLite 共用同一份口径）。

    `visible_agent_tags`：可见的归属标签（`agent:<id>`，**已按 `_clean_tag` 清理**）。
    `placeholder`：驱动占位符（PG `%s` / SQLite `?`）；`pos_fn`：取子串位置的
    函数名（PG `strpos` / SQLite `instr`）—— 与 `tag_match_sql` 同一套约定。

    口径见本节上面的注释：① 可见归属 ② `shared` ③ **无归属的** `core`。
    """
    hay = _TAG_COL_NORM.format(col=col)
    params: List[Any] = []
    parts: List[str] = []

    # ① 可见归属 + ② 共享层字面量（tag_match_sql 产出的是「任一命中」的并集）
    direct_sql, direct_args = tag_match_sql(
        col, list(visible_agent_tags) + [SHARED_TAG], placeholder, pos_fn=pos_fn)
    parts.append(direct_sql)
    params.extend(direct_args)

    # ③ core 且**没有归属**：归一化后的标签串里没有 `,agent:` ⇒ 这一行没有归属
    #    （存储层保证一行至多一个归属标签 —— `_agent_tags` 会先剔掉所有旧的
    #    `agent:` 再补一个，`replace_agent_tag` 只换一个）
    core_sql, core_args = tag_match_sql(col, [CORE_TAG], placeholder, pos_fn=pos_fn)
    owner_sql = f"{pos_fn}({hay}, {placeholder}) = 0"
    parts.append(f"({core_sql} AND {owner_sql})")
    params.extend(core_args)
    params.append(f",{RESERVED_TAG_PREFIX}")

    return "(" + " OR ".join(parts) + ")", params


def _split_tags(tags: Any) -> Tuple[set, set]:
    """`tags` 逗号串 → `(标签集合, 归属代理 id 集合)`。

    归属标签按 `agent:` 前缀取（与 `govern/visibility.py::fragment_owner` 同意图：
    逗号边界上的完整标签；分隔容错同款）。
    """
    tag_set: set = set()
    owners: set = set()
    for part in str(tags or "").replace("|", ",").split(","):
        part = part.strip()
        if not part:
            continue
        tag_set.add(part)
        if part.startswith(RESERVED_TAG_PREFIX):
            owners.add(part[len(RESERVED_TAG_PREFIX):].strip())
    return tag_set, owners


def row_visible_to_non_primary(tags: Any, visible_owners: Iterable[str]) -> bool:
    """非 primary 看得见这一行吗？（`visibility_filter_sql` 那条 SQL 片段的
    **行级镜像** —— 判据逐条一致：① 可见归属 ② `shared` ③ 无归属的 `core`）。

    `visible_owners`：看得见的归属代理 id 集合（= {自己} ∪ {共享集}）。
    """
    tag_set, owners = _split_tags(tags)
    if SHARED_TAG in tag_set:                       # ② 共享层
        return True
    if owners & {a for a in visible_owners if a}:    # ① 可见归属
        return True
    return (not owners) and CORE_TAG in tag_set      # ③ 无归属的 core


def row_visible_after_core_narrow(tags: Any, visible_owners: Iterable[str]) -> bool:
    """**core 那一档**的归属收窄（Redis 侧专用 —— 判据就是
    `row_visible_to_non_primary`，不多不少）。

    🔴 为什么只收 core 那一档：RediSearch 的查询语言只能做「标签并集」，
    表达不了 ③ ——「有 `core` **且**没有 `agent:`」不是并集，是**差集**（索引又
    没有 `WITHSUFFIXTRIE` ⇒ `agent:*` 前缀匹配用不了）⇒ core 的归属收窄只能
    在候选取回后做。**其余两档（可见归属 / `shared`）由查询式里的标签并集负责**
    ⇒ 非 core 的行这里一律放行（它们能进候选集，本来就过了查询式的收窄）。

    两条合起来与 PG/SQLite 的 SQL 片段等价：
      * core + 别人的归属 + 非共享 ⇒ 丢（SQL：core 分支不成立，且不属可见归属）
      * core + 自己的/共享集的归属 ⇒ 留
      * core + **无归属**（老数据）⇒ 留
      * core + 共享层 ⇒ 留（SQL 的 `shared` 分支）
      * 不带 core ⇒ 留（查询式已按归属/shared 收过）
    """
    tag_set, _ = _split_tags(tags)
    if CORE_TAG not in tag_set:
        return True
    return row_visible_to_non_primary(tags, visible_owners)


def fragment_key_for(text: str) -> str:
    """记忆条目 key —— 三后端 `store()` 的**同一套算法**（sha256 前 12 位）。

    调用方（控制台导入的去重判据）必须走这里，而不是自己再拼一遍 sha256：
    两处各拼一次，某一侧改了截断长度，去重就会静默失效（每条都被当成新条目
    重复入库）。`PgStorage._fragment_key_for` 直接转调本函数。
    """
    return f"memory:frag:{hashlib.sha256((text or '').encode()).hexdigest()[:12]}"


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
# 共用：确定性 tiebreaker（同分按 key 升序）
# ---------------------------------------------------------------------------

def tiebreak_by_key(
    fragments: List[Dict[str, Any]],
    score_key: str = "_combined_score",
) -> List[Dict[str, Any]]:
    """**并列 tiebreaker**：同分时按 `key` 字典序（升序）定序。

    🔴 为什么需要（2026-10 ks_recall_v1 验收锚点）：排序只用一个分字段，而分是
    浮点连乘（sim × decay × emotion × feedback × hot × attention），**同分极其常见**
    （BM25 归一化后大量候选挤在同一个值上）。`list.sort` 稳定 ⇒ 同分行沿用输入次序，
    而输入次序来自候选 SQL 的返回序 / HNSW 的近似返回序，跨次运行不保证一致 ⇒
    「同一查询连查多次 key 序列不同」的抖动就是这么来的。

    规则：**只改同分项之间的相对次序**，不改任何一行的分数、不改条数、不改分档。
    无 `_key` 的行按 `or ""` 兜底 ⇒ 同分时排在有 key 的行**之前**（检索结果恒带
    `_key`，这条只兜异常形状；PG 侧原 docstring 写的是「排在最后」，与代码不符，
    这里按代码的实际行为写，别再让文档骗人）。
    """
    if len(fragments) < 2:
        return fragments
    return sorted(fragments,
                  key=lambda f: (-float(f.get(score_key, 0.0) or 0.0),
                                 f.get("_key") or ""))


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
        after_super = kept

    # 第五道：意图惰性过期（2026-10 ks_retrieval_v3 借鉴 Hy-Memory）
    # 读路径顺手做：带 valid_until 且已过期的条目当场降级 + 从本次结果剔除。
    # 不增定时任务、不改 valid_until 现存语义（过去 valid_until 仍由检索过滤先挡掉）。
    # fail-open：降级失败（get_fragment / update_fragment_fields 抛错）只 debug，
    # 检索结果本身一个字段都不许变。
    after_expire: List[Dict[str, Any]] = []
    for f in after_super:
        if is_intent_expired(f):
            try:
                downgrade_expired_intent(storage, f)
            except Exception as e:      # noqa: BLE001
                logger.debug("storage_shared: 惰性降级抛错（fail-open）: %s: %s",
                             type(e).__name__, e)
            continue   # 从本次结果剔除；下次当普通事实召回
        after_expire.append(f)
    after_super = after_expire

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

    # 评分时钟：默认真实墙钟；评测器用 frozen_scoring_now() 钉死后这里是常量 ⇒ 跨轮可复现
    now = scoring_now()

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
    from ..domain.splitter import _STOP_WORDS                   # noqa: PLC0415

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
    from ..domain.splitter import _STOP_WORDS                   # noqa: PLC0415

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
