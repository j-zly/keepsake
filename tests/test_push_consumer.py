"""provider 侧 SSE 挂流消费者（2026-10 ks_push_consumer）的测试。

四条判据（对应任务书）：

1. 收到 `event: dm` 帧 ⇒ **水位缓存失效** + 回调收到**摘要 dict**（无正文键）；
2. 断流 ⇒ **退避后二次连接**（指数退避的第一跳就够验，不必真等 5s）；
3. `enabled=false` ⇒ **零线程零连接**（回归：没开的用户一个字节都不多跑）；
4. 消费者 / 回调抛异常 ⇒ provider 其余功能**照常**（fail-open 钉桩）。

外加**通知出口**（2026-10 ks_notify_outlet）五条判据，见文末 ⑥ 节：
dm 帧 ⇒ 通知 POST（只含元数据）/ 同水位限频 / `notify_url` 空零外呼 /
通知失败不影响水位与主路径 / feishu 载荷结构。

再外加**唤醒出口**（2026-10 ks_wake_outlet）六条判据，见文末 ⑦ 节：
dm 帧 ⇒ 唤醒 POST（签名可复算 / 载荷三键）/ `wake_url` 空零外呼 /
唤醒失败不影响水位与通知 / 追帧摘要不重复唤醒 / HMAC 对照向量 /
`dm-nudge` 每帧必唤醒。

全程 hermetic：模拟 SSE 服务端是 **stdlib http.server**（127.0.0.1 随机端口），
不连真服务端、不连 Redis / PG / 生产库；凭据是**假串**（不落任何真值）。

🔴 为什么用 **stdlib http.server** 而不是 ASGI 直调：SSE 是**流式**响应，
httpx 的 `ASGITransport` 会把整个 body 缓冲下来 —— 对一条永远不结束的流来说
那就是挂死。真 HTTP 才能一行一行地读帧（与 `tests/mcp/test_dm_sse.py` 同款理由）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from keepsake.adapters.hermes import provider as prov_mod
from keepsake.adapters.hermes.provider import KeepsakeProvider
from keepsake.core.sqlite import SqliteStorage
from keepsake.govern.events import EVENT_DM, EVENT_DM_NUDGE, frame
from keepsake.govern.messaging import Messaging

#: 假凭据（**不是**真值 —— 任务书红线：凭据不许进任何夹具）
FAKE_CREDENTIAL = "ksk_fake_push_credential_for_tests"

#: 检索词（与入库正文对得上，命中才有「相关记忆」段落）
QUERY = "回滚 流程 校验"


# --------------------------------------------------------------------------
# 夹具 / 帮手
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_push_state():
    """推送消费者与回调表是**进程级**的 ⇒ 用例之间必须清干净。"""
    prov_mod._stop_push_consumer()                      # noqa: SLF001
    prov_mod._PUSH_CALLBACKS.clear()                    # noqa: SLF001
    prov_mod._DM_HINT_CACHE.update({"ts": 0.0, "key": None})   # noqa: SLF001
    prov_mod._NOTIFY_CACHE.update({"ts": 0.0, "count": None})  # noqa: SLF001
    prov_mod._WAKE_LAST_SEQ = -1                        # noqa: SLF001
    yield
    prov_mod._stop_push_consumer()                      # noqa: SLF001
    prov_mod._PUSH_CALLBACKS.clear()                    # noqa: SLF001


@pytest.fixture()
def cred_env(monkeypatch):
    """凭据经**环境变量**注入（config.json 只写来源，不落真值）。"""
    monkeypatch.setenv("TEST_PUSH_CREDENTIAL", FAKE_CREDENTIAL)
    return FAKE_CREDENTIAL


def _provider(storage=None) -> KeepsakeProvider:
    """一个只挂了 storage 的 provider（跳过 initialize，不连任何后端）。"""
    p = object.__new__(KeepsakeProvider)
    p._config, p._storage, p._tag_filter = {}, storage, ""
    p._is_primary = True
    p._exclude_tags = ()
    p._agent_id = "agent-b"
    p._keepsake_messaging = Messaging(storage) if storage is not None else None
    p._skip_min_length = 2
    p._skip_patterns = set()
    p._role_cache = None
    return p


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    """等条件成立（后台线程 ⇒ 只能等，不能断言同步）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _MockSse:
    """极简 SSE 模拟服务端：按「剧本」逐轮发帧，剧本发完就 EOF（断流）。

    `scripts` 是**逐连接**的剧本列表：第 1 次连接拿第 1 个剧本，以此类推；
    剧本用完后每次连接都是「立刻 EOF」。
    """

    def __init__(self, scripts: List[List[str]]) -> None:
        self._scripts = list(scripts)
        self._lock = threading.Lock()
        self.connections: List[str] = []      # 每次连接带的 Authorization 头
        self._server: Any = None
        self.base_url = ""

    def __enter__(self) -> "_MockSse":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):                      # noqa: N802 —— BaseHTTPRequestHandler 的命名
                with outer._lock:
                    outer.connections.append(self.headers.get("Authorization") or "")
                    script = outer._scripts.pop(0) if outer._scripts else []
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in script:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                self.close_connection = True        # 不带 Content-Length ⇒ 以 EOF 收尾

            def log_message(self, *args):           # 静音（别把测试日志冲花）
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        return False


