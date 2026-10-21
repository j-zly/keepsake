"""代理注册表测试（`keepsake_agent_registry` 第 1 张单）。

覆盖九件事，逐条对应任务书：

1. 登记 / 幂等 / 新代理默认 pending；
2. 批准凭据的**双向**校验（对的过、错的拒、吊销后拒）；
3. `set_role` 的**全库唯一 primary**（第二个 primary 自动降旧）；
4. registry 抛异常时写闸 **fail-closed 按 sub**（降级探针）；
5. 迁移器：`agent:` 项进 exclude、`core_candidate` 忽略、二次跑不重复；
6. 本机 bootstrap 行 = approved + primary + `meta.bootstrap=local`；
7. Redis 假 client（`keepsake:agent:<id>` hash 形态）语义与 SQL 后端同款；
8. PG 真库（8.140.192.91 / keepsake_test，跑完清干净；没凭据就 skip）；
9. 凭据纪律：库里只有 sha256，明文不进存储、不进日志。

全程 hermetic：SQLite 用 `tmp_path` 临时库、Redis 用内存假 client、PG 用隔离测试库，
**绝不碰主脑的真实 config / 生产库**。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from keepsake.govern.registry import (
    CREDENTIAL_PREFIX,
    ROLE_PRIMARY,
    ROLE_SUB,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REVOKED,
    AlreadyApprovedError,
    Registry,
    hash_credential,
)

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def lite(tmp_path):
    """真 SqliteStorage（临时文件）+ 建好表的注册表。"""
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "registry.db"))
    assert st.ensure_index() is True
    reg = Registry(st)
    yield reg
    st.close()


def _roles(reg: Registry) -> Dict[str, str]:
    return {a["agent_id"]: a["role"] for a in reg.list_agents()}


def _statuses(reg: Registry) -> Dict[str, str]:
    return {a["agent_id"]: a["status"] for a in reg.list_agents()}


# --------------------------------------------------------------------------
# ① 登记 / 幂等 / pending 缺省
# --------------------------------------------------------------------------

def test_新代理默认pending_且读写全拒(lite):
    reg = lite
    row = reg.register("worker-1", "工人代理")
    assert row["status"] == STATUS_PENDING and row["role"] == ROLE_SUB
    # 未批准 ⇒ 凭据校验一律拒（没有凭据可发）
    assert reg.verify("worker-1", "")["ok"] is False
    assert reg.verify("worker-1", "")["reason"] == "not_approved"


def test_register幂等_重复登记不洗白吊销(lite):
    """🔴 登记必须幂等且**不碰已有行的状态**：进程每轮启动都 register，
    若这里刷新 status，管理员的一次吊销会被下次启动悄悄洗白。"""
    reg = lite
    reg.register("worker-1")
    reg.revoke("worker-1")
    reg.register("worker-1")
    assert _statuses(reg)["worker-1"] == STATUS_REVOKED


def test_代理不存在_读面返None不报错(lite):
    assert lite.get("查无此代理") is None
    assert lite.role_of("查无此代理") == ROLE_SUB      # 查不到一律 sub（fail-closed）
    assert lite.approve("查无此代理") is None
    assert lite.revoke("查无此代理") is False


# --------------------------------------------------------------------------
# ② 凭据：一次性明文 / 双向校验 / 吊销
# --------------------------------------------------------------------------

def test_approve返回一次性明文且库里只有哈希(lite):
    reg = lite
    reg.register("worker-1")
    credential = reg.approve("worker-1")
    assert credential.startswith(CREDENTIAL_PREFIX) and len(credential) > 20

    row = reg._be.load("worker-1")            # noqa: SLF001 —— 直读后端，验「库里没有明文」
    assert row["credential_hash"] == hash_credential(credential)
    assert credential not in json.dumps(row, ensure_ascii=False)
    assert row["credential_hint"] == credential[:8]


def test_verify对错双向(lite):
    reg = lite
    reg.register("worker-1")
    credential = reg.approve("worker-1")
    ok = reg.verify("worker-1", credential)
    assert ok["ok"] is True and ok["status"] == STATUS_APPROVED

    bad = reg.verify("worker-1", credential + "0")
    assert bad["ok"] is False and bad["reason"] == "bad_credential"
    # 🔴 失败消息不许回显凭据（收到的 / 期望的都不行）
    assert credential not in json.dumps(bad, ensure_ascii=False)


def test_吊销后凭据立即作废(lite):
    reg = lite
    reg.register("worker-1")
    credential = reg.approve("worker-1")
    assert reg.revoke("worker-1") is True
    got = reg.verify("worker-1", credential)
    assert got["ok"] is False and got["status"] == STATUS_REVOKED
    assert reg.get("worker-1")["credential_hint"] == ""      # 哈希也清了


def test_补发凭据换一条旧的即失效(lite):
    """换发（补发凭据）必须**显式** `reissue=True` —— 默认档是「已批准 ⇒ 拒」。"""
    reg = lite
    reg.register("worker-1")
    first = reg.approve("worker-1")
    second = reg.approve("worker-1", reissue=True)
    assert first != second
    assert reg.verify("worker-1", first)["ok"] is False
    assert reg.verify("worker-1", second)["ok"] is True


def test_已批准再approve默认拒_凭据与状态都不动(lite):
    """🔴 approve 原语的**默认安全档**：重复批准不再静默换发。

    已 approved 的行再 approve（不带 reissue）⇒ 抛 `AlreadyApprovedError`，
    凭据哈希 / 状态 / 角色一个都不动 —— 静默换发会让线上代理瞬间全断，
    而操作者毫无感知（重复批准的三个误伤路径：双击、治理工具再批一次、UI 状态未刷新）。
    """
    reg = lite
    reg.register("worker-1")
    first = reg.approve("worker-1")
    before = reg._be.load("worker-1")            # noqa: SLF001 —— 直读后端对照哈希
    with pytest.raises(AlreadyApprovedError):
        reg.approve("worker-1")
    after = reg._be.load("worker-1")             # noqa: SLF001
    assert after["credential_hash"] == before["credential_hash"]
    assert after["status"] == STATUS_APPROVED
    assert reg.verify("worker-1", first)["ok"] is True
    assert reg.verify_any(first)["agent_id"] == "worker-1"


def test_revoked行approve不归reissue闸管(lite):
    """回归：revoked 行重新批准照旧直接签发（它不是「已批准」）。"""
    reg = lite
    reg.register("worker-1")
    first = reg.approve("worker-1")
    reg.revoke("worker-1")
    second = reg.approve("worker-1")            # 不带 reissue 也放行
    assert second != first
    assert reg.verify("worker-1", first)["ok"] is False
    assert reg.verify("worker-1", second)["ok"] is True


def test_verify失败不落日志(lite, caplog):
    """🔴 凭据明文绝不进日志。"""
    reg = lite
    reg.register("worker-1")
    credential = reg.approve("worker-1")
    with caplog.at_level(logging.DEBUG, logger="keepsake.govern.registry"):
        reg.verify("worker-1", "ksk_" + "f" * 32)
        reg.revoke("worker-1")
    assert credential not in caplog.text


# --------------------------------------------------------------------------
# ③ 唯一 primary
# --------------------------------------------------------------------------

def test_置primary自动降旧主(lite):
    reg = lite
    reg.register("a"), reg.register("b")
    assert _roles(reg) == {"a": ROLE_SUB, "b": ROLE_SUB}
    assert reg.set_role("a", ROLE_PRIMARY) == []          # 库里还没有主脑 ⇒ 无人被降
    assert reg.set_role("b", ROLE_PRIMARY) == ["a"]      # a 自动降 sub
    assert _roles(reg) == {"a": ROLE_SUB, "b": ROLE_PRIMARY}


def test_全库永远只有一个primary(lite):
    reg = lite
    for aid in ("a", "b", "c"):
        reg.register(aid)
        reg.set_role(aid, ROLE_PRIMARY)
        primaries = [k for k, v in _roles(reg).items() if v == ROLE_PRIMARY]
        assert primaries == [aid], primaries


def test_非法角色与未知代理都报错(lite):
    reg = lite
    reg.register("a")
    with pytest.raises(ValueError):
        reg.set_role("a", "supervisor")
    with pytest.raises(KeyError):
        reg.set_role("查无此代理", ROLE_PRIMARY)


# --------------------------------------------------------------------------
# ④ exclude 开关 + ⑤ 迁移器
# --------------------------------------------------------------------------

def test_迁移器把agent项迁进exclude(lite):
    reg = lite
    report = reg.migrate_exclude_tags(
        {"retrieval": {"exclude_tags": "core_candidate,agent:worker-1, noise"}})
    assert report["migrated"] == ["worker-1"]
    assert "core_candidate" in report["ignored"]      # 候选层的默认排除由审核引擎承担
    assert "noise" in report["ignored"]
    assert reg.get("worker-1")["exclude_from_primary"] is True
    assert reg.get("worker-1")["status"] == STATUS_PENDING   # 迁移不代替批准


def test_迁移器幂等_二次跑不重复(lite):
    reg = lite
    cfg = {"retrieval": {"exclude_tags": "agent:worker-1"}}
    first = reg.migrate_exclude_tags(cfg)
    second = reg.migrate_exclude_tags(cfg)
    assert first["changed"] == ["worker-1"]
    assert second["changed"] == []                   # 二次跑没有新的变更
    assert second["migrated"] == ["worker-1"]        # 但报告口径一致（幂等不是不报告）
    assert len(reg.list_agents()) == 1


def test_迁移器只读不改用户配置(lite, tmp_path):
    """🔴 迁移器**只读**用户的 config：那个键留在文件里（写 config 是治理端点的事）。"""
    reg = lite
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"retrieval": {"exclude_tags": "agent:worker-1"}}),
                        encoding="utf-8")
    before = cfg_file.read_text(encoding="utf-8")
    reg.migrate_exclude_tags(json.loads(before))
    assert cfg_file.read_text(encoding="utf-8") == before


def test_set_exclude幂等且报是否变化(lite):
    reg = lite
    reg.register("worker-1")
    assert reg.set_exclude("worker-1", True) is True
    assert reg.set_exclude("worker-1", True) is False
    assert reg.set_exclude("查无此代理", True) is False


# --------------------------------------------------------------------------
# ⑥ 活动计数（限频）
# --------------------------------------------------------------------------

def test_touch限频60秒(lite):
    reg = lite
    reg.register("worker-1")
    assert reg.touch("worker-1", "read") is True
    assert reg.touch("worker-1", "write") is False          # 60s 内不重复写库
    row = reg.get("worker-1")
    assert row["reads"] == 1 and row["writes"] == 0
    assert row["last_seen_at"]


def test_touch写计入写计数(lite):
    reg = lite
    reg.register("worker-1")
    reg.touch("worker-1", "write")
    assert reg.get("worker-1")["writes"] == 1


# --------------------------------------------------------------------------
# ⑦ 本机 bootstrap 通道
# --------------------------------------------------------------------------

def test_本机bootstrap行approved且primary(lite):
    reg = lite
    row = reg.bootstrap_local("main", "主脑", as_primary=True)
    assert row["status"] == STATUS_APPROVED and row["role"] == ROLE_PRIMARY
    assert row["meta"]["bootstrap"] == "local"
    assert reg.verify_local("main")["ok"] is True


def test_本机bootstrap只批准不上位(lite):
    """🔴 MCP / 子代理进程（config 没声明主脑）**只批准、不上位**：
    它能读记忆，但它写 `core` 仍按 sub 降级（既有 MCP 用例钉的就是这一条）。"""
    reg = lite
    row = reg.bootstrap_local("mcp", "mcp")
    assert row["status"] == STATUS_APPROVED and row["role"] == ROLE_SUB
    assert reg.verify_local("mcp")["ok"] is True


def test_已有primary时不再自动上位(lite, caplog):
    """🔴 防「第二个主脑静默上位」：只批准、不抢角色，且冲突必须打 error。"""
    reg = lite
    reg.register("boss")
    reg.set_role("boss", ROLE_PRIMARY)
    with caplog.at_level(logging.ERROR, logger="keepsake.govern.registry"):
        row = reg.bootstrap_local("second", as_primary=True)
    assert row["status"] == STATUS_APPROVED and row["role"] == ROLE_SUB
    assert _roles(reg)["boss"] == ROLE_PRIMARY
    assert any("已有 primary" in r.message for r in caplog.records)


def test_已吊销的行不被本机bootstrap复活(lite):
    """🔴 吊销的语义是「停到人重新批准为止」；每次进程启动都复活它等于架空吊销。"""
    reg = lite
    reg.bootstrap_local("main", as_primary=True)
    reg.revoke("main")
    assert reg.bootstrap_local("main")["status"] == STATUS_REVOKED


def test_verify_local只认本地bootstrap标记(lite):
    reg = lite
    reg.register("remote")
    reg.approve("remote")
    assert reg.verify_local("remote")["ok"] is False      # 批准过，但没走本机通道
    assert reg.verify_local("remote")["reason"] == "not_local"
    assert reg.verify_local("查无此代理")["ok"] is False


# --------------------------------------------------------------------------
# ⑧ 写闸改查注册表（fail-closed 降级探针）
# --------------------------------------------------------------------------

def _provider(storage, *, registry=None, agent_id="worker-1"):
    """裸 provider 实例（不 initialize）：只装写闸要用的那几个属性。"""
    from keepsake.adapters.hermes.provider import KeepsakeProvider

    p = object.__new__(KeepsakeProvider)
    p._config = {}
    p._storage = storage
    p._tag_filter = ""
    p._agent_id = agent_id
    p._is_primary = True                # 🔴 自报 primary —— 但注册表说它是 sub
    p._exclude_tags = ()
    p._registry = registry
    p._role_cache = None
    return p


class _StoreSpy:
    def __init__(self):
        self.store_calls: List[Dict[str, str]] = []

    def store(self, text, tags="", category="", source="", fragment_type="", **_kw):
        self.store_calls.append({"text": text, "tags": tags})
        return True


class _BoomRegistry(Registry):
    """注册表整体不可达（库挂了 / 迁移未完成）—— 写闸必须 fail-closed 按 sub。"""

    def role_of(self, agent_id: str) -> str:
        raise RuntimeError("registry down")

    def verify_local(self, agent_id: str):
        raise RuntimeError("registry down")


def test_写闸按注册表角色而非自报is_primary(lite):
    """🔴 治理收口的核心一行：config 自报 primary，但注册表说是 sub ⇒ 照降级。

    子代理写 core 落候选但**不带提名日戳**（它不是「提名」，班车不自动审它）。
    """
    lite.register("worker-1")
    spy = _StoreSpy()
    _provider(spy, registry=lite).add("子代理写常驻", tags="core")
    tags = spy.store_calls[0]["tags"]
    assert "core_candidate" in tags
    assert not any(t.startswith("tier:auto-hot:") for t in tags.split(",")), tags


def test_写闸在注册表里是primary时落候选带提名戳(lite):
    """注册表说 primary（自报 sub）⇒ 以注册表为准。

    🔴 2026-10 常驻层入口改革：**primary 写 core 也不再直通**（常驻层只剩
    「审核页人工批准」与「候选满观察期自动通过」两个入口），落候选 + 提名日戳。
    两条分支（primary 有戳 / 子代理没戳）的差别，正是「判定源是注册表角色」
    在改革后仍可观测的证据。
    """
    lite.register("main")
    lite.bootstrap_local("main", as_primary=True)
    spy = _StoreSpy()
    p = _provider(spy, registry=lite, agent_id="main")
    p._is_primary = False              # 自报 sub，注册表说 primary ⇒ 以注册表为准
    p.add("主脑写常驻", tags="core,foo")
    tags = spy.store_calls[0]["tags"]
    assert "core_candidate" in tags, tags
    assert "foo" in tags, tags
    assert not tags.startswith("core,"), tags
    assert any(t.startswith("tier:auto-hot:") for t in tags.split(",")), tags


def test_注册表不可达时fail_closed按sub(lite):
    """宁可多拦一个 core 写入，不可放进一个假 primary。"""
    spy = _StoreSpy()
    _provider(spy, registry=_BoomRegistry(spy)).add("常驻", tags="core")
    assert "core_candidate" in spy.store_calls[0]["tags"]


def test_角色缓存5秒内命中不重复查库(lite):
    """approve 后最多 5s 收敛 ⇒ 角色查询有进程内缓存（否则每轮注入都查库）。"""
    lite.register("main")
    p = _provider(_StoreSpy(), registry=lite, agent_id="main")
    assert p._registry_is_primary() is False
    lite.bootstrap_local("main", as_primary=True)
    assert p._registry_is_primary() is False          # 缓存内，仍是旧值
    p._role_cache = None
    assert p._registry_is_primary() is True


# --------------------------------------------------------------------------
# ⑨ Redis 假 client（hash 形态）
# --------------------------------------------------------------------------

class FakeRedisClient:
    """只实现注册表用到的那几个 hash 命令（键值存 bytes，模拟 decode_responses=False）。"""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, bytes]] = {}

    @staticmethod
    def _key(key: Any) -> str:
        return key.decode() if isinstance(key, (bytes, bytearray)) else str(key)

    def exists(self, key):
        return 1 if self._key(key) in self.hashes else 0

    def hgetall(self, key):
        return dict(self.hashes.get(self._key(key), {}))

    def hset(self, key, field=None, value=None, mapping=None):
        slot = self.hashes.setdefault(self._key(key), {})
        pairs = dict(mapping or {})
        if field is not None:
            pairs[field] = value
        for k, v in pairs.items():
            slot[str(k)] = v if isinstance(v, bytes) else str(v).encode()

    def hincrby(self, key, field, amount=1):
        slot = self.hashes.setdefault(self._key(key), {})
        now = int(slot.get(str(field), b"0") or 0)
        slot[str(field)] = str(now + int(amount)).encode()
        return now + int(amount)

    def scan_iter(self, match="*"):
        prefix = str(match).rstrip("*")
        for key in sorted(self.hashes):
            if key.startswith(prefix):
                yield key

    def renamenx(self, key, new_key):
        """原子改名：目标已存在 ⇒ 0（**不覆盖**）；源不存在 ⇒ 0。"""
        src = self._key(key)
        dst = self._key(new_key)
        if src not in self.hashes or dst in self.hashes:
            return 0
        self.hashes[dst] = self.hashes.pop(src)
        return 1


class FakeRedisStorage:
    """Redis 后端替身（只有 `_get_client`，正是 `_backend_of` 认 Redis 的判据）。"""

    def __init__(self) -> None:
        self.client = FakeRedisClient()

    def _get_client(self):
        return self.client


@pytest.fixture()
def red():
    storage = FakeRedisStorage()
    reg = Registry(storage)
    reg.ensure_schema()
    yield reg, storage


def test_redis_register与默认pending(red):
    reg, storage = red
    reg.register("worker-1", "工人")
    assert set(storage.client.hashes) == {"keepsake:agent:worker-1"}
    assert reg.get("worker-1")["status"] == STATUS_PENDING


def test_redis_批准凭据双向(red):
    reg, _ = red
    reg.register("worker-1")
    credential = reg.approve("worker-1")
    assert reg.verify("worker-1", credential)["ok"] is True
    assert reg.verify("worker-1", "ksk_" + "0" * 32)["ok"] is False
    reg.revoke("worker-1")
    assert reg.verify("worker-1", credential)["ok"] is False


def test_redis_唯一primary(red):
    reg, _ = red
    reg.register("a"), reg.register("b")
    reg.set_role("a", ROLE_PRIMARY)
    assert reg.set_role("b", ROLE_PRIMARY) == ["a"]
    assert [k for k, v in _roles(reg).items() if v == ROLE_PRIMARY] == ["b"]


def test_redis_迁移器与排除开关(red):
    reg, _ = red
    reg.migrate_exclude_tags({"retrieval": {"exclude_tags": "core_candidate,agent:worker-1"}})
    assert reg.get("worker-1")["exclude_from_primary"] is True
    assert reg.get("worker-1")["meta"] == {}      # 迁移不写 meta


def test_redis_活动计数与限频(red):
    reg, _ = red
    reg.register("worker-1")
    assert reg.touch("worker-1", "read") is True
    assert reg.touch("worker-1", "write") is False
    row = reg.get("worker-1")
    assert row["reads"] == 1 and row["writes"] == 0


def test_redis_不可达时报错而不是静默放行():
    class _Down(FakeRedisStorage):
        def _get_client(self):
            return None

    reg = Registry(_Down())
    with pytest.raises(RuntimeError):
        reg.list_agents()


# --------------------------------------------------------------------------
# ⑩ PG 真库（隔离测试库；没凭据 / 没装驱动就 skip）
# --------------------------------------------------------------------------

ENV_PATH = Path.home() / ".keepsake_pg_test.env"
ALLOWED_HOST = "8.140.192.91"
ALLOWED_DBNAME = "keepsake_test"


def _pg_dsn() -> str:
    if not ENV_PATH.exists():
        return ""
    m = re.search(r"KEEPSAKE_PG_DSN=(.*)", ENV_PATH.read_text(encoding="utf-8"))
    return m.group(1).strip().strip("\"'") if m else ""


def _pg_dsn_is_safe(dsn: str) -> bool:
    """只允许连指定测试库（防手滑连生产）。"""
    return bool(dsn) and ALLOWED_HOST in dsn and ALLOWED_DBNAME in dsn


pytest.importorskip("psycopg", reason="未装 psycopg，跳过 PG 注册表真库测试")
_PG_DSN = _pg_dsn()
if not _pg_dsn_is_safe(_PG_DSN):
    pytest.skip(f"无测试库凭据或指向非 {ALLOWED_HOST}/{ALLOWED_DBNAME}，跳过 PG 真库测试",
                allow_module_level=True)

#: 本模块在 PG 上用到的代理 id（跑完按 id 清干净 —— 那是别人的库，不许留残渣）
_PG_IDS = ("pg-registry-worker", "pg-registry-main", "pg-registry-x")


@pytest.fixture(scope="module")
def pg_registry():
    from keepsake.core.pg import PgStorage

    storage = PgStorage(dsn=_PG_DSN, connect_timeout=30)
    reg = Registry(storage)
    assert reg.ensure_schema() is True
    yield reg
    for aid in _PG_IDS:
        try:
            with storage._tx() as cur:      # noqa: SLF001 —— 经原后端连线，不另开连接
                cur.execute("DELETE FROM ks_agent WHERE agent_id = %s", (aid,))
        except Exception as e:               # noqa: BLE001 —— 清理失败不许掩盖用例结论
            print(f"[warn] PG 清理 {aid} 失败：{type(e).__name__}")
    for aid in _PG_IDS:
        assert reg.get(aid) is None, f"PG 测试库残留：{aid}"
    storage.close()


def test_pg_登记批准凭据与唯一primary(pg_registry):
    reg = pg_registry
    assert reg.backend == "postgres"
    reg.register("pg-registry-worker")
    reg.register("pg-registry-main")
    credential = reg.approve("pg-registry-worker")
    assert credential.startswith(CREDENTIAL_PREFIX)
    assert reg.verify("pg-registry-worker", credential)["ok"] is True
    assert reg.verify("pg-registry-worker", "ksk_" + "0" * 32)["ok"] is False

    reg.set_role("pg-registry-main", ROLE_PRIMARY)
    reg.register("pg-registry-x")
    assert reg.set_role("pg-registry-x", ROLE_PRIMARY) == ["pg-registry-main"]
    assert [a["agent_id"] for a in reg.list_agents()
            if a["role"] == ROLE_PRIMARY] == ["pg-registry-x"]


def test_pg_本机bootstrap与吊销不复活(pg_registry):
    reg = pg_registry
    row = reg.bootstrap_local("pg-registry-main", as_primary=True)
    assert row["status"] == STATUS_APPROVED
    assert row["meta"].get("bootstrap") == "local"
    reg.revoke("pg-registry-main")
    assert reg.bootstrap_local("pg-registry-main")["status"] == STATUS_REVOKED


def test_pg_库里没有明文凭据(pg_registry):
    """🔴 凭据纪律：真库里也只有 sha256。"""
    reg = pg_registry
    reg.register("pg-registry-worker")
    # 🔴 上一条用例已批准过这一行（模块级夹具，PG 里同一批 id 复用）⇒
    #    换发要**显式** reissue=True（默认档是「已批准 ⇒ 拒」，不是这里要测的东西）。
    credential = reg.approve("pg-registry-worker", reissue=True)
    row = reg._be.load("pg-registry-worker")     # noqa: SLF001 —— 直读后端验纪律
    assert row["credential_hash"] == hash_credential(credential)
    assert credential not in json.dumps(row, ensure_ascii=False)
    assert os.environ.get("KEEPSAKE_PG_DSN", "") == ""    # 凭据只来自文件，不来自环境


# --------------------------------------------------------------------------
# ⑧ 主脑路径的活动计数（provider 侧挂点）
# --------------------------------------------------------------------------

def _provider_with_registry(tmp_path, agent_id: str):
    """一个**已装配注册表**的 provider（不跑完整 initialize：那条要连 Redis/embedding）。

    只摆出 `_storage` / `_registry` / `_agent_id` 三件套 —— 与 `_init_governance`
    装配完之后的状态同形，够跑读写路径。
    """
    from keepsake.adapters.hermes.provider import KeepsakeProvider
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / f"{agent_id}.db"))
    assert st.ensure_index() is True
    reg = Registry(st)
    reg.ensure_schema()
    reg.bootstrap_local(agent_id, agent_id, as_primary=True)   # 主脑本机 bootstrap
    p = KeepsakeProvider()
    p._storage = st
    p._registry = reg
    p._agent_id = agent_id
    return p, reg, st


def test_主脑检索路径记read计数(tmp_path):
    """🔴 主脑每轮都在检索，reads 恒为 0 就是挂点漏了。

    `Registry.touch()` 原先只有 MCP 闸门一个调用点（子代理连上来的那条路）；
    主脑是同进程直接调 provider、**从不经过 MCP**，所以计数必须在这里自己打。
    """
    p, reg, st = _provider_with_registry(tmp_path, "main-brain")
    try:
        # 直接经存储层落数据，**不走 provider.add** —— add 自己也会打一次活动，
        # 而 `touch()` 是 60s 限频的（按 agent_id 计），先把额度用掉就测不出读计数了。
        st.store("主脑写入的一条记忆")
        before = reg.get("main-brain")["reads"]
        p.search("主脑")
        after = reg.get("main-brain")["reads"]
        assert after == before + 1, "provider.search() 没有打活动计数"
    finally:
        st.close()


def test_主脑写入路径记write计数(tmp_path):
    p, reg, st = _provider_with_registry(tmp_path, "main-brain")
    try:
        assert p.add("第二条记忆")
        row = reg.get("main-brain")
        assert row["writes"] == 1, "provider.add() 没有打活动计数"
        assert row["last_seen_at"], "打活动必须顺带刷新 last_seen_at"
    finally:
        st.close()


def test_写入失败不记write计数(tmp_path):
    """计数是**落库**活动，不是**尝试**活动：写失败不该把 writes 撑起来。"""
    p, reg, st = _provider_with_registry(tmp_path, "main-brain")
    try:
        def _boom():
            raise RuntimeError("后端不可达")

        p._require_storage = _boom
        assert p.add("写不进去的一条") == ""
        assert reg.get("main-brain")["writes"] == 0
    finally:
        st.close()


def test_没装配注册表时不记也不崩():
    """裸实例（`_registry is None`）必须安静跳过 —— 治理不许把对话带崩。"""
    from keepsake.adapters.hermes.provider import KeepsakeProvider

    p = KeepsakeProvider()
    p._registry = None
    p._agent_id = "main-brain"
    p._touch("read")      # 不抛即通过


class _TouchSpy:
    """只记 `touch()` 调用的注册表替身 —— 绕开 60s 限频，能逐次断言。"""

    def __init__(self):
        self.calls = []

    def touch(self, agent_id, kind="read"):
        self.calls.append((agent_id, kind))
        return True


def test_prefetch真检索记read而skip路径不记(tmp_path):
    """🔴 主脑每轮注入走 `prefetch()`，它**不经** `self.search()` 直调存储层 ——
    D 单只挂 search()/get() 时，主脑 reads 实测恒 0（面板「主代理 0/0」）。

    两条判据一起守：真检索要记；被 `_should_search` 挡掉的问题**不许**记
    （skip 是没发生检索，不是检索了没结果）。用 spy 而非真注册表，
    否则 60s 限频会把「误挂在 skip 路径上」这个 bug 吃掉。
    """
    from keepsake.adapters.hermes.provider import KeepsakeProvider
    from keepsake.core.sqlite import SqliteStorage

    st = SqliteStorage(path=str(tmp_path / "prefetch.db"))
    assert st.ensure_index() is True
    spy = _TouchSpy()
    p = KeepsakeProvider()
    p._storage = st
    p._registry = spy
    p._agent_id = "main-brain"
    p._skip_min_length = 2
    p._skip_patterns = set()
    p._tag_filter = ""
    p._exclude_tags = ()
    try:
        st.store("主脑写入的一条记忆")
        p.prefetch("主脑")
        assert spy.calls == [("main-brain", "read")], "prefetch 真检索没有打 read 计数"

        spy.calls.clear()
        assert p.prefetch("") == ""          # 长度不足 ⇒ skip
        assert spy.calls == [], "skip 掉的问题不该记检索活动"
    finally:
        st.close()


# --------------------------------------------------------------------------
# ⑪ 改 id（换主键）：唯一性判断 + 凭据原样搬（Redis 侧）
# --------------------------------------------------------------------------

def test_redis_换id整行搬走凭据不变(red):
    """🔴 改 id = 换主键：整行搬到新键下，**凭据原样搬** ⇒ 已发的凭据继续有效。

    Redis 侧的 key 布局沿用原样（`keepsake:agent:<id>` hash），不另造键格式。
    """
    reg, storage = red
    reg.register("worker-1", "工人")
    credential = reg.approve("worker-1")
    assert reg.set_id("worker-1", "worker-2") is True
    assert reg.get("worker-1") is None
    row = reg.get("worker-2")
    assert row["name"] == "工人" and row["status"] == "approved"
    # 凭据不换：旧凭据还能用，反查出来的是**新 id**
    assert reg.verify_any(credential)["agent_id"] == "worker-2"
    assert reg.verify("worker-2", credential)["ok"] is True
    assert reg.verify("worker-1", credential)["ok"] is False
    assert set(storage.client.hashes) == {"keepsake:agent:worker-2"}


def test_redis_换id唯一性判断是第一职责(red):
    """🔴 行不存在 / 新 id 已存在 / 新旧同串 ⇒ 一律 False（**绝不覆盖**别人的行）。"""
    reg, _ = red
    reg.register("worker-1")
    reg.register("worker-2")
    assert reg.set_id("worker-1", "worker-2") is False      # 新 id 已存在
    assert reg.set_id("worker-1", "worker-1") is False      # 新旧同串
    assert reg.set_id("查无此代理", "worker-3") is False     # 行不存在
    assert reg.get("worker-1") is not None
    assert reg.get("worker-2") is not None


def test_sqlite_换id整行搬走凭据不变(lite):
    """SQL 侧同款语义（`UPDATE ks_agent SET agent_id=...` ⇒ 其余列一个都不动）。"""
    reg = lite
    reg.register("worker-1", "工人")
    credential = reg.approve("worker-1")
    assert reg.set_id("worker-1", "worker-2") is True
    assert reg.get("worker-1") is None
    row = reg.get("worker-2")
    assert row["name"] == "工人" and row["status"] == "approved"
    assert reg.verify_any(credential)["agent_id"] == "worker-2"
    assert reg.set_id("worker-2", "worker-1") is True       # 换回去
    assert reg.get("worker-1") is not None
