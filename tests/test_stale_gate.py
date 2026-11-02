"""stale 标签机械排除闸（ks_stale_gate）——检索面 + 常驻注入面双向实测。

判据单点 = storage_shared.is_stale_flagged；闸点两处 = apply_v2_filters（三后端共用）
+ provider._core_frags（常驻装配）。测试用假 storage（只喂 _fetch_superseded_by 空表）
走纯函数路径，夹具一律 sqlite/内存（红线：禁 RedisStorage 直调本机残留 redis）。
"""

from keepsake.core.shared import apply_v2_filters, is_stale_flagged


class _FakeStorage:
    _v2_min_score = 0.0

    def _fetch_superseded_by(self, keys):
        return {}


class _ThinFakeStorage(_FakeStorage):
    """PG 薄候选形态：行内无 tags，判据走 _fetch_tags 兜底钩子。"""

    def _fetch_tags(self, keys):
        return {"k_thin_stale": "core,stale-copy", "k_thin_live": "core,agent:x"}


def _f(key, tags, score=0.9, sim=0.9):
    return {"_key": key, "tags": tags, "content": "c-" + key,
            "_combined_score": score, "_sim": sim}


def _thin(key, score=0.9, sim=0.9):
    return {"_key": key, "content": "md5fake", "_combined_score": score, "_sim": sim}


# ── 判据本身：红绿边界 ────────────────────────────────────────────────

class TestIsStaleFlagged:
    def test_plain_stale_tags(self):
        for t in ("superseded", "stale-key", "stale-copy", "deprecated"):
            assert is_stale_flagged(f"core,infra,{t},agent:x") is True

    def test_superseded_by_value_form(self):
        assert is_stale_flagged("superseded-by-memory:frag:abcd") is True

    def test_word_boundary_no_false_hit(self):
        # not-deprecated / superseded_note 这类**不得**误命中（token 边界保护）
        assert is_stale_flagged("not-deprecated,superseded_note") is False
        assert is_stale_flagged("agent:superseded") is False

    def test_empty_and_nonstr(self):
        assert is_stale_flagged("") is False
        assert is_stale_flagged(None) is False
        assert is_stale_flagged(["superseded"]) is False  # 非串不处理（调用方给原生 tags 串）

    def test_whitespace_tolerance(self):
        assert is_stale_flagged("core, deprecated ,infra") is True


# ── 检索面：apply_v2_filters 机械排除 ────────────────────────────────

class TestRetrievalGate:
    def test_stale_rows_excluded_live_kept(self):
        frags = [
            _f("k_stale1", "core,superseded"),
            _f("k_stale2", "stale-key,agent:zly-hermes"),
            _f("k_stale3", "superseded-by-memory:frag:x"),
            _f("k_deprec", "deprecated"),
            _f("k_live", "core,agent:zly-hermes"),
            _f("k_cand", "core_candidate,infra"),
        ]
        out = apply_v2_filters(_FakeStorage(), frags)
        keys = {f["_key"] for f in out}
        assert keys == {"k_live", "k_cand"}     # stale 全剔，正常行与候选行都在

    def test_no_stale_passthrough_unchanged(self):
        frags = [_f("a", "core"), _f("b", "infra")]
        out = apply_v2_filters(_FakeStorage(), frags)
        assert [f["_key"] for f in out] == ["a", "b"]

    def test_thin_rows_gate_via_fetch_tags(self):
        """PG/SQLite 生产形态：薄候选行无 tags ⇒ 判据必须走 _fetch_tags 兜底。"""
        frags = [_thin("k_thin_stale"), _thin("k_thin_live")]
        out = apply_v2_filters(_ThinFakeStorage(), frags)
        assert [f["_key"] for f in out] == ["k_thin_live"]

    def test_thin_rows_no_hook_fail_open(self):
        """后端没钩子 ⇒ 不误杀（fail-open，行内无 tags 也无声明可执行）。"""
        frags = [_thin("k_thin_stale"), _thin("k_thin_live")]
        out = apply_v2_filters(_FakeStorage(), frags)
        assert len(out) == 2

    def test_empty_input(self):
        assert apply_v2_filters(_FakeStorage(), []) == []


# ── 常驻注入面：_core_frags 出口过滤 ─────────────────────────────────

class TestCoreInjectionGate:
    def test_stale_core_row_not_injected(self, tmp_path, monkeypatch):
        """带 core 但同时带 stale 标签的行：检索/注入两面都必须隐身。"""
        monkeypatch.setenv("KEEPSAKE_DB_PATH", str(tmp_path / "ks.sqlite3"))
        from keepsake.adapters.hermes import provider as prov

        import time as _t
        prov._CORE_CACHE["ts"] = _t.time()   # 命中缓存路径（绕开真存储）
        prov._CORE_CACHE["frags"] = [
            {"key": "memory:frag:stalerow", "tags": "core,superseded", "content": "旧口径"},
            {"key": "memory:frag:live", "tags": "core,agent:main-brain", "content": "权威"},
        ]

        class _P:  # 只借判据路径：直接调方法本体，绕开存储与注册表
            _core_fragments = prov.KeepsakeProvider._core_fragments
            _visibility_scope = lambda self, a="", r="": type("S", (), {"is_primary": True, "caller_id": "m"})()
            _registry = None

        got = _P()._core_fragments()
        assert [f["key"] for f in got] == ["memory:frag:live"]
