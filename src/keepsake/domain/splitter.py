"""文本切分工具 + 情感分析 + **英文词干化** — 按语义完整性切分文本，检测情感倾向。"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import jieba
import jieba.posseg  # 显式导入（该版本 jieba 无 lazy 加载，否则 posseg.lcut 抛 AttributeError 被吞）

logger = logging.getLogger("keepsake.splitter")

# 自定义领域词典路径（由 discover_synonyms 自动生成）
_DOMAIN_DICT = Path.home() / '.config' / 'keepsake' / 'jieba_dict.txt'

#: 🔴 **测试钩子**：注入「主脑式」用户词典（含英文词条）的路径环境变量。
#:
#: 为什么需要它：本仓（88）默认**没有**用户词典 ⇒ jieba 切英文时走 finalseg 的
#: `re_skip` 分支，英文整词原样保留 ⇒ `tests/test_english_stemming.py` 全绿。
#: 那是**同态自证**：两个后端在「同一份词典」下同生共死，看不见「词典里有英文
#: 词条」这一档（主脑树那份 3026 行词典就踩在这档上）。
#: 带 `KEEPSAKE_TEST_USERDICT=tests/fixtures/mini_userdict.txt` 跑第二遍，
#: 才真的踩到主脑那条路。
#:
#: 只在**测试**里用：生产环境永远是 `~/.config/keepsake/jieba_dict.txt` 那一份。
TEST_USERDICT_ENV = "KEEPSAKE_TEST_USERDICT"

#: 已加载过的测试词典（绝对路径）—— 同一份只加载一次。
#: 理由：`jieba.load_userdict` 是**累加**的（`add_word` 每次 `total += freq`），
#: 重复加载会把 jieba 的总词频抬高 ⇒ DAG 的切分口径随加载次数漂移 ⇒
#: 测试结果不可复现。领域词典**不**受此限（它必须能重载，见下面的 docstring）。
_loaded_test_dicts: set = set()


def init_domain_dict() -> None:
    """加载/重载自定义领域词典。

    首次 import 时自动调用一次。插件 initialize() 时也调用一次，
    确保发 /new 后词典被重新加载（此时词典文件可能已被 discover_synonyms 更新）。
    重复调用安全（jieba.load_userdict 是累加的）。

    另加载 `KEEPSAKE_TEST_USERDICT` 指定的测试词典（同一份只加载一次）——
    见 `TEST_USERDICT_ENV` 的说明。路径不存在时**告警**（不静默跳过：
    静默跳过会让「双词典环境自测」变成又一次同态自证）。
    """
    if _DOMAIN_DICT.exists():
        jieba.load_userdict(str(_DOMAIN_DICT))
    _load_test_userdict()


def _load_test_userdict() -> None:
    """加载 `KEEPSAKE_TEST_USERDICT` 指定的词典（未设置 ⇒ 什么都不做）。"""
    raw = os.environ.get(TEST_USERDICT_ENV)
    if not raw:
        return
    path = Path(raw)
    if not path.exists():
        logger.warning(
            "keepsake: %s=%s 指向的文件不存在 —— 测试词典**没加载**，"
            "这一轮跑的是「无用户词典」环境（双词典自测不成立）",
            TEST_USERDICT_ENV, raw,
        )
        return
    key = str(path.resolve())
    if key in _loaded_test_dicts:
        return
    jieba.load_userdict(key)
    _loaded_test_dicts.add(key)
    logger.info("keepsake: 已加载测试用户词典 %s", key)


# 首次 import 时自动加载
init_domain_dict()

NEWLINE = "\n"

# ---------------------------------------------------------------------------
# 英文词干化（Porter2）—— 索引侧与查询侧共用的**唯一**归一函数
# ---------------------------------------------------------------------------
#
# 🔴 **对称铁律**（本节存在的全部理由）：词干化必须是**同一个函数**在
#    **索引侧（content_tsv 生成）**与**查询侧（tsquery / FTS MATCH 构造）**
#    各调一次。只在一侧词干化 = 召回全废（索引侧存 'run'、查询侧查 'runs'
#    ⇒ 恒 0 命中），而且**不报错、不告警**，只是静默地搜不到。
#    ⇒ 归一逻辑单点在本模块（domain 层，跨后端共用），三个后端的分词入口
#    都调它，谁都不许自己写一份。
#
# 🔴 **为什么只对「纯 ASCII 字母词」词干化**：
#    * 中文词元（"回测"）—— Porter 是英文算法，碰了就是噪声；
#    * 含数字/符号的**标识符**（"StrategyCompare01"、"v2"）—— 它们是精确名，
#      词干化会把 "strategycompare01" 砍成 "strategycompare"，于是「按名字找」
#      反而找不到了；这类词原样进索引（子串兜底档负责它们，见 PG 的 trgm 档）；
#    * 长度 < 3 的短词 —— 词干化收益为负（"is"/"as" 这类虚词本就不该变）。
#
# 🔴 **豁免：标签前缀与系统保留字**（"agent" / "tier" / "core" / "shared" …）。
#    标签是**精确逗号边界**判据（`storage_shared.tag_match_sql`），一旦被词干化
#    （"shared" → "share"）就会与标签列对不上 ⇒ 写闸与可见性判据失效。
#    标签那一路的词元另走 `tsv_lexemes(..., stem=False)`（见 core/shared.py）。

try:  # 依赖缺失时**降级为「不词干化」**，绝不 import 期炸掉整个包
    from snowballstemmer import stemmer as _snowball_stemmer
except ImportError:  # pragma: no cover — 依赖装不上时的降级路径
    _snowball_stemmer = None

#: **不进词干化**的词：标签前缀 + 治理保留字 + 可见性相关标签。
#: 与 `storage_shared.RESERVED_TAG_EXACT` / `RESERVED_TAG_PREFIX` 同一套词，
#: 在本模块单列一份是因为 domain 层不许 import core（依赖方向单向）。
STEM_EXEMPT = frozenset({
    "agent", "tier", "core", "core_candidate", "shared",
    "corrected", "consumed", "superseded",
})

#: 词干化的最大迭代轮数（Porter2 的不动点迭代，实测 ≤4 轮收敛）。
_STEM_MAX_ROUNDS = 4

_STEMMER = None
_STEM_WARNED = False


def _get_stemmer():
    """取（或懒建）英文词干器；依赖缺失返回 None（= 不词干化）。"""
    global _STEMMER, _STEM_WARNED
    if _STEMMER is None and _snowball_stemmer is not None:
        _STEMMER = _snowball_stemmer("english")
    if _STEMMER is None and not _STEM_WARNED:
        _STEM_WARNED = True
        logger.warning(
            "keepsake: snowballstemmer 不可用 —— 英文词干化降级为「不词干化」"
            "（召回面变小，但不会错召回；装依赖：pip install 'snowballstemmer>=2.2'）"
        )
    return _STEMMER


def stem_english(word: str) -> str:
    """纯 ASCII 英文词 → Porter2 词干；其余词原样返回。

    🔴 **幂等**（对词干再词干化仍是同一个词干）：Porter2 **不是**幂等的
    （"achilles" → "achill" → "achil"），所以这里迭代到**不动点**为止。
    幂等是必须的：同义词表按词干存储、查询侧再词干化一次，两次不一致
    ⇒ 映射双轨错位（扩出来的同义词对不上索引里的词干）。
    """
    if not word:
        return word
    if len(word) < 3:
        return word
    if not word.isascii() or not word.isalpha():
        return word          # 中文 / 含数字或符号的标识符 / 短词：一律原样
    low = word.lower()
    if low in STEM_EXEMPT:
        return low          # 标签与保留字：词干化会破坏精确匹配判据
    stemmer = _get_stemmer()
    if stemmer is None:
        return low
    cur = low
    for _ in range(_STEM_MAX_ROUNDS):
        nxt = stemmer.stemWord(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur or low


def stem_english_tokens(tokens: Iterable[str]) -> List[str]:
    """逐词词干化（列表版；非字符串元素原样返回）。"""
    out: List[str] = []
    for t in tokens or ():
        out.append(stem_english(t) if isinstance(t, str) else t)
    return out


# ---------------------------------------------------------------------------
# 分词管道：**ASCII 词先行摘出**，其余段才进 jieba
# ---------------------------------------------------------------------------
#
# 🔴 **为什么英文不能喂 jieba**（2026-10 ks_stem_fix 的根因）：
#    jieba 的 DAG / HMM 是**按中文语料训练**的。用户词典里一旦出现英文条目
#    —— 哪怕只是 "it" / "ie" / "ed" 这种两字碎片（旧口径的
#    `generate_jieba_dict` 恰恰会把它们收进去）—— jieba 就会拿词典里的碎片
#    去切**整句英文**：
#        "many cities were visited during the trip"
#          → many / cities / were / vis / it / ed / during / the / trip
#    ⇒ **索引侧存碎片、查询侧发整词** ⇒ 恒 0 命中；碎片还污染 BM25 词频
#      （doclen 变长、tf 变噪），检索名次被垃圾 token 带偏。
#    这类失败**不报错、不告警**，只是静默搜不到 —— 最难查的一类。
#
# 🔴 **口径**（对外函数签名不变，只改内部顺序）：
#    1. 正则先摘 **ASCII 词**（`[A-Za-z][A-Za-z0-9_\-']*`，含驼峰 / 带连字符词）；
#    2. 剩下的段（中文 / 数字 / 标点）才进 jieba 切；
#    3. 两段按**原文位置序**合并（BM25 不吃顺序，保序只为 token 流可读可比对）。
#
# 🔴 **纯数字不进 ASCII 词正则**（"2026" 以数字开头）⇒ 它留在「其余段」里
#    由 jieba 原样吐出，不会被丢；"XG-Alpha" 这类**驼峰/连字符**整词则由正则
#    整块摘出，再按词元口径切成 "XG" / "Alpha"（与 PG `to_tsvector('simple')`
#    和 SQLite `unicode61` 同口径，doclen 计数才对得上）。

#: **ASCII 词**（英文 / 驼峰 / 带连字符）：`[A-Za-z][A-Za-z0-9_\-']*`
#:
#: 🔴 前置断言 `(?<![A-Za-z0-9])`：**不许从字母数字串的中间切**。
#:    "3f9a2b1c"（uuid 前 8 位、版本号…）以数字开头 ⇒ 字母 'f' 处**不是**词首；
#:    若在这里切开，"3f9a2b1c" 就被劈成 "3" + "f9a2b1c" ⇒ 索引侧少了整块
#:    ⇒ 查询侧发整块、命中恒 0（改前的 jieba 是整块保留的）。整块留给 jieba，
#:    切法与改前逐字相同。
ASCII_WORD_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_\-']*")

#: 词元口径 = `\w+`（与 `core/shared.py` 的 `LEXEME_RE` **逐字同形**）。
#: domain 层不许 import core（依赖方向单向）⇒ 这里单列一份；
#: 两边不同步会漂 ⇒ `tests/test_stem_ascii_pipeline.py` 有断言盯着。
_WORD_RE = re.compile(r"\w+", re.UNICODE)

#: **ASCII 词**的词元口径 = 词字符（不含下划线）或下划线串。
#:
#: 🔴 为什么单独一份：`\w+` 把下划线算作词字符 ⇒ "claude_user" 会是一个词元；
#:    但**查询侧** `_sanitize_terms` 按 `/-_.` 拆子词（"claude" + "user"），
#:    索引侧不拆 ⇒ 两侧口径对不齐 ⇒ 查 "claude_user" 恒 0 命中。
#:    这里跟 jieba 自己的切法一致（`jieba.lcut("claude_user")` = claude / _ / user），
#:    下划线单独成词元 ⇒ 索引侧与查询侧逐字对齐。
_ASCII_LEXEME_RE = re.compile(r"[^\W_]+|_+")


def _jieba_lexemes(chunk: str) -> List[str]:
    """一段**非 ASCII 词**的文本 → 词元（jieba 切 + `\w+` 切）。"""
    if not chunk or not chunk.strip():
        return []
    return _WORD_RE.findall(" ".join(w.strip() for w in jieba.lcut(chunk)))


def iter_ascii_parts(text: str):
    """文本 → 按**原文位置序**的 `(片段, 是不是 ASCII 词)` 序列。

    🔴 这是「英文不进 jieba」那条规则的**唯一**切分实现：索引侧词元
    （`segment_lexemes`）与实体抽取（`extract_entities`）都走它 ⇒ 两侧口径
    不会漂（各写一份切分器就会漂，漂了就是索引/实体两路词元对不齐）。
    """
    pos = 0
    for m in ASCII_WORD_RE.finditer(text or ""):
        if m.start() > pos:
            yield text[pos:m.start()], False
        yield m.group(), True
        pos = m.end()
    if pos < len(text or ""):
        yield text[pos:], False


def segment_lexemes(text: str, stem: bool = True) -> List[str]:
    """文本 → 词元列表（**ASCII 先行摘出** + 非 ASCII 段才进 jieba）。

    🔴 这是索引侧（`content_tsv`）与查询侧共用的**唯一**分词管道；
    英文词**永远不经过 jieba** ⇒ 用户词典里有没有英文条目都不影响切分
    （治的正是「同态自证」：两侧在有没有词典两种环境下都得全绿）。

    `stem=True` 时对词元追加 Porter2 词干化（`stem_english`）：中文词元与
    含数字的标识符原样返回（见 `stem_english` 的 docstring）。
    `stem=False` 用于**标签流**（标签是精确逗号边界判据，词干化会破坏它）。
    """
    if not text:
        return []
    lexemes: List[str] = []
    for part, is_word in iter_ascii_parts(text):
        if is_word:
            lexemes.extend(_ASCII_LEXEME_RE.findall(part))
        else:
            lexemes.extend(_jieba_lexemes(part))
    if not stem:
        return lexemes
    return stem_english_tokens(lexemes)

# ---------------------------------------------------------------------------
# 常见英文缩写（不在此边界断句）
# ---------------------------------------------------------------------------

_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "dept",
    "inc", "ltd", "co", "corp", "capt", "gen", "sgt", "lt", "maj", "col",
    "gov", "rep", "sen", "pres", "vice", "pres", "hon", "esq", "phd", "md",
    "ave", "blvd", "rd", "ct", "dr", "est", "inst", "univ",
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "al", "fig", "vol", "no", "pp", "ex",
})

_STOP_WORDS = {
    "这个", "那个", "什么", "怎么", "为什么", "可以", "没有",
    "但是", "如果", "因为", "所以", "而且", "然后", "还是",
    "就是", "不是", "一个", "我们", "你们", "他们", "已经",
    "可以", "可能", "应该", "需要", "这样", "那样", "这里",
    "那里", "这个", "这些", "那些", "之后", "之前", "时候",
    "的", "了", "在", "是", "我", "有", "和", "就", "不",
    "人", "都", "一", "一个", "上", "也", "很", "到", "说",
    "要", "去", "你", "会", "着", "没有", "看", "好", "自己",
    "这",
}

# 英文通用虚词表（function words）—— 仅限高频语法词，宁松勿紧。
# 故意**不收**技术/常用词（api/sql/log/ssh/db/key/get/put/run/use/new/set 等），
# 防误杀。中文虚词沿用上面的 _STOP_WORDS。
_ENG_FUNCTION_WORDS = frozenset({
    "the", "and", "for", "you", "are", "was", "were", "with", "from",
    "this", "that", "these", "those", "they", "them", "your",
    "have", "has", "had", "would", "could", "should",
    "into", "out", "off", "over", "under",
    "again", "once", "ever",
    "some", "any", "all", "each", "every", "both", "more", "most",
    "other", "others", "only", "same", "very",
    "here", "there", "when", "where", "why", "how",
    "which", "what", "who", "whom",
    "because", "since", "though", "however", "therefore",
    "about", "above", "below", "before", "after", "while",
    "until", "unless", "also", "just", "then", "than", "too",
    "nor", "own", "such", "not", "but", "am", "is", "be", "been", "being",
    "do", "does", "did", "done",
    "will", "can", "may", "might", "must", "shall",
    "our", "yours", "his", "her", "its", "my",
})


# ---------------------------------------------------------------------------
# 情感关键词表（支持中英文）
# ---------------------------------------------------------------------------

_POSITIVE_KEYWORDS: Dict[str, float] = {
    # 强烈正面 (×1.5)
    "太棒了": 1.5, "太好了": 1.5, "非常满意": 1.5, "爱了": 1.5,
    "绝了": 1.5, "牛逼": 1.5, "太强了": 1.5,
    # 中等正面 (×1.0)
    "喜欢": 1.0, "不错": 1.0, "好用": 1.0, "满意": 1.0,
    "推荐": 1.0, "值得": 1.0, "好使": 1.0, "方便": 0.8,
    "清晰": 0.8, "直观": 0.8, "nice": 0.8, "good": 0.8,
    "great": 1.0, "awesome": 1.2, "excellent": 1.3,
}

_NEGATIVE_KEYWORDS: Dict[str, float] = {
    # 强烈负面 (×1.5)
    "太差了": 1.5, "垃圾": 1.5, "恶心": 1.5, "废物": 1.5,
    "烂": 1.3, "垃圾东西": 1.5,
    # 中等负面 (×1.0)
    "讨厌": 1.0, "不好用": 1.0, "不满意": 1.0, "糟糕": 1.0,
    "烦": 0.8, "失望": 1.0, "差评": 1.0, "不行": 0.8,
    "bad": 1.0, "terrible": 1.3, "hate": 1.2, "useless": 1.0,
}

# 否定词 — 出现在情感词前 3 个字符内会反转情感
_NEGATORS = {"不", "没", "别", "勿", "无", "not", "no", "n't", "never"}


def analyze_sentiment(text: str) -> Tuple[float, str]:
    """分析文本情感倾向。

    返回 (sentiment_score, sentiment_label):
        sentiment_score: -1.0 ~ 1.0, 负值=负面, 正值=正面
        sentiment_label: "positive" / "negative" / "neutral"

    算法:
      1. 扫描正负面关键词，匹配时检测前文否定词
      2. 累计正负得分，归一化到 -1~1
      3. |score| < 0.15 视为 neutral
    """
    if not text or not text.strip():
        return 0.0, "neutral"

    text_lower = text.lower().strip()
    pos_score = 0.0
    neg_score = 0.0

    # 扫描正面关键词
    for kw, intensity in _POSITIVE_KEYWORDS.items():
        idx = text_lower.find(kw.lower())
        if idx == -1:
            continue
        # 检查前文是否有否定词
        start = max(0, idx - 4)
        prefix = text_lower[start:idx].strip()
        negated = any(n in prefix for n in _NEGATORS)
        if negated:
            neg_score += intensity * 0.5
        else:
            pos_score += intensity

    # 扫描负面关键词
    for kw, intensity in _NEGATIVE_KEYWORDS.items():
        idx = text_lower.find(kw.lower())
        if idx == -1:
            continue
        start = max(0, idx - 4)
        prefix = text_lower[start:idx].strip()
        negated = any(n in prefix for n in _NEGATORS)
        if negated:
            pos_score += intensity * 0.5
        else:
            neg_score += intensity

    # 归一化到 -1~1
    total = pos_score + neg_score
    if total == 0:
        return 0.0, "neutral"

    score = (pos_score - neg_score) / max(total, 0.1)
    score = max(-1.0, min(1.0, score))

    if score > 0.15:
        label = "positive"
    elif score < -0.15:
        label = "negative"
    else:
        label = "neutral"

    return round(score, 4), label


def extract_keywords(text: str, max_keywords: int = 5) -> List[str]:  # G2 证据：CJK u4e00 门槛
    """从文本中提取关键词（基于 jieba 分词）。

    使用 jieba 进行中文分词 + 词频统计，过滤停用词后返回
    高频词作为关键词。英文词单独提取（3 字母以上）。
    """
    if not text or not text.strip():
        return []

    text_lower = text.lower().strip()
    candidates: List[str] = []

    # 1. 用 jieba 做中文分词
    words = jieba.lcut(text)
    # 过滤停用词 + 长度 >= 2（单字词一般是语气词/助词）
    # CJK 门槛：必须含 CJK 字符才收；jieba HMM 会把英文切碎（in/an/ce/be/dd/em），
    # 这些纯 ASCII 碎渣从该分支一律丢弃，英文提取完全交给下面 regex 分支。
    chinese_words = [
        w for w in words
        if len(w) >= 2
        and w not in _STOP_WORDS
        and not w.isdigit()
        and len(set(w)) > 1  # 过滤 "哈哈" "AA" 类重复词
        # CJK 字符门槛：纯 ASCII 碎渣不进
        and re.search(r"[一-鿿]", w)  # CJK 字符门槛（G2 证据：源里 u4e00 字样）
    ]
    candidates.extend(chinese_words)

    # 2. 提取英文词（3 字母以上）
    eng_words = re.findall(r"\b[a-zA-Z]{3,}\b", text_lower)
    # 既过滤中文虚词表里的英文残留，又过滤 _ENG_FUNCTION_WORDS
    candidates.extend([
        w for w in eng_words
        if w not in _STOP_WORDS and w not in _ENG_FUNCTION_WORDS
    ])

    # 3. 按频次降序
    freq = Counter(candidates)
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    return [w for w, _ in sorted_words[:max_keywords]]


# ---------------------------------------------------------------------------
# 句子切分
# ---------------------------------------------------------------------------

# 预编译边界正则 — 同时匹配中文和英文句末标点
# 中文：。！？ 后直接切（不跟在实际可能出现的引号后）
# 英文：.！？后跟空白、大写字母、标点或行尾才切
# 保护场景在后处理 _split_sentences 中处理
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[。！？])'                                            # 中文句末标点
    r'|'
    r'(?<=[！？])'                                              # 中英文通用叹号/问号
    r'|'
    # 英文 .!? 后跟空白字符或文本结束才切
    r'(?<=[.!?])(?=\s|$)'                                      # 仅空白或行尾
)


def _split_sentences(text: str) -> List[str]:
    """将段落切分为句级片段，优先保留语义完整。

    保护规则:
      - 数字间句点（3.14、v1.0）
      - 缩写句点（Mr.、Dr.、U.S.A.）
      - 省略号（... → …）
      - 中文 。 后跟引号不单独切
    """
    # 1. 归一化连点 → …（保留至少 3 个点时才归一化）
    text = re.sub(r'\.{3,}', '…', text)
    text = re.sub(r'…{2,}', '…', text)

    # 2. 用正则切分
    raw_parts = _SENTENCE_SPLIT_RE.split(text)

    # 3. 后处理：合并因缩写/数字/单字母误切的部分
    merged: List[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue

        if merged:
            last = merged[-1]
            # 如果上一片段的末尾看起像是一个缩写或数字（如 Mr、3.14、U.S.）
            # 或者当前片段很短（<=3 字符）且不以大写字母开头 → 应合并
            prev_tail = last.rstrip()
            is_abbrev = (
                _looks_like_abbreviation(prev_tail)
                or _ends_with_digit_dot(prev_tail)
            )
            is_fragment = len(part) <= 3 and not part[0].isupper()
            if is_abbrev or is_fragment:
                merged[-1] = last + part
                continue

        merged.append(part)

    return merged


def _looks_like_abbreviation(text: str) -> bool:
    """检查文本末尾是否像缩写（Mr.、Dr.、U.S.A.、etc.）。"""
    # 去掉末尾空白
    text = text.rstrip()
    m = re.search(r'\b([A-Za-z]{1,5})\.$', text)
    if not m:
        return False
    word = m.group(1).lower()
    # 在缩写列表中，或是 1-2 个全大写字母（如 U.S. -> U 和 S 各一段）
    return word in _ABBREVIATIONS or (word.isupper() and len(word) <= 2)


def _ends_with_digit_dot(text: str) -> bool:
    """检查文本是否以数字+句点结尾（如 '3.14' 中的 '3.'）。"""
    return bool(re.search(r'\d\.$', text.rstrip()))


# ---------------------------------------------------------------------------
# 主切分入口
# ---------------------------------------------------------------------------


def split_text(text: str, max_chars: int = 500) -> List[str]:
    """按语义完整性切分文本。

    策略：
      1. 先按段落（连续空行）切分
      2. 超长段落按智能句子边界切分（保护数字、缩写、引号）
      3. 过短碎片（<10 字）与相邻片段合并

    参数:
        text: 要切分的文本
        max_chars: 单个碎片最大字符数

    返回:
        切分后的文本列表
    """
    if not text or not text.strip():
        return []

    raw_paras = re.split(r"\n\s*\n", text.strip())
    segments: List[str] = []

    for para in raw_paras:
        para = para.strip()
        if not para:
            continue

        if len(para) <= max_chars:
            segments.append(para)
            continue

        # 超长段落：按智能句子边界切
        sentences = _split_sentences(para)
        chunk = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(chunk) + len(s) > max_chars and chunk:
                segments.append(chunk.strip())
                chunk = s
            else:
                chunk += s + NEWLINE
        if chunk.strip():
            segments.append(chunk.strip())

    # 合并过短碎片（<10 字符）到前一个
    final: List[str] = []
    for seg in segments:
        if len(seg) < 10 and final:
            final[-1] += NEWLINE + seg
        else:
            final.append(seg)

    return [s for s in final if len(s) >= 10]


# ---------------------------------------------------------------------------
# 实体提取 — 用于实体关系图
# ---------------------------------------------------------------------------

# 大写缩写/英文实体: BTC, ETH, ZG, MACD 等
_ENTITY_ENGLISH_RE = re.compile(r"[A-Z][A-Z0-9]{1,}(?:/[A-Z0-9]+)*")  # BTC, ETH, ZG, ZD

# v1.5: 英文碎词噪音（jieba 把长英文拆成的片段/常见虚词）
_ENTITY_ENG_STOP = frozenset({
    "in", "an", "ce", "be", "dd", "em", "g", "of", "to", "on", "at", "is",
    "am", "or", "nd", "st", "th", "nt", "ab", "cd", "ef", "gh",
})

# 中文平台/项目名常见词缀
_ENTITY_CHN_SUFFIXES = {"公司", "平台", "集团", "科技", "网络", "学院", "大学", "社区", "基金", "项目", "团队", "部门"}

# 已知高频实体白名单（本领域常见名词）
_ENTITY_KNOWN = frozenset({
    "缠论", "中枢", "三买", "三卖", "顶背驰", "底背驰", "金叉", "死叉",
    "以太坊", "比特币", "知乎", "B站", "抖音", "微博", "公众号", "视频号",
    "微信", "QQ", "Telegram", "币安", "Bitget", "Binance",
    "小红书", "TradeApi", "Hermes",
})

# 价格数字: 5-6位数（常见crypto价格）
_ENTITY_PRICE_RE = re.compile(r"(?<!\d)([6-9]\d{4,5})(?!\d)")

def segment_query(query: str) -> list[str]:
    """对搜索查询进行分词。

    对包含中文的查询使用 jieba 分词，英文按空格切分。
    返回分词后的词条列表，用于 RediSearch 全文搜索。
    """
    if not query or not query.strip():
        return []

    query = query.strip()
    # 检测是否包含中文
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", query))

    if not has_chinese:
        # 纯英文/数字：按空格切分
        return [t for t in query.split() if t.strip()]

    # 中文：jieba 只切**非 ASCII 词段** —— 英文整词先摘出来。
    # 🔴 为什么不能把整句喂 jieba：用户词典里的英文条目/碎片会把整句英文切碎
    #    （"visited" → "vis" + "it" + "ed"）⇒ 查询侧跟着碎，与索引侧（整词）
    #    口径不一致 ⇒ 假召回。理由与 `segment_lexemes` 同一份，见那里的说明。
    words: List[str] = []
    pos = 0
    for m in ASCII_WORD_RE.finditer(query):
        words.extend(w for w in jieba.lcut(query[pos:m.start()]) if w.strip())
        pos = m.end()
    words.extend(w for w in jieba.lcut(query[pos:]) if w.strip())

    # v1.4: 补充完整英文/数字词 — jieba 可能把英文切碎（embedding → em/be/dd/in/g），
    # 整词追加保证 TAG/entities 精确匹配能命中
    eng_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", query)
    lower_set = {w.lower() for w in words}
    for w in eng_words:
        if w.lower() not in lower_set:
            words.append(w)
            lower_set.add(w.lower())

    return words




def extract_entities(text: str) -> list[str]:
    """从文本中提取候选实体（零LLM，纯jieba + regex + 白名单）。

    返回去重后的实体名列表，按出现顺序排列。
    """
    if not text or not text.strip():
        return []

    entities: list[str] = []
    seen: set[str] = set()

    # 0. 已知白名单实体
    text_lower = text.lower()
    for known in _ENTITY_KNOWN:
        if known.lower() in text_lower:
            key = known.lower()
            if key not in seen:
                entities.append(known)
                seen.add(key)

    # 1. 实体提取：**英文整词走正则，其余段才进 jieba posseg**
    #
    # 🔴 2026-10 ks_stem_fix：英文**永不经过 jieba**。用户词典里有英文条目时，
    #    posseg 会把整句英文按词典碎片切开（"deployment" → "deploy" + "ment"）
    #    ⇒ 碎片被当"实体"收进索引（实体流与正文流**都**进 content_tsv）
    #    ⇒ 垃圾 token 进索引、污染 BM25 词频，还造出一堆垃圾实体。
    #    口径：按原文位置序走 —— ASCII 词走正则（沿用下面 'eng' 分支的规则），
    #    非 ASCII 段才进 posseg。
    for part, is_ascii_word in iter_ascii_parts(text):
        if is_ascii_word:
            # 英文词（BTC, ETH 等）—— 与 posseg 的 'eng' 分支同规则，
            # 但拿的是**整词**（jieba 给的是碎片）
            for tok in _ASCII_LEXEME_RE.findall(part):
                key = tok.lower()
                # v1.5: 过滤英文碎词噪音（jieba 把长英文拆成 in/an/ce 等）+ 常见虚词
                if len(key) >= 3 and key not in _ENTITY_ENG_STOP and key not in seen:
                    entities.append(tok.upper())
                    seen.add(key)
            continue
        try:
            words = jieba.posseg.lcut(part)
            for w, flag in words:
                w_stripped = w.strip()
                if len(w_stripped) < 2:
                    continue
                # 人名/机构/地名
                if flag in ("nr", "nr1", "nr2", "nrj", "nrf", "nt", "ns", "nsf"):
                    key = w_stripped.lower()
                    if key not in seen:
                        entities.append(w_stripped)
                        seen.add(key)
                # 其他专名（nz）— 只收含中文或首字母大写的英文词
                # （HMM/领域词典会把英文碎片标 nz，如 Binance→in/an；也会把 Redis 标 nz）
                elif flag == "nz":
                    is_chinese = bool(re.search(r"[\u4e00-\u9fff]", w_stripped))
                    is_title_eng = (
                        w_stripped[0].isupper()
                        and w_stripped.isalnum()
                        and len(w_stripped) >= 3
                    )
                    if is_chinese or is_title_eng:
                        key = w_stripped.lower()
                        if key not in seen:
                            entities.append(w_stripped)
                            seen.add(key)
                # 英文词（BTC, ETH 等）—— 非 ASCII 段里只剩数字/符号时会走到这里
                elif flag == "eng":
                    key = w_stripped.lower()
                    # v1.5: 过滤英文碎词噪音（jieba 把长英文拆成 in/an/ce 等）+ 常见虚词
                    if len(key) >= 3 and key not in _ENTITY_ENG_STOP and key not in seen:
                        entities.append(w_stripped.upper())
                        seen.add(key)
        except Exception:
            pass

    # 2. regex 补充 — 大写缩写
    for m in _ENTITY_ENGLISH_RE.finditer(text):
        token = m.group()
        key = token.lower()
        if len(token) >= 2 and key not in seen:
            entities.append(token)
            seen.add(key)

    # 3. regex 补充 — 技术术语
    for token in ("ZG", "ZD", "MACD", "DIF", "DEA", "HIST", "RSI", "OBV", "EMA", "SMA", "BOLL",
                  "buy1", "sell1", "buy2", "sell2"):
        if token.lower() in text_lower:
            key = token.lower()
            if key not in seen:
                entities.append(token.upper())
                seen.add(key)

    # 4. 中文名+词缀组合: 如 "小米公司" 里的 "小米"
    for suffix in _ENTITY_CHN_SUFFIXES:
        idx = text.find(suffix)
        if idx >= 1:
            # 取 suffix 前一个词（1-6字）
            start = max(0, idx - 12)
            prefix = text[start:idx]
            # 尝试用 jieba 分词找最后一个有意义的词
            try:
                words = jieba.lcut(prefix)
                for w in reversed(words):
                    w = w.strip()
                    if len(w) >= 2 and w not in _STOP_WORDS:
                        key = w.lower()
                        if key not in seen:
                            entities.append(w)
                            seen.add(key)
                        break
            except Exception:
                pass

    # 5. 价格数字（5-6位数，如 63000）
    for m in _ENTITY_PRICE_RE.finditer(text):
        token = m.group()
        key = token
        if key not in seen:
            entities.append(token)
            seen.add(key)

    return entities