def _dm_frame(seq: int = 7, from_id: str = "worker-1", unread: int = 2) -> str:
    return frame(EVENT_DM, {"seq": seq, "from": from_id, "unread": unread})


def _dm_frame_with_body(seq: int = 7, from_id: str = "worker-1",
                         unread: int = 2) -> str:
    """一帧**额外带 body 字段**的 dm —— 用来证明通知出口不会把正文带出去。

    线上帧本来就不带正文（`publish_dm` 只放 seq/from/unread）；这一帧是**故意
    多带一个 body** 的探针：出口只认元数据 ⇒ 正文既不进载荷也不进日志。
    """
    return frame(EVENT_DM, {"seq": seq, "from": from_id, "unread": unread,
                            "body": "机密正文不该出现在通知里"})


# --------------------------------------------------------------------------
# ① 收到 dm 帧 ⇒ 水位失效 + 回调拿到摘要（无正文）
# --------------------------------------------------------------------------

def test_收到dm帧_水位失效且回调拿到摘要(cred_env, caplog):
    """🔴 一帧 `event: dm` ⇒ 水位缓存失效 + 回调拿到 `{event,seq,from,unread}`。"""
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)

        # 先把水位填成「刚刚提醒过」的形状 ⇒ 帧到了必须把它作废
        prov_mod._DM_HINT_CACHE.update({"ts": time.monotonic(),   # noqa: SLF001
                                        "key": (2, ("worker-1",))})
        with caplog.at_level(logging.INFO, logger="keepsake"):
            p._init_push_consumer({"push_consumer": {
                "enabled": True, "base_url": srv.base_url,
                "credential_env": "TEST_PUSH_CREDENTIAL",
            }})
            assert _wait_until(lambda: bool(got)), "消费者没收到帧"

    summary = got[0]
    assert set(summary) == {"event", "seq", "from", "unread"}, summary
    assert summary["event"] == EVENT_DM
    assert summary["seq"] == 7 and summary["from"] == "worker-1" and summary["unread"] == 2
    # 🔴 摘要里不许有正文（私信正文永远走信箱本体）
    assert "content" not in summary and "body" not in summary
    assert "正文" not in json.dumps(summary, ensure_ascii=False)

    # 水位已失效 ⇒ 下一轮 prefetch 必重新查库并提醒（不等 60s 窗口）
    assert prov_mod._DM_HINT_CACHE["key"] is None      # noqa: SLF001

    # 凭据走 Authorization 头（真值只在 env，config 里只有来源）
    assert srv.connections and srv.connections[0] == f"Bearer {FAKE_CREDENTIAL}"

    # 一行摘要日志（无正文）
    assert any("收到推送事件" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]


