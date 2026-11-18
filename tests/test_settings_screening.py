"""检索筛选（screening）的 `default_on` 语义 + tools 层行为矩阵。

🔴 **2026-11 ks_contract_zombie_retire**：控制台那两条旧端点
（`GET/PUT /api/v1/settings/screening`）已退役 —— screening 的读写现由
`/api/v1/settings/ai/screening`（AI 配置页那一节）承担，读写两面的完整断言
迁到 `tests/test_settings_ai.py`。本文件保留**与端点无关**的那部分：
`default_on` 语义（tools 层）与「旧端点确实已退役」这条回归闸。

本文件钉死两件事：

  1. **旧端点已退役** —— 那两条路径不许再冒出来（迭代了就不留旧的）；
     筛选配置的读写现由 AI 配置页那一节承担。
  2. **tools 层 `default_on` 行为矩阵**（`mcp_server.tools`）：
     未传 screen ⇒ 看 default_on；显式传 false ⇒ 照关（调用方意志优先）；
     `enabled=false` ⇒ default_on 无效。

🔴 **配置隔离**：`KEEPSAKE_CONFIG` 一律指到 `tmp_path` 下的临时文件
（不指文件就会回落到 HOME 里那份真配置，测试会悄悄读到并被 PUT 写坏）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.adapters.hermes import provider as provider_module
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core import screening
from keepsake.core.sqlite import SqliteStorage
from keepsake.mcp_server import tools as mcp_tools

TOKEN = "screening-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 模块级别名：假 LLM 的 `post(url, json=...)` 形参把 `json` 遮住了。
json_dumps = json.dumps

#: 🔴 已退役的两条旧端点（读写两面现由 `/api/v1/settings/ai/screening` 承担）。
RETIRED_GET = "/api/v1/settings/screening"
RETIRED_PUT = "/api/v1/settings/screening"


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def cfg_path(tmp_path, monkeypatch) -> Path:
    """**指文件**的临时 config 路径（不指文件就会回落到 HOME 里那份真配置）。"""
    p = tmp_path / "config.json"
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(p))
    provider_module._CORE_BUDGET_CACHE.clear()
    screening.invalidate_cfg_cache()
    return p


@pytest.fixture()
def storage(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "screening.db"), embedder=None, embed_dim=8,
                      is_primary=True)
    assert s.ensure_index() is not False
    return s


@pytest.fixture()
def client(monkeypatch, storage, cfg_path):
    """退役闸要一个真客户端（看旧端点确实不在路由表里）。"""
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def write_cfg(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 退役闸

def test_旧端点已退役_路由表里没有(client):
    """🔴 迭代了就不留旧的：旧读面/写面不该再冒出来（读写两面归 AI 配置页）。

    判据看**路由表**（不是打 404）：控制台根上挂了静态文件 Mount，
    没挂路由的路径会落到 Mount 上，回 405 而不是 404 —— 路由表才是准的。
    """
    from conftest import flat_routes

    app = client.app
    paths = {getattr(r, "path", "") for r in flat_routes(app)}
    assert RETIRED_GET not in paths
    assert RETIRED_PUT not in paths
    # 新家仍在（screening 的读写归 AI 配置页那一节）
    assert "/api/v1/settings/ai/{section_id}" in paths


# ---------------------------------------------------------------- tools 层

class _FakeProvider:
    """最小 provider：只实现 memory_search 用到的那几个原语。"""

    def __init__(self, hits: List[Dict[str, Any]]) -> None:
        self._hits = hits

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        return [dict(h) for h in self._hits[:limit]]

    def capabilities(self) -> Any:
        raise NotImplementedError


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """假 LLM 通道：记下调用，回一个 `{"keep": [...]}`。"""

    def __init__(self, keep: List[int]) -> None:
        self._keep = keep
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
        # 参数名照 httpx 的 `json=`（调用点就是按关键字传的）；模块里的 json
        # 在这个作用域被遮住 ⇒ 用模块级别名拼回包。
        self.calls.append({"url": url, "body": json})
        return _FakeResponse({"choices": [{"message": {
            "role": "assistant",
            "content": json_dumps({"keep": self._keep}),
        }}]})


def _hits(n: int) -> List[Dict[str, Any]]:
    return [{"key": f"frag:{i}", "content": f"正文 {i}", "tags": "",
             "_combined_score": 1.0 - i * 0.01} for i in range(1, n + 1)]


def _write_screening_cfg(cfg_path: Path, key_file: Path, **over: Any) -> None:
    section = {"enabled": True, "default_on": False,
               "model": "MiniMax-M3.1-Flash",
               "base_url": "https://api.minimax.cn/v1",
               "api_key_file": str(key_file),
               "max_pick": 10, "timeout_s": 90, "max_tokens": 4096}
    section.update(over)
    write_cfg(cfg_path, {"screening": section})
    screening.invalidate_cfg_cache()


def test_default_on开且未传screen_筛选发生(monkeypatch, cfg_path, tmp_path):
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, default_on=True)
    client = _FakeClient([1, 3])
    monkeypatch.setattr(screening, "get_client",
                        lambda base_url, timeout: client)
    out = mcp_tools.memory_search(_FakeProvider(_hits(12)), "问题", 12)
    assert out["screened"] == {"picked": 2, "of": 12}
    assert [r["key"] for r in out["results"]] == ["frag:1", "frag:3"]
    assert client.calls, "default_on 生效时必须真的筛"


def test_default_on开但显式screen_false_不筛(monkeypatch, cfg_path, tmp_path):
    """🔴 调用方意志优先：显式传 false 一律不筛（default_on 不是后门）。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, default_on=True)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("显式 screen=false 时不该发请求")

    monkeypatch.setattr(screening, "get_client", boom)
    out = mcp_tools.memory_search(_FakeProvider(_hits(12)), "问题", 12, screen=False)
    assert "screened" not in out
    assert out["count"] == 12


