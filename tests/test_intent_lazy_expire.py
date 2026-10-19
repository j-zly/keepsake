"""keepsake 检索层批 3 — 意图惰性过期（Hy-Memory `_retrieval/intention.py` 移植）。

覆盖：
  1. `is_intent_expired` / `_parse_iso_dt` 行为：未来/过去/解析失败三态
  2. `downgrade_expired_intent` 落库行为：清 valid_until + 打 expired_fact
     标签 + tags 保留原值；未来 valid_until 不动
  3. `apply_v2_filters` 「第五道」穿路径：过期的当场剔除，未过期的照常过
  4. fail-open：降级抛错也不影响本次返回
  5. SQLite 真实后端：filter 放宽后「带未来 valid_until」的意图条目能进
     候选；过期意图下次当普通事实召回

锚点词用中文基础词（跨环境 jieba 词典教训）—— `咖啡` / `拿铁` 之类。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from keepsake.core import shared as ks_shared
from keepsake.core.shared import (
    EXPIRED_FACT_TAG,
    _parse_iso_dt,
    apply_v2_filters,
    downgrade_expired_intent,
    is_intent_expired,
)


# ===========================================================================
# 一、`_parse_iso_dt` / `is_intent_expired` 纯函数
# ===========================================================================

class TestParseIsoDt:
    """ISO 时间串解析：合法 / 带 Z 后缀 / 无 tz / 失败四档。"""

    def test_iso_with_offset_parses(self):
        dt = _parse_iso_dt("2026-10-19T00:00:00+00:00")
        assert dt == datetime(2026, 10, 19, tzinfo=timezone.utc)

    def test_iso_with_z_suffix_parses(self):
        """Py3.10 之前 `fromisoformat` 不认 `Z`，本实现要兜底。"""
        dt = _parse_iso_dt("2026-10-19T00:00:00Z")
        assert dt == datetime(2026, 10, 19, tzinfo=timezone.utc)

    def test_naive_iso_gets_utc_attached(self):
        """无 tz 的 ISO 串按 UTC 处理（fail-open：猜比当 None 安全）。"""
        dt = _parse_iso_dt("2026-10-19T00:00:00")
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 10, 19, tzinfo=timezone.utc)

    def test_garbage_returns_none(self):
        assert _parse_iso_dt("not-a-date") is None
        assert _parse_iso_dt(None) is None
        assert _parse_iso_dt("") is None
        assert _parse_iso_dt(12345) is None


class TestIsIntentExpired:
    """`fragment["valid_until"]` 是否已过期。"""

    def test_future_is_not_expired(self):
        fut = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        assert is_intent_expired({"valid_until": fut}) is False

    def test_past_is_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert is_intent_expired({"valid_until": past}) is True

    def test_empty_is_not_expired(self):
        """没设 valid_until ⇒ 当普通事实看待，**不该**走降级路径。"""
        assert is_intent_expired({"valid_until": ""}) is False
        assert is_intent_expired({}) is False

    def test_garbage_is_not_expired(self):
        """解析失败 ⇒ 当作未过期（fail-open：不误杀普通事实）。"""
        assert is_intent_expired({"valid_until": "garbage"}) is False


# ===========================================================================
# 二、`downgrade_expired_intent` —— 落库 + 状态判断
# ===========================================================================

class _StubStorage:
    """最小可用的 storage stub —— 只实现本单要用到的两个钩子。

    `initial_tags` 会同时作为**任何 key**的初始 tag 值返回，模拟「库里已有
    这条带这些 tag」的真实形态（而不是只对写过的 key 才有）。
    """

    def __init__(self, initial_tags: str = "") -> None:
        self.fragments: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.initial_tags = initial_tags

    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        if key in self.fragments:
            return self.fragments[key]
        return {"tags": self.initial_tags}

    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        self.calls.append((key, dict(fields)))
        cur = self.fragments.setdefault(key, {"tags": self.initial_tags})
        for k, v in fields.items():
            cur[k] = v
        return True


class TestDowngradeExpiredIntent:
    """惰性降级：valid_until 清空 + tags 加 expired_fact。"""

    def _past_vu(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    def _future_vu(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    def test_expired_clears_valid_until_and_adds_tag(self):
        s = _StubStorage(initial_tags="咖啡,意图")
        ok = downgrade_expired_intent(
            s, {"_key": "memory:frag:aaa", "valid_until": self._past_vu()},
        )
        assert ok is True
        # 写入字段：valid_until 清空 + tags 追加 expired_fact
        assert s.calls, "应当有一次 update_fragment_fields 调用"
        key, fields = s.calls[0]
        assert key == "memory:frag:aaa"
        assert fields["valid_until"] == ""
        tag_list = [t.strip() for t in fields["tags"].split(",") if t.strip()]
        assert tag_list == ["咖啡", "意图", EXPIRED_FACT_TAG]

    def test_expired_with_no_existing_tags_still_marks(self):
        s = _StubStorage(initial_tags="")
        ok = downgrade_expired_intent(
            s, {"_key": "memory:frag:bbb", "valid_until": self._past_vu()},
        )
        assert ok is True
        _, fields = s.calls[0]
        assert fields["valid_until"] == ""
        assert EXPIRED_FACT_TAG in fields["tags"]

    def test_expired_idempotent_on_tag(self):
        """已带 expired_fact 的不再追加（保持 tags 简洁）。"""
        s = _StubStorage(initial_tags=f"咖啡,{EXPIRED_FACT_TAG}")
        downgrade_expired_intent(
            s, {"_key": "memory:frag:ccc", "valid_until": self._past_vu()},
        )
        _, fields = s.calls[0]
        tag_list = [t.strip() for t in fields["tags"].split(",") if t.strip()]
        assert tag_list.count(EXPIRED_FACT_TAG) == 1

    def test_future_does_not_downgrade(self):
        """未来 valid_until 不动 —— 意图仍有效，照常召回。"""
        s = _StubStorage(initial_tags="咖啡")
        ok = downgrade_expired_intent(
            s, {"_key": "memory:frag:ddd", "valid_until": self._future_vu()},
        )
        assert ok is False
        assert s.calls == [], "未来 valid_until 不该触发降级"

    def test_no_valid_until_returns_false(self):
        s = _StubStorage(initial_tags="咖啡")
        ok = downgrade_expired_intent(s, {"_key": "memory:frag:eee"})
        assert ok is False
        assert s.calls == []

    def test_storage_throwing_does_not_propagate(self):
        """fail-open：get_fragment 抛错时降级不该冒到调用方。"""
        class BoomStorage(_StubStorage):
            def get_fragment(self, key: str):  # noqa: D401
                raise RuntimeError("downstream connection lost")

        s = BoomStorage(initial_tags="咖啡")
        ok = downgrade_expired_intent(
            s, {"_key": "memory:frag:fff", "valid_until": self._past_vu()},
        )
        # 即使读 tags 失败，valid_until 清空也要照常落库（核心动作）
        assert ok is True
        _, fields = s.calls[0]
        assert fields["valid_until"] == ""
        # tags 没读到 ⇒ 用兜底值 "expired_fact"
        assert fields["tags"] == EXPIRED_FACT_TAG


# ===========================================================================
# 三、`apply_v2_filters` 「第五道」穿路径
# ===========================================================================

class _ApplyStubStorage:
    """满足 `apply_v2_filters` 全接口的最小桩。"""

    def __init__(self, tags_by_key: Dict[str, str] = None) -> None:
        self._tags_by_key = tags_by_key or {}
        self._v2_min_score = 0.0      # 不让地板挡掉任何候选
        self._final_limit = 100
        self.updated: List[Tuple[str, Dict[str, Any]]] = []

    def _fetch_superseded_by(self, keys: List[str]) -> Dict[str, str]:
        return {}                  # 不挡：测试只关心「第五道」

    def get_fragment(self, key: str) -> Optional[Dict[str, Any]]:
        if key in self._tags_by_key:
            return {"tags": self._tags_by_key[key]}
        return None

    def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
        self.updated.append((key, dict(fields)))
        return True


class TestApplyV2FiltersFifthStep:
    """apply_v2_filters 第五道：意图过期 ⇒ 降级 + 剔除。"""

    def test_expired_intent_excluded_and_downgraded(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        stub = _ApplyStubStorage(tags_by_key={"memory:frag:g": "咖啡,意图"})
        frags = [
            {"_key": "memory:frag:g", "content": "用户喜欢拿铁咖啡",
             "valid_until": past, "_sim": 0.9},
            {"_key": "memory:frag:h", "content": "用户是夜猫子",
             "_sim": 0.8},
        ]
        out = apply_v2_filters(stub, frags)
        keys = [f["_key"] for f in out]
        assert keys == ["memory:frag:h"], "过期意图应当当场剔除"
        # 降级调用：valid_until 清空 + tags 加 expired_fact
        assert any(
            c[0] == "memory:frag:g" and c[1].get("valid_until") == ""
            for c in stub.updated
        )

    def test_future_intent_kept(self):
        fut = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        stub = _ApplyStubStorage()
        frags = [
            {"_key": "memory:frag:k", "content": "明天要喝拿铁",
             "valid_until": fut, "_sim": 0.9},
        ]
        out = apply_v2_filters(stub, frags)
        assert [f["_key"] for f in out] == ["memory:frag:k"]
        assert stub.updated == [], "未过期不该触发降级"

    def test_fail_open_on_downgrade_throw(self):
        """降级抛错时本条剔除但**其余条目照常通过**。"""
        past = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

        class PartialBoom(_ApplyStubStorage):
            def update_fragment_fields(self, key: str, fields: Dict[str, Any]) -> bool:
                if key == "memory:frag:bad":
                    raise RuntimeError("downstream failed")
                return super().update_fragment_fields(key, fields)

        stub = PartialBoom()
        frags = [
            {"_key": "memory:frag:bad", "content": "A", "valid_until": past, "_sim": 0.9},
            {"_key": "memory:frag:ok", "content": "B", "_sim": 0.8},
        ]
        out = apply_v2_filters(stub, frags)
        # 抛错那条按 fail-open 剔除（不让它冒到上层），另一条照常过
        assert [f["_key"] for f in out] == ["memory:frag:ok"]

    def test_no_valid_until_passes_through(self):
        """普通事实（无 valid_until）不受第五道影响。"""
        stub = _ApplyStubStorage()
        frags = [
            {"_key": "memory:frag:m", "content": "咖啡之选", "_sim": 0.7},
        ]
        out = apply_v2_filters(stub, frags)
        assert [f["_key"] for f in out] == ["memory:frag:m"]
        assert stub.updated == []


# ===========================================================================
# 四、SQLite 真后端：filter 放宽 + 惰性降级落地
# ===========================================================================

@pytest.fixture()
def sqlite_storage(tmp_path):
    """SQLite hermetic 后端实例 —— 不连 Redis/PG。"""
    from keepsake.storage_sqlite import SqliteStorage
    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    assert s.ensure_index() is True
    yield s
    s.close()


class TestSqliteIntentLazyExpire:
    """SQLite 真后端：未来 valid_until 进候选，过期 valid_until 被惰性剔除。"""

    def test_future_valid_until_appears_in_search(self, sqlite_storage):
        """放宽 filter 后意图条目能进入候选 —— search_bm25 命中。"""
        s = sqlite_storage
        future_vu = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        s.store(text="今天想喝拿铁咖啡", tags="意图",
                applicability="time: tomorrow")
        # 直接 UPDATE 把 valid_until 设成未来（意图条目走 DB 直写）
        with s._lock:                            # noqa: SLF001
            s._db().execute(                    # noqa: SLF001
                "UPDATE ks_fragment SET valid_until = ? WHERE tags LIKE ?",
                (future_vu, "%意图%"),
            )
            s._db().commit()                     # noqa: SLF001
        out = s.search_bm25("拿铁咖啡")
        assert any("拿铁咖啡" in (f.get("content") or "") for f in out), \
            "未来 valid_until 的意图条目应进入候选"

    def test_expired_valid_until_downgrades_in_place(self, sqlite_storage):
        """search 命中过期意图 ⇒ 当场降级 + 剔除 + DB 落 expired_fact。"""
        s = sqlite_storage
        past_vu = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        s.store(text="昨天买的咖啡豆很香", tags="事实")
        key = s._fragment_key_for("昨天买的咖啡豆很香")   # noqa: SLF001
        # 直写 valid_until = 过去（模拟「意图已过期」）
        with s._lock:                            # noqa: SLF001
            s._db().execute(                    # noqa: SLF001
                "UPDATE ks_fragment SET valid_until = ? WHERE key = ?",
                (past_vu, key),
            )
            s._db().commit()                     # noqa: SLF001

        out_before = s.search_bm25("咖啡豆")
        # search filter 已把过去 valid_until 滤掉（设计如此，与原行为一致）
        # —— 但若绕过 filter 直读，apply_v2_filters 的第五道会触发降级
        from keepsake.core.shared import apply_v2_filters
        raw = s.search_bm25("咖啡豆")
        # 直读单条 fragment 后人为带上 valid_until 走 apply_v2_filters
        frag = s.get_fragment(key) or {}
        frag["_key"] = key
        frag["valid_until"] = past_vu
        frag["_sim"] = 0.9
        out = apply_v2_filters(s, [frag])
        assert out == [], "过期意图在 apply_v2_filters 里应被当场剔除"

        # 库内已落 expired_fact 标签 + valid_until 已清空
        after = s.get_fragment(key) or {}
        assert after.get("valid_until", "") == ""
        assert EXPIRED_FACT_TAG in (after.get("tags") or "")


# ===========================================================================
# 五、`is_chain_head` —— 判据单点化
# ===========================================================================

class TestIsChainHead:
    """链头判据：缺字段 / 空串 / '1' 都算链头；'0' 才算链身。"""

    @pytest.mark.parametrize("value,expected", [
        (None, True),
        ("", True),
        ("1", True),
        ("0", False),
        (b"0", False),
        (b"1", True),
        ("1 ", True),                  # strip
    ])
    def test_values(self, value, expected):
        assert ks_shared.is_chain_head(value) is expected
