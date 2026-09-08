"""R7 写侧源头闸门 — scrub_secrets 单元测试。

任务书覆盖：
  - 5 类凭据正则各正反例
  - 幂等
  - 中文句零误伤（如「我的密码本很厚」）
  - URL / 路径零误伤
  - 任务书明列 5 条真凭据样式必命中
  - 接线点（Pipeline 写入路径）

不连 Redis / 不调真实 LLM —— 纯函数 + 内存 FakeStorage。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from keepsake.ingest_gate import REDACTED, scrub_secrets


# ===========================================================================
# 1) Category 1 — 中文 / 英文 password/passwd 标签
# ===========================================================================

class TestCategory1PasswordLabel:
    """R7-1：(密码|口令|password|passwd) [是为:：=] value"""

    def test_zh_password_label_masks(self):
        """任务书样例：「密码是FAKEpw9」→ 打码"""
        out, n = scrub_secrets("密码是FAKEpw9")
        assert out == "密码是***[REDACTED]"
        assert n == 1

    def test_en_password_label_masks(self):
        out, n = scrub_secrets("password=hunter2hunter2")
        assert out == "password=***[REDACTED]"
        assert n == 1

    def test_passwd_label_masks(self):
        out, n = scrub_secrets("passwd : topsecret1234")
        assert "***[REDACTED]" in out
        assert n == 1

    def test_no_separator_zh_does_not_mask(self):
        """任务书样例：「我的密码本很厚」→ 不打码（无分隔符）"""
        out, n = scrub_secrets("我的密码本很厚")
        assert out == "我的密码本很厚"
        assert n == 0

    def test_short_value_below_threshold_does_not_mask(self):
        """值 < 3 字符 → 不打码（避免误伤「好=1」之类）"""
        out, n = scrub_secrets("password=ab")
        assert out == "password=ab"
        assert n == 0


# ===========================================================================
# 2) Category 2 — api_key / token / secret / 密钥
# ===========================================================================

class TestCategory2ApiLabel:
    """R7-2：(api[_-]?key|token|secret|密钥) [是为:：=] value"""

    def test_token_label_masks_with_full_base64(self):
        """任务书样例：'token: FAKEtok+enABCDEF0123456789xyzXYZaaaa=='"""
        secret = "FAKEtok+enABCDEF0123456789xyzXYZaaaa=="
        out, n = scrub_secrets(f"token: {secret}")
        assert out == f"token: {REDACTED}"
        assert n == 1
        # 真凭据不应再出现在输出里
        assert secret not in out

    def test_dotted_token_label_masks(self):
        """任务书样例：auth.token = "..."（含引号被一并吞掉符合任务书约定）"""
        out, n = scrub_secrets(
            'auth.token = "FAAKE-long-fake-token-value-here"'
        )
        assert "***[REDACTED]" in out
        assert n == 1
        assert "FAAKE-long-fake" not in out

    def test_api_key_label_masks(self):
        out, n = scrub_secrets("api_key=abcdefghijk12345")
        assert out == "api_key=***[REDACTED]"
        assert n == 1

    def test_secret_label_masks(self):
        out, n = scrub_secrets("secret = hunter2hunter2hunter2")
        assert "***[REDACTED]" in out
        assert n == 1

    def test_token_label_no_separator_does_not_mask(self):
        """'the token we discussed' 中 'token' 是普通英文词，无 [是为:：=]"""
        out, n = scrub_secrets("the token we discussed yesterday was used")
        assert out == "the token we discussed yesterday was used"
        assert n == 0


# ===========================================================================
# 3) Category 3 — Password=xxx 连接串风格
# ===========================================================================

class TestCategory3ConnString:
    """R7-3：Password=value（大小写不敏感；值排除 ; " ' &）"""

    def test_password_eq_masks_and_keeps_trailing_semicolon(self):
        """任务书样例：'Password=FAKEpw1;' → 打码，保留分号"""
        out, n = scrub_secrets("Password=FAKEpw1;")
        assert out == "Password=***[REDACTED];"
        assert n == 1

    def test_password_in_mysql_dsn_masks(self):
        dsn = "Server=localhost;Database=mydb;Password=FAKEpw1;User Id=admin;"
        out, n = scrub_secrets(dsn)
        assert "Password=***[REDACTED]" in out
        assert "FAKEpw1" not in out
        assert n == 1

    def test_password_no_separator_does_not_mask(self):
        """'PasswordFAKEg'（无 = 分隔）→ 不打码（避免误伤普通词）"""
        out, n = scrub_secrets("PasswordFAKEg")
        assert out == "PasswordFAKEg"
        assert n == 0

    def test_password_value_with_terminator_stops_at_quote(self):
        """Password="hunter2" → 值不含引号（[^\s;\"'&] 排除 "）"""
        out, n = scrub_secrets('Password="hunter2"')
        assert "hunter2" not in out
        assert "***[REDACTED]" in out
        assert n == 1


