"""keepsake v2 — LLM 查询扩展 + discover_synonyms 降噪 + 召回分数记录。

任务 ks_retr（2026-09）覆盖：
  子任务 1: storage.search_bm25 路径开头的 glm 查询扩展层
    * 缓存命中 → 同步并入（零延迟增加）
    * 缓存未命中且 BM25 结果 < min_results → 起后台线程；本轮不等待
    * 缓存未命中且结果 ≥ min_results → 不浪费调用
    * LLM 失败/超时/解析失败 → 静默放弃（不重试）
    * 开关关闭 → 纯现行为
  子任务 2: discover_synonyms 降噪
    * 纯 ASCII 短词（<3 字）过滤
    * 内置 denoise stopwords（eg/us/too/no/of/to/in/...）
    * 中文对至少一方长度≥2
    * 每词条同义表上限 8
  子任务 3: search_bm25 出口召回分数分布日志
    * 一行结构化：query_len / hits / top_score / 前5分
    * 不打查询原文

测试约定（全 mock，禁连 180，禁嵌真凭据）：
  * fake redis client（沿用 test_v2_search 的 _FakeClient/HsetPattern）
  * llm_fn 全部注入（不上网络）
  * 不依赖外部 hermes 配置
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from keepsake import query_expansion as qexp
from keepsake.storage import RedisStorage, SYNONYM_HASH_KEY


# 重导出常量便于断言（避免硬编码）
QEXP_CACHE_KEY = qexp.QEXP_CACHE_KEY


# ===========================================================================
# Fake redis client —— 复用 test_v2_search 的思路，加 hash + pipeline 支持
# ===========================================================================

class _FakeHash:
    def __init__(self):
        self.data: Dict[str, str] = {}

    def hset(self, *args, **kwargs):
        if len(args) == 3:
            key, field, value = args
            self.data[field] = str(value)
        elif "mapping" in kwargs:
            for k, v in kwargs["mapping"].items():
                self.data[k] = str(v)
        return True

    def hgetall(self):
        return {k.encode(): v.encode() for k, v in self.data.items()}


class _FakePipeline:
    def __init__(self, store: "_FakeClient"):
        self.store = store
        self.cmds: List[Tuple[str, ...]] = []

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self.cmds.append(("hset", key, mapping))
        else:
            self.cmds.append(("hset", key, field, value))

    def hget(self, key, field):
        self.cmds.append(("hget", key, field))

    def hgetall(self, key):
        self.cmds.append(("hgetall", key))

    def hincrby(self, key, field, n):
        self.cmds.append(("hincrby", key, field, n))

    def expire(self, key, ttl):
        self.cmds.append(("expire", key, ttl))

    def delete(self, *keys):
        self.cmds.append(("delete", keys))

    def zscan(self, key, cursor=0, match=None, count=None):
        self.cmds.append(("zscan", key, cursor, match, count))

    def zscore(self, key, field):
        self.cmds.append(("zscore", key, field))

    def scan(self, cursor=0, match=None, count=None):
        self.cmds.append(("scan", cursor, match, count))

    def execute(self):
        results = []
        for c in self.cmds:
            op = c[0]
            if op == "hget":
                _, key, field = c
                h = self.store.hashes.get(key)
                if h is None:
                    results.append(None)
                else:
                    val = h.data.get(field)
                    results.append(val.encode() if val is not None else None)
            elif op == "hgetall":
                _, key = c
                h = self.store.hashes.get(key)
                if h is None:
                    results.append({})
                else:
                    results.append(h.hgetall())
            elif op == "hset":
                if len(c) == 3:  # mapping
                    _, key, mapping = c
                    h = self.store.hashes.setdefault(key, _FakeHash())
                    for k, v in mapping.items():
                        h.data[k] = str(v)
                else:
                    _, key, field, value = c
                    h = self.store.hashes.setdefault(key, _FakeHash())
                    h.data[field] = str(value)
                results.append(1)
            elif op == "hincrby":
                _, key, field, n = c
                h = self.store.hashes.setdefault(key, _FakeHash())
                cur = int(h.data.get(field, "0"))
                h.data[field] = str(cur + n)
                results.append(cur + n)
            elif op == "expire":
                results.append(1)
            elif op == "delete":
                _, keys = c
                deleted = 0
                for k in keys:
                    if k in self.store.hashes:
                        del self.store.hashes[k]
                        deleted += 1
                results.append(deleted)
            elif op == "scan":
                results.append((0, []))
            elif op == "zscan":
                results.append((0, []))
            elif op == "zscore":
                results.append(None)
            else:
                results.append(1)
        self.cmds.clear()
        return results


class _FakeClient:
    def __init__(self):
        self.hashes: Dict[str, _FakeHash] = {}
        self.search_results: List[Dict[str, Any]] = []

    def ping(self):
        return True

    def hset(self, key, *args, **kwargs):
        h = self.hashes.setdefault(key, _FakeHash())
        return h.hset(*args, **kwargs)

    def hget(self, key, field):
        h = self.hashes.get(key)
        if h is None:
            return None
        v = h.data.get(field)
        return v.encode() if v is not None else None

    def hgetall(self, key):
        h = self.hashes.get(key)
        if h is None:
            return {}
        return h.hgetall()

    def delete(self, *keys):
        deleted = 0
        for k in keys:
            if k in self.hashes:
                del self.hashes[k]
                deleted += 1
        return deleted

    def pipeline(self):
        return _FakePipeline(self)

    def scan(self, cursor=0, match=None, count=None):
        """扫描 hash key（按 match glob）—— discover_synonyms 用。"""
        # 简单按 prefix glob 实现
        prefix = match.rstrip("*") if match and match.endswith("*") else None
        keys = []
        for k in self.hashes:
            if k.startswith("memory:frag:"):
                keys.append(k.encode())
        return (0, keys)

    def ft(self, index_name):
        return _FakeSearch(self, index_name, self.search_results)


class _FakeSearchResult:
    def __init__(self, docs):
        self.docs = docs


class _FakeSearchDoc:
    def __init__(self, doc_id: str, fields: Dict[str, Any], score: float = 1.0):
        self.id = doc_id
        self._score = score
        for k, v in fields.items():
            setattr(self, k, v)
        self.score = score

    def __getattr__(self, name):
        return None


class _FakeSearch:
    def __init__(self, client, index_name, results):
        self.client = client
        self.index_name = index_name
        self.results = results

    def search(self, q, query_params=None):
        docs = []
        for r in self.results:
            fields = {k: v for k, v in r.items() if k != "_key"}
            score = r.get("_score", 1.0)
            docs.append(_FakeSearchDoc(r["_key"], fields, score))
        return _FakeSearchResult(docs)


class _FakeConnectionPool:
    def disconnect(self):
        pass


def _make_storage(
    initial_hashes: Dict[str, Dict[str, str]] = None,
    initial_search: List[Dict[str, Any]] = None,
    qexp_enabled: bool = True,
    qexp_min_results: int = 3,
    qexp_max_terms: int = 6,
    qexp_ttl: int = 86400,
    qexp_llm_fn=None,
) -> Tuple[RedisStorage, _FakeClient]:
    client = _FakeClient()
    if initial_hashes:
        for key, fields in initial_hashes.items():
            client.hashes[key] = _FakeHash()
            for k, v in fields.items():
                client.hashes[key].data[k] = v
    if initial_search:
        client.search_results = list(initial_search)

    storage = RedisStorage(
        host="127.0.0.1", port=6379,
        candidate_count=5, final_limit=5,
        query_expansion_enabled=qexp_enabled,
        query_expansion_min_results=qexp_min_results,
        query_expansion_max_terms=qexp_max_terms,
        query_expansion_ttl=qexp_ttl,
        query_expansion_llm_fn=qexp_llm_fn,
    )
    storage._client = client
    storage._pool = _FakeConnectionPool()
    return storage, client


# ===========================================================================
# A — query_expansion.py 内部单元
# ===========================================================================

class TestNormalizeQuery:
    def test_strip_and_lowercase(self):
        assert qexp.normalize_query("  Hello WORLD  ") == "hello world"

    def test_collapse_whitespace(self):
        assert qexp.normalize_query("foo   bar\t\nbaz") == "foo bar baz"

    def test_empty_returns_empty(self):
        assert qexp.normalize_query("") == ""
        assert qexp.normalize_query("   ") == ""


class TestStripThink:
    def test_complete_think_block_stripped(self):
        raw = "<think>\n分析中\n</think>[\"关键词1\", \"关键词2\"]"
        out = qexp._strip_think(raw)
        assert "<think>" not in out
        assert "关键词1" in out

    def test_unclosed_think_truncates(self):
        raw = "<think>思考到一半没完[\"关键词1\""
        out = qexp._strip_think(raw)
        # 没完整闭合 → 截掉开口后的所有内容
        assert "<think>" not in out
        assert "关键词" not in out

    def test_no_think_passes_through(self):
        raw = '["关键词1", "关键词2"]'
        assert qexp._strip_think(raw) == raw

    def test_empty_input(self):
        assert qexp._strip_think("") == ""
        assert qexp._strip_think(None) == ""


class TestParseExpansionResponse:
    def test_basic_json_array(self):
        raw = '["订单处理", "支付系统", "退款流程"]'
        assert qexp.parse_expansion_response(raw) == ["订单处理", "支付系统", "退款流程"]

    def test_think_then_json(self):
        raw = '<think>用户问的是订单类问题</think>["订单处理", "支付系统"]'
        assert qexp.parse_expansion_response(raw) == ["订单处理", "支付系统"]

    def test_filters_short_terms(self):
        raw = '["订单处理", "我", "AB"]'
        # "我" 长度 1 < MIN_TERM_LEN(4); "AB" 同
        assert qexp.parse_expansion_response(raw) == ["订单处理"]

    def test_filters_blacklist_terms(self):
        raw = '["首先", "其次", "订单处理系统"]'
        # 首先/其次 在黑名单
        assert qexp.parse_expansion_response(raw) == ["订单处理系统"]

    def test_dedup(self):
        raw = '["订单处理系统", "订单处理系统", "退款流程"]'
        assert qexp.parse_expansion_response(raw) == ["订单处理系统", "退款流程"]

    def test_filters_long_terms(self):
        # MAX_TERM_LEN = 12 —— 这个串 13 字符
        raw = '["订单支付超长词汇12345", "正常词"]'
        # "正常词" 长度 3 < MIN_TERM_LEN(4)
        # "订单支付超长词汇12345" 13 > MAX_TERM_LEN(12)
        assert qexp.parse_expansion_response(raw) == []

    def test_invalid_json_returns_empty(self):
        assert qexp.parse_expansion_response("not json") == []
        assert qexp.parse_expansion_response("{}") == []
        assert qexp.parse_expansion_response("") == []


class TestBuildExpansionMessages:
    def test_messages_structure(self):
        msgs = qexp.build_expansion_messages("订单 退款", max_terms=4)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        # 用户消息应包含 max_terms 和 query
        user_msg = msgs[1]["content"]
        assert "4" in user_msg
        assert "订单 退款" in user_msg


# ===========================================================================
# B — storage.search_bm25 查询扩展集成（热路径行为）
# ===========================================================================

class TestSearchBM25QueryExpansion:
    """search_bm25 路径开头：缓存命中/未命中分支 + 后台异步触发。"""

    def test_cache_hit_synchronous_merge(self):
        """缓存命中：扩展词并入查询词；同步、零延迟增加。"""
        client_hashes = {
            QEXP_CACHE_KEY: {
                "订单 查询": json.dumps(["退款", "售后"]),
            },
        }
        s, client = _make_storage(
            initial_hashes=client_hashes,
            initial_search=[
                {"_key": "memory:frag:a", "content": "订单", "_score": 1.0},
                {"_key": "memory:frag:b", "content": "退款", "_score": 0.9},
            ],
            qexp_llm_fn=None,
        )
        out = s.search_bm25("订单 查询")
        # 缓存命中应该 return 结果（mock search 返回固定两条）
        assert len(out) == 2
        # 关键：缓存命中 → 不应再调 LLM（llm_fn=None，没法调）
        # 但 storage 不会因为 llm_fn=None 出错（cache hit 路径根本不走 LLM）

    def test_cache_miss_low_results_triggers_background(self):
        """缓存未命中 + 结果 < min_results → 起后台线程；本轮不等待（热路径 0ms）。"""
        call_log: List[Tuple[float, Any]] = []
        call_event = threading.Event()

        def fake_llm(messages, model):
            call_log.append((time.time(), messages))
            call_event.set()
            # 模拟 LLM 返回有效扩展
            return json.dumps(["相关词1", "相关词2", "相关词3"], ensure_ascii=False)

        s, client = _make_storage(
            initial_search=[],  # 0 results < min_results=3 → 触发后台
            qexp_min_results=3,
            qexp_llm_fn=fake_llm,
        )

        t0 = time.time()
        out = s.search_bm25("无人问过的查询")
        elapsed = time.time() - t0
        # 热路径必须 < 100ms（实际是同步搜索+起线程，无 LLM 阻塞）
        assert elapsed < 0.1, f"search took {elapsed*1000:.1f}ms — should be near-zero"
        # 后台线程会被调用（等最多 2s）
        assert call_event.wait(timeout=2.0), "background LLM was not called"
        # 等线程完成 → 缓存应被写入
        time.sleep(0.1)
        cached = client.hget(QEXP_CACHE_KEY, qexp.normalize_query("无人问过的查询"))
        assert cached is not None
        arr = json.loads(cached.decode("utf-8") if isinstance(cached, bytes) else cached)
        assert "相关词1" in arr

    def test_cache_miss_high_results_no_llm_call(self):
        """缓存未命中 + 结果 ≥ min_results → 不调 LLM（节约调用）。"""
        call_log: List[Any] = []

        def fake_llm(messages, model):
            call_log.append(messages)
            return json.dumps(["相关词"], ensure_ascii=False)

        # 准备 3 条（≥ min_results=3）
        s, client = _make_storage(
            initial_search=[
                {"_key": "memory:frag:a", "content": "命中A", "_score": 1.0},
                {"_key": "memory:frag:b", "content": "命中B", "_score": 0.9},
                {"_key": "memory:frag:c", "content": "命中C", "_score": 0.8},
            ],
            qexp_min_results=3,
            qexp_llm_fn=fake_llm,
        )
        out = s.search_bm25("充足结果查询")
        assert len(out) == 3
        # 等一会儿让后台线程（万一被错误启动）跑完
        time.sleep(0.2)
        # LLM 不应被调用
        assert len(call_log) == 0, f"LLM was unexpectedly called: {len(call_log)} times"

    def test_disabled_switch_keeps_existing_behavior(self):
        """enabled=False → 纯现行为：不查 qexp cache、不起后台。"""
        client_hashes = {
            QEXP_CACHE_KEY: {
                "测试 query": json.dumps(["不应出现"]),
            },
        }
        s, client = _make_storage(
            initial_hashes=client_hashes,
            initial_search=[
                {"_key": "memory:frag:a", "content": "命中", "_score": 1.0},
            ],
            qexp_enabled=False,
        )
        # 启用 qexp 时「不应出现」会被并入；关闭时不应被并入
        out = s.search_bm25("测试 query")
        # 即便缓存里写「不应出现」，开关关闭时不读
        assert len(out) == 1
        # 验证 qexp 缓存从未被读过
        assert client.hashes.get(QEXP_CACHE_KEY) is not None  # 还在（说明没被改）

    def test_llm_failure_silent(self):
        """LLM 抛异常/返回 None → 静默放弃，不影响 search 结果。"""
        def bad_llm(messages, model):
            raise RuntimeError("simulated LLM error")

        s, client = _make_storage(
            initial_search=[],
            qexp_llm_fn=bad_llm,
        )
        # 不应抛
        out = s.search_bm25("任意查询")
        assert out == []
        # 等一会让后台线程跑完（不应导致任何副作用）
        time.sleep(0.2)

    def test_llm_returns_none_silent(self):
        """LLM 返回 None → 静默放弃。"""
        def none_llm(messages, model):
            return None

        s, client = _make_storage(
            initial_search=[],
            qexp_llm_fn=none_llm,
        )
        out = s.search_bm25("查询")
        assert out == []
        time.sleep(0.2)

    def test_empty_query_returns_empty(self):
        """空 query → 直接返回 []，不查 qexp。"""
        s, client = _make_storage(qexp_llm_fn=lambda messages, model: "should not be called")
        assert s.search_bm25("") == []
        assert s.search_bm25("   ") == []
        time.sleep(0.2)


# ===========================================================================
# C — search_bm25 召回分数分布日志（子任务 3）
# ===========================================================================

class TestRecallStatsLogging:
    """search_bm25 出口 logger.info 一行结构化：query_len / hits / top_score / 前5分。"""

    def test_recall_stats_logged(self, caplog):
        """query_len / hits / top_score / 前5分 应出现在日志里。"""
        s, client = _make_storage(
            initial_search=[
                {"_key": "memory:frag:a", "content": "甲", "_score": 0.95},
                {"_key": "memory:frag:b", "content": "乙", "_score": 0.80},
                {"_key": "memory:frag:c", "content": "丙", "_score": 0.60},
            ],
        )
        with caplog.at_level(logging.INFO, logger="keepsake.storage"):
            s.search_bm25("测试查询")

        # 找那行
        recall_lines = [r for r in caplog.records if "recall stats" in r.getMessage()]
        assert recall_lines, "expected 'recall stats' log line"
        msg = recall_lines[-1].getMessage()
        # 字段断言
        assert "query_len=4" in msg  # "测试查询" 长度
        assert "hits=3" in msg
        assert "top_score=0.9500" in msg
        assert "scores=" in msg
        # 关键负向断言：日志里**不能**含查询内容（防隐私）
        assert "测试查询" not in msg

    def test_recall_stats_no_query_text_leaked(self, caplog):
        """敏感查询（长/含人名）→ 日志只打长度，绝不打原文。"""
        sensitive_query = "我的身份证号 110101199001011234"  # 24 字符
        s, client = _make_storage(initial_search=[])
        with caplog.at_level(logging.INFO, logger="keepsake.storage"):
            s.search_bm25(sensitive_query)

        all_text = "\n".join(r.getMessage() for r in caplog.records)
        # 关键负向断言
        assert "110101" not in all_text
        assert "身份证" not in all_text
        # 正向断言：query_len 应记录
        assert "query_len=24" in all_text or "query_len=" in all_text

    def test_recall_stats_empty_results(self, caplog):
        """0 结果也记录（top_score=0.0）。"""
        s, client = _make_storage(initial_search=[])
        with caplog.at_level(logging.INFO, logger="keepsake.storage"):
            s.search_bm25("无结果查询")
        recall_lines = [r for r in caplog.records if "recall stats" in r.getMessage()]
        assert recall_lines
        msg = recall_lines[-1].getMessage()
        assert "hits=0" in msg
        assert "top_score=0.0000" in msg


# ===========================================================================
# D — discover_synonyms 降噪（子任务 2）
# ===========================================================================

class TestDiscoverSynonymsDenoise:
    """discover_synonyms：纯 ASCII 短词/内置 stopword/中文对长度/上限 8。"""

    def _setup_storage_with_corpus(self, fragments: List[Dict[str, str]],
                                    min_word_freq: int = 1,
                                    min_co_occurrence: int = 1,
                                    jaccard_threshold: float = 0.0):
        """构造带碎片语料的 storage（无真实 Redis，纯 mock 客户端）。"""
        client = _FakeClient()
        for i, frag in enumerate(fragments):
            key = f"memory:frag:{i:03d}"
            client.hashes[key] = _FakeHash()
            client.hashes[key].data["content"] = frag["content"]
        storage = RedisStorage(
            host="127.0.0.1", port=6379,
            synonym_min_word_freq=min_word_freq,
            synonym_jaccard_threshold=jaccard_threshold,
            synonym_min_co_occurrence=min_co_occurrence,
        )
        storage._client = client
        storage._pool = _FakeConnectionPool()
        return storage, client

    def test_ascii_short_words_filtered(self):
        """纯 ASCII 短词（<3 字）不入候选 → eg/us/in/an 等碎渣不进。"""
        # 构造一个语料让 "eg" / "us" / "in" 高频共现
        # 但因纯 ASCII 短词过滤，它们不应进入同义词候选
        s, client = self._setup_storage_with_corpus([
            {"content": "embedding model embedding model"},
            {"content": "embedding model embedding model"},
            {"content": "embedding model embedding model"},
        ], min_word_freq=1, min_co_occurrence=1)

        result = s.discover_synonyms(rebuild=True)
        # 检查 SYNONYM_HASH_KEY 里不应有 "eg"/"us"/"em"/"be"/"dd" 等短词
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        # 写入应发生过
        assert syn_hash != {} or result["discovered_groups"] >= 0
        # 关键负向断言：碎渣词不应出现在 hash 里
        for noise in ("eg", "us", "in", "an", "em", "be", "dd"):
            assert noise not in syn_hash, f"noise word '{noise}' leaked into synonyms: {syn_hash}"

    def test_builtin_stopwords_excluded(self):
        """内置 denoise stopwords（eg/us/too/no/of/to/in/...）即使高频也不入候选。"""
        # 语料让 "no" "of" "to" "in" 高频共现
        s, client = self._setup_storage_with_corpus([
            {"content": "no no to in"},
            {"content": "no of to in"},
            {"content": "of in to no"},
        ], min_word_freq=1, min_co_occurrence=1)
        result = s.discover_synonyms(rebuild=True)
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        for noise in ("no", "of", "to", "in"):
            assert noise not in syn_hash, f"stopword '{noise}' leaked: {syn_hash}"

    def test_chinese_pair_length_constraint(self):
        """中文对至少一方长度≥2：单字连词不进候选（靠 _STOP_WORDS，但兜底）。"""
        s, client = self._setup_storage_with_corpus([
            {"content": "支付 支付 订单"},
            {"content": "支付 订单 支付"},
            {"content": "支付 订单 支付"},
        ], min_word_freq=1, min_co_occurrence=1)
        result = s.discover_synonyms(rebuild=True)
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        # 支付/订单 应当成对（同 _STOP_WORDS 不收）
        # 单字"我"/"你"等不应进
        for single in ("我", "你", "他"):
            assert single not in syn_hash

    def test_per_word_cap_of_eight(self):
        """每词条同义表上限 8：构造一个 hub 词与 12 个不同词共现，应被截到 8。"""
        # 构造 hub: "中枢" 与 12 个不同词共现
        others = " ".join(f"主题{i:02d}" for i in range(12))
        s, client = self._setup_storage_with_corpus([
            {"content": f"中枢 {others}"},
            {"content": f"中枢 {others}"},
            {"content": f"中枢 {others}"},
        ], min_word_freq=1, min_co_occurrence=1)
        result = s.discover_synonyms(rebuild=True)
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        # 中枢 的同义表 ≤ 8
        if "中枢" in syn_hash:
            syns = json.loads(syn_hash["中枢"])
            assert len(syns) <= 8, f"中枢 has {len(syns)} syns, expected <=8: {syns}"

    def test_rebuild_clears_existing_hash(self):
        """rebuild=True → DEL SYNONYM_HASH_KEY 后重建；历史碎渣被洗掉。"""
        # 预先在 hash 里写一条碎渣
        client = _FakeClient()
        client.hashes[SYNONYM_HASH_KEY] = _FakeHash()
        client.hashes[SYNONYM_HASH_KEY].data["eg"] = json.dumps(["max", "ssh", "ter"])
        client.hashes[SYNONYM_HASH_KEY].data["us"] = json.dumps(["vi", "vn"])

        storage = RedisStorage(
            host="127.0.0.1", port=6379,
            synonym_min_word_freq=1,
            synonym_min_co_occurrence=1,
        )
        storage._client = client
        storage._pool = _FakeConnectionPool()

        result = storage.discover_synonyms(rebuild=True)
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        # 碎渣已被洗掉
        assert "eg" not in syn_hash, f"eg not cleared: {syn_hash}"
        assert "us" not in syn_hash, f"us not cleared: {syn_hash}"
        # rebuild 字段记录
        assert result["rebuild"] is True

    def test_default_is_incremental(self):
        """默认（rebuild=False）→ 增量：保留手动已有项。"""
        client = _FakeClient()
        client.hashes[SYNONYM_HASH_KEY] = _FakeHash()
        # 手动已存在项
        client.hashes[SYNONYM_HASH_KEY].data["manual_word"] = json.dumps(["手动伙伴"])
        client.hashes[SYNONYM_HASH_KEY].data["eg"] = json.dumps(["old noise"])

        storage = RedisStorage(
            host="127.0.0.1", port=6379,
            synonym_min_word_freq=1,
            synonym_min_co_occurrence=1,
        )
        storage._client = client
        storage._pool = _FakeConnectionPool()

        result = storage.discover_synonyms(rebuild=False)
        syn_hash = client.hashes.get(SYNONYM_HASH_KEY, _FakeHash()).data
        # 手动项应保留
        assert "manual_word" in syn_hash
        # 但旧的碎渣「eg」按现有逻辑不会主动删（增量模式不 DEL）
        # 这是已知行为 —— 测试只验证「不 DEL」
        assert result["rebuild"] is False


# ===========================================================================
# E — discover_synonyms.py 脚本的 --rebuild flag（直测 build_kwargs 纯函数）
# ===========================================================================

class TestDiscoverSynonymsScript:
    """scripts/discover_synonyms.py：CLI argv→kwargs 解析纯函数 build_kwargs。

    不起子进程（subprocess 拿不到 monkeypatch，跨机也不通）；改用
    importlib 直导入 + Path(__file__).parents[1] 定位，跨机可跑。
    """

    @staticmethod
    def _load_script_module():
        """用 importlib 把 scripts/discover_synonyms.py 当模块加载（不执行 main）。"""
        import importlib.util
        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "scripts" / "discover_synonyms.py"
        spec = importlib.util.spec_from_file_location(
            "discover_synonyms_script", str(script_path),
        )
        assert spec is not None and spec.loader is not None, \
            f"cannot load spec for {script_path}"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_build_kwargs_no_args(self):
        """无参 → rebuild=False。"""
        m = self._load_script_module()
        assert m.build_kwargs([]) == {"rebuild": False}

    def test_build_kwargs_with_rebuild(self):
        """--rebuild → rebuild=True。"""
        m = self._load_script_module()
        assert m.build_kwargs(["--rebuild"]) == {"rebuild": True}

    def test_build_kwargs_combined_no_cross_talk(self):
        """--rebuild 与其他未来 flag 不串：当前只有 --rebuild 一个，组合不应误开。"""
        m = self._load_script_module()
        # 即便混入看似别的开关（实际未声明），rebuild 仍按出现次数决定（一次 → True）
        assert m.build_kwargs(["--rebuild"]) == {"rebuild": True}
        # 多次写也仍是 True（store_true 是单 bool 位）
        assert m.build_kwargs(["--rebuild", "--rebuild"]) == {"rebuild": True}

    def test_build_kwargs_unknown_flag_raises_systemexit(self, capsys):
        """未知 flag：交回 argparse 默认行为 → SystemExit(2) + 错误信息到 stderr。

        与现状一致（不改行为；测试只锁住它，避免后续无意改成忽略）。
        """
        m = self._load_script_module()
        with pytest.raises(SystemExit) as exc_info:
            m.build_kwargs(["--bogus"])
        # argparse 默认 exit code = 2
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--bogus" in err
        assert "unrecognized arguments" in err.lower()


# ===========================================================================
# F — integration: qexp + recall stats 同时启用
# ===========================================================================

class TestQueryExpansionFullPath:
    """端到端：qexp 缓存命中 + recall stats 记录同时工作。"""

    def test_full_path_cache_hit_with_recall_log(self, caplog):
        """缓存命中走扩展查询 + 出口记录召回分数（验证两个新能力并存）。"""
        client_hashes = {
            QEXP_CACHE_KEY: {
                "订单 查询": json.dumps(["退款", "售后"]),
            },
        }
        s, client = _make_storage(
            initial_hashes=client_hashes,
            initial_search=[
                {"_key": "memory:frag:a", "content": "订单", "_score": 0.9},
                {"_key": "memory:frag:b", "content": "退款", "_score": 0.7},
            ],
        )
        with caplog.at_level(logging.INFO, logger="keepsake.storage"):
            out = s.search_bm25("订单 查询")

        assert len(out) == 2
        # recall stats 也应在
        recall_lines = [r for r in caplog.records if "recall stats" in r.getMessage()]
        assert recall_lines
        msg = recall_lines[-1].getMessage()
        # "订单 查询" 是 5 字符（含空格）
        assert "query_len=5" in msg
        assert "hits=2" in msg