"""常驻核心层（core tag）—— 写入原文 / 每轮全量注入 / 检索不重复 / 失败降级。

🔴 **本文件锁死的是「记忆不许被悄悄改坏或悄悄消失」**，不是某个格式细节：
  1. `on_memory_write` **原文**入库（不过 LLM 提炼），且幂等
  2. `system_prompt_block` 每轮把 core 记忆条目**原文**注入
  3. 读库失败 / 超时 ⇒ **fail-open**（返回静态文案，绝不抛、绝不阻塞对话）
  4. 超预算截断时**必须留一行可见说明**，不许静默丢
  5. `prefetch` 排除 core 记忆条目（核心层已无条件注入，重复注入是故障）
  6. 三后端 `list_by_tag` 都是**逗号边界的完整标签相等**（`core` ≠ `core-x`）

除 SQLite 真库用例外全部 hermetic（内存 fake storage，零网络零数据库）。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from keepsake import KeepsakeProvider, _CORE_CACHE, _CORE_MAX_CHARS, _has_tag
from keepsake.storage_sqlite import SqliteStorage


# ---------------------------------------------------------------------------
# fake storage：只实现本文件用到的四个原语
# ---------------------------------------------------------------------------

class FakeStorage:
    """内存 fake —— `list_by_tag` / `store` / `delete_fragments_batch` / `search`。

    `list_by_tag` 抛错或 sleep 由构造参数控制（fail-open 两条用例要用）。
    """

    def __init__(self, rows: List[Dict[str, Any]] | None = None,
                 *, list_raises: bool = False, list_delay: float = 0.0):
        self.rows: List[Dict[str, Any]] = list(rows or [])
        self.list_calls = 0
        self.store_calls: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self._list_raises = list_raises
        self._list_delay = list_delay

    def list_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        self.list_calls += 1
        if self._list_delay:
            import time
            time.sleep(self._list_delay)
        if self._list_raises:
            raise RuntimeError("list_by_tag 炸了")
        from keepsake import _has_tag as has
        out = [dict(r) for r in self.rows if has(r.get("tags", ""), tag)]
        out.sort(key=lambda r: (r.get("updated_at", ""), r.get("key", "")))
        return out

    def store(self, text: str, tags: str = "", category: str = "", source: str = "",
              fragment_type: str = "", **_kw) -> bool:
        self.store_calls.append({"text": text, "tags": tags, "category": category,
                                 "source": source, "fragment_type": fragment_type})
        self.rows.append({"key": f"memory:frag:{len(self.rows)}", "content": text,
                          "tags": tags, "updated_at": ""})
        return True

    def delete_fragments_batch(self, keys: List[str]) -> int:
        self.deleted.extend(keys)
        self.rows = [r for r in self.rows if r.get("key") not in keys]
        return len(keys)

    def search(self, query: str, tag_filter: str = "", **_kw) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r.get("_search_hit")]

    def fragment_exists(self, key: str):
        return False


def _provider(storage) -> KeepsakeProvider:
    """构造一个只挂了 storage 的 provider（跳过 initialize，不连任何后端）。"""
    p = object.__new__(KeepsakeProvider)
    p._config = {}
    p._storage = storage
    p._tag_filter = ""
    p._skip_min_length = 2
    p._skip_patterns = set()
    return p


@pytest.fixture(autouse=True)
def _clear_core_cache():
    """核心层缓存是进程级的 —— 每个用例前后清干净，否则跨用例串味。"""
    _CORE_CACHE["ts"] = 0.0
    _CORE_CACHE["frags"] = None
    yield
    _CORE_CACHE["ts"] = 0.0
    _CORE_CACHE["frags"] = None


PROBE_TEXT = "【探针】端口 8091，路径 /opt/net/x，口令位置 secrets/x.pass"


# ---------------------------------------------------------------------------
# 一、写入：原文、幂等、动作语义
# ---------------------------------------------------------------------------

def test_write_stores_verbatim_without_distillation():
    """🔴 原文逐字入库：标点、空格一字不差，且不经 LLM 提炼/摘要改写。"""
    st = FakeStorage()
    _provider(st).on_memory_write("add", "memory", PROBE_TEXT)
    assert len(st.store_calls) == 1
    assert st.store_calls[0]["text"] == PROBE_TEXT, "core 层必须是原文，改写即记忆损坏"
    assert st.store_calls[0]["tags"] == "core,memory"
    assert st.store_calls[0]["fragment_type"] == "core"


def test_write_is_idempotent_on_same_content():
    """同一条常驻记忆写两次 ⇒ 库里仍只有一条（重复注入是用户能看出来的故障）。"""
    st = FakeStorage()
    p = _provider(st)
    p.on_memory_write("add", "memory", PROBE_TEXT)
    st.store_calls.clear()
    p.on_memory_write("add", "memory", PROBE_TEXT)
    assert st.store_calls == []


def test_write_never_raises_into_hermes():
    """任何异常都吞掉 + warning —— 本方法在 Hermes memory 工具的成功路径上同步调用。"""
    class Boom(FakeStorage):
        def store(self, *_a, **_k):
            raise RuntimeError("库炸了")

    _provider(Boom()).on_memory_write("add", "memory", PROBE_TEXT)   # 不抛即过


def test_unknown_action_is_skipped_with_warning(caplog):
    """识别不了的 action ⇒ warning + 跳过，绝不猜着写。"""
    st = FakeStorage()
    with caplog.at_level("WARNING"):
        _provider(st).on_memory_write("frobnicate", "memory", PROBE_TEXT)
    assert st.store_calls == []
    assert "frobnicate" in caplog.text


def test_replace_deletes_old_then_writes_new():
    """replace 带 old_text ⇒ 先删旧再写新。"""
    st = FakeStorage([{"key": "memory:frag:old", "content": "旧记忆",
                       "tags": "core,memory", "updated_at": ""}])
    _provider(st).on_memory_write("replace", "memory", "新记忆", {"old_text": "旧记忆"})
    assert st.deleted == ["memory:frag:old"]
    assert [c["text"] for c in st.store_calls] == ["新记忆"]


def test_replace_without_old_entry_degrades_to_add():
    """找不到旧条目 ⇒ 按新增处理（重写一条记忆时这是唯一不丢内容的做法）。"""
    st = FakeStorage()
    _provider(st).on_memory_write("update", "user", "新记忆", {"old_text": "库里没有的"})
    assert [c["text"] for c in st.store_calls] == ["新记忆"]


def test_remove_deletes_the_entry():
    st = FakeStorage([{"key": "memory:frag:x", "content": "该删的",
                       "tags": "core,memory", "updated_at": ""}])
    _provider(st).on_memory_write("remove", "memory", "该删的", {"old_text": "该删的"})
    assert st.deleted == ["memory:frag:x"]
    assert st.store_calls == []


def test_remove_by_substring_old_text_deletes_the_one_entry():
    """🔴 Hermes memory 工具的 old_text 是**短子串**不是全文 —— 只做全文相等会让
    remove/replace 静默删不掉旧条目（同一事实库里两份的根因）。"""
    st = FakeStorage([{"key": "memory:frag:sub", "content": "keepsake 检索候选池已 20→200，回滚备 config.json.bak",
                       "tags": "core,memory", "updated_at": ""}])
    _provider(st).on_memory_write("remove", "memory", "", {"old_text": "候选池已 20→200"})
    assert st.deleted == ["memory:frag:sub"]
    assert st.store_calls == []


def test_replace_by_substring_old_text_replaces_not_duplicates():
    """replace 用子串定位旧条目 ⇒ 旧的删掉、新的写入，绝不留下两份。"""
    st = FakeStorage([{"key": "memory:frag:old2", "content": "端口是 8091（bai-proxy 转发）",
                       "tags": "core,memory", "updated_at": ""}])
    _provider(st).on_memory_write("replace", "memory", "端口是 9091（改过了）", {"old_text": "端口是 8091"})
    assert st.deleted == ["memory:frag:old2"]
    assert [c["text"] for c in st.store_calls] == ["端口是 9091（改过了）"]


def test_previous_content_wins_over_old_text():
    """镜像带过来的旧条目全文优先于 old_text：old_text 碰巧是另一条的子串也不许删错。"""
    st = FakeStorage([
        {"key": "memory:frag:target", "content": "甲条目：主脑 IP 39.96.44.98 且端口 8091",
         "tags": "core,memory", "updated_at": ""},
        {"key": "memory:frag:decoy", "content": "乙条目：端口 8091 也出现过",
         "tags": "core,memory", "updated_at": ""},
    ])
    _provider(st).on_memory_write("replace", "memory", "新条目",
                                  {"old_text": "端口 8091",
                                   "previous_content": "甲条目：主脑 IP 39.96.44.98 且端口 8091"})
    assert st.deleted == ["memory:frag:target"], "必须按全文命中目标，而不是命中子串诱饵"
    assert [c["text"] for c in st.store_calls] == ["新条目"]


def test_ambiguous_substring_is_not_guessed():
    """子串命中多条 ⇒ 跳过并 warning（宁可不删，不能删错记忆）。"""
    st = FakeStorage([
        {"key": "memory:frag:a", "content": "配置在 /opt/net 下", "tags": "core,memory", "updated_at": ""},
        {"key": "memory:frag:b", "content": "备份也在 /opt/net 下", "tags": "core,memory", "updated_at": ""},
    ])
    _provider(st).on_memory_write("remove", "memory", "", {"old_text": "/opt/net"})
    assert st.deleted == []
    assert st.store_calls == []


# ---------------------------------------------------------------------------
# 二、注入：每轮全量、格式固定、空时不留空行
# ---------------------------------------------------------------------------

def test_core_block_injected_verbatim():
    st = FakeStorage([
        {"key": "k1", "content": "常驻记忆甲", "tags": "core,memory", "updated_at": "1"},
        {"key": "k2", "content": "常驻记忆乙", "tags": "core,user", "updated_at": "2"},
    ])
    out = _provider(st).system_prompt_block()
    assert "<keepsake-core>" in out and "</keepsake-core>" in out
    assert "- 常驻记忆甲" in out and "- 常驻记忆乙" in out
    assert out.startswith("你有Keepsake记忆系统")   # 既有 7 行静态文案保留


def test_empty_core_leaves_no_blank_line():
    """核心层为空 ⇒ 不产生多余空行（否则每轮白插一个空段）。"""
    out = _provider(FakeStorage()).system_prompt_block()
    assert "<keepsake-core>" not in out
    assert out == out.rstrip()
    assert "热门话题用 keepsake_topics() 查询。" in out


def test_core_read_is_cached_60s():
    """缓存 60s：同一 provider 连调两次只查一次库。"""
    st = FakeStorage([{"key": "k", "content": "甲", "tags": "core", "updated_at": "1"}])
    p = _provider(st)
    p.system_prompt_block()
    p.system_prompt_block()
    assert st.list_calls == 1


def test_write_invalidates_core_cache_immediately():
    """🔴 写完立刻失效缓存 —— 否则用户改的记忆要等 60s 才在上下文里变。"""
    st = FakeStorage()
    p = _provider(st)
    p.system_prompt_block()
    p.on_memory_write("add", "memory", PROBE_TEXT)
    out = p.system_prompt_block()
    assert PROBE_TEXT in out
    assert st.list_calls == 2


def test_oversize_core_is_truncated_with_visible_note():
    """超预算 ⇒ 截断 + 一行可见说明（不许静默丢）。"""
    big = "x" * (_CORE_MAX_CHARS // 2 + 100)
    st = FakeStorage([
        {"key": "k1", "content": big, "tags": "core", "updated_at": "1"},
        {"key": "k2", "content": big, "tags": "core", "updated_at": "2"},
        {"key": "k3", "content": "第三条", "tags": "core", "updated_at": "3"},
    ])
    out = _provider(st).system_prompt_block()
    assert "[core 层已截断：共 3 条，本次注入 1 条]" in out
    assert "第三条" not in out


# ---------------------------------------------------------------------------
# 三、fail-open：读库失败 / 超时都绝不阻塞对话
# ---------------------------------------------------------------------------

def test_list_failure_is_fail_open():
    """读库抛异常 ⇒ 不抛，且仍返回含静态文案的字符串。"""
    out = _provider(FakeStorage(list_raises=True)).system_prompt_block()
    assert "你有Keepsake记忆系统" in out
    assert "<keepsake-core>" not in out


def test_slow_read_is_fail_open(monkeypatch):
    """耗时超 2s 预算 ⇒ 本轮不注入（宁可少一轮上下文也不能卡死对话）。"""
    st = FakeStorage([{"key": "k", "content": "甲", "tags": "core", "updated_at": "1"}],
                     list_delay=2.2)
    out = _provider(st).system_prompt_block()
    assert "你有Keepsake记忆系统" in out
    assert "甲" not in out


# ---------------------------------------------------------------------------
# 四、检索层不重复注入 core
# ---------------------------------------------------------------------------

def test_prefetch_excludes_core_fragments():
    """🔴 core 记忆条目已由 system_prompt_block 无条件全量注入，prefetch 不得再吐一遍。"""
    st = FakeStorage([
        {"key": "c1", "content": "核心层专属记忆", "tags": "core,memory",
         "updated_at": "1", "_search_hit": True},
        {"key": "n1", "content": "普通检索记忆", "tags": "conversation",
         "updated_at": "2", "_search_hit": True, "_combined_score": 1.0},
    ])
    out = _provider(st).prefetch("端口")
    assert "普通检索记忆" in out
    assert "核心层专属记忆" not in out


# ---------------------------------------------------------------------------
# 五、tag 判据：逗号边界的完整标签相等（三处实现同一口径）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tags,hit", [
    ("core", True),
    ("core,memory", True),
    ("memory,core", True),
    ("c, core", True),          # 迁移行：逗号两侧带空格
    ("core-x", False),          # 子串陷阱
    ("xcore", False),
    ("memory", False),
    ("", False),
])
def test_has_tag_is_comma_bounded(tags, hit):
    assert _has_tag(tags, "core") is hit


def test_unimplemented_backend_raises_instead_of_empty():
    """🔴 未实现的后端必须抛 NotImplementedError，**绝不静默返回空列表**。

    静默空 = 核心层悄悄变空、agent 丢掉全部常驻记忆且无任何告警 ——
    这是本契约最危险的失败形态，比抛错坏得多。
    """
    from keepsake.storage_base import StorageBase

    class OnlySearch(StorageBase):
        """只实现检索原语的后端（其余全继承基类的 raise）。"""

        def health_check(self): return True
        def ensure_index(self): return True
        def close(self): pass
        def store(self, text, tags="", category="", source="",
                  fragment_type="", sentiment_score=None, sentiment_label=None): return True
        def supersede_fragment(self, old_key, new_key): return True
        def correct_fragments(self, keys): return 0
        def record_feedback(self, fragment_key, is_positive): return True
        def get_fragment(self, key): return None
        def get_fragments_batch(self, keys): return {}
        def fragment_exists(self, key): return False
        def touch_fragment(self, key): return False
        def set_supersedes(self, new_key, old_key): return False
        def search(self, query, tag_filter="", agent_id="", is_primary=None): return []
        def search_bm25(self, query, tag_filter="", agent_id="", is_primary=None): return []
        def search_knn(self, query, tag_filter="", agent_id="", is_primary=None): return []
        def match_attention(self, content, top_n=10): return 0.0
        def match_hot_topics(self, text, limit=10): return 0.0
        def get_hot_topics(self, limit=10, period="all"): return []
        def entity_timeline(self, entity, limit=20): return []
        def scan_fragment_keys(self, cursor="", limit=200, prefix="memory:frag:"): return "", []
        def write_fragments_batch(self, rows): return 0
        def update_fragment_fields(self, key, fields): return False
        def delete_fragments_batch(self, keys): return 0
        def discover_synonyms(self, rebuild=False): return {}
        def generate_jieba_dict(self, output_path=None): return {}

    with pytest.raises(NotImplementedError):
        OnlySearch().list_by_tag("core")


def test_all_three_backends_implement_list_by_tag():
    """三个后端都必须真实现（漏一个 ⇒ 该后端上核心层恒空）。"""
    from keepsake.storage import RedisStorage
    from keepsake.storage_pg import PgStorage

    for cls in (RedisStorage, PgStorage, SqliteStorage):
        assert "list_by_tag" in cls.__dict__, f"{cls.__name__} 未实现 list_by_tag"


# ---------------------------------------------------------------------------
# 七、Redis 侧 list_by_tag：假 client（不连真 Redis），锁 SCAN 多页 + 边界 + 排序
# ---------------------------------------------------------------------------

def test_redis_list_by_tag_scans_all_pages(tmp_path, monkeypatch):
    """🔴 SCAN 游标必须真走多页 —— 只扫第一页 = 核心层随库增大静默漏条目。

    假 client 吐**两页不相交**的 key（真 SCAN 保证不重复吐同一 key），
    顺带验边界（`xcore`/`core-x` 不命中）与排序（`updated_at` 升序）。
    """
    from keepsake.storage import RedisStorage

    hashes = {
        "memory:frag:a": {"content": b"core-A", "tags": b"core,memory",
                          "updated_at": b"2026-01-02"},
        "memory:frag:b": {"content": b"core-B", "tags": b"memory,core",
                          "created": b"2026-01-01"},
        "memory:frag:c": {"content": b"xcore-D", "tags": b"xcore"},
        "memory:frag:d": {"content": b"core-x-E", "tags": b"core-x"},
        "memory:frag:e": {"content": b"core-F", "tags": b"c, core"},   # 迁移行
        "memory:frag:z": {"content": b"normal", "tags": b"conversation"},
    }
    all_keys = [k.encode() for k in hashes]
    half = len(all_keys) // 2

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def scan(self, cursor=0, match=None, count=None):
            self.calls += 1
            return (0, all_keys[half:]) if self.calls > 1 else (7, all_keys[:half])

        def pipeline(self):
            class P:
                def __init__(self):
                    self.q = []

                def hmget(self, key, *fields):
                    self.q.append((key, fields))

                def execute(self):
                    out = [[hashes.get(k, {}).get(f) for f in fs] for k, fs in self.q]
                    self.q = []
                    return out
            return P()

    client = FakeClient()
    monkeypatch.setattr(RedisStorage, "_get_client", lambda self: client)
    rows = RedisStorage.list_by_tag(object.__new__(RedisStorage), "core")

    assert client.calls == 2, "SCAN 游标没走到第二页"
    assert [r["content"] for r in rows] == ["core-F", "core-B", "core-A"]
    assert "xcore-D" not in [r["content"] for r in rows]
    assert "core-x-E" not in [r["content"] for r in rows]
    # 缺 updated_at 的老记忆条目回落到 created ⇒ 排序键恒存在（None/str 混排会抛 TypeError）
    assert rows[1]["updated_at"] == "2026-01-01"


# ---------------------------------------------------------------------------
# 六、SQLite 真库：list_by_tag 的落库形态与排序（hermetic，tmp_path 临时库）
# ---------------------------------------------------------------------------

def test_sqlite_list_by_tag_is_comma_bounded_and_sorted(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "core.db"))
    assert s.ensure_index() is True
    try:
        # 同批写入，created 相同 ⇒ 只靠 key 兜底排序；再单独补一条 updated 更晚的。
        s.store(text="甲", tags="core,memory", category="core", fragment_type="core")
        s.store(text="乙", tags="memory,core", category="core", fragment_type="core")
        s.store(text="丙", tags="core-x", category="core", fragment_type="core")
        s.store(text="丁", tags="conversation", category="c", fragment_type="memory")

        got = s.list_by_tag("core")
        contents = [f["content"] for f in got]
        assert "丙" not in contents, "core-x 不该命中 core"
        assert "丁" not in contents, "无 core 标签的记忆条目不该出现"
        assert set(contents) == {"甲", "乙"}
        # 排序键恒存在（PG 无 updated_at 列、Redis 无这根字段时都回落 created）
        assert all(f["updated_at"] for f in got)
        assert [f["updated_at"] for f in got] == sorted(f["updated_at"] for f in got)
    finally:
        s.close()


def test_sqlite_list_by_tag_roundtrips_through_provider(tmp_path):
    """真库 + 真 provider 串起来：写入 → 注入（证明存储与注入层字段名对得上）。"""
    s = SqliteStorage(path=str(tmp_path / "core2.db"))
    assert s.ensure_index() is True
    try:
        p = _provider(s)
        p.on_memory_write("add", "memory", PROBE_TEXT)
        _CORE_CACHE["ts"] = 0.0      # 绕开写入时已失效的缓存，读回真库
        _CORE_CACHE["frags"] = None
        out = p.system_prompt_block()
        assert PROBE_TEXT in out
        assert out.count(PROBE_TEXT) == 1, "写入后只应有一份 core 注入"
    finally:
        s.close()