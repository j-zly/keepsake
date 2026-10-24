"""检索筛选配置上控制台（settings/screening）+ `default_on` 语义。

本文件钉死四件最容易悄悄坏掉的事：

  1. **PUT 只动 screening 一节** —— config.json 里还有存储后端与 LLM 通道，
     「改筛选」顺手重排/丢键会让 provider 读不到 DSN。断言的是**其余键逐字节
     不变**（含键序）。
  2. **越界 400 带人话** —— 控制台提交是机器输入：半截阈值绝不许落盘
     （落在磁盘上的就是下一轮检索真在用的值）。
  3. **写前备份 + 审计** —— 备份写不成 ⇒ 一条都不写；被拒的写也要留痕。
  4. **GET 缺节返回骨架 + `configured: false`** —— 「没配过」与「配成默认值」
     在页面上必须分得开；`key_present` 让「填错路径 ⇒ 静默不筛」当场可见。

另加 **tools 层**（`mcp_server.tools`）的 `default_on` 行为矩阵：
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

GET = "/api/v1/settings/screening"
PUT = "/api/v1/settings/screening"


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
def audit_dir(tmp_path, monkeypatch):
    d = tmp_path / "console_audit"
    monkeypatch.setenv("CONSOLE_AUDIT_DIR", str(d))
    return d


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "console_ops"
    monkeypatch.setenv("CONSOLE_BACKUP_DIR", str(d))
    return d


@pytest.fixture()
def client(monkeypatch, storage, audit_dir, backup_dir, cfg_path):
    monkeypatch.setenv("CONSOLE_TOKEN", TOKEN)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def write_cfg(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_cfg(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_lines(audit_dir) -> List[dict]:
    out: List[dict] = []
    for f in sorted(audit_dir.glob("audit-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def body_of(**over: Any) -> Dict[str, Any]:
    """一份合法的 PUT body（`over` 覆盖某一项）。"""
    payload: Dict[str, Any] = {
        "confirm": True,
        "enabled": True,
        "default_on": True,
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimax.cn/v1",
        "api_key_file": "/tmp/screen_key",
        "max_pick": 10,
        "timeout_s": 90,
        "max_tokens": 4096,
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------- 读端点

def test_GET要token(client):
    assert client.get(GET).status_code == 401


def test_GET缺节返回骨架并标configured_false(client, cfg_path):
    """config.json 里没有 screening 节 ⇒ 回缺省骨架 + configured=false。"""
    write_cfg(cfg_path, {"storage": {"backend": "sqlite"}})
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["configured"] is False
    assert b["enabled"] is False          # 缺省 = 关死
    assert b["default_on"] is False
    # 骨架要**完整**（前端不许写死任何一个数字）
    for key in ("model", "base_url", "api_key_file", "max_pick", "timeout_s",
                "max_tokens"):
        assert b.get(key) not in (None, ""), key
    assert isinstance(b["key_present"], bool)


def test_GET有节时回全量并标configured_true(client, cfg_path):
    write_cfg(cfg_path, {"screening": {"enabled": True, "default_on": True,
                                       "model": "m-x", "base_url": "https://x/v1",
                                       "api_key_file": "/tmp/k", "max_pick": 7,
                                       "timeout_s": 30, "max_tokens": 1024}})
    b = client.get(GET, headers=AUTH).json()
    assert b["configured"] is True
    assert b["enabled"] is True and b["default_on"] is True
    assert b["model"] == "m-x" and b["max_pick"] == 7
    assert b["timeout_s"] == 30 and b["max_tokens"] == 1024


def test_GET配置坏了不500(client, cfg_path):
    """只读面：config 读不出来 ⇒ 按缺省回，绝不掀翻看板。"""
    cfg_path.write_text("{ 这不是 json", encoding="utf-8")
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is False


def test_GET只读不碰config(client, cfg_path):
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200
    assert not cfg_path.exists()


# ---------------------------------------------------------------- PUT 写端点

def test_PUT要token且必须带confirm(client, cfg_path):
    assert client.put(PUT, json=body_of(confirm=False), headers=AUTH).status_code == 400
    assert client.put(PUT, json=body_of(), headers=None).status_code == 401


def test_PUT只动screening节_其余键逐字节不变(client, cfg_path):
    before = {"storage": {"backend": "sqlite", "path": "/tmp/x.db"},
              "retrieval": {"top_k": 8, "exclude_tags": ["core"]},
              "llm": {"base_url": "https://example.invalid", "model": "m"}}
    write_cfg(cfg_path, before)
    r = client.put(PUT, json=body_of(max_pick=5), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    after = read_cfg(cfg_path)
    # 原有键不许换位（screening 是新加的一节，排在末尾）
    assert [k for k in after if k != "screening"] == list(before), \
        "顶层键序不许被改筛选这件事动过"
    assert "screening" in after
    assert after["storage"] == before["storage"]
    assert after["llm"] == before["llm"]
    assert list(after["retrieval"]) == ["top_k", "exclude_tags"]
    assert after["retrieval"] == before["retrieval"]
    # 🔴 其余键**逐字节**不变（不只看解析后的值）
    raw_before = json.dumps(before, ensure_ascii=False, sort_keys=False)
    raw_after = json.dumps({k: v for k, v in after.items() if k != "screening"},
                            ensure_ascii=False, sort_keys=False)
    assert raw_after == raw_before
    assert after["screening"]["max_pick"] == 5
    assert after["screening"]["enabled"] is True
    assert after["screening"]["default_on"] is True


def test_PUT写前备份原config(client, cfg_path, backup_dir):
    before = {"screening": {"enabled": False}}
    write_cfg(cfg_path, before)
    r = client.put(PUT, json=body_of(), headers=AUTH)
    assert r.status_code == 200, r.text
    backups = sorted(backup_dir.rglob("config.screening.*.json"))
    assert backups, "改配置前必须把原 config 整份备份"
    assert json.loads(backups[-1].read_text(encoding="utf-8")) == before


def test_PUT留审计(client, cfg_path, audit_dir):
    write_cfg(cfg_path, {})
    client.put(PUT, json=body_of(), headers=AUTH)
    client.put(PUT, json=body_of(confirm=False), headers=AUTH)   # 被拒
    lines = [ln for ln in audit_lines(audit_dir) if ln["action"] == "settings.screening"]
    assert [ln["ok"] for ln in lines] == [True, False]
    assert any("backup=" in ln["reason"] for ln in lines)


@pytest.mark.parametrize("name,value", [
    ("max_pick", 0), ("max_pick", 31),
    ("timeout_s", 9), ("timeout_s", 301),
    ("max_tokens", 511), ("max_tokens", 16385),
])
def test_PUT越界400带人话(client, cfg_path, name, value):
    write_cfg(cfg_path, {"screening": {"enabled": False}})
    r = client.put(PUT, json=body_of(**{name: value}), headers=AUTH)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "screening_out_of_range"
    assert name in detail["message"] and str(value) in detail["message"]
    # 越界不许落盘
    assert read_cfg(cfg_path)["screening"] == {"enabled": False}


@pytest.mark.parametrize("name,value", [
    ("model", ""), ("base_url", "   "), ("api_key_file", ""),
])
def test_PUT空串400(client, cfg_path, name, value):
    write_cfg(cfg_path, {"screening": {"enabled": False}})
    r = client.put(PUT, json=body_of(**{name: value}), headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "screening_invalid"
    assert read_cfg(cfg_path)["screening"] == {"enabled": False}


def test_PUT允许key文件暂不存在_回包带key_present(client, cfg_path, tmp_path):
    """key 文件可以后补 —— 但「路径没找到」必须**当场可见**（不静默失效）。"""
    missing = tmp_path / "还没有这个key文件"
    write_cfg(cfg_path, {})
    r = client.put(PUT, json=body_of(api_key_file=str(missing)), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["key_present"] is False

    missing.write_text("k", encoding="utf-8")
    b = client.get(GET, headers=AUTH).json()
    assert b["key_present"] is True


def test_PUT改完下一轮就生效(client, cfg_path):
    """热生效走既有 mtime 机制：写完下一轮检索读到的就是新值。"""
    write_cfg(cfg_path, {})
    client.put(PUT, json=body_of(enabled=True, default_on=True), headers=AUTH)
    screening.invalidate_cfg_cache()
    cfg = screening.load_cfg()
    assert cfg["enabled"] is True and cfg["default_on"] is True


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
