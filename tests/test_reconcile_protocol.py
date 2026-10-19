"""调和协议批 2（Hy-Memory 移植）—— 四类裁决 + 主体闸 + 受控标签。

## 这个文件钉死的事

  1. **裁决枚举**：ADD / UPDATE / SUPERSEDE / VOID / NOOP 五类；旧提示词吐的
     `DELETE` 映射成 VOID（换提示词不必同时换模型）；非法 action 仍一律 NOOP。
  2. **SUPERSEDE**：走契约 v2 写路径 —— `supersede_fragment(old, new, reason)`
     带裁决给的 reason，旧条转链身（`is_latest='0'`）、新条记 reason。
  3. **VOID**（= 旧 DELETE 的正名）：旧条逻辑删（`supersede_reason='voided'`），
     **不写新事实** —— 「永不召回但保留正文」才是可查证的。
  4. **主体闸**：裁决声明的 subject 双边命中不了正文 ⇒ SUPERSEDE/UPDATE 一律
     降级 ADD + 一行日志（「John 喝黑咖啡」不许动「用户喝黑咖啡」）。
  5. **受控元认知标签**：`low_confidence` / `context_dependent` 放行，其余自造剥离。
  6. **提示词协议**：四例 few-shot（顶替链 / 链头禁抄 / 异主体拆 / 复发独立）
     与四条判定规则都在提示词里 —— 提示词是这单的产品本体，漏写就是行为缺失。

设计要点：不连真实后端（FakeStorage 记调用），`_call_llm` 脚本化返回。
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

from keepsake.core.base import StorageBase
from keepsake.core.pipeline import (
    EXTRACT_USER_TEMPLATE,
    UPDATE_SYSTEM,
    UPDATE_USER_TEMPLATE,
    VOID_KEY,
    VOID_REASON,
    Fact,
    Pipeline,
    Turn,
    Verdict,
    normalize_subject,
)
from keepsake.core.shared import META_COGNITIVE_TAGS, sanitize_distill_tags
from keepsake.core.sqlite import SqliteStorage
from keepsake.core.redis import RedisStorage
from keepsake.core.pg import PgStorage


# ===========================================================================
# FakeStorage —— 与 pipeline 期望的接口一致（记调用，不真写库）
# ===========================================================================

class FakeStorage:
    """只记调用的最小存储面。签名与 base.py 逐参数对齐（契约 v2 的 reason）。"""

    def __init__(self) -> None:
        self.stored: List[Dict[str, Any]] = []
        self.fragments: Dict[str, Dict[str, Any]] = {}
        self.supersede_calls: List[Tuple[str, str, str]] = []
        self.set_supersedes_calls: List[Tuple[str, str]] = []

    @staticmethod
    def key_for(text: str) -> str:
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    def store(self, text: str, tags: str = "", category: str = "",
              source: str = "", fragment_type: str = "",
              applicability: str = "") -> bool:
        key = self.key_for(text)
        row = {"key": key, "content": text, "tags": tags, "category": category}
        self.stored.append(row)
        self.fragments[key] = row
        return True

    def search_bm25(self, query: str, tag_filter: str = "",
                    **kwargs: Any) -> List[Dict[str, Any]]:
        # 候选 = 全库（不做相关性筛），让本文件的用例只钉协议分支、不掺检索排序
        return [{"_key": k, "content": r["content"], "tags": r.get("tags", ""),
                 "category": "fact_v2"}
                for k, r in self.fragments.items()]

    def supersede_fragment(self, old_key: str, new_key: str, reason: str = "") -> bool:
        self.supersede_calls.append((old_key, new_key, reason))
        old = self.fragments.get(old_key)
        if old is None:
            return False
        old["superseded_by"] = new_key
        old["is_latest"] = "0"
        if not new_key or new_key == VOID_KEY:      # 逻辑删：reason 记旧条
            old["supersede_reason"] = reason
            return True
        new = self.fragments.get(new_key)
        if new is not None and new_key != old_key:
            new["is_latest"] = "1"
            new["supersede_reason"] = reason
        return True

    def set_supersedes(self, new_key: str, old_key: str) -> bool:
        self.set_supersedes_calls.append((new_key, old_key))
        return True

    def fragment_exists(self, key: str) -> Optional[bool]:
        return key in self.fragments

    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.fragments.get(key)
        return dict(row) if row else None

    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        row = self.fragments.get(key)
        if row is None:
            return False
        row.update(fields)
        return True


def _pipeline(storage: FakeStorage, llm_fn) -> Pipeline:
    return Pipeline(storage=storage, llm_fn=llm_fn)


def _scripted_llm(facts_json: str, verdict_json: str):
    """提取相返回 facts_json、更新相返回 verdict_json（脚本化 LLM）。"""
    calls = {"n": 0}

    def llm(messages, model):
        calls["n"] += 1
        return facts_json if calls["n"] == 1 else verdict_json

    return llm


# ===========================================================================
# 1) 裁决枚举：五类 + 旧 DELETE 的兼容映射
# ===========================================================================

class TestVerdictEnum:
    """`_parse_action` 只认合法 JSON，action 不在五类内一律 NOOP。"""

    @pytest.mark.parametrize("action", ["ADD", "UPDATE", "SUPERSEDE", "VOID", "NOOP"])
    def test_five_actions_recognized(self, action: str):
        v = Pipeline._parse_action(f'{{"action":"{action}","target_key":"k1"}}')
        assert v.action == action
        assert v.target_key == "k1"

    def test_legacy_DELETE_maps_to_VOID_and_logs(self, caplog):
        """旧提示词输出 DELETE → VOID + 一行 info 日志（可观测，不静默改名）。"""
        with caplog.at_level(logging.INFO, logger="keepsake.pipeline"):
            v = Pipeline._parse_action('{"action":"DELETE","target_key":"k1"}')
        assert v.action == "VOID"
        assert v.target_key == "k1"
        assert "DELETE" in caplog.text and "VOID" in caplog.text

    @pytest.mark.parametrize("raw", [
        '{"action":"REPLACE","target_key":"x"}',
        '{"action":"SHADOW","target_key":"x"}',   # 别名不认：改名不改词表之外的东西
        '{"action":"","target_key":"x"}',
        "该信息与旧记忆不冲突，无需 UPDATE",
        '{"action":"UPDATE","target_key":',        # JSON 语法错误
    ])
    def test_illegal_or_unknown_action_is_noop(self, raw: str):
        assert Pipeline._parse_action(raw) == Verdict("NOOP")

    def test_subject_and_reason_are_parsed(self):
        v = Pipeline._parse_action(
            '{"action":"SUPERSEDE","target_key":"k1","subject":"用户",'
            '"reason":"居住城市由上海变更为杭州"}'
        )
        assert v.subject == "用户"
        assert v.reason == "居住城市由上海变更为杭州"

    def test_missing_subject_reason_default_empty(self):
        """老式输出没有 subject/reason → 空串（不是 None，闸按「没声明」放行）。"""
        v = Pipeline._parse_action('{"action":"UPDATE","target_key":"k1"}')
        assert v.subject == "" and v.reason == ""


# ===========================================================================
# 2) SUPERSEDE：走契约 v2 写路径，带 reason
# ===========================================================================

class TestSupersedeWritePath:
    """SUPERSEDE = 两条独立事实、新顶旧；差别由 reason 承载。"""

    def test_supersede_passes_reason_and_maintains_chain(self):
        st = FakeStorage()
        st.store(text="用户常驻上海", category="fact_v2")
        old_key = st.key_for("用户常驻上海")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户常驻杭州","kind":"state_change"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"用户",'
            '"reason":"居住城市由上海变更为杭州"}' % old_key,
        ))
        result = p._process_window([Turn("我搬到杭州了", "好的", 0.0)], [])

        assert result.supersedes == 1, "SUPERSEDE 记在独立计数器上"
        new_key = st.key_for("用户常驻杭州")
        assert st.supersede_calls == [(old_key, new_key, "居住城市由上海变更为杭州")]
        # 契约 v2 链维护：旧条转链身、新条记 reason
        assert st.fragments[old_key]["is_latest"] == "0"
        assert st.fragments[new_key]["is_latest"] == "1"
        assert st.fragments[new_key]["supersede_reason"] == "居住城市由上海变更为杭州"
        # 新条正文只写新状态（链头禁抄规则的落点：库里没有旧细节）
        assert "上海" not in st.fragments[new_key]["content"]

    def test_supersede_without_reason_gets_default(self):
        """LLM 漏填 reason（该字段必填）→ 兜底值，不留空串。"""
        st = FakeStorage()
        st.store(text="用户常驻上海", category="fact_v2")
        old_key = st.key_for("用户常驻上海")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户常驻杭州","kind":"state_change"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"用户"}' % old_key,
        ))
        p._process_window([Turn("我搬到杭州了", "好的", 0.0)], [])
        assert st.supersede_calls[0][2] == "superseded"

    def test_update_keeps_reason_empty(self):
        """UPDATE = 同一条记忆的下一版 ⇒ 不带 reason（批 1 落库值逐字不变）。"""
        st = FakeStorage()
        st.store(text="用户要求做前端分页", category="fact_v2")
        old_key = st.key_for("用户要求做前端分页")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户的前端分页已完成上线","kind":"state_change"}]}',
            '{"action":"UPDATE","target_key":"%s","subject":"用户"}' % old_key,
        ))
        result = p._process_window([Turn("分页上线了", "好的", 0.0)], [])
        assert result.updates == 1
        assert result.supersedes == 0
        assert st.supersede_calls == [(old_key, st.key_for("用户的前端分页已完成上线"), "")]

    def test_new_fact_without_subject_in_text_is_downgraded(self):
        """新条正文里没有主体 → 闸判不了同异，降级 ADD（宁可多一条也别错顶）。

        这条钉的是闸的**误伤方向**：主体判据的精度依赖提取相「content 以主体
        开头」（已写进 EXTRACT 提示词第 7 条），一旦新条省略主语，两侧就无从比对
        ⇒ 方向安全地降级，不猜。
        """
        st = FakeStorage()
        st.store(text="用户要求做前端分页", category="fact_v2")
        old_key = st.key_for("用户要求做前端分页")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"前端分页已完成上线","kind":"state_change"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"用户",'
            '"reason":"分页上线"}' % old_key,
        ))
        result = p._process_window([Turn("分页上线了", "好的", 0.0)], [])
        assert result.adds == 1 and result.supersedes == 0
        assert st.supersede_calls == []
        assert "superseded_by" not in st.fragments[old_key]


# ===========================================================================
# 3) VOID：旧条逻辑删，不写新事实
# ===========================================================================

class TestVoidWritePath:
    """VOID = 逻辑删（`supersede_reason='voided'`），永不召回但正文保留。"""

    def test_void_marks_reason_and_writes_no_new_fact(self):
        st = FakeStorage()
        st.store(text="用户每周五要开会", category="fact_v2")
        old_key = st.key_for("用户每周五要开会")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"以后改成每周三开会","kind":"fact"}]}',
            '{"action":"VOID","target_key":"%s","subject":"用户"}' % old_key,
        ))
        result = p._process_window([Turn("改周三了", "好的", 0.0)], [])

        assert result.deletes == 1, "VOID 沿用既有 deletes 计数器（对外字段不断线）"
        assert st.supersede_calls == [(old_key, VOID_KEY, VOID_REASON)]
        # 逻辑删：链身 + 正文保留（可查证、可回滚）
        assert st.fragments[old_key]["is_latest"] == "0"
        assert st.fragments[old_key]["supersede_reason"] == VOID_REASON
        assert st.fragments[old_key]["content"] == "用户每周五要开会"

    def test_legacy_DELETE_end_to_end_still_voids(self):
        """旧提示词端到端：DELETE → VOID 一条龙（换提示词当天不必同时换模型）。"""
        st = FakeStorage()
        st.store(text="用户每周五要开会", category="fact_v2")
        old_key = st.key_for("用户每周五要开会")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"以后改成每周三开会","kind":"fact"}]}',
            '{"action":"DELETE","target_key":"%s"}' % old_key,
        ))
        result = p._process_window([Turn("改周三了", "好的", 0.0)], [])
        assert result.deletes == 1
        assert st.supersede_calls == [(old_key, VOID_KEY, VOID_REASON)]

    def test_supersede_hitting_core_still_not_sealed(self):
        """🔴 core 防暗改闸**一字不动**：SUPERSEDE 撞上 core 仍只提名不封边。"""
        from keepsake.core.tiering import CORE_TAG, DEMOTE_CANDIDATE_TAG

        st = FakeStorage()
        st.store(text="用户固定用 A 方案", category="fact_v2")
        core_key = st.key_for("用户固定用 A 方案")
        st.fragments[core_key]["tags"] = f"{CORE_TAG},memory"

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户改用 B 方案了","kind":"state_change"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"用户",'
            '"reason":"方案由 A 改为 B"}' % core_key,
        ))
        p._process_window([Turn("改用 B 了", "好的", 0.0)], [])

        assert st.supersede_calls == [], "常驻条目绝不许被管线封边"
        assert "superseded_by" not in st.fragments[core_key]
        assert DEMOTE_CANDIDATE_TAG in st.fragments[core_key]["tags"]


# ===========================================================================
# 4) 主体闸：主体不同不许合并
# ===========================================================================

class TestSubjectGate:
    """normalize 等值闸（折叠空白 + 小写），不过就降级 ADD + 日志。"""

    def test_normalize_folds_whitespace_and_case(self):
        assert normalize_subject("  John  Smith ") == normalize_subject("JohnSmith")
        assert normalize_subject("用户 常驻上海") == "用户常驻上海"
        assert normalize_subject(None) == ""

    @pytest.mark.parametrize("action", ["SUPERSEDE", "UPDATE"])
    def test_foreign_subject_degrades_to_add(self, action: str):
        """「John 喝黑咖啡」不许动「用户喝黑咖啡」→ 降级 ADD，且旧条仍在结果面。"""
        st = FakeStorage()
        st.store(text="用户喝黑咖啡", category="fact_v2")
        old_key = st.key_for("用户喝黑咖啡")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"John 喝黑咖啡","kind":"fact"}]}',
            '{"action":"%s","target_key":"%s","subject":"John","reason":"换人"}' % (action, old_key),
        ))
        result = p._process_window([Turn("John 也喝黑咖啡", "好的", 0.0)], [])

        assert st.supersede_calls == [], "主体不同不许顶掉别人"
        assert "superseded_by" not in st.fragments[old_key], "旧条必须仍是链头"
        assert result.adds == 1, "降级为 ADD：两条事实并存"
        assert st.key_for("John 喝黑咖啡") in st.fragments

    def test_same_subject_passes_gate(self):
        st = FakeStorage()
        st.store(text="用户常驻上海", category="fact_v2")
        old_key = st.key_for("用户常驻上海")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户常驻杭州","kind":"state_change"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"用户",'
            '"reason":"居住城市变更"}' % old_key,
        ))
        result = p._process_window([Turn("搬到杭州了", "好的", 0.0)], [])
        assert result.supersedes == 1
        assert st.supersede_calls, "同主体不该被拦"

    def test_gate_logs_one_line(self, caplog):
        """降级必须留日志（静默降级 = 「我的记忆怎么没合并」，没人查得到）。"""
        st = FakeStorage()
        st.store(text="用户喝黑咖啡", category="fact_v2")
        old_key = st.key_for("用户喝黑咖啡")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"John 喝黑咖啡","kind":"fact"}]}',
            '{"action":"SUPERSEDE","target_key":"%s","subject":"John","reason":"换人"}' % old_key,
        ))
        with caplog.at_level(logging.WARNING, logger="keepsake.pipeline"):
            p._process_window([Turn("John 也喝黑咖啡", "好的", 0.0)], [])
        assert "subject gate" in caplog.text
        assert "John" in caplog.text

    def test_undeclared_subject_is_not_blocked(self):
        """老式输出没带 subject → 不拦（拦了会把线上 UPDATE 全打成 ADD）。"""
        st = FakeStorage()
        st.store(text="用户常驻上海", category="fact_v2")
        old_key = st.key_for("用户常驻上海")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户常驻杭州","kind":"state_change"}]}',
            '{"action":"UPDATE","target_key":"%s"}' % old_key,
        ))
        result = p._process_window([Turn("搬到杭州了", "好的", 0.0)], [])
        assert result.updates == 1
        assert st.supersede_calls

    def test_gate_pure_unit(self):
        """闸本身的判据（供上面的端到端钉口径）。"""
        ok = Pipeline._subject_gate_ok
        assert ok("用户", "用户常驻杭州", "用户常驻上海")
        assert ok("john", "John 喝黑咖啡", "john 喜欢黑咖啡")   # 大小写折叠
        assert not ok("John", "John 喝黑咖啡", "用户喝黑咖啡")   # 异主体
        assert not ok("用户", "John 喝黑咖啡", "用户喝黑咖啡")   # 新条里没有声明的主体
        assert ok("", "任意", "任意")                          # 没声明 = 不拦

    def test_void_is_not_subject_gated(self):
        """闸只管「合并类」动作：VOID 不写新事实，不存在串主体的可能。"""
        st = FakeStorage()
        st.store(text="用户喝黑咖啡", category="fact_v2")
        old_key = st.key_for("用户喝黑咖啡")

        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"John 喝黑咖啡","kind":"fact"}]}',
            '{"action":"VOID","target_key":"%s","subject":"John"}' % old_key,
        ))
        p._process_window([Turn("John 也喝黑咖啡", "好的", 0.0)], [])
        assert st.supersede_calls == [(old_key, VOID_KEY, VOID_REASON)]


# ===========================================================================
# 5) 受控元认知标签：两个放行，其余自造照剥
# ===========================================================================

class TestMetaCognitiveTags:
    """`low_confidence` / `context_dependent` 受控放行，自造标签一律剥。"""

    def test_sanitize_keeps_only_controlled_two(self):
        assert sanitize_distill_tags("low_confidence") == "low_confidence"
        assert sanitize_distill_tags("context_dependent") == "context_dependent"
        assert sanitize_distill_tags(
            "low_confidence,context_dependent") == "low_confidence,context_dependent"
        # 自造的一律剥；治理保留字也不许由自动提炼声明
        assert sanitize_distill_tags("low_confidence,cool,core") == "low_confidence"
        assert sanitize_distill_tags("") == ""
        assert sanitize_distill_tags(None) == ""

    def test_sanitize_dedups(self):
        assert sanitize_distill_tags("low_confidence,low_confidence") == "low_confidence"

    def test_controlled_tags_are_single_pointed(self):
        """判据单点：`META_COGNITIVE_TAGS` 就是放行清单本身（不多不少两个）。"""
        assert META_COGNITIVE_TAGS == ("low_confidence", "context_dependent")

    def test_fact_tags_flow_into_stored_tags(self):
        st = FakeStorage()
        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"上线时间记不太准","kind":"fact",'
            '"tags":["low_confidence","我自造的"]}]}',
            '{"action":"ADD"}',
        ))
        p._process_window([Turn("好像是三四月份吧", "好的", 0.0)], [])
        tags = st.stored[0]["tags"]
        assert "low_confidence" in tags
        assert "我自造的" not in tags, "自造标签照剥"
        assert "pipeline:v2" in tags

    def test_tags_accepts_array_or_comma_string(self):
        """提取相的 tags 两种写法都收（数组 / 逗号串）。"""
        arr = Pipeline._parse_facts(
            '{"facts":[{"content":"X","kind":"fact","tags":["low_confidence"]}]}')
        assert arr[0].tags == "low_confidence"
        s = Pipeline._parse_facts(
            '{"facts":[{"content":"X","kind":"fact","tags":"low_confidence"}]}')
        assert s[0].tags == "low_confidence"
        # 老式输出没有 tags 字段 → 空串，既有构造点逐字不受影响
        assert Pipeline._parse_facts('{"facts":[{"content":"X","kind":"fact"}]}')[0].tags == ""

    def test_no_tags_keeps_base_tags_exact(self):
        """没自评标签时 tags 与批 1 逐字一致（不带空逗号）。"""
        assert Pipeline._tags_for(Fact("X", "fact")) == "kind:fact,pipeline:v2"
        assert Pipeline._tags_for(Fact("X", "fact", "low_confidence")) == \
            "kind:fact,pipeline:v2,low_confidence"


# ===========================================================================
# 6) 提示词协议：规则与四例 few-shot 都在
# ===========================================================================

class TestPromptProtocol:
    """提示词是这单的产品本体：规则与 few-shot 漏一条就是行为缺失。"""

    def test_update_prompt_lists_five_actions(self):
        for token in ("ADD", "UPDATE", "SUPERSEDE", "VOID", "NOOP"):
            assert token in UPDATE_SYSTEM
            assert token in UPDATE_USER_TEMPLATE

    def test_supersede_only_when_mutually_exclusive(self):
        assert "不能同时为真" in UPDATE_USER_TEMPLATE
        assert "一律不许" in UPDATE_USER_TEMPLATE and "SUPERSEDE" in UPDATE_USER_TEMPLATE

    def test_chain_head_must_not_copy_old_detail(self):
        assert "只写新状态" in UPDATE_USER_TEMPLATE
        assert "不许抄" in UPDATE_USER_TEMPLATE

    def test_subject_rule_present(self):
        assert "主体不同永不合并" in UPDATE_USER_TEMPLATE
        assert "subject" in UPDATE_USER_TEMPLATE

    def test_recurrence_is_independent_add(self):
        assert "另一次发生" in UPDATE_USER_TEMPLATE

    def test_four_few_shots_present(self):
        """四例：顶替链 / 链头禁抄 / 异主体拆 / 复发独立。"""
        assert "顶替链" in UPDATE_USER_TEMPLATE
        assert "链头禁抄" in UPDATE_USER_TEMPLATE
        assert "异主体拆" in UPDATE_USER_TEMPLATE
        assert "复发独立" in UPDATE_USER_TEMPLATE

    def test_extract_prompt_mentions_controlled_tags_only(self):
        assert "low_confidence" in EXTRACT_USER_TEMPLATE
        assert "context_dependent" in EXTRACT_USER_TEMPLATE
        assert "不要自造其他标签" in EXTRACT_USER_TEMPLATE

    def test_update_prompt_renders_with_format(self):
        """模板占位符全填得上（少一个就是线上 KeyError）。"""
        text = UPDATE_USER_TEMPLATE.format(
            new_fact="用户常驻杭州", new_kind="state_change",
            top_k=5, candidates_block="[1] key=memory:frag:x kind=fact_v2\n    用户常驻上海",
        )
        assert "用户常驻杭州" in text and "memory:frag:x" in text


# ===========================================================================
# 7) 接口签名收口：三后端与 base.py 逐参数一致（过渡 allowlist 已删）
# ===========================================================================

class TestInterfaceSignaturesTightened:
    """契约 v2 的 `applicability` / `reason` 已在 base.py 收口 ⇒ 漂移判据恢复严格。"""

    @pytest.mark.parametrize("method,extra", [
        ("store", "applicability"), ("supersede_fragment", "reason"),
    ])
    def test_base_declares_contract_v2_params(self, method: str, extra: str):
        params = inspect.signature(getattr(StorageBase, method)).parameters
        assert extra in params, f"{method} 接口必须声明 {extra}"
        assert params[extra].default == ""

    @pytest.mark.parametrize("backend", [RedisStorage, PgStorage, SqliteStorage],
                             ids=["redis", "pg", "sqlite"])
    def test_backends_expose_contract_v2_params(self, backend):
        store_p = inspect.signature(backend.store).parameters
        sup_p = inspect.signature(backend.supersede_fragment).parameters
        assert store_p["applicability"].default == ""
        assert sup_p["reason"].default == ""

    def test_strict_drift_probe_not_vacuous(self):
        """收紧后判据不许空转：伪后端多一个带默认值的可选参数**也**判漂移。

        这条是收紧本身的回归 —— 「allowlist 删了，但严格分支写成了
        『只要无默认值的才算漂移』」是这次收口最可能的错法。
        """
        from tests.test_storage_interface import _signature_mismatches

        class Drifted(SqliteStorage):
            def store(self, text: str, tags: str = "", category: str = "",
                      source: str = "", fragment_type: str = "",
                      sentiment_score: Optional[float] = None,
                      sentiment_label: Optional[str] = None,
                      applicability: str = "",
                      surprise_me: str = "") -> bool:   # 接口外的可选入参
                return True

        assert "store" in _signature_mismatches(Drifted)


# ===========================================================================
# 8) 回归护栏：既有 UPDATE/NOOP 行为一字不变
# ===========================================================================

class TestBackwardCompat:
    """向后兼容：老提示词/老 LLM 输出不许因为这单炸掉或改行为。"""

    def test_noop_still_writes_nothing(self):
        st = FakeStorage()
        st.store(text="用户常驻上海", category="fact_v2")
        p = _pipeline(st, _scripted_llm(
            '{"facts":[{"content":"用户常驻杭州","kind":"state_change"}]}',
            "该信息与旧记忆不冲突，无需 UPDATE",
        ))
        result = p._process_window([Turn("搬到杭州了", "好的", 0.0)], [])
        assert result.noops == 1
        assert len(st.stored) == 1 and st.stored[0]["content"] == "用户常驻上海", \
            "NOOP 零写入（库里那一条是本用例自己 seed 的）"
        assert st.supersede_calls == []

    def test_drain_result_counters_default_zero(self):
        """新增的 supersedes 计数器默认 0（既有看板读旧字段不受影响）。"""
        from keepsake.core.pipeline import DrainResult
        r = DrainResult()
        assert (r.supersedes, r.updates, r.deletes, r.noops) == (0, 0, 0, 0)