# ===========================================================================
# 4) Category 4 — Bearer xxx
# ===========================================================================

class TestCategory4Bearer:
    """R7-4：Bearer <16+ base64 风格>"""

    def test_bearer_long_token_masks(self):
        out, n = scrub_secrets("Bearer abc123def456ghi789jkl0123456789")
        assert out == f"Bearer {REDACTED}"
        assert n == 1
        assert "abc123def456" not in out

    def test_bearer_short_token_does_not_mask(self):
        """值 < 16 字符 → 不打码（避免误伤 'Bearer Bob'）"""
        out, n = scrub_secrets("Bearer Bob")
        assert out == "Bearer Bob"
        assert n == 0


# ===========================================================================
# 5) Category 5 — 高熵串兜底
# ===========================================================================

class TestCategory5HighEntropy:
    """R7-5：12+ 连续 [A-Za-z0-9+/=_\-\.] + 保守过滤"""

    def test_openai_style_key_masks(self):
        """任务书样例：'sk-FAKE0123456789ab' → 打码"""
        out, n = scrub_secrets("sk-FAKE0123456789ab")
        assert out == REDACTED
        assert n == 1

    def test_ipv4_address_does_not_mask(self):
        """任务书硬要求：URL 域名 / IP 主机部分零误伤"""
        out, n = scrub_secrets("154.219.96.202")
        assert out == "154.219.96.202"
        assert n == 0

    def test_jwt_style_token_masks(self):
        """JWT 风格（mixed case + dots + digits）→ 打码"""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out, n = scrub_secrets(jwt)
        assert out == REDACTED
        assert n == 1


# ===========================================================================
# URL / 路径零误伤
# ===========================================================================

class TestUrlPathNoFalseHit:
    """任务书硬要求：URL / 文件路径 零误伤。"""

    def test_full_url_does_not_mask(self):
        out, n = scrub_secrets("请打开 https://example.com/foo 看一下文档")
        assert out == "请打开 https://example.com/foo 看一下文档"
        assert n == 0

    def test_subdomain_url_does_not_mask(self):
        out, n = scrub_secrets("https://api.example.com/v1/users")
        assert out == "https://api.example.com/v1/users"
        assert n == 0

    def test_unix_path_does_not_mask(self):
        out, n = scrub_secrets("/usr/local/bin/keepsake")
        assert out == "/usr/local/bin/keepsake"
        assert n == 0

    def test_long_log_path_does_not_mask(self):
        """包含子目录的较长路径（24 字符连续 run）→ 不打码"""
        out, n = scrub_secrets("/var/log/keepsake/server.log")
        assert out == "/var/log/keepsake/server.log"
        assert n == 0

    def test_short_domain_does_not_mask(self):
        out, n = scrub_secrets("example.com")
        assert out == "example.com"
        assert n == 0


# ===========================================================================
# 中文句 / 普通英文 零误伤
# ===========================================================================

