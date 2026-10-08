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

import os
import re
import uuid
from pathlib import Path

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