"""Redis 后端 —— 归属级联（`reassign_agent`）定向用例。

本仓库**没有**真 Redis 的测试库（Redis 只在生产用），所以这一层走**假 client**：
只实现 `reassign_agent` 用到的那几个命令（`scan` / `pipeline.hget` / `hset`），
键值存 bytes（模拟 `decode_responses=False`，读出来是 bytes —— 解码路径要一起测）。

🔴 三后端语义必须一致：PG / SQLite / Redis 的 `reassign_agent` 都是
「把 `tags` 里的 `agent:<old>` 精确换成 `agent:<new>`，返回搬动行数」。
判据单点在 `storage_shared.replace_agent_tag`（逗号边界精确相等）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from keepsake.storage import RedisStorage


class FakeRedisClient:
    """最小 fake redis —— 只实现 `reassign_agent` 用到的那几个命令。"""

    def __init__(self, hashes: Dict[str, Dict[str, str]]) -> None:
        # 字段名用 str（与 redis-py 一致），**值存 bytes**（decode_responses=False）
        self.hashes: Dict[str, Dict[str, bytes]] = {
            k: {f: v.encode() for f, v in fields.items()} for k, fields in hashes.items()
        }

    def scan(self, cursor: int = 0, match: str = "*", count: int = None) -> Tuple[int, List[bytes]]:
        prefix = str(match).rstrip("*")
        keys = sorted(k for k in self.hashes if k.startswith(prefix))
        return 0, [k.encode() for k in keys]

    def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key: str, field: str = None, value: Any = None,
             mapping: Dict[str, Any] = None) -> int:
        slot = self.hashes.setdefault(key, {})
        if mapping:
            for f, v in mapping.items():
                slot[str(f)] = v.encode() if isinstance(v, str) else v
        if field is not None:
            slot[str(field)] = value.encode() if isinstance(value, str) else value
        return 1

    def pipeline(self):
        client = self

        class _Pipe:
            def __init__(self) -> None:
                self.q: List[Tuple[str, str]] = []

            def hget(self, key: str, field: str) -> "_Pipe":
                self.q.append((key, field))
                return self

            def execute(self) -> List[Any]:
                out = [client.hget(k, f) for k, f in self.q]
                self.q = []
                return out

        return _Pipe()


@pytest.fixture()
def redis_store(monkeypatch):
    """造一个只连假 client 的 RedisStorage（不经真 Redis、不落盘）。"""

    def _make(hashes: Dict[str, Dict[str, str]]) -> Tuple[RedisStorage, FakeRedisClient]:
        client = FakeRedisClient(hashes)
        monkeypatch.setattr(RedisStorage, "_get_client", lambda self: client)
        return RedisStorage.__new__(RedisStorage), client

    return _make


def test_reassign_agent_搬归属标签(redis_store):
    """🔴 归属只有一个落库形态（`tags` 里的 `agent:<id>`），搬它 = 换这个词。"""
    store, client = redis_store({
        "memory:frag:a": {"content": "部署流程要点", "tags": "agent:old-agent"},
        "memory:frag:b": {"content": "别人的记忆", "tags": "agent:other"},
    })
    assert store.reassign_agent("old-agent", "new-agent") == 1
    assert client.hashes["memory:frag:a"]["tags"] == b"agent:new-agent"
    # 没有归属的行一个都不许动
    assert client.hashes["memory:frag:b"]["tags"] == b"agent:other"


def test_reassign_agent_不误伤别的代理(redis_store):
    """🔴 `agent:win` 与 `agent:win-desktop` 是两个代理：改前者不许动后者。"""
    store, client = redis_store({
        "memory:frag:a": {"content": "打包机甲", "tags": "agent:win"},
        "memory:frag:b": {"content": "打包机乙", "tags": "agent:win-desktop"},
    })
    assert store.reassign_agent("win", "win-new") == 1
    assert client.hashes["memory:frag:a"]["tags"] == b"agent:win-new"
    assert client.hashes["memory:frag:b"]["tags"] == b"agent:win-desktop"


def test_reassign_agent_历史行也搬(redis_store):
    """🔴 consumed / superseded 的历史行**一起搬**（归属是事实，不是活跃状态）。"""
    store, client = redis_store({
        "memory:frag:a": {"content": "被合并吞掉的记忆",
                          "tags": "agent:old-agent", "fragment_type": "consumed"},
    })
    assert store.reassign_agent("old-agent", "new-agent") == 1
    assert client.hashes["memory:frag:a"]["tags"] == b"agent:new-agent"
    # 历史属性不动，只搬归属
    assert client.hashes["memory:frag:a"]["fragment_type"] == b"consumed"


def test_reassign_agent_没有归属或同串时搬不动(redis_store):
    store, client = redis_store({
        "memory:frag:a": {"content": "没有归属标签", "tags": "shared"},
    })
    assert store.reassign_agent("old-agent", "new-agent") == 0
    assert store.reassign_agent("same-id", "same-id") == 0
    assert client.hashes["memory:frag:a"]["tags"] == b"shared"
