"""PgStorage 真库读写测试（对 88 的 keepsake_test 库跑）。

🔴 凭据规则：
  * DSN 只从 `/home/claude_user/.keepsake_pg_test.env`（KEEPSAKE_PG_DSN）读
  * 读不到 / 没装 psycopg → **skip，不 fail**（这台机器没库时也要能跑全套）
  * 凭据绝不进代码、日志、断言输出（报错只打印异常类型，不打印 DSN）

🔴 只连这一个测试库：host 必须是 8.140.192.91 / dbname 必须是 keepsake_test，
   否则 skip（防手滑连到生产）。

测试结束删除本次写入的全部行（不留残渣）。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Dict

import pytest

from keepsake.splitter import extract_entities, extract_keywords
from keepsake.storage_pg import PgStorage

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"

# 本次运行写入的所有碎片 key（teardown 按 key 清；整表不 TRUNCATE —— 那是别人的库）
_WROTE_KEYS: list[str] = []
_TOPICS: list[str] = []


def _read_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _dsn_is_safe(dsn: str) -> bool:
    """只允许连指定测试库。"""
    return bool(dsn) and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn


psycopg = pytest.importorskip("psycopg", reason="未装 psycopg（pip install 'psycopg[binary]'）")
DSN = _read_dsn()
if not _dsn_is_safe(DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 真库测试",
                allow_module_level=True)


@pytest.fixture(scope="module")
def pg():
    """建表 + 建实例；整个模块共用一条连接（远端库建连要 2~4s，逐测试重连太慢）。"""
    storage = PgStorage(dsn=DSN, connect_timeout=30)
    storage.ensure_index()
    yield storage
    storage.close()


@pytest.fixture(autouse=True)
def _cleanup(pg):
    """每个测试后清掉本次写入的行（按 key / 按本轮关键词）。"""
    yield
    with pg._tx() as cur:      # noqa: SLF001 — 测试内清理自有数据
        if _WROTE_KEYS:
            cur.execute("DELETE FROM ks_fragment WHERE key = ANY(%s)", (list(_WROTE_KEYS),))
            cur.execute("DELETE FROM ks_entity_timeline WHERE frag_key = ANY(%s)",
                        (list(_WROTE_KEYS),))
            _WROTE_KEYS.clear()
        if _TOPICS:
            cur.execute("DELETE FROM ks_hot_topic WHERE topic = ANY(%s)", (list(_TOPICS),))
            cur.execute("DELETE FROM ks_attention WHERE topic = ANY(%s)", (list(_TOPICS),))
            cur.execute("DELETE FROM ks_hot_topic_seen WHERE topic = ANY(%s)", (list(_TOPICS),))
            _TOPICS.clear()


def _text(tag: str) -> str:
    """每条测试用独立文本 ⇒ 独立 key，绝不撞既有数据。"""
    _TOPICS.append(tag)
    return f"用户偏好使用 {tag} 方案处理数据库迁移，快照键 {uuid.uuid4().hex[:8]}"


def test_health_check_and_ensure_index(pg):
    assert pg.health_check() is True
    assert pg.ensure_index() is True      # 幂等：再跑一次不炸


def test_store_get_roundtrip_field_parity(pg):
    """store → get 往返：字段名与 Redis hash 语义对齐，值一致。"""
    text = _text("PG往返校验")
    assert pg.store(text, tags="shared,test", category="pref", source="unit",
                    fragment_type="fact", sentiment_score=1.5, sentiment_label="positive") is True
    key = pg._fragment_key_for(text)     # noqa: SLF001 — 与 Redis 同算法，测试需对齐
    _WROTE_KEYS.append(key)

    frag = pg.get_fragment(key)
    assert frag is not None
    assert frag["content"] == text
    assert frag["tags"] == "shared,test"
    assert frag["category"] == "pref"
    assert frag["source"] == "unit"
    assert frag["fragment_type"] == "fact"
    assert frag["sentiment_score"] == "1.5"          # 与 Redis 侧一样存字符串
    assert frag["sentiment_label"] == "positive"
    assert frag["feedback_score"] == "0"
    assert "created" in frag and frag["created"]
    # 稀疏语义：没写的字段不进 dict（同 Redis hash 只存写过的字段）
    assert "superseded_by" not in frag

    assert pg.get_fragment("memory:frag:不存在的键") is None
    assert pg.get_fragment("") is None


def test_get_fragments_batch(pg):
    """批量取：存在的进结果，缺失的不进。"""
    t1, t2 = _text("PG批量一"), _text("PG批量二")
    pg.store(t1)
    pg.store(t2)
    k1, k2 = pg._fragment_key_for(t1), pg._fragment_key_for(t2)   # noqa: SLF001
    _WROTE_KEYS.extend([k1, k2])

    out = pg.get_fragments_batch([k1, k2, "memory:frag:缺失键"])
    assert set(out) == {k1, k2}
    assert out[k1]["content"] == t1
    assert pg.get_fragments_batch([]) == {}


def test_supersede_fragment_marks_old_key(pg):
    """封边：旧键打 superseded_by/superseded_at，新键不受影响。"""
    t_old, t_new = _text("PG旧事实"), _text("PG新事实")
    pg.store(t_old)
    old_key = pg._fragment_key_for(t_old)   # noqa: SLF001
    _WROTE_KEYS.append(old_key)
    pg.store(t_new)
    new_key = pg._fragment_key_for(t_new)   # noqa: SLF001
    _WROTE_KEYS.append(new_key)

    assert pg.supersede_fragment(old_key, new_key) is True
    old = pg.get_fragment(old_key)
    assert old["superseded_by"] == new_key
    assert old["superseded_at"]
    assert "superseded_by" not in pg.get_fragment(new_key)

    assert pg.supersede_fragment("", new_key) is False


def test_store_same_text_archives_previous_version(pg):
    """同内容重复 store：旧版标 valid_until + is_archived，新版另起键，feedback 保留。"""
    text = _text("PG去重")
    pg.store(text, tags="v1")
    first_key = pg._fragment_key_for(text)   # noqa: SLF001
    _WROTE_KEYS.append(first_key)
    pg.record_feedback(first_key, True)          # feedback = 1

    pg.store(text, tags="v2")
    # 新版键带 :<epoch> 后缀，时间戳由 store 内部生成 —— 按前缀查回来，不猜
    with pg._ro() as cur:                        # noqa: SLF001 — 取 store 自己写的键
        cur.execute("SELECT key FROM ks_fragment WHERE key LIKE %s", (f"{first_key}:%",))
        new_keys = [r[0] for r in cur.fetchall()]
    assert len(new_keys) == 1, f"应恰好产生一个新版本键，实际 {new_keys}"
    second_key = new_keys[0]
    _WROTE_KEYS.append(second_key)

    old = pg.get_fragment(first_key)
    assert old["is_archived"] == "1"
    assert old["valid_until"]
    assert old["feedback_score"] == "1"          # 反馈不因重写而重置

    fresh = pg.get_fragments_batch([second_key]).get(second_key)
    assert fresh is not None and fresh["tags"] == "v2"
    # 两个键都在库里（timeline 登记的是新版键）
    assert len(pg.get_fragments_batch([first_key, second_key])) == 2


def test_record_feedback_changes_weight(pg):
    """反馈：有用 +1 / 没用 -2。"""
    text = _text("PG反馈")
    pg.store(text)
    key = pg._fragment_key_for(text)   # noqa: SLF001
    _WROTE_KEYS.append(key)

    assert pg.record_feedback(key, True) is True
    assert pg.get_fragment(key)["feedback_score"] == "1"
    pg.record_feedback(key, True)
    assert pg.get_fragment(key)["feedback_score"] == "2"
    pg.record_feedback(key, False)
    assert pg.get_fragment(key)["feedback_score"] == "0"
    pg.record_feedback(key, False)
    assert pg.get_fragment(key)["feedback_score"] == "-2"


def test_correct_fragments(pg):
    """纠正：打 corrected 标签 + feedback_score=-1，返回处理条数。"""
    t1, t2 = _text("PG纠正一"), _text("PG纠正二")
    pg.store(t1, tags="shared")
    pg.store(t2, tags="shared")
    k1, k2 = pg._fragment_key_for(t1), pg._fragment_key_for(t2)   # noqa: SLF001
    _WROTE_KEYS.extend([k1, k2])

    assert pg.correct_fragments([k1, k2, "memory:frag:缺失键"]) == 2
    frag = pg.get_fragment(k1)
    assert "corrected" in frag["tags"].split(",")
    assert frag["feedback_score"] == "-1"
    assert frag["corrected_at"]
    # 再纠正一次不重复加标签
    pg.correct_fragments([k1])
    assert pg.get_fragment(k1)["tags"].count("corrected") == 1
    assert pg.correct_fragments([]) == 0


def test_hot_topics_and_match(pg):
    """热词：统计入榜 + match_hot_topics 命中 + 空文本/不命中返回 0。"""
    text = _text("PG热词专题")
    pg.store(text)
    _WROTE_KEYS.append(pg._fragment_key_for(text))   # noqa: SLF001

    keywords = extract_keywords(text, max_keywords=5)
    assert keywords, "测试样本必须能提取到关键词"
    topics = pg.get_hot_topics(limit=500, period="all")
    assert topics, "store 后应有热词统计"
    listed = {t["topic"]: t["count"] for t in topics}
    for kw in keywords:
        assert kw in listed, f"关键词 {kw!r} 未入热词榜"
        assert listed[kw] > 0

    # 命中刚写入的文本 → 权重 ≈ 1.0（last_seen 就是现在）
    hits = pg.match_hot_topics(text, limit=500)
    assert hits > 0.5, f"刚写入的文本应命中热词，实际 {hits}"

    daily = {t["topic"] for t in pg.get_hot_topics(limit=500, period="daily")}
    assert set(keywords) <= daily, "日榜应与全局榜同源"
    assert pg.match_hot_topics("") == 0.0
    assert pg.match_hot_topics("完全无关的句子 xyzzy", limit=500) == 0.0


def test_match_attention(pg):
    """注意力加权：无命中 = 1.0，命中话题 > 1.0（且不超 boost_max）。"""
    assert pg.match_attention("") == 1.0
    text = _text("PG注意力探针")
    pg.store(text)
    _WROTE_KEYS.append(pg._fragment_key_for(text))   # noqa: SLF001
    boosted = pg.match_attention(text, top_n=50)
    assert 1.0 <= boosted <= 1.5, f"注意力加权越界: {boosted}"


def test_entity_timeline(pg):
    """实体时间线：写入含实体的文本后，按实体能倒序取回。"""
    marker = uuid.uuid4().hex[:8]
    text = f"项目 Apollo{marker} 的部署流程由张三负责，Apollo{marker} 上周完成灰度。"
    pg.store(text)
    _WROTE_KEYS.append(pg._fragment_key_for(text))   # noqa: SLF001

    # 实体名以 extractor 抽出的为准（大小写/切词规则不在本测试的管辖范围）
    entities = extract_entities(text)
    assert entities, f"样本未抽出实体，测试前提失效: {text!r}"
    timeline = pg.entity_timeline(entities[0], limit=10)
    assert timeline, f"实体 {entities[0]} 应有时间线记录"
    assert any(text in item["content"] for item in timeline)
    assert all(item["created"] for item in timeline)

    assert pg.entity_timeline("") == []
    assert pg.entity_timeline(f"绝不存在实体{marker}") == []


def test_close_is_idempotent():
    """close 可重复调用（接口约定）。"""
    storage = PgStorage(dsn=DSN, connect_timeout=30)
    storage.close()
    storage.close()


def test_wrong_dsn_raises_instead_of_silently_succeeding():
    """🔴 负向：错误 DSN 必须明确报错，绝不静默成功。"""
    bad = PgStorage(dsn="postgresql://nobody:nopass@127.0.0.1:1/nonexistent_db")
    with pytest.raises(Exception) as exc:
        bad.store("坏 DSN 也必须报错")
    # 断言里不能出现口令 —— 只看异常类型
    assert type(exc.value).__name__ in ("OperationalError", "InterfaceError"), \
        f"预期 psycopg 连接类异常，实际 {type(exc.value).__name__}"
    assert "nopass" not in str(exc.value), "异常信息不得回显凭据"
    assert bad.health_check() is False

# =============================================================================
# 批 2：PG 检索（合成数据 → 真库断言）
# =============================================================================

_EMBED_DIM = 1536


class _FakeEmbedder:
    """确定性假 embedder（不调任何 LLM/网络）。

    维度必须等于 `PgStorage` 构造时的 embed_dim（1536）——PG 的 `vector(N)` 列
    维度由 ensure_index 首次建表时定死，ensure_index 会拒绝维度漂移。
    向量由文本的 sha256 派生 ⇒ 同文本恒得同向量，且文本越像共享前缀越多、距离越小。
    """

    _registered = True
    _model = "test-hash-1536"
    dimension = _EMBED_DIM

    def get_embedding(self, text: str):
        raw = hashlib.sha256((text or "").encode("utf-8")).digest()
        vals = [(b + 1) / 256.0 for b in raw] * (_EMBED_DIM // 32 + 1)
        vec = vals[:_EMBED_DIM]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


@pytest.fixture(scope="module")
def pg_search():
    """带 embedder 的实例（is_primary=True → 不限 agent 隔离，检索测试更聚焦）。"""
    storage = PgStorage(dsn=DSN, connect_timeout=30, embedder=_FakeEmbedder(),
                        embed_dim=_EMBED_DIM, is_primary=True, final_limit=5)
    storage.ensure_index()
    yield storage
    storage.close()


# 四个语料主题：每个主题一条；marker 保证本轮写入的 key 与其他轮次不撞
_CORPUS = [
    ("数据库迁移方案", "用户偏好使用 PostgreSQL 方案处理数据库迁移，快照键键值标记甲"),
    ("生图流水线", "ComfyUI 生图 pipeline 部署在 202 服务器端口 8188，标记乙"),
    ("无关闲聊", "今天午饭吃了面条和西红柿，与技术话题完全无关，标记丙"),
    ("任务调度", "agent-worker 把任务调度到 88 节点执行，标记丁"),
]


def _seed_corpus(pg, marker: str) -> Dict[str, str]:
    """写入 4 条合成记忆，返回 {主题: key}。"""
    keys: Dict[str, str] = {}
    for topic, text in _CORPUS:
        pg.store(f"{text} {marker}", tags="shared")
        key = pg._fragment_key_for(f"{text} {marker}")   # noqa: SLF001
        keys[topic] = key
        _WROTE_KEYS.append(key)
    return keys


def test_bm25_hits_the_relevant_fragment(pg_search):
    """BM25：查「数据库迁移」必须命中对应那条，且给出正分与降序。"""
    marker = uuid.uuid4().hex[:8]
    keys = _seed_corpus(pg_search, marker)
    _TOPICS.append(marker)

    res = pg_search.search_bm25(f"数据库迁移 {marker}")
    assert res, f"BM25 应当命中本轮写入的碎片（marker={marker}）"
    assert res[0]["_key"] == keys["数据库迁移方案"]
    assert res[0]["_bm25_score"] > 0
    assert res[0]["_sim"] > 0
    scores = [r["_combined_score"] for r in res]
    assert scores == sorted(scores, reverse=True), "结果必须按综合分降序"


def test_bm25_sanitize_splits_path_and_hyphen_terms(pg_search):
    """🔴 `_sanitize_terms` 的路径/连字符拆子词必须生效（上游 58bf19c/d00b7eb 的坑）。

    库里没有 `/home/...` 这种整串时，整串查必然 0 命中；拆成子词后能命中
    真正含该子词的条目 —— 且绝不能抛 SQL 语法错。
    """
    marker = uuid.uuid4().hex[:8]
    text = f"工程目录约定：代码放在 /home/claude_user/ 下，标记戊"
    pg_search.store(f"{text} {marker}", tags="shared")
    key = pg_search._fragment_key_for(f"{text} {marker}")   # noqa: SLF001
    _WROTE_KEYS.append(key)
    _TOPICS.append(marker)

    # 单元级：整串被拆成子词，绝不作为单个 lexeme 丢给 tsquery
    from keepsake.storage import _sanitize_terms
    parts = _sanitize_terms(["/home/claude_user/trade-platform/"])
    assert {"home", "claude", "user", "trade", "platform"} <= set(parts), parts
    assert "/home/claude_user/trade-platform/" not in parts

    # 集成级：查整条路径不抛语法错，且靠拆出的子词命中到那条含 claude_user 的碎片
    hit = pg_search.search_bm25(f"/home/claude_user/trade-platform/ {marker}")
    assert [r["_key"] for r in hit] == [key]
    # 只给子词，同样命中
    assert [r["_key"] for r in pg_search.search_bm25(f"claude_user {marker}")] == [key]


def test_knn_returns_closest_and_reports_cosine_distance(pg_search):
    """KNN：查「数据库迁移」时对应那条的余弦距离应最小，且落在 [0,2]。"""
    marker = uuid.uuid4().hex[:8]
    keys = _seed_corpus(pg_search, marker)
    _TOPICS.append(marker)

    query = f"用户偏好使用 PostgreSQL 方案处理数据库迁移，快照键键值标记甲 {marker}"
    res = pg_search.search_knn(query)
    assert res, "KNN 应当返回候选（本轮每条都写了向量）"
    assert res[0]["_key"] == keys["数据库迁移方案"]
    dists = [r["_knn_score"] for r in res]
    assert all(0.0 <= d <= 2.0 for d in dists), f"余弦距离应落在 [0,2]，实际 {dists}"
    # 目标那条的余弦距离是最小值。
    # 🔴 列表本身**不**按距离升序 —— search_knn 之后要走共用的
    # _rerank_with_decay（时间衰减/情绪/反馈/热词/注意力重排），返回序是综合分序。
    # 这与 Redis 侧 search_knn 完全一致，不能在这里断言距离有序。
    assert res[0]["_knn_score"] == min(dists)
    # _sim 是 1 - dist/2，越近越高
    assert res[0]["_sim"] >= res[-1]["_sim"]


def test_hybrid_search_fuses_both_paths(pg_search):
    """混合检索 = BM25 + KNN 两路都跑，RRF 融合后仍返回共同命中的那条。"""
    marker = uuid.uuid4().hex[:8]
    keys = _seed_corpus(pg_search, marker)
    _TOPICS.append(marker)

    query = f"数据库迁移 {marker}"
    bm = {r["_key"] for r in pg_search.search_bm25(query)}
    knn = {r["_key"] for r in pg_search.search_knn(query)}
    assert keys["数据库迁移方案"] in bm, "BM25 路应命中"
    assert keys["数据库迁移方案"] in knn, "KNN 路应命中"

    fused = pg_search.search(query)
    assert fused, "混合检索应返回结果"
    assert fused[0]["_key"] == keys["数据库迁移方案"]
    assert "_combined_score" in fused[0] and "_weights" in fused[0]


def test_search_respects_v2_filters(pg_search):
    """v2 后置过滤：consumed 与 superseded 都要被剔除（共用实现）。"""
    marker = uuid.uuid4().hex[:8]
    keys = _seed_corpus(pg_search, marker)
    _TOPICS.append(marker)

    # 打 consumed 标记 → search 应过滤掉
    pg_search.correct_fragments([keys["无关闲聊"]])
    with pg_search._tx() as cur:                          # noqa: SLF001
        cur.execute("UPDATE ks_fragment SET fragment_type = 'consumed' WHERE key = %s",
                    (keys["无关闲聊"],))
    assert all(r["_key"] != keys["无关闲聊"] for r in pg_search.search(f"数据库迁移 {marker}"))

    # 封边 → superseded 的那条被剔除
    target = keys["数据库迁移方案"]
    pg_search.supersede_fragment(target, keys["任务调度"])
    assert all(r["_key"] != target for r in pg_search.search(f"数据库迁移 {marker}"))


def test_search_agent_isolation(pg_search):
    """非主脑且给了 agent_id：只搜得到自己的 + shared 的。"""
    marker = uuid.uuid4().hex[:8]
    mine = f"仅属于 agent alpha 的私密记忆 {marker}"
    other = f"仅属于 agent beta 的私密记忆 {marker}"
    for text in (mine, other):
        pg_search.store(text, tags=f"agent:{text.split(' ')[2]}")
        _WROTE_KEYS.append(pg_search._fragment_key_for(text))    # noqa: SLF001
    _TOPICS.append(marker)

    seen = {r["content"] for r in pg_search.search(f"私密记忆 {marker}",
                                                   agent_id="alpha", is_primary=False)}
    assert mine in seen
    assert other not in seen, "别的 agent 的碎片不该被看到"


def test_empty_query_is_explicit_not_silent(pg_search, caplog):
    """空查询：返回空列表，但**必须留日志**（明确，不是静默故障）。"""
    with caplog.at_level("WARNING"):
        assert pg_search.search_bm25("") == []
        assert pg_search.search_knn("") == []
        assert pg_search.search("   ") == []
    assert caplog.text.count("empty query") >= 3, f"空查询必须有 WARNING：{caplog.text}"


def test_ensure_index_is_idempotent_with_search_columns(pg_search):
    """ensure_index 幂等：tsv / embedding 两列与两个索引重复建不炸。"""
    assert pg_search.ensure_index() is True
    with pg_search._ro() as cur:                      # noqa: SLF001
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ks_fragment'
              AND indexname IN ('idx_ks_fragment_content_tsv', 'idx_ks_fragment_embedding_hnsw')
            ORDER BY indexname
        """)
        found = [r[0] for r in cur.fetchall()]
    assert found == ["idx_ks_fragment_content_tsv", "idx_ks_fragment_embedding_hnsw"]


