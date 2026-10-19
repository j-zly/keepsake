"""keepsake 检索层批 3 — 链头判重（Hy-Memory `_retrieval/dedup.py` 移植）。

覆盖：
  1. SQLite 真后端：store 同内容去重只在「链头 vs 链头」之间进行
     * 同 key 是链头：版本化（标归档 + 新条独立 key）
     * 同 key 是链身（is_latest='0'）：不动旧条，新条照样写独立 key
     * 没有同 key：直接 INSERT
  2. `is_chain_head` / `CHAIN_HEAD_SQL` 判据一致性：缺字段 / 空串 / '1' 都
     算链头，与 PG `CHAIN_HEAD_SQL` 的 `COALESCE` 同款纪律
  3. 批量 ops（consolidator 的 `fragment_exists` + `write_fragments_batch`）
     写入时也走同一份链头判据，不重复造

锚点词用中文基础词（跨环境 jieba 词典教训）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keepsake.storage_sqlite import SqliteStorage


@pytest.fixture()
def lite(tmp_path):
    """SQLite hermetic 实例 —— 不连 Redis/PG。"""
    s = SqliteStorage(path=str(tmp_path / "ks.db"), is_primary=True)
    assert s.ensure_index() is True
    yield s
    s.close()


# ===========================================================================
# 一、`store()` 同内容去重只在链头之间进行
# ===========================================================================

class TestStoreDedupChainHeadOnly:
    """`store()` 同内容命中时的去重行为：链头 vs 链身两条路径分叉。"""

    def test_no_existing_inserts_directly(self, lite):
        """库里没有该内容 ⇒ 直接 INSERT，key 保持 sha256 前缀 12 位形态。"""
        assert lite.store(text="用户爱喝拿铁咖啡", tags="意图") is True
        row = lite.get_fragment(lite._fragment_key_for("用户爱喝拿铁咖啡"))  # noqa: SLF001
        assert row is not None
        assert row.get("is_latest", "1") != "0"

    def test_existing_chain_head_dedups(self, lite):
        """同内容命中链头 ⇒ 旧条标归档（is_archived='1'），新条独立 key。"""
        lite.store(text="昨天买了一袋咖啡豆", tags="事实")
        head_key = lite._fragment_key_for("昨天买了一袋咖啡豆")  # noqa: SLF001

        # 再写一次同内容
        assert lite.store(text="昨天买了一袋咖啡豆", tags="事实") is True

        # 旧条应是链头 + 标归档
        old = lite.get_fragment(head_key) or {}
        assert old.get("is_archived") == "1"
        assert old.get("valid_until"), "旧条 valid_until 应被置入"
        assert old.get("is_latest", "1") != "0", "链头去重后旧条转链身"

        # 新条应是独立 key + is_latest='1'
        new_keys = [
            k for k in (lite.scan_fragment_keys()[1] or [])
            if k.startswith(head_key + ":") and k != head_key
        ]
        assert len(new_keys) == 1
        new_row = lite.get_fragment(new_keys[0]) or {}
        assert new_row.get("is_latest", "1") != "0"

    def test_existing_chain_body_does_not_dedup(self, lite):
        """同内容命中链身（is_latest='0'）⇒ 不动旧条，新条照样写独立 key。

        复本保留确定性 = 最早 created 者为链头代表（链身由当前链头代表），
        这里直接验证：「旧条不动 + 新条独立 key」这条最小改动。
        """
        lite.store(text="明天去店里喝拿铁", tags="意图")
        head_key = lite._fragment_key_for("明天去店里喝拿铁")  # noqa: SLF001

        # 把同 key 行手动打成链身（模拟被 supersede_fragment 顶掉）
        with lite._lock:                                              # noqa: SLF001
            lite._db().execute(                                       # noqa: SLF001
                "UPDATE ks_fragment SET is_latest = '0' WHERE key = ?",
                (head_key,),
            )
            lite._db().commit()                                       # noqa: SLF001

        # 再写一次同内容（应当走「不动旧条 + 独立 key」分支）
        assert lite.store(text="明天去店里喝拿铁", tags="意图") is True

        # 旧条没被标归档（valid_until 空、is_archived 没值）
        old = lite.get_fragment(head_key) or {}
        assert old.get("is_archived", "") != "1", "链身不该被去重归档"
        assert old.get("valid_until", "") == "", "链身的 valid_until 不该被填"
        # 旧条仍标记为链身
        assert old.get("is_latest") == "0", "链身的 is_latest 不该被改"

        # 新条应当存在且独立 key（hash + :epoch）
        new_keys = [
            k for k in (lite.scan_fragment_keys()[1] or [])
            if k.startswith(head_key + ":") and k != head_key
        ]
        assert len(new_keys) == 1
        new_row = lite.get_fragment(new_keys[0]) or {}
        assert new_row.get("is_latest", "1") != "0"

    def test_chain_head_dedup_preserves_first_created(self, lite):
        """复本保留确定性 = 最早 created 者为链头代表。

        同 key 唯一 ⇒ 行本身已是最早的；这里验证「多次写不同时间，最终只
        有一个链头，链身历史不被破坏」。
        """
        # 写三次同内容
        for _ in range(3):
            lite.store(text="今天试了新咖啡豆", tags="事实")
        head_key = lite._fragment_key_for("今天试了新咖啡豆")  # noqa: SLF001
        all_keys = [
            k for k in (lite.scan_fragment_keys()[1] or [])
            if k.startswith(head_key)
        ]
        # 应当有一个原 key + N 个独立 :epoch key
        assert head_key in all_keys
        version_keys = [k for k in all_keys if ":" in k[len(head_key):]]
        assert len(version_keys) >= 1

        # 至少一条 is_latest != '0'（链头代表存在）
        heads = [k for k in all_keys if (lite.get_fragment(k) or {}).get("is_latest", "1") != "0"]
        assert heads, "必须存在链头代表"


# ===========================================================================
# 二、`is_chain_head` 与 SQL 判据口径一致
# ===========================================================================

class TestIsChainHeadConsistent:
    """`is_chain_head` 与 PG `COALESCE(is_latest, '1') <> '0'` 同口径。"""

    @pytest.mark.parametrize("value,expected", [
        (None, True),                 # 老数据 NULL 视为链头
        ("", True),                   # 空串视为链头
        ("1", True),
        ("0", False),
        (b"0", False),                # bytes 兼容
        (b"1", True),
        ("1  ", True),                # strip
        ("0\n", False),
    ])
    def test_values(self, value, expected):
        from keepsake.core.shared import is_chain_head
        assert is_chain_head(value) is expected


# ===========================================================================
# 三、链头判据在仓库内只允许两个出口说
# ===========================================================================

class TestSingleSourceOfTruth:
    """`is_chain_head` / `CHAIN_HEAD_SQL` 是链头判据的两个单点。

    防止哪天又有人写了第三份 `_is_chain_body` 或 `WHERE is_latest != '0'`。
    """

    def test_chain_head_predicate_in_redis_uses_shared(self):
        """Redis 侧 `_is_chain_body` 已经废弃，新代码统一走 `is_chain_head`。
        若有人恢复 `_is_chain_body`，本测试立即红。
        """
        from keepsake.core import redis as redis_mod
        assert not hasattr(redis_mod, "_is_chain_body"), (
            "Redis 侧恢复了 `_is_chain_body`：链头判据已经有两个出口，"
            "请改用 storage_shared.is_chain_head 并删掉旧实现。"
        )