def test_dm_nudge帧同样触发(cred_env):
    """🔴 `event: dm-nudge`（人工点提醒）走同一条路。"""
    with _MockSse([[frame(EVENT_DM_NUDGE, {"seq": 3, "from": "", "unread": 1})]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        assert _wait_until(lambda: bool(got))
    assert got[0]["event"] == EVENT_DM_NUDGE and got[0]["unread"] == 1


# --------------------------------------------------------------------------
# ② 断流 ⇒ 退避后二次连接
# --------------------------------------------------------------------------

def test_断流后退避重连(cred_env, monkeypatch):
    """🔴 首轮 EOF ⇒ 退避后**二次连接**（重连是真能力，不是日志里的一句承诺）。"""
    monkeypatch.setattr(prov_mod, "_PUSH_RECONNECT_BASE_S", 0.05)   # noqa: SLF001
    monkeypatch.setattr(prov_mod, "_PUSH_RECONNECT_MAX_S", 0.2)     # noqa: SLF001
    with _MockSse([[], [_dm_frame()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        assert _wait_until(lambda: bool(got)), "断流后没有二次连接"

    assert len(srv.connections) >= 2, srv.connections
    assert got[0]["seq"] == 7


# --------------------------------------------------------------------------
# ③ enabled=false ⇒ 零线程零连接（回归）
# --------------------------------------------------------------------------

def test_关闭时零线程零连接(cred_env):
    """🔴 没开的用户：一个线程都不起、一个连接都不发。"""
    before = threading.active_count()
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        p._init_push_consumer({"push_consumer": {
            "enabled": False, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        time.sleep(0.3)
        assert srv.connections == [], "关闭状态不该发任何连接"
        assert prov_mod._PUSH_CONSUMER is None            # noqa: SLF001
        assert threading.active_count() <= before + 1     # 没有新线程（+1 留给 pytest 自身）
        assert not any(t.name == "keepsake-push" for t in threading.enumerate())


def test_缺凭据时不挂流(cred_env, monkeypatch):
    """🔴 开了但没有凭据 ⇒ **不挂流**（不拿空凭据去连，也不抛）。"""
    monkeypatch.delenv("TEST_PUSH_CREDENTIAL", raising=False)
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        time.sleep(0.3)
        assert srv.connections == []
        assert prov_mod._PUSH_CONSUMER is None            # noqa: SLF001


# --------------------------------------------------------------------------
# ④ 消费者 / 回调抛异常 ⇒ provider 其余功能照常（fail-open）
# --------------------------------------------------------------------------

@pytest.fixture()
def st(tmp_path):
    """真 SqliteStorage（临时文件）。"""
    storage = SqliteStorage(path=str(tmp_path / "push.db"), is_primary=True)
    assert storage.ensure_index() is True
    storage.store(text=f"{QUERY} 的正式条目", tags="shared",
                  category="probe", source="probe")
    yield storage
    storage.close()


def test_回调抛异常不影响其余回调与主路径(cred_env, st):
    """🔴 一个回调炸了 ⇒ 只记日志；其余回调照收，provider 照常干活。"""
    with _MockSse([[_dm_frame()], [_dm_frame(seq=8)]]) as srv:
        p = _provider(st)
        seen: List[Dict[str, Any]] = []

        def _boom(_summary):
            raise RuntimeError("宿主的回调炸了")

        p.on_push_event(_boom)
        p.on_push_event(seen.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        assert _wait_until(lambda: bool(seen)), "回调异常不该连累后面的回调"

    # provider 其余功能照常（检索路径一个字节没变）
    out = p.prefetch(QUERY)
    assert "相关记忆" in out, out
    assert p.add("另一条 记忆 写入") != ""


def test_服务端连不上时provider照常(st):
    """🔴 推送层彻底不可用（连不上）⇒ 记忆读写一行都不受影响。"""
    p = _provider(st)
    # 127.0.0.1:1 —— 必然连不上
    p._init_push_consumer({"push_consumer": {
        "enabled": True, "base_url": "http://127.0.0.1:1",
        "credential_env": "TEST_PUSH_CREDENTIAL",
    }})
    time.sleep(0.3)
    out = p.prefetch(QUERY)
    assert "相关记忆" in out, out
    assert p.add("另一条 记忆 写入") != ""
    assert p.search("回滚 流程 校验")


def test_消费者初始化失败不影响provider(st, monkeypatch):
    """🔴 消费者装配阶段炸了 ⇒ provider 初始化**不许失败**（fail-open 钉桩）。"""

    def _boom(*_a, **_kw):
        raise RuntimeError("起线程失败")

    monkeypatch.setattr(prov_mod, "_PushConsumer", _boom)
    p = _provider(st)
    p._init_push_consumer({"push_consumer": {
        "enabled": True, "base_url": "http://127.0.0.1:1",
        "credential_env": "TEST_PUSH_CREDENTIAL",
    }})                                        # 不抛 ⇒ 主路径不受影响
    assert "相关记忆" in p.prefetch(QUERY)


# --------------------------------------------------------------------------
# ⑤ 契约层：可选扩展不许绑死宿主
# --------------------------------------------------------------------------

def test_可选契约不进基础契约():
    """🔴 `on_push_event` 是**可选**扩展 —— 基础契约不许因此多一个必实现成员。"""
    from keepsake.adapters.generic.protocol import MemoryProvider, PushEventConsumer

    members = sorted(getattr(MemoryProvider, "__protocol_attrs__", None)
                     or [n for n in vars(MemoryProvider) if not n.startswith("_")])
    assert "on_push_event" not in members, members
    assert "on_push_event" in sorted(
        getattr(PushEventConsumer, "__protocol_attrs__", None)
        or [n for n in vars(PushEventConsumer) if not n.startswith("_")])


def test_通用契约层不import_hermes():
    """🔴 依赖方向铁律：可选契约不许把契约层绑死 Hermes。"""
    import ast
    from pathlib import Path

    import keepsake.adapters.generic as generic_pkg

    pkg_dir = Path(generic_pkg.__file__).resolve().parent
    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.level:
                mod = ("." * node.level) + (node.module or "")
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            assert "hermes" not in (mod or ""), f"{path.name} 引用了 {mod}"


# --------------------------------------------------------------------------
# ⑥ 通知出口（2026-10 ks_notify_outlet）—— 空闲期把人叫醒
#
# 五条判据（对应任务书）：
#   ① dm 帧 ⇒ mock 收到 notify POST，载荷含条数、**无正文**；
#   ② 同水位限频窗口内第二帧不重复喊，水位升高立刻再喊；
#   ③ `notify_url` 空 ⇒ **零外呼**（回归）；
#   ④ notify POST 抛异常 ⇒ 水位照常置、消费者不死；
#   ⑤ feishu 格式载荷结构断言。
#
# 全程 hermetic：`httpx.post` 被 monkeypatch 拦下 ⇒ **不连真渠道**；
# 通知 URL 用假地址（`.invalid` 保留域，永不可解析）。
# --------------------------------------------------------------------------

#: 假通知 URL（**不是**真值 —— 任务书红线：URL 真值不得进提交物）。
FAKE_NOTIFY_URL = "http://notify.invalid/hook"


@pytest.fixture()
def notify_calls(monkeypatch):
    """拦下通知出口的 HTTP POST —— 记录载荷，不连真渠道。"""
    import httpx

    calls: List[Dict[str, Any]] = []

    def _fake_post(url, json=None, timeout=None, **_kw):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return None

    monkeypatch.setattr(httpx, "post", _fake_post)
    return calls


def test_dm帧触发通知且载荷只含元数据(cred_env, notify_calls):
    """🔴 ① dm 帧 ⇒ 恰好一次 notify POST；载荷只有条数/发件人，**没有正文**。"""
    with _MockSse([[_dm_frame_with_body()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "notify_url": FAKE_NOTIFY_URL,
        }})
        assert _wait_until(lambda: bool(notify_calls)), "消费者没发出通知"

    assert len(notify_calls) == 1, notify_calls
    call = notify_calls[0]
    assert call["url"] == FAKE_NOTIFY_URL
    assert call["timeout"] == 5
    # 载荷**逐字**断言：只含条数与发件人，多一个字都不行
    assert call["json"] == {"text": "📩 你有 2 条未读私信（来自: worker-1）"}, call["json"]
    # 🔴 红线：载荷里**绝不能有私信正文**（帧里带的 body 也不许被带出去）
    assert "机密正文" not in json.dumps(call["json"], ensure_ascii=False)


def test_同水位限频且水位升高立刻再喊(cred_env, notify_calls):
    """🔴 ② 同水位（unread 不变）窗口内不重复喊；unread 升高立刻再喊。"""
    with _MockSse([[_dm_frame(seq=7, unread=2),
                    _dm_frame(seq=8, unread=2),      # 同水位 ⇒ 窗口内不喊
                    _dm_frame(seq=9, unread=3)]]) as srv:   # 升高 ⇒ 立刻喊
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "notify_url": FAKE_NOTIFY_URL,
            "notify_min_interval_s": 120,
        }})
        assert _wait_until(lambda: len(got) == 3 and len(notify_calls) >= 2)
        time.sleep(0.3)                    # 给「本该被限频掉的那一帧」留机会

    assert len(notify_calls) == 2, notify_calls
    texts = [c["json"]["text"] for c in notify_calls]
    assert "2 条" in texts[0] and "3 条" in texts[1], texts


def test_notify_url为空时零外呼(cred_env, notify_calls):
    """🔴 ③ `notify_url` 空 ⇒ **零外呼**（回归：现状逐字不变）。"""
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
        }})
        assert _wait_until(lambda: bool(got)), "帧都没收到（回归坏了）"
        time.sleep(0.2)
    assert notify_calls == [], notify_calls


def test_通知失败不影响水位与消费者(cred_env, monkeypatch, st, caplog):
    """🔴 ④ notify POST 抛异常 ⇒ 水位照常置、消费者不死、主路径照常。"""
    import httpx

    def _boom(url, json=None, timeout=None, **_kw):
        raise RuntimeError("渠道挂了")

    monkeypatch.setattr(httpx, "post", _boom)
    with _MockSse([[_dm_frame(seq=7, unread=2),
                    _dm_frame(seq=8, unread=3)]]) as srv:
        p = _provider(st)
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        with caplog.at_level(logging.WARNING, logger="keepsake"):
            p._init_push_consumer({"push_consumer": {
                "enabled": True, "base_url": srv.base_url,
                "credential_env": "TEST_PUSH_CREDENTIAL",
                "notify_url": FAKE_NOTIFY_URL,
            }})
            assert _wait_until(lambda: len(got) == 2), "通知失败不该连累消费者"

    # 水位照常置（失效）⇒ 下一轮 prefetch 必重新查库并提醒
    assert prov_mod._DM_HINT_CACHE["key"] is None      # noqa: SLF001
    # 失败只记 warning，不抛
    assert any("私信通知发送失败" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]
    # provider 其余功能照常
    assert "相关记忆" in p.prefetch(QUERY)


def test_feishu格式载荷结构(cred_env, notify_calls):
    """🔴 ⑤ `notify_format=feishu` ⇒ 载荷是飞书群机器人的形状。"""
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "notify_url": FAKE_NOTIFY_URL,
            "notify_format": "feishu",
        }})
        assert _wait_until(lambda: bool(notify_calls))

    payload = notify_calls[0]["json"]
    assert payload["msg_type"] == "text"
    assert payload["content"]["text"].startswith("📩")
    assert "2" in payload["content"]["text"]
    # 载荷里仍然没有正文
    assert "机密正文" not in json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# ⑦ 唤醒出口（2026-10 ks_wake_outlet）—— 叫醒宿主会话去读信
