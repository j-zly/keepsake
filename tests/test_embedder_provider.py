"""embedder provider 口径统一（2026-10 embedder_provider_hygiene）。

钉四件事：

  1. **env 影子开关退役**：``KEEPSAKE_EMBEDDER`` 这个环境变量**不再生效**
     —— 唯一配置源是 config.json（/ 调用方实参）。env 悄悄启用 dashscope
     会让「页面上看到的」与「实际跑的」不是一回事；
  2. **逃生门留着**：显式传 ``provider="dashscope"`` 仍能构造出 dashscope 实例
     （不做删除式阉割 —— 真有在用 dashscope 的人）；
  3. **GET /settings/ai 的 embedder.provider 是只读展示项**（``readonly: true``，
     展示文案由后端给，前端零硬编码），但 **PUT 侧仍接受 provider 写入**；
  4. **三卡字段顺序统一**：接口地址 → 模型 → 凭据（状态条）→ 其它
     （screening 独有数值参数排最后）。

🔴 **配置隔离**：``KEEPSAKE_CONFIG`` 一律指到 ``tmp_path`` 下的临时文件
（不指文件就会回落到 HOME 里那份真配置，测试会悄悄读到并被 PUT 写坏）；
密钥目录 ``KEEPSAKE_AI_KEYS_DIR`` 同样指到临时目录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core import embedder as embedder_mod
from keepsake.core import screening
from keepsake.core.embedder import OpenAIEmbedder, create_embedder
from keepsake.core.sqlite import SqliteStorage

TOKEN = "embedder-provider-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

GET = "/api/v1/settings/ai"

#: 假凭据（**只存在于测试里**：断言它绝不出现在响应体里）。
FAKE_KEY = "sk-fake-key-do-not-leak-654321"


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
    s = SqliteStorage(path=str(tmp_path / "embedder_provider.db"), embedder=None,
                      embed_dim=8, is_primary=True)
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


def sections_of(client) -> Dict[str, dict]:
    body = client.get(GET, headers=AUTH).json()
    return {s["id"]: s for s in body["sections"]}


def field_of(section: dict, key: str) -> dict:
    for f in section["fields"]:
        if f["key"] == key:
            return f
    raise AssertionError(f"节 {section['id']} 里没有字段 {key}")


class _DashScopeStub:
    """dashscope 家族的替身：一眼看出 create_embedder 到底选了哪个 provider。"""

    def __init__(self, **kw: Any):
        self.kw = kw


@pytest.fixture()
def dashscope_stub(monkeypatch):
    """把注册表里的 dashscope 换成替身（测试完自动还原）。"""
    monkeypatch.setitem(embedder_mod._EMBEDDER_PROVIDERS, "dashscope", _DashScopeStub)
    return _DashScopeStub


# ------------------------------------------------- 1. env 影子开关退役

def test_env影子开关已退役_设了也不生效(monkeypatch, dashscope_stub):
    """KEEPSAKE_EMBEDDER=dashscope **不再**能启用 dashscope 分支。"""
    monkeypatch.setenv("KEEPSAKE_EMBEDDER", "dashscope")
    emb = create_embedder()
    assert not isinstance(emb, dashscope_stub), \
        "env 不得再启用 dashscope 分支（唯一配置源 = config.json / 实参）"
    assert isinstance(emb, OpenAIEmbedder), "无 provider = 显式走 OpenAI 兼容实现"


def test_env影子开关退役_源码里不再读这个变量():
    """源码级钉死：模块里不再有 os.environ.get("KEEPSAKE_EMBEDDER"...) 这一读。"""
    src = Path(embedder_mod.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("KEEPSAKE_EMBEDDER"' not in src


def test_无配置时provider归一为openai(monkeypatch):
    """空白/带空格的 provider 归一成 openai（协议缺省，不是影子开关）。"""
    monkeypatch.delenv("KEEPSAKE_EMBEDDER", raising=False)
    assert create_embedder(provider="").__class__ is OpenAIEmbedder
    assert create_embedder(provider="  OpenAI  ").__class__ is OpenAIEmbedder


# ------------------------------------------------- 2. 逃生门仍在

def test_显式传provider_dashscope仍可构造(dashscope_stub):
    """显式传 provider="dashscope" ⇒ 仍走 dashscope 分支（逃生门不阉割）。"""
    emb = create_embedder(provider="dashscope")
    assert isinstance(emb, dashscope_stub)


def test_显式传provider_dashscope用真实注册表也能构造():
    """真实注册表下也构造得出来（不抛）。"""
    emb = create_embedder(provider="dashscope", model="text-embedding-v2")
    assert isinstance(emb, OpenAIEmbedder)


# ------------------------------------------------- 3. GET 只读展示项

def test_GET_embedder的provider是只读展示项(client, cfg_path):
    write_cfg(cfg_path, {"embedder": {"provider": "dashscope",
                                      "base_url": "https://x/v1",
                                      "model": "text-embedding-v2",
                                      "api_key": FAKE_KEY}})
    f = field_of(sections_of(client)["embedder"], "provider")
    assert f["readonly"] is True, "provider 必须是只读展示项（前端不再给输入框）"
    assert f["display"], "只读展示文案由后端给（前端零硬编码）"
    assert f["value"] == "dashscope", "真值仍在响应里（API 消费者可见）"
    # 凭据真值仍不下发
    assert FAKE_KEY not in client.get(GET, headers=AUTH).text


def test_PUT仍接受provider写入_逃生门留着(client, cfg_path):
    r = client.put(f"{GET}/embedder", json={"confirm": True, "provider": "dashscope"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    assert read_cfg(cfg_path)["embedder"]["provider"] == "dashscope"


# ------------------------------------------------- 4. 三卡字段顺序统一

@pytest.mark.parametrize("sid", ["llm", "embedder", "screening"])
def test_三卡字段顺序统一_接口地址先于模型先于凭据(client, cfg_path, sid):
    write_cfg(cfg_path, {"llm": {"base_url": "https://x/v1", "model": "m"},
                         "embedder": {"provider": "openai", "base_url": "https://x/v1",
                                      "model": "text-embedding-3-small"},
                         "screening": {"base_url": "https://x/v1", "model": "m"}})
    keys = [f["key"] for f in sections_of(client)[sid]["fields"]]
    cred = "key_file" if sid == "llm" else "api_key_file"
    assert keys.index("base_url") < keys.index("model"), f"{sid}：接口地址先于模型"
    assert keys.index("model") < keys.index(cred), f"{sid}：模型先于凭据"
    assert keys.index(cred) < keys.index("api_key"), f"{sid}：凭据路径先于凭据值"


def test_embedder只读provider排最后_screening数值参数排最后(client, cfg_path):
    write_cfg(cfg_path, {"embedder": {"provider": "openai", "model": "m"},
                         "screening": {"model": "m"}})
    secs = sections_of(client)
    emb_keys = [f["key"] for f in secs["embedder"]["fields"]]
    assert emb_keys[-1] == "provider", "只读展示项排最后（可编辑三件在前）"
    scr_keys = [f["key"] for f in secs["screening"]["fields"]]
    assert scr_keys[-3:] == ["max_pick", "timeout_s", "max_tokens"], \
        "screening 独有数值参数排最后"