def test_search_result_shape_matches_redis_contract(pg_search):
    """🔴 返回结构一致性：PG 检索结果的键集合与 Redis 侧契约完全一致。

    为什么这条最关键：上层 `_rrf_fuse` / `_rerank_with_decay` 按**同样的键**取值
    （`_sim` / `_combined_score` / `_weights` / `feedback_score` …）。
    键不一致不会报错，只会**静默错排** —— 评测两套后端时会得出「PG 更差」的
    假结论，而真因只是键名漂了。

    不连 Redis 也能做契约比对：Redis 侧的字段集是 `storage.py` 中 search_bm25 /
    search_knn 里的**字面量元组**，用 AST 取出来当契约（源码即事实，不靠记忆）。
    """
    import ast
    import inspect
    import textwrap

    from keepsake import storage as storage_mod
    from keepsake.storage import RedisStorage

    # --- 1. 从 Redis 侧源码抽出「结果会带哪些普通字段」的字面量契约 ---
    # inspect.getsource 返回的是缩进过的片段，要 dedent 才能 parse
    tree = ast.parse(textwrap.dedent(inspect.getsource(RedisStorage.search_bm25)))
    contract = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and node.target.id == "field" and isinstance(node.iter, ast.Tuple)):
            contract = {ast.literal_eval(e) for e in node.iter.elts}
            break
    assert contract, "RedisStorage.search_bm25 的返回字段元组没找到 —— 契约变了？"
    # Redis 侧另外无条件加的三个键
    redis_keys = contract | {"_key", "_bm25_score", "_sim", "_combined_score", "_weights"}

    # --- 2. PG 侧真实跑一遍，取实际键集合 ---
    marker = uuid.uuid4().hex[:8]
    keys = _seed_corpus(pg_search, marker)
    _TOPICS.append(marker)
    # 字段最全的一条：category/source/fragment_type 都填上 ⇒ 这些键必须真的出现，
    # 否则「subset 通过」只是因为 PG 侧压根没产出这些字段（那是缺陷，不是稀疏）。
    full = f"工程目录约定：代码放在 /home/claude_user/ 下，标记己 {marker}"
    pg_search.store(full, tags="shared", category="convention", source="unit-test",
                    fragment_type="fact")
    _WROTE_KEYS.append(pg_search._fragment_key_for(full))     # noqa: SLF001

    # 恒定必现的键：空值不进 dict（与 Redis hash 稀疏语义一致），所以
    # 「一定出现」的这几个之外，其余是**至多**出现，不是**一定**出现。
    always = {"content", "_key", "_sim", "_combined_score", "_weights"}
    for method, score_key in ((pg_search.search_bm25, "_bm25_score"),
                              (pg_search.search_knn, "_knn_score")):
        res = method(f"数据库迁移 {marker}")
        assert res, f"{method.__name__} 应当有结果"
        got = set(res[0])
        expect = (redis_keys if score_key == "_bm25_score"
                  else (redis_keys - {"_bm25_score"}) | {score_key})
        assert got <= expect, (
            f"{method.__name__} 出现了 Redis 侧不存在的键（上层按同键取值会静默错排）: "
            f"{sorted(got - expect)}"
        )
        assert (always | {score_key}) <= got, (
            f"{method.__name__} 缺少恒定键: {sorted((always | {score_key}) - got)}"
        )

        # 字段齐全的那条：普通字段必须与契约逐字对齐（category/source/fragment_type…）
        by_key = {r["_key"]: r for r in (method(f"claude_user {marker}") or [])}
        full_key = pg_search._fragment_key_for(full)      # noqa: SLF001
        assert full_key in by_key, f"{method.__name__} 没召回字段齐全的那条"
        plain = set(by_key[full_key]) - {score_key, "_sim", "_combined_score",
                                         "_weights", "_key"}
        assert plain == (contract - {"invalid_at"}), (
            f"{method.__name__} 字段齐全时普通字段集合与 Redis 契约不符\n"
            f"  Redis 契约: {sorted(contract - {'invalid_at'})}\n"
            f"  PG 实际   : {sorted(plain)}"
        )

    # --- 3. 混合检索（RRF 后）同样不得越界 ---
    fused = pg_search.search(f"数据库迁移 {marker}")
    assert fused
    assert set(fused[0]) <= redis_keys
    assert always <= set(fused[0])

    # --- 4. 契约常量本身两边一致（storage_shared 是唯一定义处）---
    from keepsake.storage_shared import SEARCH_FIELDS
    assert set(SEARCH_FIELDS) == contract, (
        "storage_shared.SEARCH_FIELDS 与 Redis 源码里的字面量不同步"
    )
    assert storage_mod.RedisStorage is RedisStorage
    assert keys["数据库迁移方案"]   # corpus 确实建出来了