#
# 六条判据（对应任务书）：
#   ① dm 帧 ⇒ mock 收到 wake POST，头含 `X-Hub-Signature-256: sha256=…`
#      且 HMAC 可用测试 secret 复算一致，载荷三键齐（from/unread/seq）；
#   ② `wake_url` 空 ⇒ **零外呼**（回归）；
#   ③ POST 非 2xx / 抛异常 ⇒ 水位与 notify 照常、消费者不死；
#   ④ 追帧摘要（重连补报的同一份状态）**不触发** wake；
#   ⑤ HMAC 对照向量钉死（固定 secret + body ⇒ 期望 hex 常数）；
#   ⑥ `dm-nudge` 帧（人工催办）**每帧必** wake（不参与 seq 去重）。
#
# 全程 hermetic：`httpx.post` 被 monkeypatch 拦下 ⇒ **不连真网关**；
# wake URL / 密钥都是**假值**（`.invalid` 保留域 + 假 secret，任务书红线：
# 密钥真值不得进提交物）。
# --------------------------------------------------------------------------

#: 假唤醒 URL（**不是**真值 —— 任务书红线：URL 真值不得进提交物）。
FAKE_WAKE_URL = "http://wake.invalid/hook"

#: 假订阅密钥（**不是**真值 —— 同上）。
FAKE_WAKE_SECRET = "test-wake-secret-not-a-real-one"