def test_default_on开但enabled关_不筛(monkeypatch, cfg_path, tmp_path):
    """总开关关着 ⇒ default_on 无效（default_on 不是绕过总开关的后门）。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, enabled=False, default_on=True)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("总开关关着时不该发请求")

    monkeypatch.setattr(screening, "get_client", boom)
    out = mcp_tools.memory_search(_FakeProvider(_hits(12)), "问题", 12)
    assert "screened" not in out
    assert out["count"] == 12


def test_default_on关且未传screen_不筛(monkeypatch, cfg_path, tmp_path):
    """默认关死：没配 default_on 时行为与接入前逐字一致。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, default_on=False)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("default_on 关着时不该发请求")

    monkeypatch.setattr(screening, "get_client", boom)
    out = mcp_tools.memory_search(_FakeProvider(_hits(12)), "问题", 12)
    assert "screened" not in out
    assert out["count"] == 12


def test_explicit_screen_true照筛(monkeypatch, cfg_path, tmp_path):
    """显式传 true 一律筛（与 default_on 无关）。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, default_on=False)
    client = _FakeClient([2])
    monkeypatch.setattr(screening, "get_client",
                        lambda base_url, timeout: client)
    out = mcp_tools.memory_search(_FakeProvider(_hits(12)), "问题", 12, screen=True)
    assert out["screened"] == {"picked": 1, "of": 12}
    assert client.calls


def test_kb_search也吃default_on(monkeypatch, cfg_path, tmp_path):
    """kb_search 与 memory_search 同一套哨兵语义。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    _write_screening_cfg(cfg_path, key_file, default_on=True)
    items = [{"doc_id": str(i), "title": f"标题 {i}", "snippet": f"摘要 {i}",
              "chunk_id": f"chunk:{i}"} for i in range(1, 13)]

    def fake_search(*, query: str, limit: int = 10, agent_scope: Any = None) -> Dict[str, Any]:
        return {"mode": "ilike", "items": items[:limit]}

    monkeypatch.setattr(mcp_tools.kb_service, "search_documents", fake_search)
    client = _FakeClient([2, 5])
    monkeypatch.setattr(screening, "get_client",
                        lambda base_url, timeout: client)
    out = mcp_tools.kb_search("问题", 12, registry=object())
    assert out["screened"] == {"picked": 2, "of": 12}
    assert [r["id"] for r in out["results"]] == ["2", "5"]
