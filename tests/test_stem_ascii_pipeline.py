"""英文 **ASCII 先行分词管道** —— 索引侧不再把英文喂给 jieba（2026-10 ks_stem_fix）。

🔴 **为什么要有这个文件**：主脑那份 3026 行用户词典里**含英文条目**，jieba 拿
词典里的英文碎片去切**整句英文**：

    "many cities were visited during the trip"
      → many / cities / were / vis / it / ed / during / the / trip
      "this city has a huge library"
      → this / ci / ty / has / a / huge / library

⇒ **索引侧存碎片、查询侧发整词** ⇒ 恒 0 命中，而且碎片还污染 BM25 词频
（doclen 变长、tf 变噪）。这类失败**不报错、不告警**，只是静默搜不到。

🔴 **本文件是「双词典环境自测」**：所有断言在两种环境都要成立 ——
  ① 默认环境（本仓没有用户词典）；
  ② `KEEPSAKE_TEST_USERDICT=tests/fixtures/mini_userdict.txt`（模拟主脑那份
     **含英文条目**的用户词典）。
改前这两套环境是**同一种**结果（本仓没词典 ⇒ jieba 走 finalseg 的 `re_skip`
分支，英文整词原样保留 ⇒ 一律全绿）—— 那就是**同态自证**：看不见「词典里有
英文词条」这一档，主脑树就栽在那档上。

本文件 **hermetic**：只用 `tmp_path` 里的临时 SQLite 库 + 独立的
`jieba.Tokenizer` 实例（不污染全局 jieba），不连任何外部服务。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from keepsake.domain.splitter import (
    ASCII_WORD_RE,
    TEST_USERDICT_ENV,
    _WORD_RE,
    segment_lexemes,
    stem_english,
)
from keepsake.storage_shared import LEXEME_RE, jieba_dict_entries, jieba_dict_keep

#: 迷你用户词典（5 行，含英文条目/碎片）—— 模拟主脑那份词典的**关键一档**。
MINI_USERDICT = Path(__file__).resolve().parent / "fixtures" / "mini_userdict.txt"

#: 会被旧口径（整句喂 jieba）切碎的英文句子。
FRAGILE_SENTENCES = [
    "many cities were visited during the trip",
    "this city has a huge library",
    "a shared note about deployment",
    "we are generating the weekly report",
]


def _old_pipeline(text: str, tokenizer) -> list:
    r"""**改前**的口径：整句先喂 jieba，再按 `\w+` 取词元。

    用独立的 `jieba.Tokenizer` 实例跑（不污染全局 jieba）⇒ 任何时候都能
    复现「有用户词典」那一档的碎块。
    """
    return LEXEME_RE.findall(
        " ".join(w.strip() for w in tokenizer.lcut(text or "")))


# ---------------------------------------------------------------------------
# ① 管道本身：ASCII 词整块摘出，不经 jieba
# ---------------------------------------------------------------------------

def test_ascii_words_are_extracted_whole():
    """🔴 英文句子的词元必须**逐词**等于原文的词 —— 一个都不许被切碎。"""
    for text in FRAGILE_SENTENCES:
        words = text.split()
        assert segment_lexemes(text, stem=False) == words, (
            f"英文被切碎了: {text!r} → {segment_lexemes(text, stem=False)}")


def test_ascii_regex_covers_camel_case_and_hyphen():
    r"""驼峰 / 带连字符的词是**整词**（不进 jieba），但按 `\w+` 切成词元。"""
    assert ASCII_WORD_RE.findall("XG-Alpha v2 needs-attention") == [
        "XG-Alpha", "v2", "needs-attention"]
    # 词元口径仍按 `\w+` 切（与 PG to_tsvector('simple') / SQLite unicode61 同口径）
    assert segment_lexemes("XG-Alpha", stem=False) == ["XG", "Alpha"]
    assert segment_lexemes("needs-attention", stem=False) == ["needs", "attention"]


def test_numbers_and_cjk_are_not_dropped():
    """🔴 纯数字（"2026"）不匹配 ASCII 词正则 ⇒ 留给 jieba 原样吐出，不许丢。"""
    lex = segment_lexemes("XG-Alpha(2026-09-22 复盘)要点", stem=False)
    joined = "".join(lex)
    for want in ("XG", "Alpha", "2026", "复盘", "要点"):
        # 意图 = **不丢**（数字与中文都在词元流里）；中文怎么切由 jieba+词典决定，
        # 「要点」在含用户词典的环境会被切成「要」+「点」⇒ 断言按字符流包含，
        # 不锁切法（切法随词典漂移是 jieba 的本性，锁了就是环境依赖断言）。
        assert want in lex or want in joined, f"丢了 {want}: {lex}"


def test_digit_leading_token_is_not_split():
    r"""🔴 "3f9a2b1c"（uuid 前 8 位）**不许**被劈成 "3" + "f9a2b1c"。

    改前 jieba 整块保留；改后若从字母数字串的**中间**切开，索引侧就少了整块
    ⇒ 查询侧发整块、命中恒 0（PG 侧 `test_hybrid_search_fuses_both_paths`
    就是被这条绊倒的 —— 它的语料里带 uuid marker）。
    """
    lex = segment_lexemes("标记甲 3f9a2b1c", stem=False)
    assert "3f9a2b1c" in lex, lex
    assert "f9a2b1c" not in lex, lex


def test_underscore_identifier_matches_query_side_split():
    r"""🔴 "claude_user" 的切法必须与**查询侧** `_sanitize_terms` 拆子词一致。

    查询侧按 `/-_.` 拆 ⇒ 发 "claude" + "user"；索引侧若留成 "claude_user"
    一个词元 ⇒ 两侧口径对不齐 ⇒ 查 "claude_user" 恒 0 命中（只靠子串兜底档
    撑着，BM25 主路是废的）。
    """
    lex = segment_lexemes("/home/claude_user/", stem=False)
    assert "claude" in lex and "user" in lex, lex
    assert "claude_user" not in lex, lex


def test_cjk_still_segmented_by_jieba():
    """中文段**照旧**进 jieba 切（这次的改动只动英文那一路）。"""
    assert segment_lexemes("部署流程分四批", stem=False) == ["部署", "流程", "分", "四批"]


# ---------------------------------------------------------------------------
# ② 迷你用户词典：证明「有英文词条」那一档是真的会坏
# ---------------------------------------------------------------------------

def test_mini_userdict_breaks_the_old_pipeline():
    """🔴 夹具**必须真的能弄坏旧口径** —— 否则这轮自测又是同态自证。

    用独立的 `jieba.Tokenizer` 加载迷你词典（不污染全局 jieba），复现旧口径
    （整句喂 jieba）的碎块；再断言新口径**不受影响**。
    """
    import jieba

    assert MINI_USERDICT.exists(), f"夹具缺失: {MINI_USERDICT}"
    tokenizer = jieba.Tokenizer()          # 独立实例：不污染全局 jieba
    tokenizer.load_userdict(str(MINI_USERDICT))

    broken = 0
    for text in FRAGILE_SENTENCES:
        old = _old_pipeline(text, tokenizer)
        if old != text.split():
            broken += 1
        # 新口径：**任何**词典环境下都必须是整词
        assert segment_lexemes(text, stem=False) == text.split(), (
            f"新口径也被切碎了: {text!r} → {segment_lexemes(text, stem=False)}")
    assert broken >= 3, (
        "迷你词典没能弄坏旧口径 —— 夹具失效，双词典自测不成立")


def test_mini_userdict_env_is_actually_loaded():
    """🔴 `KEEPSAKE_TEST_USERDICT` 指了路径 ⇒ 词典必须**真的**加载进 jieba。

    路径写错 / 文件缺失时这里必须红：否则「第二遍全绿」只是又跑了一遍
    默认环境（同态自证）。
    """
    raw = os.environ.get(TEST_USERDICT_ENV)
    if not raw:
        pytest.skip(f"{TEST_USERDICT_ENV} 未设置（默认环境那一遍）")
    import jieba
    jieba.initialize()
    for word, freq in (("cities", 5), ("deploy", 3)):
        assert jieba.dt.FREQ.get(word) == freq, (
            f"测试词典没加载进 jieba（{word} 的词频是 "
            f"{jieba.dt.FREQ.get(word)}，期望 {freq}）")


# ---------------------------------------------------------------------------
# ③ 词典再生成路径：纯 ASCII 词一律不进用户词典
# ---------------------------------------------------------------------------

def test_jieba_dict_keep_rejects_ascii():
    """纯 ASCII 词不进 jieba 词典；中英混排（"Redis缓存"）**留**。"""
    assert jieba_dict_keep("cities") is False
    assert jieba_dict_keep("it") is False
    assert jieba_dict_keep("2026") is False
    assert jieba_dict_keep("备份") is True
    assert jieba_dict_keep("Redis缓存") is True


def test_generated_jieba_dict_excludes_ascii_words():
    """🔴 `generate_jieba_dict` 收词条时过滤纯 ASCII 词（三个后端共用判据）。"""
    entries = jieba_dict_entries(
        {"备份": 5, "策略": 4, "cities": 9, "deploy": 7, "it": 6, "2026": 8},
        ["手工罕见词", "cities", "Redis缓存"],
        min_freq=2,
    )
    words = {w for w, _ in entries}
    assert "备份" in words and "策略" in words, sorted(words)
    assert "手工罕见词" in words, sorted(words)     # 中文同义词 term 照旧提频到 3
    assert "Redis缓存" in words, sorted(words)      # 中英混排照旧进词典
    assert not (words & {"cities", "deploy", "it", "2026"}), sorted(words)
    assert all(not w.isascii() for w, _ in entries), entries


# ---------------------------------------------------------------------------
# ④ 词元口径同步 + 豁免语义不变
# ---------------------------------------------------------------------------

def test_word_re_stays_in_sync_with_storage_lexeme_re():
    """domain 层单列的词元正则必须与 `storage_shared.LEXEME_RE` **逐字同形**。"""
    assert _WORD_RE.pattern == LEXEME_RE.pattern == r"\w+"


def test_stem_exempt_semantics_unchanged():
    """🔴 豁免表语义不变：标签/保留字不被词干化（精确逗号边界判据不许破口）。"""
    assert stem_english("shared") == "shared"
    assert stem_english("core") == "core"
    assert stem_english("agent") == "agent"
    # 标签流（stem=False）原样返回，不词干化
    assert segment_lexemes("shared", stem=False) == ["shared"]
    assert segment_lexemes("shared", stem=True) == ["shared"]
    # 普通英文照旧词干化
    assert segment_lexemes("cities", stem=True) == ["citi"]
    assert segment_lexemes("cities", stem=False) == ["cities"]


# ---------------------------------------------------------------------------
# ⑤ 端到端：索引侧（SQLite）不落碎片
# ---------------------------------------------------------------------------

def test_index_side_keeps_whole_words(tmp_path):
    """🔴 落库的 `content_tsv` 里是**整词**，不是碎片（BM25 词频不被污染）。"""
    from keepsake.storage_sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    try:
        assert s.ensure_index() is True
        assert s.store("a shared note about deployment") is True
        assert s.store("many cities were visited during the trip") is True
        with s._lock:
            rows = s._db().execute(
                "SELECT content, content_tsv FROM ks_fragment").fetchall()
    finally:
        s.close()

    by_content = {content: (tsv or "").split() for content, tsv in rows}

    # "shared" 是标签/保留字（豁免词干化）⇒ 落库就是 "shared"，不是碎片
    shared = by_content["a shared note about deployment"]
    assert "shared" in shared, shared
    assert "shar" not in shared, shared
    # "deployment" 的**词干**是 "deploy"（Porter2）—— 落库的是词干，不是碎片
    assert "deploy" in shared, shared
    assert "ment" not in shared, shared

    # "cities" → 词干 "citi"；"visited" → 词干 "visit"（整词词干化，不是碎片）
    cities = by_content["many cities were visited during the trip"]
    assert "citi" in cities, cities
    assert "ci" not in cities, cities
    assert "visit" in cities, cities
    assert "vis" not in cities, cities


def test_stored_english_still_recalled(tmp_path):
    """索引整词 + 查询整词 ⇒ 互查命中（对称铁律在两种词典环境下都成立）。"""
    from keepsake.storage_sqlite import SqliteStorage

    s = SqliteStorage(path=str(tmp_path / "ks2.db"), is_primary=True)
    try:
        assert s.ensure_index() is True
        assert s.store("many cities were visited during the trip") is True
        assert s.store("this city has a huge library") is True
        hits = s.search_bm25("city")
        assert hits, "索引 cities、查询 city 应当命中"
        hits = s.search_bm25("cities")
        assert hits, "索引 city、查询 cities 应当命中"
    finally:
        s.close()