#: HMAC 对照向量：固定 secret + 固定 body ⇒ 固定 hex（钉死签名口径）。
HMAC_VECTOR_SECRET = FAKE_WAKE_SECRET
HMAC_VECTOR_BODY = b'{"from":"worker-1","unread":2,"seq":7}'
HMAC_VECTOR_HEX = "58189a8b2c9bd475a868df6f2847df218c168321f875d0400173853785085636"


@pytest.fixture()
def wake_env(monkeypatch):
    """订阅密钥经**环境变量**注入（config.json 只写来源，不落真值）。"""
    monkeypatch.setenv("TEST_WAKE_SECRET", FAKE_WAKE_SECRET)
    return FAKE_WAKE_SECRET


class _FakeResp:
    """最小响应替身（`httpx.post` 的返回物只需要 `status_code`）。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


@pytest.fixture()
def wake_calls(monkeypatch):
    """拦下唤醒出口的 HTTP POST —— 记录**原始字节**与头，不连真网关。

    同时兼容通知出口（`json=`）—— 两条旁路共用 `httpx.post`。
    """
    import httpx

    calls: List[Dict[str, Any]] = []

    def _fake_post(url, json=None, timeout=None, content=None, headers=None, **_kw):
        calls.append({"url": url, "json": json, "content": content,
                      "headers": dict(headers or {}), "timeout": timeout})
        return _FakeResp(200)

    monkeypatch.setattr(httpx, "post", _fake_post)
    return calls


def _wakes(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只挑出唤醒出口的 POST（通知出口走的是同一个 `httpx.post`）。"""
    return [c for c in calls if c["url"] == FAKE_WAKE_URL]


