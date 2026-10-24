"""「AI 配置」页后端（`/api/v1/settings/ai`）—— 五节集中配置 + 连通检测。

本文件钉死六件最容易悄悄坏掉的事：

  1. **GET 回五节**（llm / embedder / screening / query_expansion / tiering_review），
     字段与数值**全部来自 config.json**（前端零写死）。
  2. **凭据真值永不下发**：secret 字段只回 `{"set": …}`（路径型另带 `path`），
     响应体里**搜不到** key 原文。
  3. **PUT 只动目标节** —— 其余节**逐字节**不变（含键序）：config.json 里还有
     存储后端、班车站等配置，「改 AI」顺手重排/丢键就能让 provider 读不到 DSN。
  4. **越界 400 带人话**（控制台提交是机器输入：半截值绝不许落盘）。
  5. **内联旧 key 的迁移提示**：embedder 的明文内联 key ⇒ 回 `migrate_hint`
     （不自动删，删不删由人决定）。
  6. **probe 结构化**：llm / embedder / screening 各走各的真请求（**mock http 层，
     不连真网**），成功带各自的细节、失败带 HTTP 状态与截断的错误摘要，
     **永不抛穿**、**不写库**、**计入审计**。

🔴 **配置隔离**：`KEEPSAKE_CONFIG` 一律指到 `tmp_path` 下的临时文件
（不指文件就会回落到 HOME 里那份真配置，测试会悄悄读到并被 PUT 写坏）；
密钥目录 `KEEPSAKE_AI_KEYS_DIR` 同样指到临时目录。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.api.routers import ops as ops_router
from keepsake.core import screening
from keepsake.core.sqlite import SqliteStorage

TOKEN = "ai-settings-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 模块级别名：假 LLM 的 `post(url, json=...)` 形参把 `json` 遮住了。
json_dumps = json.dumps

GET = "/api/v1/settings/ai"
SECTION_IDS = ["llm", "embedder", "screening", "query_expansion", "tiering_review"]

#: 假凭据（**只存在于测试里**：断言它绝不出现在响应体 / 审计里）。
FAKE_KEY = "sk-fake-key-do-not-leak-123456"


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def cfg_path(tmp_path, monkeypatch) -> Path:
    """**指文件**的临时 config 路径（不指文件就会回落到 HOME 里那份真配置）。"""
    p = tmp_path / "config.json"
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(p))
    screening.invalidate_cfg_cache()
    return p


@pytest.fixture()
def keys_dir(tmp_path, monkeypatch) -> Path:
    """密钥文件目录（生产是 ~/.config/keepsake/ai_keys；测试指临时目录）。"""
    d = tmp_path / "ai_keys"
    monkeypatch.setenv("KEEPSAKE_AI_KEYS_DIR", str(d))
    return d


@pytest.fixture()
def storage(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "ai_settings.db"), embedder=None, embed_dim=8,
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
def client(monkeypatch, storage, audit_dir, backup_dir, cfg_path, keys_dir):
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


def sections_of(client) -> Dict[str, dict]:
    body = client.get(GET, headers=AUTH).json()
    return {s["id"]: s for s in body["sections"]}


def field_of(section: dict, key: str) -> dict:
    for f in section["fields"]:
        if f["key"] == key:
            return f
    raise AssertionError(f"节 {section['id']} 里没有字段 {key}")


# ---------------------------------------------------------------- 读端点

def test_GET要token(client):
    assert client.get(GET).status_code == 401


def test_GET回五节且顺序固定(client):
    body = client.get(GET, headers=AUTH).json()
    assert [s["id"] for s in body["sections"]] == SECTION_IDS
    for s in body["sections"]:
        assert s["label"], s["id"]
        assert "enabled" in s and isinstance(s["enabled"], bool)
    # 只读端点不碰 config.json
    assert body["path"]
    assert body["backup_dir"]


def test_GET_两节无独立配置_只读说明卡(client, cfg_path):
    write_cfg(cfg_path, {"llm": {"base_url": "https://x/v1", "model": "m",
                                 "key_file": "/tmp/k"}})
    secs = sections_of(client)
    for sid in ("query_expansion", "tiering_review"):
        assert secs[sid]["fields"] == [], sid
        assert secs[sid]["editable"] is False
        assert secs[sid]["note"], "要写清复用哪一节"


def test_GET_数值字段自带上下限_前端零写死(client, cfg_path):
    write_cfg(cfg_path, {"screening": {"max_pick": 7, "timeout_s": 30,
                                       "max_tokens": 1024}})
    f = field_of(sections_of(client)["screening"], "max_pick")
    assert f["type"] == "number"
    assert f["value"] == 7
    assert f["min"] >= 1 and f["max"] > f["min"]


def test_GET_凭据真值永不下发(client, cfg_path, tmp_path):
    """🔴 核心红线：响应体里**搜不到** key 原文。"""
    key_file = tmp_path / "llm.key"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    write_cfg(cfg_path, {
        "llm": {"base_url": "https://x/v1", "model": "m", "key_file": str(key_file),
                "api_key": FAKE_KEY},
        "embedder": {"provider": "openai", "base_url": "https://x/v1",
                     "model": "text-embedding-3-small", "api_key": FAKE_KEY},
    })
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200, r.text
    assert FAKE_KEY not in r.text
    secs = sections_of(client)
    # 路径型 secret：只回 {set, path}
    kf = field_of(secs["llm"], "key_file")
    assert kf["secret"] is True
    assert kf["value"] == {"set": True, "path": str(key_file)}
    # 内联型 secret：只回 {set}
    ak = field_of(secs["embedder"], "api_key")
    assert ak["secret"] is True
    assert ak["value"] == {"set": True}


def test_GET_embedder内联key给migrate_hint(client, cfg_path):
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "m",
                                      "api_key": FAKE_KEY}})
    secs = sections_of(client)
    assert "migrate_hint" in secs["embedder"]
    assert secs["embedder"]["migrate_hint"]
    # 提示里不能带 key 真值
    assert FAKE_KEY not in client.get(GET, headers=AUTH).text


def test_GET没有内联key时不给migrate_hint(client, cfg_path):
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "m"}})
    secs = sections_of(client)
    assert not secs["embedder"].get("migrate_hint")


def test_GET只读不碰config(client, cfg_path):
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200
    assert not cfg_path.exists()


def test_GET配置坏了不500(client, cfg_path):
    cfg_path.write_text("{ 这不是 json", encoding="utf-8")
    r = client.get(GET, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [s["id"] for s in r.json()["sections"]] == SECTION_IDS


# ---------------------------------------------------------------- PUT 写端点

def test_PUT要token且必须带confirm(client, cfg_path):
    assert client.put(f"{GET}/llm", json={"confirm": False}, headers=AUTH).status_code == 400
    assert client.put(f"{GET}/llm", json={"confirm": True}, headers=None).status_code == 401


def test_PUT_未知节400(client, cfg_path):
    r = client.put(f"{GET}/nope", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "ai_section_unknown"


@pytest.mark.parametrize("sid", ["query_expansion", "tiering_review"])
def test_PUT_无独立配置的两节400_指引到llm(client, cfg_path, sid):
    """这两节复用 llm 节 ⇒ 不许在这里写（写了也是写到 llm 节去）。"""
    r = client.put(f"{GET}/{sid}", json={"confirm": True, "model": "m"}, headers=AUTH)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "ai_section_not_editable"
    assert "llm" in detail["message"]


def test_PUT_只动目标节_其余节逐字节不变(client, cfg_path):
    before = {
        "storage": {"backend": "sqlite", "path": "/tmp/x.db"},
        "retrieval": {"top_k": 8, "exclude_tags": ["core"]},
        "llm": {"base_url": "https://example.invalid", "model": "m", "key_file": "/tmp/k"},
        "embedder": {"provider": "openai", "model": "text-embedding-3-small"},
        "screening": {"enabled": False, "max_pick": 3},
        "tiering": {"enabled": True},
    }
    write_cfg(cfg_path, before)
    r = client.put(f"{GET}/llm", json={"confirm": True, "model": "new-model"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    after = read_cfg(cfg_path)
    assert [k for k in after if k != "llm"] == [k for k in before if k != "llm"], \
        "顶层键序不许被改 AI 这件事动过"
    # 其余节**逐字节**不变
    for key in ("storage", "retrieval", "embedder", "screening", "tiering"):
        assert json.dumps(after[key], ensure_ascii=False) == \
            json.dumps(before[key], ensure_ascii=False), key
    # 只改了目标节里的那一个键
    assert after["llm"]["model"] == "new-model"
    assert after["llm"]["base_url"] == before["llm"]["base_url"]
    assert after["llm"]["key_file"] == before["llm"]["key_file"]


def test_PUT_写前备份原config(client, cfg_path, backup_dir):
    before = {"llm": {"model": "m"}}
    write_cfg(cfg_path, before)
    r = client.put(f"{GET}/llm", json={"confirm": True, "model": "m2"}, headers=AUTH)
    assert r.status_code == 200, r.text
    backups = sorted(backup_dir.rglob("config.ai.*.json"))
    assert backups, "改配置前必须把原 config 整份备份"
    assert json.loads(backups[-1].read_text(encoding="utf-8")) == before


def test_PUT_留审计且不记值(client, cfg_path, audit_dir):
    write_cfg(cfg_path, {})
    client.put(f"{GET}/llm", json={"confirm": True, "model": "m2"}, headers=AUTH)
    client.put(f"{GET}/llm", json={"confirm": False, "model": "m3"}, headers=AUTH)  # 被拒
    lines = [ln for ln in audit_lines(audit_dir) if ln["action"] == "settings.ai.llm"]
    assert [ln["ok"] for ln in lines] == [True, False]
    # 🔴 审计里只许有字段名，不许有值
    assert "m2" not in json.dumps(lines, ensure_ascii=False)


@pytest.mark.parametrize("name,value", [
    ("max_pick", 0), ("max_pick", 31),
    ("timeout_s", 9), ("timeout_s", 301),
    ("max_tokens", 511), ("max_tokens", 16385),
])
def test_PUT_筛选越界400带人话(client, cfg_path, name, value):
    write_cfg(cfg_path, {"screening": {"enabled": False}})
    r = client.put(f"{GET}/screening", json={"confirm": True, name: value}, headers=AUTH)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "screening_out_of_range"
    assert name in detail["message"] and str(value) in detail["message"]
    # 越界不许落盘
    assert read_cfg(cfg_path)["screening"] == {"enabled": False}


def test_PUT_llm空串400(client, cfg_path):
    write_cfg(cfg_path, {"llm": {"model": "m"}})
    r = client.put(f"{GET}/llm", json={"confirm": True, "base_url": "   "}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "ai_invalid"
    assert read_cfg(cfg_path)["llm"] == {"model": "m"}


def test_PUT_未知字段400(client, cfg_path):
    """机器输入：字段名打错就该当场红，不许静默忽略。"""
    write_cfg(cfg_path, {})
    r = client.put(f"{GET}/llm", json={"confirm": True, "modle": "m"}, headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "ai_unknown_field"


def test_PUT_新凭据写密钥文件_600权限_config只留路径(client, cfg_path, keys_dir):
    """🔴 凭据不落 config.json：写进 600 权限的密钥文件，config 里只留路径。"""
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "m",
                                      "api_key": "old-inline-plaintext"}})
    r = client.put(f"{GET}/embedder", json={"confirm": True, "api_key": FAKE_KEY},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    key_path = Path(body["key_file"])
    assert key_path.parent == keys_dir
    assert key_path.read_text(encoding="utf-8").strip() == FAKE_KEY
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600, f"密钥文件权限必须是 0600，实际 {oct(mode)}"

    after = read_cfg(cfg_path)["embedder"]
    assert after["api_key_file"] == str(key_path)
    assert "api_key" not in after, "迁走之后明文不许留在 config.json 里"
    assert FAKE_KEY not in cfg_path.read_text(encoding="utf-8")


def test_PUT_没给新凭据时不动旧明文(client, cfg_path):
    """不填新凭据 = 不迁移（旧明文不自动删，由人决定）。"""
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "m",
                                      "api_key": "old-inline-plaintext"}})
    r = client.put(f"{GET}/embedder", json={"confirm": True, "model": "m2"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    after = read_cfg(cfg_path)["embedder"]
    assert after["api_key"] == "old-inline-plaintext"
    assert after["model"] == "m2"
    assert "api_key_file" not in after


def test_PUT_改完下一轮就生效(client, cfg_path):
    """热生效走既有 mtime 机制：写完下一轮读到的就是新值。"""
    write_cfg(cfg_path, {})
    client.put(f"{GET}/screening", json={"confirm": True, "enabled": True,
                                         "max_pick": 5}, headers=AUTH)
    screening.invalidate_cfg_cache()
    assert screening.load_cfg()["enabled"] is True
    assert screening.load_cfg()["max_pick"] == 5


# ---------------------------------------------------------------- probe

class _FakeResp:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json_dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """假 HTTP 客户端：记下调用，回一个准备好的响应。"""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
        self.calls.append({"url": url, "body": json, "headers": headers})
        return self._resp


def _llm_cfg(key_file: Path) -> Dict[str, Any]:
    return {"llm": {"base_url": "https://example.invalid/v1", "model": "m-x",
                    "key_file": str(key_file)}}


def test_probe_llm_ok(client, cfg_path, tmp_path, monkeypatch):
    key_file = tmp_path / "llm.key"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    write_cfg(cfg_path, _llm_cfg(key_file))
    fake = _FakeClient(_FakeResp(200, {"choices": [{"finish_reason": "stop",
                                                    "message": {"content": "pong"}}]}))
    monkeypatch.setattr(ops_router, "get_client", lambda base_url, timeout: fake)

    r = client.post(f"{GET}/llm/probe", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["finish_reason"] == "stop"
    assert b["latency_ms"] >= 0
    # 真发了一条最小 chat completion（max_tokens 给足 512 防思考段腰斩）
    call = fake.calls[0]
    assert call["url"] == "https://example.invalid/v1/chat/completions"
    assert call["body"]["max_tokens"] == 512
    assert call["body"]["messages"][0]["content"] == "ping"
    # 🔴 响应里不许带 key
    assert FAKE_KEY not in r.text


def test_probe_llm_失败结构化(client, cfg_path, tmp_path, monkeypatch):
    key_file = tmp_path / "llm.key"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    write_cfg(cfg_path, _llm_cfg(key_file))
    fake = _FakeClient(_FakeResp(401, None, text="{\"error\":\"invalid api key\"}"))
    monkeypatch.setattr(ops_router, "get_client", lambda base_url, timeout: fake)

    r = client.post(f"{GET}/llm/probe", headers=AUTH)
    assert r.status_code == 200, r.text          # 结构化回包，不是 5xx
    b = r.json()
    assert b["ok"] is False
    assert b["status"] == 401
    assert b["error"] == "http_401"
    assert "invalid api key" in b["message"]
    assert FAKE_KEY not in r.text


def test_probe_embedder_ok_带维度(client, cfg_path, monkeypatch):
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "text-embedding-3-small",
                                      "base_url": "https://example.invalid/v1",
                                      "api_key": FAKE_KEY}})
    fake = _FakeClient(_FakeResp(200, {"data": [{"embedding": [0.1] * 1536}]}))
    monkeypatch.setattr(ops_router, "get_client", lambda base_url, timeout: fake)

    r = client.post(f"{GET}/embedder/probe", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["dimension"] == 1536
    assert b["latency_ms"] >= 0
    assert fake.calls[0]["url"] == "https://example.invalid/v1/embeddings"
    assert FAKE_KEY not in r.text


def test_probe_embedder_错误体原样转述(client, cfg_path, monkeypatch):
    write_cfg(cfg_path, {"embedder": {"provider": "dashscope", "model": "text-embedding-v2",
                                      "base_url": "https://example.invalid/v1",
                                      "api_key": FAKE_KEY}})
    body = json_dumps({"code": 20015, "message": "Access key id is invalid"})
    fake = _FakeClient(_FakeResp(400, None, text=body))
    monkeypatch.setattr(ops_router, "get_client", lambda base_url, timeout: fake)

    r = client.post(f"{GET}/embedder/probe", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is False
    assert b["status"] == 400
    assert "20015" in b["message"]
    assert FAKE_KEY not in r.text


def test_probe_screening_走真函数_报picked(client, cfg_path, tmp_path, monkeypatch):
    key_file = tmp_path / "screen.key"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    write_cfg(cfg_path, {"screening": {"enabled": False, "model": "m-x",
                                       "base_url": "https://example.invalid/v1",
                                       "api_key_file": str(key_file)}})
    fake = _FakeClient(_FakeResp(200, {"choices": [{"message": {
        "content": json_dumps({"keep": [1]})}}]}))
    monkeypatch.setattr(screening, "get_client", lambda base_url, timeout: fake)

    r = client.post(f"{GET}/screening/probe", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["picked"] == [1]
    assert b["of"] == 2
    assert fake.calls, "screening 探测必须真发一次选择请求"


def test_probe_没配时结构化失败_不抛穿(client, cfg_path):
    write_cfg(cfg_path, {})
    r = client.post(f"{GET}/llm/probe", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is False
    assert b["error"] == "not_configured"


def test_probe_不写库不改config(client, cfg_path, tmp_path, monkeypatch):
    """🔴 probe 是读性质：不改 config.json、不写库。"""
    key_file = tmp_path / "llm.key"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    before = _llm_cfg(key_file)
    write_cfg(cfg_path, before)
    raw_before = cfg_path.read_text(encoding="utf-8")
    fake = _FakeClient(_FakeResp(200, {"choices": [{"finish_reason": "stop"}]}))
    monkeypatch.setattr(ops_router, "get_client", lambda base_url, timeout: fake)

    client.post(f"{GET}/llm/probe", headers=AUTH)
    assert cfg_path.read_text(encoding="utf-8") == raw_before


def test_probe_计入审计(client, cfg_path, audit_dir):
    write_cfg(cfg_path, {})
    client.post(f"{GET}/llm/probe", headers=AUTH)
    lines = [ln for ln in audit_lines(audit_dir)
             if ln["action"] == "settings.ai.probe.llm"]
    assert lines, "probe 也要留痕"
    assert lines[-1]["ok"] is False


def test_probe_无独立配置的两节400(client, cfg_path):
    r = client.post(f"{GET}/query_expansion/probe", headers=AUTH)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "ai_section_not_editable"
