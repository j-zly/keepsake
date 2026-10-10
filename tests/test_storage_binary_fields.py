"""带二进制字段（embed_bin）的碎片 hash 也能被批量读出 —— 红绿闭环。

🔴 2026-10-10 真 Redis（180）实测 bug：`get_fragments_batch` 对每个字段一律
`v.decode("utf-8")`，遇到 `embed_bin`（float32 blob）抛 UnicodeDecodeError →
被 `except Exception` + `logger.debug` 吞掉 → 返回空 dict → `Consolidator.
_scan_unconsolidated` 恒 0 条 → 合并/遗忘在 Redis 上静默失效。

**本文件是 hermetic 的**（不连 Redis）：用最小 fake client，但值**原样存 bytes**。
tests/test_v2_search.py 里同款的 fake 走 `str(v).encode()`，天然测不到二进制 ——
这正是上一轮测试没抓到这个 bug 的原因。
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Tuple

import pytest

from keepsake.consolidator import Consolidator
from keepsake.forgetter import Forgetter
from keepsake.storage import RedisStorage


# --------------------------------------------------------------------------
# 最小 fake redis client：值原样存 bytes（不做 str 转换）
# --------------------------------------------------------------------------

class _BytesHash:
    def __init__(self):
        self.data: Dict[bytes, Any] = {}

    def hgetall(self) -> Dict[bytes, Any]:
        return dict(self.data)


class _Pipe:
    def __init__(self, store):
        self.store = store
        self.cmds: List[Tuple[str, ...]] = []

    def hgetall(self, key):
        self.cmds.append(("hgetall", key))

    def hset(self, key, *args, **kwargs):
        self.cmds.append(("hset", key))
        h = self.store.hashes.setdefault(key, _BytesHash())
        if "mapping" in kwargs:
            for k, v in kwargs["mapping"].items():
                h.data[k.encode() if isinstance(k, str) else k] = v
        elif len(args) == 2:
            f, v = args
            h.data[f.encode() if isinstance(f, str) else f] = v

    def hincrby(self, key, field, n):
        h = self.store.hashes.setdefault(key, _BytesHash())
        cur = int(h.data.get(field.encode(), b"0"))
        h.data[field.encode()] = str(cur + n).encode()

    def execute(self):
        res = []
        for c in self.cmds:
            if c[0] == "hgetall":
                h = self.store.hashes.get(c[1])
                res.append(h.hgetall() if h else {})
            else:
                res.append(1)
        self.cmds.clear()
        return res


class _Client:
    def __init__(self):
        self.hashes: Dict[str, _BytesHash] = {}
        self.keys: List[str] = []
        self._cursors: Dict[int, str] = {}   # 整数游标 → 上一页最后一个 key

    def ping(self):
        return True

    def hgetall(self, key):
        h = self.hashes.get(key)
        return h.hgetall() if h else {}

    def hget(self, key, field):
        h = self.hashes.get(key)
        return None if h is None else h.data.get(
            field.encode() if isinstance(field, str) else field)

    def hset(self, key, *args, **kwargs):
        h = self.hashes.setdefault(key, _BytesHash())
        if "mapping" in kwargs:
            for k, v in kwargs["mapping"].items():
                h.data[k.encode() if isinstance(k, str) else k] = v
        elif len(args) == 2:
            f, v = args
            h.data[f.encode() if isinstance(f, str) else f] = v
        return 1

    def exists(self, key):
        return 1 if key in self.hashes else 0

    def delete(self, *keys):
        n = sum(1 for k in keys if self.hashes.pop(k, None) is not None)
        self.keys = [k for k in self.keys if k not in set(keys)]
        return n

    def pipeline(self):
        return _Pipe(self)

    def scan(self, cursor=0, match="*", count=100):
        """按 `prefix*` 扫；cursor = 上一页最后一个 key 的数字后缀（0 = 扫完）。

        语义对齐 `RedisStorage.scan_fragment_keys`：返回 (next_cursor, keys)，
        游标非 0 表示还有下一页。必须真正按游标前进，否则分页会死循环。
        """
        prefix = match[:-1] if match.endswith("*") else match
        keys = sorted(k for k in self.keys if k.startswith(prefix))
        start = 0
        if cursor:
            after = self._cursors[int(cursor)]      # 游标 → 上一页最后一个 key
            start = next((i for i, k in enumerate(keys) if k > after), len(keys))
        page = keys[start:start + count]
        if start + count >= len(keys) or not page:
            return 0, [k.encode() for k in page]
        nxt = int(page[-1].rsplit(":", 1)[1])
        self._cursors[nxt] = page[-1]
        return nxt, [k.encode() for k in page]


class _Pool:
    def disconnect(self):
        pass


def _vec_blob(dim: int = 6) -> bytes:
    """模拟 `RedisStorage._text_to_blob` 的输出：float32 向量 blob。"""
    return struct.pack(f"{dim}f", *[0.1 * (i + 1) for i in range(dim)])


def _add_fragment(client: _Client, key: str, content: str, with_embed: bool = True,
                  source: bytes = b"hermes_agent"):
    h = _BytesHash()
    h.data = {
        b"content": content.encode(),
        b"created": "2020-01-01T00:00:00+00:00",  # 老碎片，保证不被 max_age 过滤
        b"source": source,
        b"fragment_type": b"",
        b"level": b"1",
        b"sentiment_score": b"0.1",
        b"sentiment_label": b"neutral",
        b"feedback_score": b"0",
    }
    if with_embed:
        h.data[b"embed_bin"] = _vec_blob()
    client.hashes[key] = h
    client.keys.append(key)
    return key


def _make_storage(n: int, with_embed: bool = True,
                  source: bytes = b"hermes_agent") -> Tuple[RedisStorage, _Client]:
    client = _Client()
    # 三条内容关键词高度重叠 → 能聚成一组（min_group_size=3 / min_overlap 走默认）
    for i in range(n):
        _add_fragment(
            client, f"memory:frag:{i:04d}",
            f"用户偏好用 Python 写数据处理脚本并且喜欢用 Redis 做缓存第{i}条",
            with_embed=with_embed, source=source,
        )
    s = RedisStorage(host="127.0.0.1", port=6379)
    s._client = client
    s._pool = _Pool()
    return s, client


# ==========================================================================
# C1 — 批量读必须拿到与「有效 key 数」一致的条数
# ==========================================================================

class TestBatchReadWithBinaryFields:
    def test_batch_read_returns_all_valid_keys(self):
        """带 embed_bin 的 hash 也必须全部读出（修前返回空 dict）。"""
        s, client = _make_storage(3, with_embed=True)
        keys = sorted(client.hashes)
        out = s.get_fragments_batch(keys)
        assert len(out) == len(keys), (
            f"批量读丢数据：传入 {len(keys)} 个有效 key，只回来 {len(out)} 个")
        for k in keys:
            assert k in out

    def test_binary_field_kept_as_bytes(self):
        """二进制字段保留 bytes 原样（不被毁成乱码 str，也不丢）。"""
        s, client = _make_storage(1, with_embed=True)
        key = sorted(client.hashes)[0]
        doc = s.get_fragments_batch([key])[key]
        assert doc["content"].startswith("用户偏好")
        assert isinstance(doc["embed_bin"], bytes)
        assert doc["embed_bin"] == _vec_blob()

    def test_text_only_fragments_unchanged(self):
        """对照组：无二进制字段时返回值与改动前逐字相同（全是 str）。"""
        s, client = _make_storage(2, with_embed=False)
        keys = sorted(client.hashes)
        out = s.get_fragments_batch(keys)
        assert len(out) == 2
        for doc in out.values():
            assert all(isinstance(v, str) for v in doc.values())
            assert "embed_bin" not in doc

    def test_get_fragment_single_with_binary(self):
        """get_fragment 同型 bug（修前对带 embed_bin 的 key 返回 None）。"""
        s, client = _make_storage(1, with_embed=True)
        key = sorted(client.hashes)[0]
        frag = s.get_fragment(key)
        assert frag is not None
        assert frag["content"].startswith("用户偏好")
        assert frag["embed_bin"] == _vec_blob()

    def test_roundtrip_write_does_not_corrupt_binary(self):
        """读→写回环：bytes 原样落回 Redis，不变成 "b'\\xcd...'"。"""
        s, client = _make_storage(2, with_embed=True)
        keys = sorted(client.hashes)
        docs = s.get_fragments_batch(keys)
        s.write_fragments_batch([dict(docs[k], key=k) for k in keys])
        raw = client.hgetall(keys[0])[b"embed_bin"]
        assert raw == _vec_blob(), f"写回把向量毁成 {raw!r}"

    def test_mixed_binary_and_missing_keys(self):
        """有效 key / 缺失 key 混合时，返回条数 = 有效 key 数（不是 0）。"""
        s, client = _make_storage(2, with_embed=True)
        keys = sorted(client.hashes)
        out = s.get_fragments_batch(["memory:frag:不存在"] + keys)
        assert len(out) == 2
        assert "memory:frag:不存在" not in out


# ==========================================================================
# C2 — 端到端：合并 / 遗忘扫描必须能扫到
# ==========================================================================

class TestConsolidatorScanWithBinaryFields:
    def test_scan_unconsolidated_count_matches_written(self):
        """_scan_unconsolidated 条数 = 写入条数（修前恒 0）。"""
        s, client = _make_storage(4, with_embed=True)
        con = Consolidator(s, batch_size=2)   # 强制多页
        frags = con._scan_unconsolidated()
        assert len(frags) == 4

    def test_consolidate_dry_run_scanned_at_least_one(self):
        """合并 dry-run 的 scanned ≥ 1（修前恒 0）。"""
        s, client = _make_storage(3, with_embed=True)
        con = Consolidator(s, batch_size=100)
        stats = con.consolidate(dry_run=True)
        assert stats["scanned"] == 3
        assert stats["groups_found"] >= 1

    def test_forgetter_dry_run_finds_candidates(self):
        """遗忘 dry-run 的 candidates 不再恒 0。

        source 用 "cron" 而非 "hermes_agent"：后者被 forgetter 保护规则 2
        （用户手动存的 memory 不删）挡住，那是**正确行为**，与本 bug 无关。
        """
        s, client = _make_storage(3, with_embed=True, source=b"cron")
        fg = Forgetter(s, batch_size=100, dry_run=True)
        stats = fg.forget()
        assert stats["scanned"] == 3
        assert stats["candidates"] == 3
        assert stats["deleted"] == 0        # dry-run 不删


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