def test_dm帧触发唤醒且签名与载荷正确(cred_env, wake_env, wake_calls):
    """🔴 ① dm 帧 ⇒ 恰好一次 wake POST；签名可复算；载荷只有三键（无正文）。"""
    with _MockSse([[_dm_frame_with_body(seq=7, unread=2)]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "wake_url": FAKE_WAKE_URL,
            "wake_secret_env": "TEST_WAKE_SECRET",
        }})
        assert _wait_until(lambda: bool(_wakes(wake_calls))), "消费者没发出唤醒"

    wakes = _wakes(wake_calls)
    assert len(wakes) == 1, wakes
    call = wakes[0]
    assert call["timeout"] == 5
    # 载荷：**逐字**断言三键（from / unread / seq），多一个字都不行
    body = call["content"]
    assert isinstance(body, bytes), body
    assert json.loads(body.decode("utf-8")) == {"from": "worker-1",
                                                "unread": 2, "seq": 7}
    # 🔴 红线：载荷里绝不能有私信正文（帧里带的 body 也不许被带出去）
    assert "机密正文" not in body.decode("utf-8")
    # 签名头：HMAC-SHA256(raw body bytes, secret) 可用测试 secret 复算一致
    sig = call["headers"].get("X-Hub-Signature-256") or ""
    assert sig.startswith("sha256="), call["headers"]
    import hashlib
    import hmac
    expect = hmac.new(FAKE_WAKE_SECRET.encode("utf-8"), body,
                      hashlib.sha256).hexdigest()
    assert sig == f"sha256={expect}", (sig, expect)
    assert call["headers"].get("Content-Type") == "application/json"


def test_wake_url为空时零外呼(cred_env, wake_env, wake_calls):
    """🔴 ② `wake_url` 空 ⇒ **零外呼**（回归：现状逐字不变）。"""
    with _MockSse([[_dm_frame()]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "wake_secret_env": "TEST_WAKE_SECRET",
        }})
        assert _wait_until(lambda: bool(got)), "帧都没收到（回归坏了）"
        time.sleep(0.2)
    assert wake_calls == [], wake_calls


def test_唤醒失败不影响水位与通知且消费者不死(cred_env, wake_env, monkeypatch,
                                                  caplog, st):
    """🔴 ③ wake POST 抛异常 / 非 2xx ⇒ 水位照常、通知照常、消费者不死。"""
    import httpx

    def _boom(url, json=None, timeout=None, content=None, headers=None, **_kw):
        if url == FAKE_WAKE_URL:
            raise RuntimeError("网关挂了")
        return _FakeResp(200)

    monkeypatch.setattr(httpx, "post", _boom)
    with _MockSse([[_dm_frame(seq=7, unread=2)],
                    [_dm_frame(seq=8, unread=3)]]) as srv:
        p = _provider(st)
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        with caplog.at_level(logging.WARNING, logger="keepsake"):
            p._init_push_consumer({"push_consumer": {
                "enabled": True, "base_url": srv.base_url,
                "credential_env": "TEST_PUSH_CREDENTIAL",
                "notify_url": FAKE_NOTIFY_URL,
                "wake_url": FAKE_WAKE_URL,
                "wake_secret_env": "TEST_WAKE_SECRET",
            }})
            assert _wait_until(lambda: len(got) == 2), "唤醒失败不该连累消费者"

    # 水位照常失效 ⇒ 下一轮 prefetch 必重新查库并提醒
    assert prov_mod._DM_HINT_CACHE["key"] is None      # noqa: SLF001
    # 通知照常（通知与唤醒是两条独立旁路）
    assert prov_mod._NOTIFY_CACHE["count"] == 3        # noqa: SLF001
    # 失败只记 warning，不抛
    assert any("唤醒宿主失败" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]
    # provider 其余功能照常
    assert "相关记忆" in p.prefetch(QUERY)


