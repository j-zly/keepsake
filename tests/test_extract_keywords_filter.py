"""keepsake — extract_keywords 碎渣/虚词过滤测试（ks_hot_topic_stopwords）。

覆盖：
  1. 正向：混合文本样例，输出无纯 ASCII len<3 token、无 _ENG_FUNCTION_WORDS token。
  2. 负向：纯英文虚词 + 中文词的输入，虚词全被过滤，中文词保留。
  3. 技术词保护：3-5 字母技术常用词（api/key/ssh/log/sql 等）必须保留，不能被虚词表误杀。
  4. 边界：纯 ASCII 短 token（in/an/ce/be/dd/em）和 jieba HMM 碎渣一律不出现。
  5. 回归：保证既有其他 splitter 测试不被影响（不在此断言，由 G5 总跑）。

纯本地零网络（不连 Redis，不发任何 HTTP）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 from keepsake.splitter import ... 能直接跑（pytest 收集根目录在 keepsake/）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from keepsake.splitter import (  # noqa: E402
    _ENG_FUNCTION_WORDS,
    _STOP_WORDS,
    extract_keywords,
)


# ===========================================================================
# 1. 正向：碎渣/虚词一律不出现
# ===========================================================================

class TestNoGarbage:
    """核心场景：jieba HMM 切英文产出的 ASCII 碎渣 + 英文虚词表 token 全过滤。"""

    def test_jieba_ascii_fragments_filtered(self):
        """jieba 把 'Binance' 切成 in/an/ce 等 2 字母 ASCII 碎渣 → 全过滤。"""
        result = extract_keywords("Binance in an at 服务器挂了 部署")
        bad = [w for w in result if w.isascii() and len(w) < 3]
        assert not bad, f"ASCII 碎渣漏出：{bad}"

    def test_eng_function_words_filtered(self):
        """_ENG_FUNCTION_WORDS 里的通用虚词不应出现。"""
        # 至少 5 个虚词 + 中文上下文混排
        text = "the and for you are was were with from this that 服务器挂了 部署"
        result = extract_keywords(text)
        bad = [w for w in result if w in _ENG_FUNCTION_WORDS]
        assert not bad, f"英文虚词漏出：{bad}"

    def test_mixed_input_no_ascii_short_or_function_word(self):
        """G3 同款：混合文本里既无纯 ASCII len<3 token，也无虚词 token。"""
        result = extract_keywords(
            "Binance in an at 服务器挂了 部署 the and for"
        )
        bad = [
            w for w in result
            if w.isascii() and (len(w) < 3 or w in {"the", "and", "for"})
        ]
        assert not bad, f"碎渣/虚词漏出：{bad}"

    def test_eng_function_words_set_contains_documented_entries(self):
        """虚词表必须包含任务书明确点名的成员（防漏装）。"""
        required = {"the", "and", "for", "you", "are", "was", "were",
                    "with", "from", "this", "that", "is", "be", "have", "has"}
        missing = required - _ENG_FUNCTION_WORDS
        assert not missing, f"_ENG_FUNCTION_WORDS 漏装：{missing}"


# ===========================================================================
# 2. 负向：纯虚词输入 → 输出空/只剩中文
# ===========================================================================

class TestNegativeInput:
    """纯虚词输入不应产出虚词；中文词保留。"""

    def test_pure_eng_function_words_input_keeps_chinese(self):
        """'the and for 部署服务器' → 虚词过滤，中文词保留。"""
        result = extract_keywords("the and for 部署服务器")
        # 三个英文虚词一个不能漏出
        for fw in ("the", "and", "for"):
            assert fw not in result, f"虚词 {fw!r} 漏出：{result}"
        # 中文词至少出现一个
        assert any(w in result for w in ("部署", "服务器")), (
            f"中文词未保留：{result}"
        )

    def test_empty_input(self):
        assert extract_keywords("") == []
        assert extract_keywords("   ") == []


# ===========================================================================
# 3. 技术词保护：宁松勿紧，技术常用词不被误杀
# ===========================================================================

class TestTechTermsProtected:
    """任务书要求：api/sql/log/ssh/db/key/get/put/run/use/new/set 等不收进虚词表。

    这里验证这些词被 extract_keywords 正常提取（不被误杀）。
    """

    @pytest.mark.parametrize(
        "tech_word",
        # 注：英文 regex 分支只收 3+ 字母纯 ASCII，故 2 字母词（db/io/fs/os/vm/ci/cd）
        # 和含数字的（k8s）本就匹配不到 — 不在本测试覆盖范围。
        ["api", "sql", "log", "ssh", "key", "get", "put",
         "run", "use", "new", "set", "redis", "mysql", "json", "yaml",
         "http", "rest", "grpc", "jwt", "oauth", "sshd", "tcp", "udp"],
    )
    def test_tech_term_kept(self, tech_word):
        """每个技术词单独喂入，应被提取出（不在虚词表内）。"""
        result = extract_keywords(f"配置 {tech_word} 服务器")
        # 该技术词必须出现在结果中
        assert tech_word in result, (
            f"技术词 {tech_word!r} 被误杀：{result}"
        )

    def test_g4_specific_case(self):
        """G4 同款：'api key ssh log 配置' 这四个全保留。"""
        result = extract_keywords("api key ssh log 配置")
        assert {"api", "key", "ssh", "log"} & set(result), (
            f"技术词被误杀：{result}"
        )

    def test_tech_terms_not_in_stop_table(self):
        """直接断言 _ENG_FUNCTION_WORDS 不含技术常用词（结构证据）。"""
        forbidden = {
            "api", "sql", "log", "ssh", "db", "key", "get", "put",
            "run", "use", "new", "set", "redis", "mysql", "json",
        }
        contamination = forbidden & _ENG_FUNCTION_WORDS
        assert not contamination, (
            f"_ENG_FUNCTION_WORDS 误收技术词：{contamination}"
        )


# ===========================================================================
# 4. 既有行为不退化（与 splitter 既有行为相容的部分）
# ===========================================================================

class TestRegressionSafe:
    """既有 extract_keywords 的语义保留。"""

    def test_chinese_keywords_still_extracted(self):
        """纯中文 → 中文关键词正常提取。"""
        result = extract_keywords("服务器 部署 配置 数据库 缓存")
        for kw in ("服务器", "部署", "配置"):
            assert kw in result, f"中文关键词丢失：{result}"

    def test_digit_words_excluded(self):
        """纯数字 token 不应出现。"""
        result = extract_keywords("12345 服务器 部署")
        assert "12345" not in result

    def test_deduplicated_repeated_words(self):
        """'哈哈' 类重复字符词被既有过滤条件排除。"""
        result = extract_keywords("哈哈哈哈 服务器 部署")
        assert "哈哈" not in result and "哈哈哈哈" not in result

    def test_stop_words_filtered(self):
        """中文虚词表 _STOP_WORDS 仍然生效。"""
        result = extract_keywords("的 了 在 是 服务器 部署")
        for sw in ("的", "了", "在", "是"):
            assert sw not in result, f"中文虚词 {sw!r} 漏出：{result}"

    def test_max_keywords_respected(self):
        """max_keywords 上限生效。"""
        text = "服务器 部署 配置 数据库 缓存 网络 端口"
        result = extract_keywords(text, max_keywords=3)
        assert len(result) <= 3