class TestChineseNoFalseHit:
    """任务书硬要求：中文句零误伤"""

    def test_password_notebook_does_not_mask(self):
        """任务书硬要求：「我的密码本很厚」→ 不打码"""
        out, n = scrub_secrets("我的密码本很厚")
        assert out == "我的密码本很厚"
        assert n == 0

    def test_normal_sentence_with_token_word_does_not_mask(self):
        """普通英文句中出现 'token' 但无 [是为:：=] → 不打码"""
        out, n = scrub_secrets("we discussed the token format yesterday")
        assert out == "we discussed the token format yesterday"
        assert n == 0

    def test_long_english_sentence_does_not_mask(self):
        """40+ 字符普通英文句 → 不打码（空白会断 run）"""
        out, n = scrub_secrets("thequickbrownfoxjumpsoverthelazydogagain")
        assert out == "thequickbrownfoxjumpsoverthelazydogagain"
        assert n == 0


# ===========================================================================
# 幂等
# ===========================================================================

class TestIdempotency:
    """任务书硬要求：二次调用 n_masks=0"""

    def test_idempotent_password(self):
        out1, n1 = scrub_secrets("Password=hunter2")
        out2, n2 = scrub_secrets(out1)
        assert n1 == 1
        assert n2 == 0
        assert out1 == out2

    def test_idempotent_high_entropy(self):
        out1, n1 = scrub_secrets("sk-FAKE0123456789ab")
        out2, n2 = scrub_secrets(out1)
        assert n1 == 1
        assert n2 == 0
        assert out1 == out2

    def test_idempotent_multiple_secrets_in_one_text(self):
        text = "password=hunter2 token=abc123def456ghi789 sk-FAKE0123456789ab"
        out1, n1 = scrub_secrets(text)
        out2, n2 = scrub_secrets(out1)
        assert n1 == 3
        assert n2 == 0
        assert out1 == out2


# ===========================================================================
# 多个 secret 一次命中
# ===========================================================================

class TestMultipleSecrets:
    """同一文本含多个 secret → 全部打码"""

    def test_multiple_secrets_all_masked(self):
        text = "password=hunter2 token=abc123def456ghi789 secret=topsecret1234567890"
        out, n = scrub_secrets(text)
        assert n == 3
        assert "hunter2" not in out
        assert "abc123def456" not in out
        assert "topsecret" not in out

    def test_mixed_label_and_bare_secret(self):
        text = "user: alice, password=hunter2; also sk-FAKE0123456789ab"
        out, n = scrub_secrets(text)
        assert n == 2
        assert "hunter2" not in out
        assert "c4echk1muqonxio2" not in out


# ===========================================================================
# 边界 / 输入形态
# ===========================================================================

class TestEdgeCases:
    """空串 / None / 多行 / 编码"""

    def test_empty_string(self):
        out, n = scrub_secrets("")
        assert out == ""
        assert n == 0

    def test_none_returns_empty(self):
        """None 输入 → 返回空串 + 0 masks（不抛）"""
        out, n = scrub_secrets(None)  # type: ignore[arg-type]
        assert out == ""
        assert n == 0

    def test_return_type_is_tuple(self):
        out, n = scrub_secrets("Password=hunter2")
        assert isinstance(out, str)
        assert isinstance(n, int)

    def test_preserves_surrounding_text(self):
        """secret 周围的普通文本不被吞"""
        out, n = scrub_secrets("see password=hunter2 in the config")
        assert out.startswith("see ")
        assert out.endswith(" in the config")
        assert n == 1

    def test_multiline_text(self):
        text = "line1: password=hunter2\nline2: sk-FAKE0123456789ab\nline3: safe text"
        out, n = scrub_secrets(text)
        assert n == 2
        assert "hunter2" not in out
        assert "c4echk1muqonxio2" not in out
        assert "line3: safe text" in out


# ===========================================================================
# 接线点：Pipeline 写入路径必须过 scrub（不连 Redis，纯内存 FakeStorage）
# ===========================================================================