def test_唤醒被拒时只记警告不重试(cred_env, wake_env, monkeypatch, caplog):
    """🔴 ③' 非 2xx ⇒ 只记 warning，**不重试**（同一帧不重复打宿主）。"""
    import httpx

    def _reject(url, json=None, timeout=None, content=None, headers=None, **_kw):
        return _FakeResp(503)

    monkeypatch.setattr(httpx, "post", _reject)
    with _MockSse([[_dm_frame(seq=7)]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        with caplog.at_level(logging.WARNING, logger="keepsake"):
            p._init_push_consumer({"push_consumer": {
                "enabled": True, "base_url": srv.base_url,
                "credential_env": "TEST_PUSH_CREDENTIAL",
                "wake_url": FAKE_WAKE_URL,
                "wake_secret_env": "TEST_WAKE_SECRET",
            }})
            assert _wait_until(lambda: bool(got))
        time.sleep(0.3)                # 给「本该被重试的那次」留机会

    assert any("唤醒宿主失败" in rec.getMessage() for rec in caplog.records), \
        [rec.getMessage() for rec in caplog.records]


def test_追帧摘要不触发唤醒(cred_env, wake_env, wake_calls, monkeypatch):
    """🔴 ④ 重连补报的**追帧摘要**（同一份状态的重报）⇒ 不重复唤醒宿主。

    剧本：第 1 次连接送来一帧新信（seq=7）⇒ 唤醒一次；断流后重连，服务端
    补推**同一份**未读摘要（seq 仍是 7）⇒ 这是补报不是新事件 ⇒ 不再唤醒。
    """
    monkeypatch.setattr(prov_mod, "_PUSH_RECONNECT_BASE_S", 0.05)   # noqa: SLF001
    monkeypatch.setattr(prov_mod, "_PUSH_RECONNECT_MAX_S", 0.2)     # noqa: SLF001
    with _MockSse([[_dm_frame(seq=7, unread=2)],
                    [_dm_frame(seq=7, unread=2)]]) as srv:   # 第 2 次 = 追帧
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "wake_url": FAKE_WAKE_URL,
            "wake_secret_env": "TEST_WAKE_SECRET",
        }})
        assert _wait_until(lambda: len(got) == 2), "断流后没有二次连接"
        time.sleep(0.3)                # 给「本该被唤醒的那次」留机会

    assert len(_wakes(wake_calls)) == 1, _wakes(wake_calls)


def test_dm_nudge帧必唤醒(cred_env, wake_env, wake_calls):
    """🔴 ⑥ `dm-nudge`（人工催办）⇒ **每帧必**唤醒（seq 去重不管它）。"""
    with _MockSse([[_dm_frame(seq=7, unread=2),
                    frame(EVENT_DM_NUDGE, {"seq": 7, "from": "", "unread": 2})],
                   [_dm_frame(seq=7, unread=2)]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "wake_url": FAKE_WAKE_URL,
            "wake_secret_env": "TEST_WAKE_SECRET",
        }})
        assert _wait_until(lambda: len(got) == 2), "帧都没收到"
        time.sleep(0.2)

    wakes = _wakes(wake_calls)
    assert len(wakes) == 2, wakes          # 新信一次 + 催办一次
    assert json.loads(wakes[1]["content"].decode("utf-8")) == {
        "from": "", "unread": 2, "seq": 7}


def test_HMAC对照向量():
    """🔴 ⑤ 签名口径钉死：固定 secret + 固定 body ⇒ 固定 hex。"""
    assert prov_mod._wake_signature(          # noqa: SLF001
        HMAC_VECTOR_SECRET, HMAC_VECTOR_BODY) == f"sha256={HMAC_VECTOR_HEX}"


