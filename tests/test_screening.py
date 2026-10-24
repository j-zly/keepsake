"""检索结果 LLM 筛选（recall screening）—— 语义钉死 + MCP 面回归闸。

**不连真网**：LLM 通道用假 client 顶掉（`monkeypatch` 换 `core.screening.get_client`），
只验语义（哪几条被选、失败时怎么降级），不验厂商实现。

本文件钉的判据（逐条对应任务书）：

  ① **默认关死**：没有 `screening` 节 ⇒ `enabled=False`；
     `pick_relevant` 直接 `([], "skipped:disabled")`，一次网络都不发；
  ② **enabled + 假 LLM**：选中子集，**保序**返回，响应带 `screened.picked/of`；
  ③ **四类失败各可辨识**：坏 JSON / 429 / 超时 / 无 key ⇒ 原因文案互不相同，
     且**原结果原样放行**（fail-open：筛选坏不许让检索瘫掉）；
  ④ **越界 id 丢弃**：编号越界 / 重复 / 空数组 ⇒ 整份回答作废（`bad_answer`），
     原结果照返（不许因为模型编号乱了就静默丢掉可能有用的一条）；
  ⑤ **max_pick 生效**：既是「结果数 ≤ max_pick 就不筛」的门槛，也是选中条数上限；
  ⑥ **回归闸**：`memory_search(screen=False)` 的返回与接入前**逐字一致**
     （默认 false ⇒ 零行为变化，是本单第一红线）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from keepsake.core import screening
from keepsake.mcp_server import tools as mcp_tools


# ---------------------------------------------------------------------------
# 假 LLM 通道
# ---------------------------------------------------------------------------

class _FakeResponse:
    """httpx.Response 的最小替身。"""

    def __init__(self, payload: Any = None, status_code: int = 200,
                 broken: bool = False) -> None:
        self._payload = payload
        self.status_code = status_code
        self._broken = broken

    def json(self) -> Any:
        if self._broken:
            raise ValueError("not json")
        return self._payload


def _chat_payload(keep: Any) -> Dict[str, Any]:
    """OpenAI 兼容 chat 回包的最小形状。"""
    content = keep if isinstance(keep, str) else json.dumps({"keep": keep})
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeClient:
    """假 client：记录请求，按预设回包（或抛异常）。"""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
        self.calls.append({"url": url, "body": json, "headers": headers})
        return self._responder(json)


def _install_fake(monkeypatch: Any, responder: Any) -> _FakeClient:
    client = _FakeClient(responder)
    monkeypatch.setattr(screening, "get_client",
                        lambda base_url, timeout: client)
    return client


def _cfg(tmp_path: Path, **over: Any) -> Dict[str, Any]:
    """一份「开着」的筛选配置（key 文件落在 tmp_path 里）。"""
    key_file = tmp_path / "screen_key"
    key_file.write_text("fake-key", encoding="utf-8")
    cfg = {
        "enabled": True,
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimax.cn/v1",
        "api_key_file": str(key_file),
        "max_pick": 10,
        "timeout_s": 90,
        "max_tokens": 4096,
    }
    cfg.update(over)
    return cfg


def _candidates(n: int) -> List[Dict[str, str]]:
    return [{"content": f"候选 {i} 的正文：跟问题相关的内容"} for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# ① 默认关死
# ---------------------------------------------------------------------------

def test_没有screening节_等于显式关闭():
    cfg = screening.resolve_screening({})
    assert cfg["enabled"] is False
    assert screening.screen_enabled(cfg) is False


def test_缺省值与任务书给定一致():
    cfg = screening.resolve_screening({})
    assert cfg["model"] == "MiniMax-M3.1-Flash"
    assert cfg["base_url"] == "https://api.minimax.cn/v1"
    assert cfg["max_pick"] == 10
    assert cfg["timeout_s"] == 90
    # 🔴 4096 是实测教训值（M3.1-Flash 自适应思考吃预算）
    assert cfg["max_tokens"] == 4096


def test_非法值回落且enabled回落到关(tmp_path):
    cfg = screening.resolve_screening({"screening": {"enabled": "也许", "max_pick": 9999}})
    assert cfg["enabled"] is False          # 看不懂的值不许猜成「开」
    assert cfg["max_pick"] == 10            # 越界回落缺省


def test_关着时一次网络都不发(monkeypatch, tmp_path):
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("关着就不该发请求")

    monkeypatch.setattr(screening, "get_client", boom)
    ids, note = screening.pick_relevant("问题", _candidates(3),
                                        screening.resolve_screening({}))
    assert ids == []
    assert note == "skipped:disabled"


# ---------------------------------------------------------------------------
# ② enabled + 假 LLM
# ---------------------------------------------------------------------------

def test_选中子集且编号从1起(monkeypatch, tmp_path):
    client = _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([1, 3])))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == [1, 3]
    assert note == "ok"
    # 请求体钉死：temperature=0、max_tokens 取配置、模型取配置
    body = client.calls[0]["body"]
    assert body["temperature"] == 0
    assert body["max_tokens"] == 4096
    assert body["model"] == "MiniMax-M3.1-Flash"
    # 提问里带上了编号与正文
    prompt = body["messages"][-1]["content"]
    assert "[1]" in prompt and "[3]" in prompt


def test_提问按200字预览截断(monkeypatch, tmp_path):
    client = _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([1])))
    long_cands = [{"content": "字" * 500}]
    screening.pick_relevant("问题", long_cands, _cfg(tmp_path))
    prompt = client.calls[0]["body"]["messages"][-1]["content"]
    assert "字" * 200 in prompt
    assert "字" * 201 not in prompt


def test_正文带代码围栏也认(monkeypatch, tmp_path):
    _install_fake(monkeypatch,
                  lambda body: _FakeResponse(_chat_payload("```json\n{\"keep\": [2]}\n```")))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == [2]
    assert note == "ok"


# ---------------------------------------------------------------------------
# ③ 四类失败各可辨识 + 原结果放行
# ---------------------------------------------------------------------------

def test_坏JSON_降级bad_json(monkeypatch, tmp_path):
    _install_fake(monkeypatch, lambda body: _FakeResponse(broken=True))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == []
    assert note == "skipped:bad_json"


def test_429_降级rate_limited(monkeypatch, tmp_path):
    _install_fake(monkeypatch,
                  lambda body: _FakeResponse({"error": "rate limited"}, status_code=429))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == []
    assert note == "skipped:rate_limited"


def test_超时_降级timeout(monkeypatch, tmp_path):
    def boom(body: Any) -> Any:
        raise httpx.ReadTimeout("read timeout")

    _install_fake(monkeypatch, boom)
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == []
    assert note == "skipped:timeout"


def test_无key_降级no_key(monkeypatch, tmp_path):
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("没有 key 不该发请求")

    monkeypatch.setattr(screening, "get_client", boom)
    cfg = _cfg(tmp_path, api_key_file=str(tmp_path / "不存在的key文件"))
    ids, note = screening.pick_relevant("问题", _candidates(3), cfg)
    assert ids == []
    assert note == "skipped:no_key"


def test_四类失败原因互不相同(monkeypatch, tmp_path):
    notes = set()
    _install_fake(monkeypatch, lambda body: _FakeResponse(broken=True))
    notes.add(screening.pick_relevant("q", _candidates(3), _cfg(tmp_path))[1])
    _install_fake(monkeypatch,
                  lambda body: _FakeResponse({}, status_code=429))
    notes.add(screening.pick_relevant("q", _candidates(3), _cfg(tmp_path))[1])
    _install_fake(monkeypatch, _raise_timeout)
    notes.add(screening.pick_relevant("q", _candidates(3), _cfg(tmp_path))[1])
    notes.add(screening.pick_relevant("q", _candidates(3),
                                       _cfg(tmp_path, api_key_file="/nope/key"))[1])
    assert len(notes) == 4


def _raise_timeout(body: Any) -> Any:
    raise httpx.ReadTimeout("read timeout")


# ---------------------------------------------------------------------------
# ④ 越界 / 重复 / 空数组 ⇒ 整份回答作废
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keep", [[99], [0], [1, 1], [], [1, 2, 99]])
def test_坏答案一律bad_answer(monkeypatch, tmp_path, keep):
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload(keep)))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == []
    assert note == "skipped:bad_answer"


@pytest.mark.parametrize("content", ['{"foo": 1}', '{"keep": "3"}', '{"keep": ["a"]}'])
def test_JSON合法但形状不对_也是bad_answer(monkeypatch, tmp_path, content):
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload(content)))
    ids, note = screening.pick_relevant("问题", _candidates(3), _cfg(tmp_path))
    assert ids == []
    assert note == "skipped:bad_answer"


# ---------------------------------------------------------------------------
# ⑤ max_pick
# ---------------------------------------------------------------------------

def test_max_pick是选中条数上限(monkeypatch, tmp_path):
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload(list(range(1, 13)))))
    ids, note = screening.pick_relevant("问题", _candidates(12), _cfg(tmp_path, max_pick=10))
    assert note == "ok"
    assert len(ids) == 10
    assert ids == list(range(1, 11))       # 保序取前 N


def test_配置值离谱也不抛穿(monkeypatch, tmp_path):
    """🔴 绝不抛穿：配置坏了 / 候选形状怪了，也只降级不许炸检索。"""
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([1])))
    cfg = _cfg(tmp_path, max_tokens="不是数字", timeout_s="也不是数字")
    ids, note = screening.pick_relevant("问题", [{"content": "正文"}, "不是dict"], cfg)
    assert note == "ok" and ids == [1]


# ---------------------------------------------------------------------------
# MCP 面接线（memory_search / kb_search）
# ---------------------------------------------------------------------------

class _FakeProvider:
    """最小 provider：只实现 memory_search 用到的那几个原语。"""

    def __init__(self, hits: List[Dict[str, Any]]) -> None:
        self._hits = hits

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        return [dict(h) for h in self._hits[:limit]]

    def capabilities(self) -> Any:
        raise NotImplementedError


def _hits(n: int) -> List[Dict[str, Any]]:
    return [{"key": f"frag:{i}", "content": f"正文 {i}", "tags": "",
             "_combined_score": 1.0 - i * 0.01} for i in range(1, n + 1)]


def _use_cfg(monkeypatch: Any, cfg: Dict[str, Any]) -> None:
    monkeypatch.setattr(screening, "load_cfg", lambda: cfg)


def test_memory_search默认不筛_返回逐字不变(monkeypatch):
    """🔴 回归闸：默认 false ⇒ 返回与接入前逐字一致（连 `screened` 键都没有）。"""
    provider = _FakeProvider(_hits(3))
    out = mcp_tools.memory_search(provider, "问题")
    assert "screened" not in out
    assert out["count"] == 3
    assert [r["key"] for r in out["results"]] == ["frag:1", "frag:2", "frag:3"]
    assert out["results"][0]["content"] == "正文 1"


def test_memory_search_screen_选中子集且保序(monkeypatch, tmp_path):
    provider = _FakeProvider(_hits(12))
    _use_cfg(monkeypatch, _cfg(tmp_path, max_pick=10))
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([3, 1])))
    out = mcp_tools.memory_search(provider, "问题", 12, screen=True)
    # 保序（按原结果顺序，不按模型给的顺序）
    assert [r["key"] for r in out["results"]] == ["frag:1", "frag:3"]
    assert out["count"] == 2
    assert out["screened"] == {"picked": 2, "of": 12}


def test_memory_search_screen_关着时原样返回(monkeypatch, tmp_path):
    provider = _FakeProvider(_hits(12))
    _use_cfg(monkeypatch, screening.resolve_screening({}))
    out = mcp_tools.memory_search(provider, "问题", 12, screen=True)
    assert out["count"] == 12
    assert out["screened"] == {"skipped": "disabled"}


def test_memory_search_screen_失败时原样返回(monkeypatch, tmp_path):
    provider = _FakeProvider(_hits(12))
    _use_cfg(monkeypatch, _cfg(tmp_path))
    _install_fake(monkeypatch, lambda body: _FakeResponse(broken=True))
    out = mcp_tools.memory_search(provider, "问题", 12, screen=True)
    assert out["count"] == 12
    assert len(out["results"]) == 12
    assert out["screened"] == {"skipped": "bad_json"}


def test_memory_search_screen_结果数不超过max_pick就不筛(monkeypatch, tmp_path):
    provider = _FakeProvider(_hits(3))

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("结果数不够多时不该调 LLM")

    monkeypatch.setattr(screening, "get_client", boom)
    _use_cfg(monkeypatch, _cfg(tmp_path, max_pick=10))
    out = mcp_tools.memory_search(provider, "问题", 12, screen=True)
    assert out["count"] == 3
    assert out["screened"] == {"skipped": "not_needed"}


def test_kb_search_screen_语义同款(monkeypatch, tmp_path):
    """kb_search：候选正文取摘要（snippet），筛成功保序过滤。"""
    items = [
        {"doc_id": str(i), "title": f"标题 {i}", "snippet": f"摘要 {i}",
         "chunk_id": f"chunk:{i}"}
        for i in range(1, 13)
    ]

    def fake_search(*, query: str, limit: int = 10, agent_scope: Any = None) -> Dict[str, Any]:
        return {"mode": "ilike", "items": items[:limit]}

    monkeypatch.setattr(mcp_tools.kb_service, "search_documents", fake_search)
    _use_cfg(monkeypatch, _cfg(tmp_path, max_pick=10))
    client = _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([2, 5])))
    out = mcp_tools.kb_search("问题", 12, registry=object(), screen=True)
    assert [r["id"] for r in out["results"]] == ["2", "5"]
    assert out["count"] == 2
    assert out["screened"] == {"picked": 2, "of": 12}
    # 送进模型的是摘要正文
    prompt = client.calls[0]["body"]["messages"][-1]["content"]
    assert "摘要 1" in prompt


def test_kb_search_screen_关着时原样返回(monkeypatch, tmp_path):
    items = [{"doc_id": str(i), "title": f"标题 {i}", "snippet": f"摘要 {i}",
              "chunk_id": f"chunk:{i}"} for i in range(1, 13)]

    def fake_search(*, query: str, limit: int = 10, agent_scope: Any = None) -> Dict[str, Any]:
        return {"mode": "ilike", "items": items[:limit]}

    monkeypatch.setattr(mcp_tools.kb_service, "search_documents", fake_search)
    _use_cfg(monkeypatch, screening.resolve_screening({}))
    out = mcp_tools.kb_search("问题", 12, registry=object())
    assert "screened" not in out
    assert out["count"] == 12


# ---------------------------------------------------------------------------
# 配置热读
# ---------------------------------------------------------------------------

def test_配置热读按mtime生效(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"screening": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(cfg_file))
    screening.invalidate_cfg_cache()
    assert screening.load_cfg()["enabled"] is False

    cfg_file.write_text(json.dumps({"screening": {"enabled": True}}), encoding="utf-8")
    screening.invalidate_cfg_cache()
    assert screening.load_cfg()["enabled"] is True


def test_配置文件缺失_按关死处理(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(tmp_path / "不存在.json"))
    screening.invalidate_cfg_cache()
    assert screening.load_cfg()["enabled"] is False


# ---------------------------------------------------------------------------
# MCP 协议面：schema 里有 screen（默认 false）+ 端到端跑通
# ---------------------------------------------------------------------------

def test_两个工具的schema都带screen且默认关闭():
    from keepsake.mcp_server.server import build_server

    server = build_server(_FakeProvider([]))
    schemas = {t.name: t.inputSchema for t in server.list_tools()}
    for name in ("memory_search", "kb_search"):
        prop = schemas[name]["properties"]["screen"]
        assert prop["type"] == "boolean"
        assert prop["default"] is False


def test_MCP端到端_screen参数一路传到工具(monkeypatch, tmp_path):
    import functools

    import anyio

    from keepsake.mcp_server.server import build_server

    server = build_server(_FakeProvider(_hits(12)))
    _use_cfg(monkeypatch, _cfg(tmp_path, max_pick=10))
    _install_fake(monkeypatch, lambda body: _FakeResponse(_chat_payload([2])))
    _, structured = anyio.run(functools.partial(
        server.call_tool, "memory_search",
        {"query": "问题", "limit": 12, "screen": True}))
    out = structured["result"]
    assert [r["key"] for r in out["results"]] == ["frag:2"]
    assert out["screened"] == {"picked": 1, "of": 12}