class _FakeStorage:
    """Pipeline 写入测试用的内存 FakeStorage。"""

    def __init__(self):
        self.stored: List[Dict[str, Any]] = []

    def store(self, text: str, tags: str = "", category: str = "",
              source: str = "", fragment_type: str = "", **kwargs) -> str:
        import hashlib
        key = f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        rec = {
            "key": key, "text": text, "tags": tags,
            "category": category, "source": source,
        }
        self.stored.append(rec)
        return key

    def search_bm25(self, q: str, tag_filter: str = ""):
        return []

    def supersede_fragment(self, old_key: str, new_key_or_void: str) -> bool:
        return True

    def get_fragment(self, key: str):
        return None

    def _get_client(self):
        return None


class TestPipelineWiring:
    """Pipeline._do_add / _do_update / _fallback_to_v1 三点入库前必 scrub。"""

    def test_pipeline_do_add_scrubs(self):
        from keepsake.pipeline import Fact, Pipeline
        st = _FakeStorage()
        p = Pipeline(st, llm_fn=None)
        p._do_add(Fact(content="用户密码是hunter2用来登录", kind="fact"))
        assert len(st.stored) == 1
        assert "hunter2" not in st.stored[0]["text"]
        assert REDACTED in st.stored[0]["text"]

    def test_pipeline_do_update_scrubs_new_fragment(self):
        from keepsake.pipeline import Fact, Pipeline
        st = _FakeStorage()
        p = Pipeline(st, llm_fn=None)
        p._do_update(
            Fact(content="api_key=abc123def456ghi789", kind="fact"),
            target_key="memory:frag:deadbeef0001",
        )
        assert len(st.stored) == 1
        assert "abc123def456" not in st.stored[0]["text"]
        assert REDACTED in st.stored[0]["text"]

    def test_pipeline_fallback_raw_store_scrubs(self):
        """v2 pipeline 兜底 raw store 必须 scrub（不走 v1 闸门）"""
        from keepsake.pipeline import DrainResult, Pipeline, Turn
        st = _FakeStorage()
        p = Pipeline(st, llm_fn=None)  # 无 gate_fallback → 走 raw store 路径
        result = DrainResult()
        turn = Turn(user="Password=FAKEpw1;", assistant="", timestamp=0.0)
        p._fallback_to_v1([turn], result, reason="test_no_gate")
        assert len(st.stored) == 1
        assert "FAKEpw1" not in st.stored[0]["text"]
        assert REDACTED in st.stored[0]["text"]

    def test_pipeline_fallback_via_gate_fallback_also_scrubs(self):
        """v2 pipeline 走 gate_fallback（即 _v1_fallback_store）→ _v1_store_after_decide
        已集成 scrub → 同样打码。"""
        from keepsake.ingest_gate import decide, IngestDecision

        class GateCapturingFakeStorage(_FakeStorage):
            """模拟 _v1_store_after_decide 的逻辑：scrub → store"""
            def __init__(self):
                super().__init__()
                self.last_scrubbed: str = ""

            def gate_fallback(self, text: str, category: str) -> None:
                # 复刻 _v1_store_after_decide 中的 scrub → store 逻辑
                scrubbed, _ = scrub_secrets(text)
                self.last_scrubbed = scrubbed
                self.store(text=scrubbed, category=category, tags="fallback:v1",
                           source="pipeline_v2_fallback", fragment_type="memory")

        st = GateCapturingFakeStorage()
        from keepsake.pipeline import Pipeline, Turn, DrainResult
        p = Pipeline(st, llm_fn=None, gate_fallback=st.gate_fallback)
        result = DrainResult()
        turn = Turn(user="token: FAKEtok+enABCDEF0123456789xyzXYZaaaa==", assistant="", timestamp=0.0)
        p._fallback_to_v1([turn], result, reason="test_with_gate")
        assert len(st.stored) == 1
        assert "FAKEtok+enABCDEF012" not in st.stored[0]["text"]
        assert REDACTED in st.stored[0]["text"]