def test_唤醒载荷只含三键且不平铺套层(cred_env, wake_env, wake_calls):
    """🔴 载荷是**平铺**的三键（订阅模板按 `{from}`/`{unread}`/`{seq}` 取值）。"""
    with _MockSse([[_dm_frame(seq=7, from_id="worker-1", unread=2)]]) as srv:
        p = _provider()
        got: List[Dict[str, Any]] = []
        p.on_push_event(got.append)
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "wake_url": FAKE_WAKE_URL,
            "wake_secret_env": "TEST_WAKE_SECRET",
        }})
        assert _wait_until(lambda: bool(_wakes(wake_calls)))

    payload = json.loads(_wakes(wake_calls)[0]["content"].decode("utf-8"))
    assert set(payload) == {"from", "unread", "seq"}, payload


# --------------------------------------------------------------------------
# ⑧ 通知出口的 HMAC 签名（2026-10 ks_notify_hmac）—— 打通「飞书弹通知」最后一米
#
# 两条判据（对应任务书）：
#   ① **有密钥** ⇒ 头含 `X-Hub-Signature-256: sha256=…`，且能用**收到的原始
#      字节**复算一致（签的必须是实发的字节）；
#   ② **无密钥** ⇒ **没有**该头（裸 POST = 现状逐字回归，兼容钉钉类免签渠道）。
#
# 与唤醒出口**同源**（`_wake_signature` 同一份实现）。
# --------------------------------------------------------------------------

#: 假通知签名密钥（**不是**真值 —— 任务书红线：密钥真值不得进提交物）。
FAKE_NOTIFY_SECRET = "test-notify-secret-not-a-real-one"


class _MockWebhook:
    """假 webhook 接收端（stdlib http.server，127.0.0.1 随机端口）。

    记下**收到的原始字节**与头 —— 签名必须能用收到的字节复算出来
    （签「等价」的字节没用：收方复算的是它**收到**的那份）。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: Any = None
        self.url = ""

    def __enter__(self) -> "_MockWebhook":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):                    # noqa: N802 —— BaseHTTPRequestHandler 的命名
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.calls.append({"body": body,
                                        "headers": dict(self.headers)})
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):           # 静音（别把测试日志冲花）
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/hook"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        return False


def test_notify有密钥时带签名头且可复算_notify(cred_env, monkeypatch):
    """🔴 ① 有密钥 ⇒ 头含 `X-Hub-Signature-256`，且用**收到的字节**复算一致。"""
    import hashlib
    import hmac

    monkeypatch.setenv("TEST_NOTIFY_SECRET", FAKE_NOTIFY_SECRET)
    with _MockSse([[_dm_frame()]]) as srv, _MockWebhook() as hook:
        p = _provider()
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "notify_url": hook.url,
            "notify_secret_env": "TEST_NOTIFY_SECRET",
        }})
        assert _wait_until(lambda: bool(hook.calls)), "通知没发出去"

    call_ = hook.calls[0]
    sig = call_["headers"].get("X-Hub-Signature-256") or ""
    assert sig.startswith("sha256="), call_["headers"]
    expect = hmac.new(FAKE_NOTIFY_SECRET.encode("utf-8"), call_["body"],
                      hashlib.sha256).hexdigest()
    assert sig == f"sha256={expect}", (sig, expect)


def test_notify无密钥时没有签名头_notify(cred_env):
    """🔴 ② 无密钥 ⇒ **没有**签名头（裸 POST = 现状逐字回归，免签渠道照旧能用）。"""
    with _MockSse([[_dm_frame()]]) as srv, _MockWebhook() as hook:
        p = _provider()
        p._init_push_consumer({"push_consumer": {
            "enabled": True, "base_url": srv.base_url,
            "credential_env": "TEST_PUSH_CREDENTIAL",
            "notify_url": hook.url,
        }})
        assert _wait_until(lambda: bool(hook.calls)), "通知没发出去"

    assert "X-Hub-Signature-256" not in hook.calls[0]["headers"], \
        hook.calls[0]["headers"]
    # 载荷照旧只有元数据（无正文）
    assert json.loads(hook.calls[0]["body"].decode("utf-8")) == {
        "text": "📩 你有 2 条未读私信（来自: worker-1）"}
