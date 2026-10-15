"""常驻层注入预算 / 导入模板 / 导入即升常驻（keepsake_budget_io_templates）。

本文件钉死四件最容易悄悄坏掉的事：

  1. **预算只有一个口径** —— provider 注入路径（`_core_budget()`）与控制台
     （`api.config.core_char_budget()`）读的是**同一个函数**、同一个 config 键。
     两处各读一次，看起来「都读了 config」，但缓存粒度/兜底值一旦不同步，
     页面显示的占用率就会和注入真正截断的阈值对不上，而两边都不报错。
  2. **热生效 + 非法值兜底** —— 改完 config.json 下一轮就生效（mtime 缓存）；
     值不是正整数就回落 60000 + warning，绝不因为一个坏键让注入抛异常。
  3. **PUT 只动那一个键** —— config.json 里还有存储后端与 LLM 通道，
     「改预算」顺手重排/丢键会让 provider 读不到 DSN。
  4. **模板每行都是合法 JSON** —— 带 `#` 注释行的模板会被用户的解析器数成坏行，
     表现是「导入了 0 条」，用户无从判断是自己文件错了还是功能坏了。

🔴 **配置隔离**：`KEEPSAKE_CONFIG` 一律指到 `tmp_path` 下的临时文件。
只 `delenv` 不指文件是假绿 —— provider 读不到 env 就回落到 `~/.config/keepsake/
config.json`，真机上那份文件是存在的，测试会悄悄读到（并被 PUT 写坏）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from keepsake.adapters.hermes import provider as provider_module
from keepsake.api import config as api_config
from keepsake.api.app import create_app
from keepsake.api.deps import get_storage
from keepsake.core.sqlite import SqliteStorage

TOKEN = "budget-test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

BUDGET_PUT = "/api/v1/settings/core-budget"
TEMPLATE = "/api/v1/import/template"
IMPORT = "/api/v1/import"
DEFAULT_BUDGET = 60000

SEED: List[Dict[str, Any]] = [
    {"key": "memory:frag:aaa", "content": "常驻记忆 A：部署流程要点",
     "tags": "core,ops", "created": "2026-01-01T00:00:00+00:00"},
]


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def cfg_path(tmp_path, monkeypatch) -> Path:
    """**指文件**的临时 config 路径（不指文件就会回落到 HOME 里那份真配置）。"""
    p = tmp_path / "config.json"
    monkeypatch.setenv("KEEPSAKE_CONFIG", str(p))
    provider_module._CORE_BUDGET_CACHE.clear()
    return p


@pytest.fixture()
def storage(tmp_path):
    s = SqliteStorage(path=str(tmp_path / "budget.db"), embedder=None, embed_dim=8,
                      is_primary=True)
    assert s.ensure_index() is not False
    assert s.write_fragments_batch([dict(r) for r in SEED]) == len(SEED)
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


# ---------------------------------------------------------------- 预算单点

def test_缺键回落默认_两处同值():
    """config 里没这个键 ⇒ 60000，且 provider 与控制台读到的是同一个数。"""
    assert provider_module._core_budget() == DEFAULT_BUDGET
    assert api_config.core_char_budget() == DEFAULT_BUDGET


def test_控制台只转发provider同一函数():
    """🔴 口径单点：控制台不许自己再读一遍 config，也不许再存一份常量。"""
    src = Path(api_config.__file__).read_text(encoding="utf-8")
    assert "core_char_budget" in src
    # 函数体里出现了 provider 的 helper —— 两处是同一个函数的两个入口
    assert "_core_budget()" in src
    assert '"retrieval"' not in src, "控制台不许另起一份 config 读取口径"


@pytest.mark.parametrize("bad", ["不是数字", 0, -1, None, 1.5])
def test_非法值回落默认(cfg_path, bad):
    write_cfg(cfg_path, {"retrieval": {"core_char_budget": bad}})
    assert provider_module._core_budget() == DEFAULT_BUDGET
    assert api_config.core_char_budget() == DEFAULT_BUDGET


def test_改配置下一轮就热生效(cfg_path):
    """mtime 缓存命中判据是文件指纹，不是进程启动时刻。"""
    assert provider_module._core_budget() == DEFAULT_BUDGET
    write_cfg(cfg_path, {"retrieval": {"core_char_budget": 12345}})
    assert provider_module._core_budget() == 12345
    assert api_config.core_char_budget() == 12345
    # 尺寸变了 ⇒ 指纹变 ⇒ 再次 miss 重读（未命中前不许有任何「读不到新值」）
    write_cfg(cfg_path, {"retrieval": {"core_char_budget": 1234567}})
    assert provider_module._core_budget() == 1234567


def test_注入按配置预算截断(cfg_path):
    """端到端口径：把预算调到只够放第一条 ⇒ 第二条被截掉且显式标注。"""
    write_cfg(cfg_path, {"retrieval": {"core_char_budget": 1200}})
    provider_module._CORE_CACHE.update({"ts": 0.0, "frags": None})
    frags = [{"key": "memory:frag:a", "content": "a" * 600, "tags": "core"},
             {"key": "memory:frag:b", "content": "b" * 900, "tags": "core"}]
    prov = provider_module.KeepsakeProvider.__new__(provider_module.KeepsakeProvider)
    prov._storage = type("S", (), {"list_by_tag": staticmethod(lambda tag: frags)})()
    block = provider_module.KeepsakeProvider._core_block(prov)
    assert "a" * 600 in block and "b" * 900 not in block
    assert "core 层已截断" in block and "本次注入 1 条" in block
    provider_module._CORE_CACHE.update({"ts": 0.0, "frags": None})


# ---------------------------------------------------------------- PUT 设置

def test_PUT与模板要token(client, cfg_path):
    assert client.put(BUDGET_PUT, json={"budget": 20000, "confirm": "60000"}).status_code == 401
    assert client.get(TEMPLATE).status_code == 401


def test_PUT越界与确认闸门(client, cfg_path):
    for budget in (999, 500001, -1):
        r = client.put(BUDGET_PUT, json={"budget": budget, "confirm": "60000"}, headers=AUTH)
        assert r.status_code == 400, budget
        assert r.json()["detail"]["error"] in ("budget_out_of_range", "confirm_required")
    # 确认串缺失 / 不匹配 ⇒ 拒，且**一个字节都没写**（文件根本不该被创建）
    assert client.put(BUDGET_PUT, json={"budget": 20000}, headers=AUTH).status_code == 400
    r = client.put(BUDGET_PUT, json={"budget": 20000, "confirm": "12345"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "confirm_mismatch"
    assert not cfg_path.exists()


def test_PUT只改那一个键_其余键与键序不变(client, cfg_path):
    before = {"storage": {"backend": "sqlite", "path": "/tmp/x.db"},
              "retrieval": {"top_k": 8, "exclude_tags": ["core"]},
              "llm": {"base_url": "https://example.invalid", "model": "m"}}
    write_cfg(cfg_path, before)
    r = client.put(BUDGET_PUT, json={"budget": 20000, "confirm": "60000"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["budget"] == 20000
    assert body["path"] == str(cfg_path)

    after = read_cfg(cfg_path)
    assert list(after) == list(before), "顶层键序不许被改预算这件事动过"
    assert after["storage"] == before["storage"] and after["llm"] == before["llm"]
    assert list(after["retrieval"]) == ["top_k", "exclude_tags", "core_char_budget"]
    assert after["retrieval"]["top_k"] == 8
    assert after["retrieval"]["core_char_budget"] == 20000

    # 热生效 + 两处同口径
    assert provider_module._core_budget() == 20000
    assert api_config.core_char_budget() == 20000
    assert client.get("/api/v1/core/stats", headers=AUTH).json()["budget"] == 20000


def test_PUT写前备份原config(client, cfg_path, backup_dir):
    before = {"retrieval": {"core_char_budget": 30000}}
    write_cfg(cfg_path, before)
    r = client.put(BUDGET_PUT, json={"budget": 40000, "confirm": "30000"}, headers=AUTH)
    assert r.status_code == 200, r.text
    backups = sorted(backup_dir.rglob("config.core-budget.*.json"))
    assert backups, "改配置前必须把原 config 整份备份"
    assert json.loads(backups[-1].read_text(encoding="utf-8")) == before


def test_PUT后同尺寸改值也立刻生效(client, cfg_path):
    """🔴 指纹是 (mtime_ns, size)：20000→30000 同位数，指纹可能一字不变。
    写完必须主动失效读取侧缓存，否则「提示改成功了、注入还按老预算截断」。"""
    write_cfg(cfg_path, {"retrieval": {"core_char_budget": 20000}})
    assert provider_module._core_budget() == 20000
    r = client.put(BUDGET_PUT, json={"budget": 30000, "confirm": "20000"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert provider_module._core_budget() == 30000
    assert api_config.core_char_budget() == 30000


def test_PUT留审计(client, cfg_path, audit_dir):
    write_cfg(cfg_path, {})
    client.put(BUDGET_PUT, json={"budget": 20000, "confirm": "60000"}, headers=AUTH)
    client.put(BUDGET_PUT, json={"budget": 1, "confirm": "60000"}, headers=AUTH)  # 被拒
    lines = [ln for ln in audit_lines(audit_dir)
             if ln["action"] == "settings.core_budget"]
    assert [ln["ok"] for ln in lines] == [True, False]
    assert "60000 -> 20000" in lines[0]["reason"]


def test_PUT绝不碰HOME里那份真config(client, cfg_path, monkeypatch, tmp_path):
    """双向核：写入的是 tmp 路径，HOME 下的 config.json 不存在 ⇒ 写完仍不存在。"""
    write_cfg(cfg_path, {})
    home_cfg = Path.home() / ".config" / "keepsake" / "config.json"
    existed_before = home_cfg.exists()
    r = client.put(BUDGET_PUT, json={"budget": 20000, "confirm": "60000"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["path"] == str(cfg_path)
    assert home_cfg.exists() == existed_before


# ---------------------------------------------------------------- 导入模板

@pytest.mark.parametrize("kind,fields", [
    ("memory", {"content"}),
    ("kb", {"title", "content"}),
    ("core", {"content"}),
])
def test_模板三形态_每行都是合法JSON(client, kind, fields):
    r = client.get(TEMPLATE, params={"kind": kind}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == (
        f'attachment; filename="keepsake-import-template-{kind}.jsonl"')
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert 2 <= len(lines) <= 3
    for ln in lines:
        obj = json.loads(ln)          # 语法错就红：模板不许有注释行
        assert fields <= set(obj), ln


def test_模板三形态互不相同(client):
    bodies = {k: client.get(TEMPLATE, params={"kind": k}, headers=AUTH).text
              for k in ("memory", "kb", "core")}
    assert len(set(bodies.values())) == 3
    # core 那份要带「不必手打 core」的提示（否则用户只会去手打被拒的标签）
    assert "core_candidate" in bodies["core"]


def test_模板零口令零IP(client):
    """模板是会被下载下来公开转发的：里面不许出现任何像凭据的东西。"""
    for kind in ("memory", "kb", "core"):
        text = client.get(TEMPLATE, params={"kind": kind}, headers=AUTH).text
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", text)
        assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", text)
        assert "password" not in text.lower()


def test_模板要token且_kind非法报422(client):
    assert client.get(TEMPLATE).status_code == 401
    r = client.get(TEMPLATE, params={"kind": "不存在的形态"}, headers=AUTH)
    assert r.status_code == 422


def test_模板是只读端点(client, cfg_path):
    """只读：不挂 write_guard，也绝不碰 config.json。"""
    r = client.get(TEMPLATE, headers=AUTH)
    assert r.status_code == 200
    assert not cfg_path.exists()


# ---------------------------------------------------------------- 导入即升常驻

def test_import_dryrun回显would_promote_零写入(client, storage):
    items = [{"content": "导入即升常驻的第一条"}, {"content": "导入即升常驻的第二条"}]
    r = client.post(IMPORT, json={"items": items, "dry_run": True, "promote": True},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["would_add"] == 2 and body["would_promote"] == 2
    assert len(storage.list_by_tag("core")) == len(SEED)      # 预演零写入
    # 不勾 promote ⇒ would_promote 为 0（不改其它计数口径）
    r2 = client.post(IMPORT, json={"items": items, "dry_run": True}, headers=AUTH)
    assert r2.json()["would_add"] == 2 and r2.json()["would_promote"] == 0


def test_import实跑promote真打core标签(client, storage):
    items = [{"content": "导入即升常驻的第一条", "tags": "demo"},
             {"content": "导入即升常驻的第二条", "tags": "demo"}]
    r = client.post(IMPORT, json={"items": items, "dry_run": False, "confirm": "2",
                                  "promote": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promoted"] == 2
    for key in body["keys"]:
        row = storage.get_fragment(key)
        assert row["tags"].startswith("core,") and "demo" in row["tags"]
    assert len(storage.list_by_tag("core")) == len(SEED) + 2


def test_import不带promote就不升常驻(client, storage):
    r = client.post(IMPORT, json={"items": [{"content": "只进检索层的一条"}],
                                  "dry_run": False, "confirm": "1"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["promoted"] == 0
    assert len(storage.list_by_tag("core")) == len(SEED)


def test_import自带core标签仍拒收_口径不变(client, storage):
    """promote 是**唯一**的升常驻入口：条里手打 core 仍然条级拒收。"""
    r = client.post(IMPORT, json={"items": [
        {"content": "手打 core 标签的一条", "tags": "core"},
        {"content": "正常的一条"},
    ], "dry_run": False, "confirm": "1", "promote": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rejected_reserved"] == 1 and body["would_add"] == 1
    assert body["promoted"] == 1


def test_import_promote进审计(client, audit_dir):
    client.post(IMPORT, json={"items": [{"content": "审计里要看得见 promote=true"}],
                              "dry_run": False, "confirm": "1", "promote": True},
                headers=AUTH)
    reasons = [ln["reason"] for ln in audit_lines(audit_dir) if ln["action"] == "memory.import"]
    assert any("promote=True" in r for r in reasons)
    assert any("keys=1" in r and "promote=True" in r for r in reasons)
