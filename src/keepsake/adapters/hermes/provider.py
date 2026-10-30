"""
keepsake — Keepsake记忆系统 for Hermes Agent.

每次对话自动检索相关记忆注入上下文，支持：
  - 🔍 向量搜索 — RediSearch KNN 语义检索
  - ⏳ 时间衰减 — 新记忆权重高，旧记忆逐步降权
  - 📝 自动写入 — memory(action='add') 操作自动存档完整内容
  - 🏷️ 标签过滤 — 可选按标签范围搜索

安装: pip install keepsake
激活: config.yaml 中设置 memory.provider: keepsake

配置优先级: 环境变量 > 配置文件 > 默认值
配置文件: ~/.config/keepsake/config.json (或 KEEPSAKE_CONFIG 自定义路径)
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import inspect
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

try:
    from agent.memory_provider import MemoryProvider
    from tools.registry import tool_error
except ImportError:  # 独立测试环境回退；Hermes 实际运行时从 agent 包导入
    class MemoryProvider:
        def __init__(self, *args, **kwargs):
            pass

    def tool_error(msg):
        return f"[tool_error] {msg}"

from ...core.embedder import create_embedder, resolve_embedder_api_key
from ...core.base import BackendUnsupportedError
from ...core.redis import DEFAULT_BM25_LIMIT, RedisStorage, storage_from_config
from ...core.ops.forgetter import Forgetter
# 治理收口（2026-10 ks_agent_registry）：身份 / 角色 / 上下文的判定源。
# `govern` 只依赖标准库，这里 import 它不会与 `core/*` 成环。
from ...govern.registry import AGENT_TAG_PREFIX, Registry
from ...govern.registry import ROLE_PRIMARY as REGISTRY_ROLE_PRIMARY
# 代理互聊信箱（2026-10 ks_dm_hint）：未读私信提醒的**唯一口径**（同一个 storage）。
from ...govern.messaging import Messaging
# 记忆共享开关的可见性过滤器（2026-10 ks_share_memory）：**唯一**那份口径。
from ...govern.visibility import (
    can_remove_fragment,
    fragment_owner,
    truncate_core_fragments,
    visible_agent_ids,
    visible_core_fragments,
)
# v2（2026-09）：写侧两相管线（Mem0 风格 + 封边取代）
from ...core.pipeline import Pipeline, DEFAULT_PIPELINE_CONFIG as _LLM_PIPELINE_DEFAULTS
# 通用契约的 Capabilities（依赖方向只准 hermes → generic）
from ..generic import Capabilities

# consolidator.py 的运行时接线已于 2026-09-09 退役：
#   * 两相管线 pipeline.py 已全面接管碎片提纯（提取相+更新相，封边取代旧合并）
#   * Consolidator 的「相似度分组 → LLM 缝合多级合并」与 v2 supersede 链互不相认
#   * 类源码保留（git 可溯），仅摘掉运行接线；resolve_llm_channel / _call_llm
#     仍从 keepsake.consolidator 模块按需导入，供 v2 pipeline 复用

# ---------------------------------------------------------------------------
# 工具扇区（供 Hermes MemoryProvider 注册）
# ---------------------------------------------------------------------------

FEEDBACK_SCHEMA = {
    "name": "keepsake_feedback",
    "description": (
        "记录用户对一条记忆的反馈 — 标记有用/没用。"
        "正反馈让该记忆在未来搜索中排名更高，"
        "负反馈大幅降权（标记为没用的记忆几乎不会再出现）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fragment_key": {
                "type": "string",
                "description": "碎片的 Redis key（如 memory:frag:abc123），从相关碎片的 key 字段获得。",
            },
            "is_positive": {
                "type": "boolean",
                "description": "True = 这条记忆有用，False = 没用",
            },
        },
        "required": ["fragment_key", "is_positive"],
    },
}

HOT_TOPICS_SCHEMA = {
    "name": "keepsake_topics",
    "description": (
        "查询全局热门话题统计。返回跨会话出现最频繁的话题词。"
        "可选日榜/周榜/全局。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返回条数（默认 10，最大 30）",
                "default": 10,
            },
            "period": {
                "type": "string",
                "enum": ["all", "daily", "weekly"],
                "description": "统计周期：all=全局, daily=日榜, weekly=周榜",
                "default": "all",
            },
        },
        "required": [],
    },
}


# 日志名**钉在搬家前的模块名上**：logger 名是对外可观测行为（外部 log 配置、
# caplog 过滤都按名找），搬目录不改名，否则等于悄悄改行为。
logger = logging.getLogger("keepsake")

_DEFAULT_CONFIG_PATH = "~/.config/keepsake/config.json"

# ---------------------------------------------------------------------------
# 常驻核心层（core tag）—— 2026-10 keepsake_core_tier_v1
#
# core 碎片 = `tags` 里含 `core` 的**普通碎片**（不新增表、不新增列，三后端共用）。
# ---------------------------------------------------------------------------

#: 核心层标记 tag。检索层见到它就跳过（`prefetch`），写入层见它就原文入库。
_CORE_TAG = "core"

#: **非 primary 写 core 的降级目标**（多代理治理）：子代理对常驻层只读，
#: 它写 `core` 只能落成候选（`core_candidate`）—— **带提名日戳**（2026-10
#: ks_perm_scope：与主脑候选同一条审核/班车链），经审核后才由主脑转正。
#: 同时是主脑检索出口 `retrieval.exclude_tags` 的缺省排除标签。
CORE_CANDIDATE_TAG = "core_candidate"

#: 核心层缓存 TTL（秒）。每轮都查库没有意义 —— core 层是低频变的常驻记忆。
_CORE_CACHE_TTL = 60.0

#: **代理角色缓存 TTL（秒）**：写闸每轮都可能判定身份，而角色是低频变的。
#: 取 5s 是「approve / set_role 之后最多 5s 收敛」与「不在热路径上每轮查库」的折中。
_ROLE_CACHE_TTL = 5.0

#: 单次核心层读取的时间预算（秒）。超了就本轮不注入（fail-open），宁可少一轮
#: 上下文也不能让对话卡住 —— 记忆缺失是降级，卡死是事故。
_CORE_READ_BUDGET_S = 2.0

#: 核心层注入字符预算的**兜底默认值**。真实生效值走 `_core_budget()`
#: （读 config.json 的 `retrieval.core_char_budget`）—— 这个常量只在配置缺失/
#: 非法时兜底，不再是唯一真值源。MEMORY.md/USER.md 原先全量注入约 8 万字符，
#: 8 万 token 级的内容会挤爆上下文，所以设一个硬上限并**显式标注截断**。
_CORE_MAX_CHARS = 60000

#: 进程级缓存 `{"ts": 单调时刻, "frags": [{key, content, tags, updated_at}, ...]}`
_CORE_CACHE: Dict[str, Any] = {"ts": 0.0, "frags": None}

#: `retrieval.core_char_budget` 的进程内缓存 `{配置路径: (指纹, 预算)}`。
#: 指纹取 (mtime_ns, size) —— 照抄 `consolidator.resolve_llm_channel_cached` 的
#: 缓存手法（改了 config.json 下一轮注入就热生效，不必重启进程）。
_CORE_BUDGET_CACHE: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# 未读私信提醒（2026-10 ks_dm_hint）—— **寄生在检索上**（零定时器方案）
#
# 私信是拉取式的：接收方不主动拉就永远不知道有信。已否决 cron 哨兵
# （定时任务太多不好管理），定案 = 每轮必然发生的检索顺路带回未读数，
# 注入块末尾渲染成一行 📩 提醒。
# ---------------------------------------------------------------------------

#: 提醒的**去重窗口**（秒）：同一水位在窗口内只提醒一次；水位一变立刻再提醒。
_DM_HINT_TTL = 60.0

#: 提醒行里最多列几个发件人（再多是噪音 —— 正文等 inbox 拉）。
_DM_SENDER_LIMIT = 3

#: 进程级缓存 `{"ts": 时刻, "key": 水位}`（照抄 `_CORE_CACHE` 那套模式，不造新机制）。
#: 水位 = (未读条数, 发件人集合) —— 条数相同但换人发，也是新消息，照样该提醒。
_DM_HINT_CACHE: Dict[str, Any] = {"ts": 0.0, "key": None}


# ---------------------------------------------------------------------------
# 私信推送消费者（2026-10 ks_push_consumer）—— provider 侧挂流（最后一米）
#
# 服务端 `GET /events`（SSE）推的是**元数据帧**（seq / from / unread，无正文）。
# 本模块是那一端的**消费者**：一条守护线程挂着流，收到帧做三件事：
#
#   ① 让未读私信提醒的**水位缓存失效** —— 下一轮 prefetch 必重新查库并提醒，
#      不必等那个 60s 去重窗口（否则「刚到的信」最多晚一分钟才被看见）；
#   ② `logger.info` 记**一行**事件摘要（无正文）；
#   ③ 通知宿主可选注册的回调 `on_push_event(fn)`（摘要 dict，无正文）；
#   ④（可选）**通知出口**：旁路 POST 渠道 webhook，把人叫醒 ——
#      见下方「私信通知出口」一节（`notify_url` 为空 ⇒ 不做这件事）。
#
# 三条纪律：
#   * **fail-open**：消费者的任何异常都不许碰主路径 —— provider 初始化
#     **不因它失败**，记忆读写更不受影响（推送层坏了只是少一条快车道）；
#   * **克制，不吵宿主**：帧里只有元数据，日志一行，回调只给摘要；
#   * **凭据不落配置**：凭据从环境变量 / secrets 文件读，config.json 只写来源。
#
# 🔴 **一条流是进程级的**：服务端每代理最多 2 条并发流（新的踢旧的），
# 而 Hermes 可能为多 session 造多个 provider 实例 —— 各挂一条只会互相踢。
# 所以消费者与回调表都挂在**模块级**（同进程一个代理，够用）。
# ---------------------------------------------------------------------------

#: 重连退避（秒）：5s 起、翻倍、封顶 5min（指数退避，别把服务端打爆）。
_PUSH_RECONNECT_BASE_S = 5.0
_PUSH_RECONNECT_MAX_S = 300.0

#: 进程级消费者与回调表（**一条流** —— 见上节末段）。
_PUSH_LOCK = threading.Lock()
_PUSH_CONSUMER: Optional["_PushConsumer"] = None
_PUSH_CALLBACKS: List[Callable[[Dict[str, Any]], None]] = []


# ---------------------------------------------------------------------------
# 私信通知出口（2026-10 ks_notify_outlet）—— 空闲期把人叫醒
#
# 推送帧到达后只置水位 ⇒ 用户不冒泡，提醒就浮不出来（**空闲期盲区**）。
# 消费者收到 dm / dm-nudge 帧时**旁路** POST 一个可配置的渠道 webhook
#（例：飞书群机器人）—— 🔴 **只含元数据**（未读条数 / 发件人），**永不含正文**。
#
# 三条纪律：
#   * **fail-open**：通知失败只记 warning —— 绝不重试轰炸、绝不碰水位/主路径；
#   * **限频**：同水位（未读条数不变）在窗口内只喊一次；水位一变立刻再喊；
#   * **可关**：`notify_url` 为空 ⇒ 零外呼（行为与上线前逐字相同）。
# ---------------------------------------------------------------------------

#: 通知限频窗口的缺省值（秒）：同水位在窗口内只喊一次。
_NOTIFY_DEFAULT_MIN_INTERVAL_S = 120.0

#: 进程级通知水位 `{"ts": 单调时刻, "count": 上次通知时的未读条数}`。
#: 与 `_DM_HINT_CACHE` 同款进程级模式（一条流 ⇒ 一份水位）。
_NOTIFY_CACHE: Dict[str, Any] = {"ts": 0.0, "count": None}


def _notify_payload(text: str, fmt: str) -> Dict[str, Any]:
    """通知载荷 —— **渠道适配写死在这一处**（加新渠道只改这个函数）。

    * `generic`（缺省）：`{"text": ...}` —— 通用 webhook；
    * `feishu`：飞书群机器人吃 `{"msg_type": "text", "content": {"text": ...}}`。
    """
    if str(fmt or "").strip().lower() == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"text": text}


def _json_body(payload: Dict[str, Any]) -> bytes:
    """载荷 → **实发的原始字节**（签名必须签这个，不是签「等价」的字节）。

    🔴 参数与 httpx 内部 `encode_json` **逐字一致**（`ensure_ascii=False` /
    `separators=(",", ":")` / `allow_nan=False`）—— 不一致的话，收方拿**收到的
    字节**复算出来的签名对不上（签了等于没签）。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _notify(url: str, payload: Dict[str, Any], secret: str = "",
            timeout: float = 5.0) -> None:
    """POST 通知载荷到渠道 webhook（`httpx.post(url, json=..., timeout=...)`）。

    🔴 **有密钥才签名**（2026-10 ks_notify_hmac）：头
    `X-Hub-Signature-256: sha256=HMAC(secret, raw body)` —— 与唤醒出口 `_wake`
    **同款**（签名实现同源，见 `_wake_signature`）。**无密钥 ⇒ 裸 POST**
    （兼容钉钉类免签渠道，行为与上线前逐字相同）。

    🔴 **不吞异常**：调用方据此判断「这一帧算不算已通知」—— 吞掉异常会把
    失败的通知记成已送达。异常一律由调用方记 warning（fail-open）。
    """
    import httpx                   # 延迟 import：没装 httpx 也不影响记忆功能
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = _wake_signature(secret, _json_body(payload))
    httpx.post(url, json=payload, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# 唤醒出口（2026-10 ks_wake_outlet）—— 叫醒**宿主会话**，代理自己读信
#
# 通知出口叫醒的是**人**；唤醒出口叫醒的是**宿主**（Hermes 网关的 webhook
# 订阅，本机 127.0.0.1）：收到私信帧 ⇒ POST 一次 ⇒ 宿主直接起一轮代理会话去
# `message_inbox` 读信处理 —— 「私信不需要人转告，代理自己读和处理」。
#
# 🔴 **通知不经过 LLM，唤醒才经过** —— 两条旁路并存、各司其职、各自可关
# （`notify_url` 空 ⇒ 无人叫醒；`wake_url` 空 ⇒ 无会话被叫醒）。
#
# 三条纪律：
#   * **fail-open**：唤醒失败只记 warning —— 绝不重试轰炸、绝不碰水位/通知/主路径；
#   * **边沿触发（限频语义）**：只对**新**消息 seq 唤醒；重连补报的**追帧摘要**
#     （seq 没前进 = 同一份状态的重报）不再唤醒 ⇒ 断开/重连风暴不反复叫醒宿主；
#   * **可关**：`wake_url` 为空 ⇒ 零外呼（行为与上线前逐字相同）。
# ---------------------------------------------------------------------------

#: 进程级「已唤醒到的消息 seq」水位（**边沿触发**的边沿在哪）。
#: 与 `_NOTIFY_CACHE` 同款进程级模式（一条流 ⇒ 一份水位）。
_WAKE_LAST_SEQ = -1


def _wake_payload(summary: Dict[str, Any]) -> Dict[str, Any]:
    """唤醒载荷 —— **三个平铺键**（订阅的 prompt 模板按 `{from}`/`{unread}`/`{seq}` 取值）。

    🔴 **别再套一层**：模板取的是平铺字段，套一层就取不到了。
    🔴 只含元数据（**永不含正文**）—— 正文永远走信箱本体 `message_inbox`。
    """
    return {
        "from": str(summary.get("from") or ""),
        "unread": _as_int(summary.get("unread")),
        "seq": _as_int(summary.get("seq")),
    }


def _wake_signature(secret: str, body: bytes) -> str:
    """Hermes webhook 订阅签名：`sha256=<hex>` = HMAC-SHA256(raw body bytes, secret)。

    签的是**实际发出的原始字节** —— 所以 body 必须自己序列化（不许 httpx
    再编一遍 —— 那编出来的字节和签的字节对不上）。
    """
    digest = hmac.new(str(secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _wake(url: str, secret: str, payload: Dict[str, Any], timeout: float = 5.0) -> None:
    """POST 唤醒载荷到宿主 webhook（Hermes 网关的 webhook 订阅）。

    头：`X-Hub-Signature-256: sha256=<hex>`（订阅密钥的 HMAC-SHA256 签名）。

    🔴 **不吞异常**：非 2xx 一律抛给调用方记 warning（**失败不重试**）。
    🔴 **不碰正文**：载荷只有 from / unread / seq 三个元数据键。
    """
    import httpx                   # 延迟 import：没装 httpx 也不影响记忆功能
    body = _json_body(payload)     # 与通知出口**同一份**序列化（签名同源）
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = _wake_signature(secret, body)
    resp = httpx.post(url, content=body, headers=headers, timeout=timeout)
    status = int(getattr(resp, "status_code", 0) or 0)
    if not 200 <= status < 300:
        raise RuntimeError(f"唤醒被拒（HTTP {status}）")


def _as_int(value: Any) -> int:
    """值 → int（**非法值回落 0**，不许因为一个脏字段把整帧丢掉）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_secret(settings: dict, env_key: str, file_key: str,
                 default_env: str, label: str) -> str:
    """读一个密钥 —— **只从环境变量 / secrets 文件读**，config.json 不落真值。

    顺序：`env_key` 指名的环境变量 → `file_key` 指的文件内容。
    两处都没有 ⇒ 空串（调用方据此**不挂流 / 不发请求**，而不是拿空凭据去连）。
    """
    env_name = str(settings.get(env_key) or "").strip() or default_env
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    path = str(settings.get(file_key) or "").strip()
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("keepsake: %s文件读取失败（%s）", label, type(e).__name__)
    return ""


def _push_credential(settings: dict) -> str:
    """取推送凭据 —— **只从环境变量 / secrets 文件读**，config.json 不落真值。

    顺序：`credential_env` 指名的环境变量 → `credential_file` 指的文件内容。
    两处都没有 ⇒ 空串（调用方据此**不挂流**，而不是拿空凭据去连）。
    """
    return _read_secret(settings, "credential_env", "credential_file",
                         "KEEPSAKE_PUSH_CREDENTIAL", "推送凭据")


def _dispatch_push_event(event: str, raw: str) -> Optional[Dict[str, Any]]:
    """处理一帧推送（进程级）：失效水位 → 记一行摘要 → 通知宿主回调。

    🔴 **帧里只有元数据**（seq / from / unread）：私信正文永远走信箱本体。
    这里连日志都只记这几个字段 —— 正文进日志 = 私信进了日志。

    返回**摘要 dict**（非 dm 帧 ⇒ `None`）：通知出口据此旁路发 webhook。
    """
    if event not in ("dm", "dm-nudge"):
        return None
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    summary = {
        "event": event,
        "seq": _as_int(data.get("seq")),
        "from": str(data.get("from") or ""),
        "unread": _as_int(data.get("unread")),
    }
    # ① 水位失效：下一轮 prefetch 必重新查库并提醒（不等 60s 去重窗口）
    _DM_HINT_CACHE.update({"ts": 0.0, "key": None})
    # ② 一行摘要日志（无正文）
    logger.info("keepsake: 收到推送事件 %s（seq=%d, from=%s, unread=%d）",
                summary["event"], summary["seq"], summary["from"] or "-",
                summary["unread"])
    # ③ 宿主回调（一个炸了不许连累其余，更不许碰主路径）
    for fn in list(_PUSH_CALLBACKS):
        try:
            fn(dict(summary))
        except Exception as e:      # noqa: BLE001 —— 宿主的回调不许反噬宿主
            logger.warning("keepsake: 推送回调异常（已吞掉）: %s: %s",
                           type(e).__name__, e)
    return summary


class _PushConsumer:
    """挂着 SSE 流的守护线程（provider 侧消费者）。

    线程体只做三件事：连上去 → 读帧 → 收帧交给 `_dispatch_push_event`
    （收完帧再旁路发一次通知与一次唤醒，`notify_url` / `wake_url` 为空就不发）。
    任何异常都在 `_loop` 里被吃掉并退避重连（**fail-open**：推送层坏了不许
    影响记忆读写）。线程 `daemon=True` —— 随进程亡，不留孤儿。
    """

    def __init__(self, base_url: str, credential: str, *,
                 notify_url: str = "",
                 notify_format: str = "generic",
                 notify_min_interval_s: float = _NOTIFY_DEFAULT_MIN_INTERVAL_S,
                 notify_secret: str = "",
                 wake_url: str = "",
                 wake_secret: str = "") -> None:
        self._url = base_url.rstrip("/") + "/events"
        self._credential = credential
        # 通知出口（可选）：notify_url 为空 ⇒ 零外呼（功能关，现状逐字回归）
        self._notify_url = str(notify_url or "").strip()
        self._notify_format = str(notify_format or "generic").strip().lower() or "generic"
        # 通知签名密钥（**有才签**；空 = 裸 POST，兼容钉钉类免签渠道）
        self._notify_secret = str(notify_secret or "").strip()
        try:
            self._notify_interval = max(0.0, float(notify_min_interval_s))
        except (TypeError, ValueError):
            # 脏配置不该连累整条流 ⇒ 回落缺省窗口（通知降级，不是不发）
            self._notify_interval = _NOTIFY_DEFAULT_MIN_INTERVAL_S
        # 唤醒出口（可选）：wake_url 为空 ⇒ 零外呼（功能关，现状逐字回归）
        self._wake_url = str(wake_url or "").strip()
        self._wake_secret = str(wake_secret or "").strip()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="keepsake-push",
                                        daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()

    # ---- 通知出口 ----

    def _notify_unread(self, summary: Dict[str, Any]) -> None:
        """未读私信 ⇒ POST 渠道 webhook（**只有元数据**）。fail-open。

        限频口径：**同水位**（未读条数不变）在 `notify_min_interval_s` 窗口内
        只喊一次；水位一变（升高）立刻再喊 —— 不让人干等窗口。

        🔴 载荷**永不含正文**：只带未读条数与发件人（正文永远走信箱本体）。
        🔴 **有密钥才签名**（`X-Hub-Signature-256`，与唤醒出口同款）：没配密钥
        ⇒ 裸 POST（兼容钉钉类免签渠道，行为与上线前逐字相同）。
        🔴 失败只记 warning：既不重试轰炸，也不影响水位/主路径。
        """
        if not self._notify_url:
            return                                  # 缺省关 ⇒ 零外呼
        count = _as_int(summary.get("unread"))
        sender = str(summary.get("from") or "")
        import time as _time
        now = _time.monotonic()                    # 单调钟：不受系统时间跳变影响
        cached = _NOTIFY_CACHE
        if count == cached.get("count") and \
                now - float(cached.get("ts") or 0.0) < self._notify_interval:
            return                                  # 同水位 + 窗口内 ⇒ 不重复喊
        text = (f"📩 你有 {count} 条未读私信（来自: {sender}）" if sender
                else f"📩 你有 {count} 条未读私信")
        try:
            _notify(self._notify_url, _notify_payload(text, self._notify_format),
                    self._notify_secret)
        except Exception as e:      # noqa: BLE001 —— 通知层永远 fail-open
            logger.warning("keepsake: 私信通知发送失败（已吞掉，不影响水位/主路径）: %s: %s",
                           type(e).__name__, e)
            return                                  # 失败不记水位 ⇒ 下一帧可再试
        _NOTIFY_CACHE.update({"ts": now, "count": count})

    # ---- 唤醒出口 ----

    def _wake_unread(self, summary: Dict[str, Any]) -> None:
        """私信帧 ⇒ POST 宿主唤醒 webhook（唤起宿主一轮会话去读信处理）。fail-open。

        🔴 **通知不经过 LLM，唤醒才经过** —— 通知叫醒的是人，唤醒叫醒的是
        宿主会话；两条旁路并存、各自可关（`wake_url` 空 ⇒ 零外呼）。
        🔴 **载荷只含元数据**（from / unread / seq），**永不含正文**。
        🔴 **边沿触发（限频语义）**：只有**新**消息 seq 才唤醒；重连补报的
        **追帧摘要**（seq 没前进 = 同一份状态的重报）**不再唤醒** ——
        断开/重连风暴不会把宿主反复叫醒（那才是风暴）；而断线期间真来的
        新信（seq 前进了）照常唤醒（那是新事件，不是补报）。
        🔴 **失败只记 warning**：不重试、不碰水位/通知/主路径。
        """
        global _WAKE_LAST_SEQ
        if not self._wake_url:
            return                                  # 缺省关 ⇒ 零外呼
        event = str(summary.get("event") or "")
        seq = _as_int(summary.get("seq"))
        if event == "dm":
            if seq <= _WAKE_LAST_SEQ:
                return                              # 补报（重连追帧）/ 旧帧 ⇒ 不重复叫醒
            # 先记水位再发：失败也**不重试**（同一帧不重复打宿主）
            _WAKE_LAST_SEQ = seq
        # dm-nudge：催办语义就是「现在就去看」⇒ 每帧必唤醒（不参与 seq 去重）
        payload = _wake_payload(summary)
        try:
            _wake(self._wake_url, self._wake_secret, payload)
        except Exception as e:      # noqa: BLE001 —— 唤醒层永远 fail-open
            logger.warning("keepsake: 唤醒宿主失败（已吞掉，不重试，不影响水位/通知/主路径）: %s: %s",
                           type(e).__name__, e)
            return
        logger.info("keepsake: 已唤醒宿主（seq=%d, from=%s）",
                    seq, payload["from"] or "-")

    # ---- 线程体 ----

    def _loop(self) -> None:
        """连 → 读 → 断 → 退避 → 再连（直到被 stop）。"""
        delay = _PUSH_RECONNECT_BASE_S
        while not self._stop.is_set():
            try:
                self._connect_once()
                delay = _PUSH_RECONNECT_BASE_S      # 连上过 ⇒ 退避复位
                logger.info("keepsake: 推送流结束，将重连")
            except Exception as e:      # noqa: BLE001 —— 推送层永远 fail-open
                logger.warning("keepsake: 推送流异常（%s: %s），%.0fs 后重连",
                               type(e).__name__, e, delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, _PUSH_RECONNECT_MAX_S)

    def _connect_once(self) -> None:
        """连一次流、读到断为止（返回 = 服务端收尾；重连由 `_loop` 管）。"""
        import httpx                   # 延迟 import：没装 httpx 也不影响记忆功能

        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "GET", self._url,
                headers={"Authorization": f"Bearer {self._credential}",
                         "Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"建流被拒（HTTP {resp.status_code}）")
                logger.info("keepsake: 推送流已建立（%s）", self._url)
                event_name = ""
                data_lines: List[str] = []
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if not line:
                        # 空行 = 一帧结束（SSE 的帧分隔）
                        if event_name:
                            summary = _dispatch_push_event(event_name, "\n".join(data_lines))
                            # 通知出口 / 唤醒出口：**在置水位之后**旁路发
                            #（失败也不碰水位；两条旁路各司其职、各自可关）
                            if summary is not None:
                                self._notify_unread(summary)
                                self._wake_unread(summary)
                        event_name, data_lines = "", []
                        continue
                    if line.startswith(":"):
                        continue         # 注释帧（心跳）—— 客户端忽略
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())


def _stop_push_consumer() -> None:
    """停掉进程级推送消费者（幂等；停不掉也不影响进程退出 —— 它是 daemon）。"""
    global _PUSH_CONSUMER
    with _PUSH_LOCK:
        consumer, _PUSH_CONSUMER = _PUSH_CONSUMER, None
    if consumer is not None:
        consumer.stop()


def config_path() -> Path:
    """本进程实际读取的 `config.json` 路径（`KEEPSAKE_CONFIG` > 默认）。

    控制台的预算设置端点写的就是这个文件 —— 读写两端必须同一条解析规则，
    各写一份 env 判断迟早变成「改了却不生效」。
    """
    return Path(os.environ.get("KEEPSAKE_CONFIG") or _DEFAULT_CONFIG_PATH).expanduser()


def _core_budget() -> int:
    """常驻层注入字符预算 —— **单点真值源**（注入路径与控制台口径共用本函数）。

    取 config.json 的 `retrieval.core_char_budget`；键缺失 / 非整数 / ≤0 ⇒ 回落
    `_CORE_MAX_CHARS` 并 warning（一次，每次配置变更至多一条）。

    🔴 按文件指纹（mtime_ns, size）缓存而不是每次重读：注入路径每轮对话都走，
    而 config.json 大概率不变；无脑重读是每轮一次多余的 stat+parse。指纹变了
    自然 miss ⇒ 改完配置下一轮就生效。文件不存在也缓存住（fp=None），避免
    「配置永久缺失」时每轮都 stat 一次。

    🔴 **同尺寸改值指纹可能不变**（20000→30000 在 mtime 粒度粗的文件系统上
    mtime_ns 一字不改），所以写配置的一侧必须调 `invalidate_core_budget_cache()`；
    光靠指纹在这一种情况下会「提示改成功了、注入预算还是老值」。
    """
    path = config_path()
    key = str(path)
    try:
        st = os.stat(path)
        fp = (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    except OSError:
        fp = None

    hit = _CORE_BUDGET_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]

    budget = _CORE_MAX_CHARS
    if fp is not None:
        try:
            with open(path) as f:
                raw = (json.load(f) or {}).get("retrieval", {}).get("core_char_budget")
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.warning("keepsake: config 读取失败（%s: %s），core 预算回落 %d",
                           path, e, _CORE_MAX_CHARS)
        else:
            # 只认 int 与「整数形态的字符串」。float/bool 一律拒：`int(1.5)` 会
            # 悄悄截成 1（预算变 1 字符 = 常驻层全被截掉），`int(True)` 是 1 同理。
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                n = 0
            else:
                try:
                    n = int(raw)
                except ValueError:
                    n = 0
            if n > 0:
                budget = n
            elif raw is not None:
                logger.warning("keepsake: retrieval.core_char_budget=%r 非法，回落 %d",
                               raw, _CORE_MAX_CHARS)
    _CORE_BUDGET_CACHE[key] = (fp, budget)
    return budget


def invalidate_core_budget_cache() -> None:
    """丢弃 `_core_budget()` 的进程内缓存（改完 config.json 的一侧必须调）。

    存在理由见 `_core_budget` 文档段：指纹是 (mtime_ns, size)，同尺寸改值可能
    指纹不变，光靠指纹会漏掉这一次热生效。
    """
    _CORE_BUDGET_CACHE.clear()


#: `tiering.nominate_backfill_hits` 的进程内缓存 `{配置路径: (指纹, 条数)}`。
#: 指纹口径与 `_CORE_BUDGET_CACHE` 同款（mtime_ns, size）——同一个 config.json、
#: 同一条「改完下一轮热生效」的纪律。
_TIERING_BACKFILL_CACHE: Dict[str, Any] = {}


def _nominate_backfill_hits() -> int:
    """提名基数回填条数 —— 读 config.json 的 `tiering.nominate_backfill_hits`。

    口径只有一份：缺省值与值域判据都在 `core.tiering`（`resolve_tiering`），
    这里**不另存一份常量、也不另写一份区间** —— 写闸与班车读的是同一个键。

    🔴 缓存指纹是 (mtime_ns, size)：同尺寸改值在 mtime 粒度粗的文件系统上
    可能指纹不变 ⇒ **写配置的一侧必须调 `invalidate_tiering_cache()`**
    （控制台 PUT /api/v1/settings/tiering 已调）。
    """
    from ...core import tiering as tiering_mod

    path = config_path()
    key = str(path)
    try:
        st = os.stat(path)
        fp = (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    except OSError:
        fp = None

    hit = _TIERING_BACKFILL_CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]

    count = int(tiering_mod.DEFAULTS["nominate_backfill_hits"])
    if fp is not None:
        try:
            with open(path) as f:
                raw = json.load(f) or {}
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.warning("keepsake: config 读取失败（%s: %s），提名基数回落 %d",
                           path, e, count)
        else:
            try:
                count = int(tiering_mod.resolve_tiering(raw)["nominate_backfill_hits"])
            except (TypeError, ValueError, KeyError):
                logger.warning("keepsake: tiering.nominate_backfill_hits 非法，回落 %d",
                               count)
    _TIERING_BACKFILL_CACHE[key] = (fp, count)
    return count


def invalidate_tiering_cache() -> None:
    """丢弃 `_nominate_backfill_hits()` 的进程内缓存（改完 config.json 的一侧必须调）。"""
    _TIERING_BACKFILL_CACHE.clear()


def _has_tag(tags: Any, tag: str) -> bool:
    """`tags` 字段（逗号串）里是否含 `tag` —— **逗号边界的完整标签相等**。

    与 `storage_shared.tag_match_sql`（PG/SQLite 的 SQL 判据）、
    `storage._tags_padded`（Redis 的 Python 判据）同一口径：`core` 不会命中
    `core-x` / `xcore`。三处判据必须同步改，否则「写进去了但检索排不掉」。
    """
    if not tags or not tag:
        return False
    norm = str(tags).replace("|", ",")
    for ch in ((" ,", ","), (", ", ",")):
        norm = norm.replace(*ch)
    return f",{tag}," in f",{norm},"


def _owner_kwargs(store_fn: Any, caller_agent_id: str) -> Dict[str, Any]:
    """存储层 `store()` 的**归属章覆盖值**（2026-10 ks_owner_stamp）。

    🔴 `owner_agent_id` **不在通用契约里**（契约不该为某一个宿主的「远端写入」
    开洞）—— 所以这里先看签名再决定带不带：别的存储实现没有这个参数 ⇒
    照常调（不猜、不用 try/except 吞异常，那会把写失败吞成「没写进去」）。

    空 `caller_agent_id`（本机直写/提炼管线）⇒ **不带** ⇒ 存储层落进程默认身份。
    """
    owner = str(caller_agent_id or "").strip()
    if not owner:
        return {}
    try:
        params = inspect.signature(store_fn).parameters
    except (TypeError, ValueError):       # 签名取不到（内置/C 扩展）⇒ 按不支持处理
        params = {}
    if "owner_agent_id" not in params:
        logger.warning(
            "keepsake: 存储层 store() 不认 owner_agent_id —— 本次写入的归属章"
            "会落进程默认身份而不是调用方 %s", owner)
        return {}
    return {"owner_agent_id": owner}


def _load_json_config() -> dict:
    """从 JSON 配置文件加载配置。

    路径来源（优先级高到低）:
      1. 环境变量 KEEPSAKE_CONFIG
      2. ~/.config/keepsake/config.json
    文件不存在时返回空 dict。
    """
    path_str = os.environ.get("KEEPSAKE_CONFIG") or _DEFAULT_CONFIG_PATH
    path = Path(path_str).expanduser()
    if not path.exists():
        logger.debug("keepsake: config file not found at %s", path)
        return {}
    try:
        with open(path) as f:
            cfg: dict = json.load(f)
        logger.info("keepsake: loaded config from %s", path)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("keepsake: failed to load config from %s: %s", path, e)
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 覆盖 base。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _build_query_expansion_llm_fn(cfg: dict):
    """构造一个 LLM callable 给 query_expansion 用。

    返回 (messages, model) -> Optional[str]。失败时返回 None（不抛），
    避免后台线程把 daemon 拖死。

    实现：
        * 复用 consolidator.resolve_llm_channel_cached（按 mtime 热生效）
        * 复用 consolidator._call_llm（统一错误处理 / 重试）
        * 通道 unconfigured 或 key 缺失 → 返回 None
    """
    from ...core.ops.consolidator import resolve_llm_channel_cached, _call_llm

    def _call(messages, model):
        try:
            ch = resolve_llm_channel_cached(cfg)
        except Exception as e:
            logger.debug("keepsake: qexp resolve_llm_channel failed: %s", e)
            return None
        if not ch.get("valid"):
            return None
        try:
            return _call_llm(messages, model, channel=ch, max_retries=0)
        except Exception as e:
            logger.debug("keepsake: qexp _call_llm raised: %s", e)
            return None

    return _call


class _ReadScope(NamedTuple):
    """一次读取的**可见范围**（`provider._visibility_scope` 算出来的唯一形状）。

    * `caller_id` —— 调用方的 agent_id（空 = 本进程自己）。
    * `is_primary` —— 调用方是不是 primary（primary ⇒ 永远全量可见）。
    * `visible` —— **看得见的归属代理**集合；`None` = 不过滤（全量）。
      非 primary 时 = {自己} ∪ {已批准且打开「共享记忆」的代理}。
    * `explicit` —— 调用方是不是**别人**（MCP 远端凭据反查出来的身份）：
      是 ⇒ 存储层的 `agent_id` / `is_primary` 要按它传，不能沿用存储自己的。
    """

    caller_id: str = ""
    is_primary: bool = False
    visible: Optional[Tuple[str, ...]] = None
    explicit: bool = False


class KeepsakeProvider(MemoryProvider):
    """
    Keepsake记忆提供者。

    和 Hermes builtin 内存共存，不冲突。每轮对话自动检索相关记忆
    注入上下文。仅 memory(action='add') 操作时存储完整内容。

    配置优先级（高→低）:
      1. 环境变量 (KEEPSAKE_REDIS_HOST, KEEPSAKE_EMBEDDER_MODEL 等)
      2. JSON 配置文件 (~/.config/keepsake/config.json)
         （嵌入协议 provider 只认配置文件：KEEPSAKE_EMBEDDER 影子开关已退役）
      3. config.yaml memory.keepsake 节（由 Hermes 传入）
      4. 硬编码默认值
    """

    _initialized: bool = False
    _storage: Optional[RedisStorage] = None
    _tag_filter: str = ""
    _is_primary: bool = False
    #: 主脑检索出口要剔除的噪音标签（`retrieval.exclude_tags`，初始化时读入 +
    #: 治理收口后并入注册表里标了 `exclude_from_primary` 的代理标签）。
    #: 类属性兜底 = 未 initialize 的实例（如测试直接挂 storage）不排除任何东西。
    _exclude_tags: Tuple[str, ...] = ()
    _forgetter: Optional[Forgetter] = None
    _last_maintenance: float = 0.0
    _maintenance_interval: float = 7200.0  # 每 2h 跑一次维护
    #: 代理注册表（`keepsake.govern.registry.Registry`）：身份 / 角色 / 上下文的唯一真相源。
    #: **类属性兜底 None** = 没装配注册表（裸实例 / 单测），此时写闸回落到 config 自报。
    _registry: Optional[Any] = None
    #: 信箱句柄（`govern.messaging.Messaging`）：**未读私信提醒**用（2026-10 ks_dm_hint）。
    #: 与注册表**同一个 storage** ⇒ 不另开连接、不另拼 DSN。
    _keepsake_messaging: Optional[Any] = None
    #: 本进程自己的 agent_id（注册表登记与角色查询用）
    _agent_id: str = ""
    #: 角色查询的进程内缓存 `{"ts": 单调时刻, "is_primary": bool}`
    _role_cache: Optional[Dict[str, Any]] = None

    def __init__(self, **config):
        """
        参数（通过 config.yaml memory 节传入）:

            memory:
              provider: keepsake
                redis_host: 127.0.0.1
                redis_port: 6379
                top_k: 5
                candidate_k: 10
                tag_filter: ""
                embedder:
                  provider: openai
                  api_key: sk-xxx
                  base_url: https://api.openai.com/v1
                  model: text-embedding-3-small
        """
        super().__init__()
        self._config = config

    # ------------------------------------------------------------------
    # 配置合并
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_config(inline_cfg: dict) -> dict:
        """按优先级合并配置源，返回最终配置。

        合并顺序（后覆盖前）: 默认值 ← JSON 文件 ← 环境变量 ← inline
        inline = Hermes 的 config.yaml memory.keepsake 或 __init__ 传参
        """
        # 1. 硬编码默认值（不含 embedder — 由配置文件/环境变量按需开启）
        cfg: dict = {
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "redis_password": "",
            "top_k": 5,
            "candidate_k": 10,
            "tag_filter": "",
            "synonym_min_word_freq": 10,
            "synonym_jaccard_threshold": 0.5,
            "synonym_min_co_occurrence": 3,
            "entity_cooc_top_n": 3,
            "entity_cooc_min_count": 2,
            # 写闸门 v1（2026-09）：enabled 默认开、max_len 默认 2000
            "ingest_gate": {"enabled": True, "max_len": 2000},
            # v2 写侧两相管线（2026-09）：默认开启，缺 LLM 时自动回落 v1
            "llm_pipeline": dict(_LLM_PIPELINE_DEFAULTS),
            # v2 检索侧相似度地板（按 _sim 归一化）
            "v2_min_score": 0.05,
            # LLM 通道配置（2026-09 ks_noqwen）：base_url/model/key_file/api_key；
            # 空节/缺字段 = 无有效 LLM 通道 → pipeline 不启动 + 调用方走 v1 兜底
            # （不再硬编码回落付费模型——见 consolidator.resolve_llm_channel）
            "llm": {},
            # 2026-09 ks_retr：LLM 查询扩展（治词汇鸿沟；热路径零拖慢）
            # enabled 默认开；缓存未命中且结果 < min_results 才起后台扩展
            "retrieval": {
                "query_expansion": {
                    "enabled": True,
                    "min_results": 3,
                    "max_terms": 6,
                    "cache_ttl": 86400,
                },
            },
            # 2026-10 ks_push_consumer：provider 侧挂 SSE 流（私信推送最后一米）。
            # 🔴 **缺省关**（零线程零连接）；凭据**只从环境变量 / secrets 文件读**
            # （`credential_env` / `credential_file`），config.json 不落真值。
            # 🔴 **mtime 热生效不强求**：这一节是**进程启动时**读的（线程随网关
            #    重启装载）；改了 config.json 要重启网关才生效（如实写在这里，
            #    别让人以为改完就通）。
            "push_consumer": {
                "enabled": False,
                "base_url": "",          # SSE 服务端根地址（消费者自己拼 /events）
                "credential_env": "KEEPSAKE_PUSH_CREDENTIAL",
                # 通知出口（空闲期把人叫醒）：notify_url 为空 = 功能关（零外呼）。
                # 🔴 同水位限频（缺省 120s）：未读数不变则窗口内不重复喊，
                #    水位升高立刻再喊。notify_format 决定载荷形状
                #    （generic / feishu）—— 渠道适配写死在 adapter 一处。
                "notify_url": "",
                "notify_min_interval_s": 120,
                "notify_format": "generic",
                # 通知签名密钥（**只写来源，不写真值**）：配了才带
                # `X-Hub-Signature-256: sha256=HMAC(secret, raw body)`；
                # 不配 = 裸 POST（兼容钉钉类免签渠道，现状逐字不变）。
                "notify_secret_env": "KEEPSAKE_NOTIFY_SECRET",
                "notify_secret_file": "",
            },
        }

        # 2. JSON 配置文件覆盖
        json_cfg = _load_json_config()
        cfg = _deep_merge(cfg, json_cfg)

        # 3. 环境变量覆盖
        env_overrides = {
            "redis_host": os.environ.get("KEEPSAKE_REDIS_HOST"),
            "redis_port": os.environ.get("KEEPSAKE_REDIS_PORT"),
            "redis_password": os.environ.get("KEEPSAKE_REDIS_PASSWORD"),
            "top_k": os.environ.get("KEEPSAKE_TOP_K"),
            "candidate_k": os.environ.get("KEEPSAKE_CANDIDATE_K"),
            "tag_filter": os.environ.get("KEEPSAKE_TAG_FILTER"),
            "agent_id": os.environ.get("KEEPSAKE_AGENT_ID"),
            "is_primary": os.environ.get("KEEPSAKE_IS_PRIMARY"),
        }
        for key, val in env_overrides.items():
            if val is not None:
                cfg[key] = val

        # 4. inline（Hermes 传入的 config.yaml 配置）覆盖
        cfg = _deep_merge(cfg, inline_cfg)

        # 5. 验证 agent_id 必须配置
        agent_id = cfg.get("agent_id")
        if agent_id is None or agent_id == "":
            raise ValueError("agent_id must be configured in config file, environment variable, or inline config")

        # 6. 解析 is_primary，默认为 false
        is_primary = cfg.get("is_primary", False)
        if isinstance(is_primary, str):
            is_primary = is_primary.lower() in ("true", "1", "yes", "on")
        cfg["is_primary"] = bool(is_primary)

        # 6.1 解析 `retrieval.exclude_tags`（主脑检索出口的噪音排除，逗号分隔标签串）
        # 缺省 = CORE_CANDIDATE_TAG（子代理写 core 降级出来的候选层不进主脑检索）；
        # 显式空串 = 不排除。None（键存在但没值）按缺省处理，容错写法照抄 is_primary。
        raw_exclude = cfg.get("retrieval", {}).get("exclude_tags", CORE_CANDIDATE_TAG)
        if raw_exclude is None:
            raw_exclude = CORE_CANDIDATE_TAG
        cfg["exclude_tags"] = [t.strip() for t in str(raw_exclude).split(",") if t.strip()]

        # 7. 加载 skip patterns 配置
        # skip_min_length: int，默认 2，从 config.json 的 skip_min_length 读取
        skip_min_length = cfg.get("skip_min_length", 2)
        cfg["skip_min_length"] = skip_min_length

        # skip_patterns_file: str，默认空字符串，从 config.json 的 skip_patterns_file 读取
        skip_patterns_file = cfg.get("skip_patterns_file", "")
        if skip_patterns_file:
            skip_patterns_file = Path(skip_patterns_file).expanduser()
            if skip_patterns_file.exists():
                try:
                    with open(skip_patterns_file) as f:
                        patterns = set()
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                patterns.add(line.lower())
                    cfg["skip_patterns"] = patterns
                except Exception as e:
                    logger.warning("keepsake: failed to load skip patterns from %s: %s", skip_patterns_file, e)
            else:
                cfg["skip_patterns"] = set()
        else:
            cfg["skip_patterns"] = set()

        return cfg

    def _should_search(self, query: str) -> bool:
        """判断当前用户消息是否需要检索碎片。

        跳过条件：
          1. 长度 < skip_min_length（默认 2）
          2. query 精确匹配外部文件中的 skip pattern（忽略大小写）
        """
        q = query.strip()
        min_len = int(getattr(self, '_skip_min_length', 2))
        if len(q) < min_len:
            return False
        patterns = getattr(self, '_skip_patterns', [])
        if q.lower() in patterns:
            return False
        return True

    # ------------------------------------------------------------------
    # MemoryProvider 接口
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "keepsake"

    def is_available(self) -> bool:
        try:
            import redis as _  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """初始化 — 加载配置、连接 Redis、自动创建 index。

        2026-09 ks_pipefix：把合并完成的 cfg 深拷贝存到 self._resolved_config。
        修复前 _init_pipeline 用 self._config（Hermes 传入的 inline 原始配置，
        不带 llm 节）解析 channel，导致 v2 pipeline 错判 unconfigured 不启动。
        修复后 _init_pipeline 读 self._resolved_config，与日志/同步链保持单源。
        _channel_refresher / resolve_llm_channel_cached 按文件路径 mtime 重读，
        不依赖此属性，不受影响。
        """
        cfg = self._resolve_config(self._config)
        # 深拷贝：防后续步骤或外部测试修改 cfg 时反向污染 _config 链
        # （测试里 monkeypatch inline 后再 resolve 会得到新 dict，本属性不变）
        self._resolved_config = copy.deepcopy(cfg)

        # 2026-10 ks_push_consumer：推送消费者（可选）。**放在存储装配之前** ——
        # SSE 是服务端的能力，跟本地存储通不通无关；存储还没就绪时更该早点挂上。
        # fail-open：起不来只记日志，**绝不让 provider 初始化失败**。
        self._init_push_consumer(cfg)

        # 加载/重载 jieba 自定义词典（发 /new 时生效）
        from ...domain.splitter import init_domain_dict
        init_domain_dict()

        redis_host = cfg.get("redis_host", "127.0.0.1")
        redis_port = int(cfg.get("redis_port", 6379))
        top_k = int(cfg.get("top_k", 5))
        candidate_k = int(cfg.get("candidate_k", 10))
        self._tag_filter = cfg.get("tag_filter", "")
        # 多代理治理：非 primary 写 core 的降级闸；主脑检索出口的噪音排除
        self._is_primary = bool(cfg.get("is_primary", False))
        self._exclude_tags = tuple(cfg.get("exclude_tags", (CORE_CANDIDATE_TAG,)))

        embed_cfg = cfg.get("embedder", {})
        embed_provider = embed_cfg.get("provider", "").strip().lower()
        # 只有显式配置了 embedder provider 才创建，否则走 BM25-only 模式
        if embed_provider and embed_provider not in ("", "default", "none"):
            embedder = create_embedder(
                provider=embed_cfg.get("provider", ""),
                # 凭据：内联 api_key 优先，其次 api_key_file（控制台「AI 配置」页
                # 写的就是文件型；口径见 core.embedder.resolve_embedder_api_key）
                api_key=resolve_embedder_api_key(embed_cfg),
                base_url=embed_cfg.get("base_url", ""),
                model=embed_cfg.get("model", ""),
            )
            embed_dim = embedder.dimension
            logger.info(
                "keepsake: embedder enabled (%s, dim=%d)",
                embed_provider, embed_dim,
            )
        else:
            embedder = None
            embed_dim = 1536
            logger.info("keepsake: BM25-only mode (no embedder configured)")

        # 2026-10 ks_pg_b1：后端由 `storage.backend` 决定（缺省 redis = 行为不变）。
        # 显式传 redis_cls=RedisStorage —— 这样 `keepsake.RedisStorage` 的
        # monkeypatch 仍能拦到构造（既有测试靠它打桩，不连真实 Redis）。
        self._storage = storage_from_config(
            config=cfg,
            redis_cls=RedisStorage,
            embedder=embedder,
            host=redis_host,
            port=redis_port,
            password=cfg.get("redis_password") or None,
            candidate_count=candidate_k,
            final_limit=top_k,
            embed_dim=embed_dim,
            # 🔴 2026-10 ks_recall_v1：候选池默认值与三个后端的 DEFAULT_BM25_LIMIT
            # 同源（此前这里写死 10，storage.py 侧写死 20，两处默认值本身就不一致）。
            # 旧配置键 `bm25_limit` 不变，配了就以配置为准。
            bm25_limit=int(cfg.get("bm25_limit", DEFAULT_BM25_LIMIT)),
            decay_half_days=int(cfg.get("decay_half_days", 60)),
            embed_cache_ttl=int(cfg.get("embed_cache_ttl", 3600)),
            sentiment_boost_positive=float(cfg.get("sentiment_boost_positive", 1.5)),
            sentiment_boost_negative=float(cfg.get("sentiment_boost_negative", 1.3)),
            feedback_positive_boost=float(cfg.get("feedback_positive_boost", 1.3)),
            feedback_negative_penalty=float(cfg.get("feedback_negative_penalty", 0.5)),
            hot_topic_boost=float(cfg.get("hot_topic_boost", 1.2)),
            hot_topic_decay_half_days=int(cfg.get("hot_topic_decay_half_days", 30)),
            emotion_intensity_factor=float(cfg.get("emotion_intensity_factor", 0.4)),
            attention_boost_max=float(cfg.get("attention_boost_max", 1.5)),
            attention_base_increment=float(cfg.get("attention_base_increment", 2.0)),
            attention_emotion_factor=float(cfg.get("attention_emotion_factor", 1.5)),
            agent_id=cfg.get("agent_id", ""),
            is_primary=cfg.get("is_primary", False),
            synonym_min_word_freq=int(cfg.get("synonym_min_word_freq", 10)),
            synonym_jaccard_threshold=float(cfg.get("synonym_jaccard_threshold", 0.5)),
            synonym_min_co_occurrence=int(cfg.get("synonym_min_co_occurrence", 3)),
            entity_cooc_top_n=int(cfg.get("entity_cooc_top_n", 3)),
            entity_cooc_min_count=int(cfg.get("entity_cooc_min_count", 2)),
            # v2（2026-09）：检索侧相似度地板，按 _sim 归一化值过滤
            v2_min_score=float(cfg.get("v2_min_score", 0.05)),
            # 2026-09 ks_retr：LLM 查询扩展配置（热路径零延迟）
            query_expansion_enabled=bool(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("enabled", True)
            ),
            query_expansion_min_results=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("min_results", 3)
            ),
            query_expansion_max_terms=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("max_terms", 6)
            ),
            query_expansion_ttl=int(
                cfg.get("retrieval", {}).get("query_expansion", {}).get("cache_ttl", 86400)
            ),
            # 2026-09 ks_retr：把 LLM 通道绑成 query_expansion_llm_fn
            # （通道未配置/无效 → 传 None → 后台扩展调用静默失败，不影响热路径）
            query_expansion_llm_fn=_build_query_expansion_llm_fn(cfg),
        )

        # 自动创建/验证 index
        if not self._storage.ensure_index():
            # 2026-10 ks_pg_b1：探活改走后端中立的 health_check()，
            # 不再在消息里假定后端一定是 Redis（后端由 storage.backend 决定）
            logger.warning(
                "keepsake: 存储后端 %s 未就绪（health_check=%s, redis %s:%s）",
                type(self._storage).__name__, self._storage.health_check(),
                redis_host, redis_port,
            )
            return

        self._initialized = True
        logger.info(
            "keepsake: connected (session=%s, top_k=%d, tag_filter=%s)",
            session_id, top_k, self._tag_filter or "(none)",
        )

        # 治理收口（2026-10 ks_agent_registry）：自登记 + 角色归注册表 + 迁移退役键
        self._init_governance(cfg)

        # 初始化 skip patterns 配置
        self._skip_min_length = cfg.get("skip_min_length", 2)
        self._skip_patterns = cfg.get("skip_patterns", set())

        # 写闸门配置（ingest_gate v1，2026-09）
        self._gate_cfg = cfg.get("ingest_gate", {"enabled": True, "max_len": 2000})

        # 解析 LLM 通道（base_url/model/api_key）—— 2026-09 ks_noqwen 起无 llm 节 = unconfigured
        # Consolidator 退役后保留此单独 import：函数仍从 consolidator 模块取，
        # 但不再构造 Consolidator 实例。
        from ...core.ops.consolidator import resolve_llm_channel
        llm_channel = resolve_llm_channel(cfg)

        # Consolidator 退役（2026-09-09）：提纯职能由 v2 两相管线（pipeline.py）接管，
        # 其 supersede 封边链与 Consolidator 的「相似度分组 → LLM 缝合」互不相认。
        # 仅保留 Forgetter（守护模式）。
        self._forgetter = Forgetter(
            storage=self._storage,
            max_age_days=int(cfg.get("forget_max_age_days", 30)),
            dry_run=bool(cfg.get("forget_dry_run", True)),
        )
        logger.info("keepsake: maintenance engines initialized (Consolidator retired)")

        # v2（2026-09）：写侧两相管线 — 仅在 enabled 且有 LLM 时启动
        self._init_pipeline(cfg.get("llm_pipeline", {}) or {})

        # 自动注册定时任务（仅在首次启动时创建）
        self._ensure_cron_jobs()

    # ------------------------------------------------------------------
    # 定时任务自动注册
    # ------------------------------------------------------------------

    CRON_JOBS = [
        {
            "name": "memory-maintenance",
            "script": "memory-maintenance.py",
            "schedule": {"kind": "interval", "minutes": 120, "display": "every 120m"},
        },
        {
            "name": "synonym-discovery-daily",
            "script": "discover_synonyms.py",
            "schedule": {"kind": "cron", "expr": "0 */8 * * *", "display": "0 */8 * * *"},
        },
        {
            "name": "记忆去重",
            "script": "dedup-memory.sh",
            "schedule": {"kind": "cron", "expr": "0 * * * *", "display": "0 * * * *"},
        },
    ]

    @staticmethod
    def _ensure_cron_jobs() -> None:
        """启动时自动注册三条定时任务（不存在才创建），并确保脚本文件到位。"""
        cron_dir = Path("~/.hermes/cron").expanduser()
        scripts_dir = Path("~/.hermes/scripts").expanduser()
        jobs_file = cron_dir / "jobs.json"

        if not jobs_file.exists():
            logger.info("keepsake: cron jobs.json not found, skipping auto-register")
            return

        # 读取现有任务
        try:
            data = json.loads(jobs_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("keepsake: failed to read jobs.json")
            return

        jobs = data.get("jobs", [])
        existing_names = {j.get("name") for j in jobs}

        # 获取插件包的 cron/ 目录（作为脚本源）
        pkg_cron = Path(__file__).resolve().parent.parent.parent / "cron"

        now = datetime.now(timezone.utc).isoformat()

        for jdef in KeepsakeProvider.CRON_JOBS:
            name = jdef["name"]
            script = jdef["script"]
            if name in existing_names:
                continue

            # 确保脚本文件存在
            target = scripts_dir / script
            if not target.exists() and pkg_cron.exists():
                src = pkg_cron / script
                if src.exists():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(src.read_text())
                        target.chmod(0o755)
                        logger.info("keepsake: installed cron script %s", script)
                    except OSError as e:
                        logger.warning("keepsake: failed to install %s: %s", script, e)

            new_job = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "prompt": "",
                "skills": [],
                "skill": None,
                "model": None,
                "provider": None,
                "base_url": None,
                "script": script,
                "no_agent": True,
                "context_from": None,
                "schedule": jdef["schedule"].copy(),
                "schedule_display": jdef["schedule"]["display"],
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": now,
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "last_delivery_error": None,
                "deliver": "local",
                "origin": None,
                "enabled_toolsets": None,
                "workdir": None,
                "profile": None,
                "fire_claim": None,
            }
            jobs.append(new_job)
            logger.info("keepsake: auto-registered cron job '%s'", name)

        if any(jdef["name"] not in existing_names for jdef in KeepsakeProvider.CRON_JOBS):
            data["jobs"] = jobs
            data["updated_at"] = now
            try:
                jobs_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                logger.info("keepsake: cron jobs.json updated")
            except OSError as e:
                logger.warning("keepsake: failed to write jobs.json: %s", e)

    def system_prompt_block(self, caller_agent_id: str = "",
                            caller_role: str = "") -> str:
        parts = [
            "你有Keepsake记忆系统（keepsake），连接在 Redis + RediSearch 上。",
            "当执行 memory(action='add') 操作时，系统会自动存储完整内容并支持后续检索。",
            "相关的记忆条目就在下面「相关记忆」段落里，直接使用即可。",
            "记忆综合排序 = BM25相似度 × 时间衰减 × 情感权重 × 反馈权重 × 热门话题权重。",
            "正反馈用 keepsake_feedback(key, positive=True) 标记有用，",
            "负反馈用 keepsake_feedback(key, positive=False) 标记没用。",
            "热门话题用 keepsake_topics() 查询。",
        ]
        block = self._core_block(caller_agent_id, caller_role)
        if block:
            parts.append("")
            parts.append(block)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 常驻核心层（core tag）—— 2026-10 keepsake_core_tier_v1
    #
    # 两套记忆并存的根因：Hermes 本体每轮全量注入 MEMORY.md/USER.md（约 8 万字符），
    # keepsake 只做按查询召回的「相关记忆」段落。本层把常驻记忆收进碎片库，
    # 让 keepsake 成为**单一记忆系统**：core 碎片每轮原文全量注入，
    # 其余碎片仍走 prefetch 检索。
    #
    # 🔴 **本层绝不抛异常、绝不阻塞对话**（fail-open）：
    #   * 读库异常 / 耗时超预算 → 只返回静态文案 + logger.warning
    #   * 截断时必须留一行可见的截断说明，不许静默丢条目
    # 理由：`system_prompt_block` 在 Hermes 每轮组装 system prompt 的关键路径上，
    # 在这里抛出去等于整轮对话失败 —— 记忆是增强项，不该有能力让对话瘫掉。
    # ------------------------------------------------------------------

    def _core_fragments(self, caller_agent_id: str = "",
                        caller_role: str = "") -> List[Dict[str, Any]]:
        """取核心层碎片，带 60s 进程内缓存；任何异常/超时都退化成空列表 + warning。

        缓存是进程级而非实例级：core 层在一次对话里只应查一次库，而 Hermes
        可能为多 session 构造 provider 实例。

        🔴 **缓存里存的是原始行，可见性过滤在缓存之后做**（2026-10 ks_share_memory）：
        否则「先给子代理看一遍、再给主脑看一遍」的进程级缓存会把上一个调用方的
        可见范围漏给下一个（主脑于是看不到自己该看的那部分）。
        """
        import time as _time

        now = _time.time()
        cached = _CORE_CACHE.get("frags")
        if cached is not None and now - _CORE_CACHE["ts"] < _CORE_CACHE_TTL:
            frags = list(cached)
        else:
            try:
                start = _time.time()
                frags = list(self._storage.list_by_tag(_CORE_TAG))
                elapsed = _time.time() - start
            except Exception as e:
                logger.warning("keepsake: core 层读取失败（fail-open，只注入静态文案）: %s", e)
                return []
            if elapsed > _CORE_READ_BUDGET_S:
                logger.warning("keepsake: core 层读取耗时 %.1fs 超预算 %.1fs（fail-open，本轮不注入）",
                               elapsed, _CORE_READ_BUDGET_S)
                return []
            _CORE_CACHE["ts"] = now
            _CORE_CACHE["frags"] = frags
        # 同口径可见性：非 primary 只留**自己的** + 已共享代理的 core
        # （primary 的 core 也要 primary 自己开共享才外露 —— 对称规则）
        scope = self._visibility_scope(caller_agent_id, caller_role)
        if scope.is_primary or self._registry is None:
            return frags
        return visible_core_fragments(frags, self._registry, scope.caller_id)

    def core_budget_for(self, caller_agent_id: str = "") -> int:
        """**本次读取方代理**的注入字符预算（per-agent 优先，缺省回退全局）。

        * `caller_agent_id` 非空 ⇒ **MCP 远端调用方**（身份由闸门按凭据反查）
          ⇒ 取注册表里**它那一行**的 `core_budget_chars`；
        * `caller_agent_id` 为空 ⇒ **本进程自己**（主脑进程；它的 agent_id 就是
          `main-brain`）⇒ 取注册表里**本进程那一行**的值。

        🔴 **不缓存**：改完预算必须**立刻生效**（照 `share_memory` 那条纪律）——
        代理数是个位数，一次小查询足够；缓存一加就有「改了没生效」的窗口。
        🔴 那一行没有预算（NULL）/ 注册表不可用 ⇒ 回退全局 `_core_budget()`
        （老库补列落 NULL ⇒ 存量行一个都没被改过）。
        """
        registry = getattr(self, "_registry", None)
        aid = str(caller_agent_id or getattr(self, "_agent_id", "") or "").strip()
        if registry is not None and aid:
            getter = getattr(registry, "core_budget_chars", None)
            if callable(getter):
                try:
                    value = getter(aid)
                except Exception as e:      # noqa: BLE001 —— 读不到就回退全局
                    logger.warning("keepsake: 代理 %s 的注入预算读取失败（回退全局）: %s",
                                   aid, type(e).__name__)
                    value = None
                if value:
                    return int(value)
        return _core_budget()

    def _core_block(self, caller_agent_id: str = "", caller_role: str = "") -> str:
        """渲染 `<keepsake-core>…</keepsake-core>` 块；无内容时返回空串（不留空行）。

        🔴 预算按**本次读取方代理**取（`core_budget_for`）：该代理自己配了
        `core_budget_chars` 就用它的，没配才回退全局。
        """
        if not self._storage:
            return ""
        frags = self._core_fragments(caller_agent_id, caller_role)
        if not frags:
            return ""
        budget = self.core_budget_for(caller_agent_id)
        kept, dropped = truncate_core_fragments(frags, budget)
        if dropped:
            # 超预算 ⇒ 截断 + **一行可见说明**（不许静默丢条目）。
            lines = [f"- {str(f.get('content', ''))}" for f in kept]
            lines.append(f"[core 层已截断：共 {len(frags)} 条，本次注入 {len(kept)} 条]")
            logger.warning("keepsake: core 层超 %d 字符预算，截断为 %d 条",
                           budget, len(kept))
        else:
            lines = [f"- {str(f.get('content', ''))}" for f in frags]
        return "\n".join(["<keepsake-core>", *lines, "</keepsake-core>"])

    # ------------------------------------------------------------------
    # 未读私信提醒（2026-10 ks_dm_hint）—— 寄生在本轮检索上
    #
    # 私信是**拉取式**的：接收方不主动拉就永远不知道有信。已否决 cron 哨兵
    # （定时任务太多不好管理）⇒ 定案：把提醒寄生在每轮必然发生的检索上，
    # 零新组件、零定时器、零额外请求。
    # ------------------------------------------------------------------

    def _dm_hint_line(self) -> str:
        """未读私信提醒行（📩 …）—— **没有未读就返回空串**（静默是默认态）。

        三条纪律：

        * **fail-open**：信箱读失败（PG 抖动 / schema 未就绪）⇒ 不提醒，
          绝不把异常抛进对话关键路径（与本文件其余注入路径同款）；
        * **不塞正文**：只取条数与发件人，正文等 `message_inbox` 拉（省 token）；
        * **防刷屏**：同一水位在 60s 窗口内只提醒一次；水位一变立刻再提醒。
        """
        box = self._keepsake_messaging
        agent_id = (self._agent_id or "").strip()
        if box is None or not agent_id:
            return ""
        import time as _time

        now = _time.monotonic()          # 单调钟：去重窗口不受系统时间跳变影响
        try:
            summary = box.unread_summary(agent_id, sender_limit=_DM_SENDER_LIMIT)
        except Exception as e:      # noqa: BLE001 —— 提醒读不到就不提醒
            logger.warning("keepsake: 未读私信计数失败（fail-open，不提醒）: %s: %s",
                           type(e).__name__, e)
            return ""
        count = int(summary.get("count") or 0)
        senders = [str(s) for s in (summary.get("senders") or [])]
        if count <= 0:
            # 水位归零 ⇒ 缓存也归零（否则「读完又收到同样条数」会被当成没变化而漏提醒）
            _DM_HINT_CACHE.update({"ts": now, "key": None})
            return ""
        key = (count, tuple(senders))
        cached = _DM_HINT_CACHE
        if cached.get("key") == key and now - float(cached.get("ts") or 0.0) < _DM_HINT_TTL:
            return ""              # 同一水位、窗口内 ⇒ 不重复刷屏
        _DM_HINT_CACHE.update({"ts": now, "key": key})
        src = ("（来自: " + "、".join(senders) + "）") if senders else ""
        return f"📩 你有 {count} 条未读私信{src}。请调用 message_inbox 处理。"

    # ------------------------------------------------------------------
    # 推送消费者（2026-10 ks_push_consumer）—— **可选**扩展
    #
    # 契约在 `adapters/generic/protocol.py` 的 `PushEventConsumer`（可选契约，
    # 不在基础契约里 —— 宿主不实现也不影响任何既有行为）。
    # ------------------------------------------------------------------

    def on_push_event(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        """注册推送事件回调（**可选**能力；不注册也不影响任何行为）。

        回调收到的是一个**摘要 dict**：`{"event", "seq", "from", "unread"}`
        —— 🔴 **没有正文**（私信正文永远走信箱本体 `message_inbox`；
        正文进推送层 = 正文进了日志与代理内存）。

        🔴 回调在**消费者线程**里被调用 ⇒ 它必须快、必须自己吞异常
        （本实现已经替它兜了一层：抛异常只记 warning，不影响其余回调、
        不影响主路径）。同一个回调重复注册只算一次。
        """
        if not callable(fn):
            raise TypeError("on_push_event 需要一个可调用对象")
        with _PUSH_LOCK:
            if fn not in _PUSH_CALLBACKS:
                _PUSH_CALLBACKS.append(fn)

    def _init_push_consumer(self, cfg: dict) -> None:
        """按配置起推送消费者（**fail-open**：失败只记日志，绝不影响初始化）。

        配置节 `push_consumer`：
          * `enabled`（缺省 **false**）—— 关着就是**零线程零连接**；
          * `base_url` —— SSE 服务端根地址（消费者自己拼 `/events`）；
          * `credential_env` / `credential_file` —— 凭据来源（**不落真值**）；
          * `notify_url` —— 渠道 webhook 地址（**空 = 通知功能关，零外呼**）；
          * `notify_min_interval_s` —— 同水位限频窗口（缺省 120s）；
          * `notify_format` —— 载荷格式（`generic` / `feishu`，缺省 generic）；
          * `notify_secret_env` / `notify_secret_file` —— 通知签名密钥来源
            （**不落真值**；**有密钥才签名** `X-Hub-Signature-256`，
            读不到 ⇒ 裸 POST = 现状逐字不变，兼容免签渠道）；
          * `wake_url` —— 宿主唤醒 webhook 地址（**空 = 唤醒功能关，零外呼**）；
          * `wake_secret_env` / `wake_secret_file` —— 订阅密钥来源
            （**不落真值**；读不到 ⇒ 不唤醒）。

        🔴 进程级**一条流**：多个 provider 实例（Hermes 多 session）各挂一条
        只会互相踢（服务端每代理最多 2 条并发流，新的踢旧的）。
        """
        global _PUSH_CONSUMER
        try:
            settings = (cfg or {}).get("push_consumer") or {}
            if not isinstance(settings, dict):
                return
            if not bool(settings.get("enabled", False)):
                return                      # 缺省关 ⇒ 零线程零连接
            base_url = str(settings.get("base_url") or "").strip()
            credential = _push_credential(settings)
            if not base_url or not credential:
                logger.warning("keepsake: 推送消费者已开启，但 base_url / 凭据缺失（不挂流）")
                return
            notify_url = str(settings.get("notify_url") or "").strip()
            notify_format = (str(settings.get("notify_format") or "generic")
                             .strip().lower() or "generic")
            try:
                notify_interval = float(settings.get(
                    "notify_min_interval_s", _NOTIFY_DEFAULT_MIN_INTERVAL_S))
            except (TypeError, ValueError):
                notify_interval = _NOTIFY_DEFAULT_MIN_INTERVAL_S
            if notify_interval < 0:
                notify_interval = 0.0
            # 通知签名密钥（**有才签**）：读不到 ⇒ 裸 POST（免签渠道照旧能用）
            notify_secret = _read_secret(settings, "notify_secret_env",
                                         "notify_secret_file",
                                         "KEEPSAKE_NOTIFY_SECRET", "通知订阅密钥")
            # 唤醒出口（可选）：wake_url 为空 ⇒ 零外呼；密钥读不到 ⇒ 也不唤醒
            #（没有签名 = 订阅鉴权过不去，发了也是白发）
            wake_url = str(settings.get("wake_url") or "").strip()
            wake_secret = _read_secret(settings, "wake_secret_env", "wake_secret_file",
                                       "KEEPSAKE_WAKE_SECRET", "唤醒订阅密钥")
            if wake_url and not wake_secret:
                logger.warning("keepsake: 已配置 wake_url，但读不到订阅密钥（不唤醒）")
                wake_url = ""
            with _PUSH_LOCK:
                if _PUSH_CONSUMER is not None and _PUSH_CONSUMER.alive():
                    logger.info("keepsake: 推送消费者已在运行（复用现有流）")
                    return
                consumer = _PushConsumer(
                    base_url, credential,
                    notify_url=notify_url,
                    notify_format=notify_format,
                    notify_min_interval_s=notify_interval,
                    notify_secret=notify_secret,
                    wake_url=wake_url,
                    wake_secret=wake_secret,
                )
                consumer.start()
                _PUSH_CONSUMER = consumer
            logger.info("keepsake: 推送消费者已启动（%s/events）", base_url)
        except Exception as e:      # noqa: BLE001 —— 推送层永远 fail-open
            logger.warning("keepsake: 推送消费者启动失败（fail-open，不影响主路径）: %s: %s",
                           type(e).__name__, e)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """根据用户消息检索相关碎片，注入到上下文。"""
        # 未读私信提醒：独立于检索（一次 SELECT COUNT，成本可忽略）⇒ 放在
        # 「省钱闸」之前算。寒暄/短消息轮跳过检索，但提醒不能跟着一起跳过
        # （否则短消息轮永远收不到未读私信提醒）。
        hint = self._dm_hint_line()

        if not self._should_search(query):
            return hint

        if not query or not self._storage:
            return hint

        import time as _time

        # 检查工作流锁（后端无关探测：Redis 读 keepsake:workflow_lock key；
        # PG 后端没有这张「全局开关表」⇒ 恒 False = 锁不存在，放行检索）
        try:
            if self._storage.fragment_exists("keepsake:workflow_lock"):
                logger.debug("keepsake: workflow lock active, skipping search")
                return ""
        except Exception as e:
            logger.debug("keepsake: workflow lock probe failed, proceeding: %s", e)

        start = _time.time()
        # 可见范围与 `search()` 同一份口径（2026-10 ks_share_memory）：
        # 本进程是 primary 时不过滤（现状逐字不变），是子代理时按共享集收窄。
        scope = self._visibility_scope()
        fragments = self._storage.search(
            query.strip(),
            tag_filter=self._tag_filter,
            **self._scope_kwargs(scope),
        )
        elapsed = _time.time() - start
        # 活动计数：🔴 主脑每轮注入走的是 prefetch 而不是 self.search()
        # （判完 skip 就直调存储层），只挂 search()/get() 时 reads 恒为 0。
        # 挂点放在**真正发起检索之后**：被 skip 掉的问题没检索过，不记。
        self._touch("read")

        # core 碎片由 system_prompt_block 无条件全量注入，这里必须排除 ——
        # 否则同一条记忆每轮出现两次（一次全量、一次检索），且两份可能不同版本。
        fragments = [f for f in fragments if not _has_tag(f.get("tags", ""), _CORE_TAG)]
        fragments = self._filter_excluded(fragments)

        # 未读私信提醒：>0 才有一行，=0 什么都不加。
        # （hint 在本方法开头已算好，这里**复用**变量，别再算第二遍 ——
        #   `_dm_hint_line` 内部有 60s 水位去重，算两遍会把提醒自己吃掉。）
        if not fragments:
            return hint

        lines = ["<keepsake>"]
        lines.append(f"# 相关记忆 (检索耗时 {elapsed:.1f}s)")
        lines.append("")
        for i, frag in enumerate(fragments, 1):
            lines.append(f"[{i}] {frag.get('content', '')}")
            tags = frag.get("tags", "")
            combined = frag.get("_combined_score", 0)
            weights = frag.get("_weights", {})
            info_parts = []
            if tags:
                info_parts.append(f"标签: {tags}")
            info_parts.append(f"综合: {combined:.2f}")
            if weights:
                info_parts.append(f"w: sim={weights.get('sim',0):.2f} decay={weights.get('decay',0):.2f} "
                                  f"emotion={weights.get('emotion',1):.1f} fb={weights.get('feedback',1):.1f} "
                                  f"hot={weights.get('hot_topic',1):.1f}")
            # 情感标签可视化
            sent_label = frag.get("sentiment_label", "")
            if sent_label and sent_label != "neutral":
                sent_score = frag.get("sentiment_score", "0")
                icon = "😊" if sent_label == "positive" else "😠"
                info_parts.append(f"{icon} {sent_label}({sent_score})")
            lines.append(f"    ({', '.join(info_parts)})")
            lines.append("")

        lines.append("</keepsake>")
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """v2（2026-09）：格式闸门 → v2 pipeline 入队 → 兜底 v1 decide() 直存。"""
        if not self._storage or not user_content or not user_content.strip():
            return
        text = user_content.strip()
        decision, existing_meta = self._gate_decide(text, "turn_memory")
        if decision.action == "reject":
            return
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None and getattr(pipeline, "_llm_fn", None) is not None:
            try:
                pipeline.enqueue(text, assistant_content or "")
                return
            except Exception as e:
                logger.warning("keepsake: pipeline.enqueue failed (%s); falling back to v1", e)
        self._v1_store_after_decide(text, decision, existing_meta, "turn_memory", "hermes_agent", "")

    def _v1_fallback_store(self, user_text: str, category: str = "turn_memory") -> None:
        """v2 pipeline 失败时的窗口级兜底（与 sync_turn 的 v1 路径同源）。"""
        if not self._storage or not user_text or not user_text.strip():
            return
        decision, existing_meta = self._gate_decide(user_text.strip(), category)
        if decision.action == "reject":
            return
        self._v1_store_after_decide(user_text.strip(), decision, existing_meta, category, "pipeline_v2_fallback", "fallback:v1")

    def _gate_decide(self, text: str, category: str):
        """R1-R7 闸门裁决 + 探测同 hash 既有 meta（v1 与 v2 共享）。"""
        from ...core.ingest_gate import decide
        frag_key = f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        existing_meta = None
        try:
            if self._storage.fragment_exists(frag_key):
                existing_meta = {"key": frag_key}
        except Exception as e:
            # ⚠️ 不再静默：探测异常会让 R6 去重失效（同一内容会被当新碎片再存一次）。
            #   降级本身可接受（fail-open），但必须留痕，否则读日志看不出「去重没跑」。
            logger.warning("keepsake: 既有碎片探测失败，R6 去重本次跳过 (%s)", e)
        return decide(text, category, existing_meta, getattr(self, "_gate_cfg", None)), existing_meta

    def _v1_store_after_decide(self, text, decision, existing_meta, category, source, extra_tags) -> None:
        """decide() 之后直存（含 update_state 闭环；v1 与 v2 兜底共享）。

        R7 接线：store 路径入库前先 scrub_secrets（仅 store 路径；
        update_state 路径只刷时间戳，按任务书约定不 scrub）。
        """
        from ...core.ingest_gate import scrub_secrets, update_state_only
        if decision.action == "update_state":
            update_state_only(self._storage, existing_meta)
            return
        # R7：入库前凭据打码（sync_turn v1 + v2 兜底共用此点）
        try:
            scrubbed_text, _n_masks = scrub_secrets(text)
            if scrubbed_text:
                text = scrubbed_text
        except Exception as e:
            logger.debug("keepsake: scrub_secrets failed in _v1_store_after_decide: %s", e)
        try:
            tags = "conversation" + ("," + extra_tags if extra_tags else "")
            self._storage.store(text=text, tags=tags, category=category, source=source,
                                fragment_type="memory")
        except Exception as e:
            logger.warning("keepsake: _v1_store_after_decide failed: %s", e)

    def _init_pipeline(self, llm_pipe_cfg: dict) -> None:
        """v2（2026-09）：构建 Pipeline 实例并启动 daemon（LLM 不可用则跳过）。

        2026-09 ks_noqwen 变更：
          * 移除硬编码 model 字段兜底；配置无效直接走 v1 fallback
          * 注入 channel_refresher 给 Pipeline，每次 _drain_now 开头按 mtime 重读
            config.json → 改完下一处理窗口生效，无需重启网关

        2026-09 ks_pipefix 变更：
          * channel 解析源由 self._config（Hermes 传入的 inline 原始配置，
            通常无 llm 节）改为 self._resolved_config（initialize() 里合并完成的
            cfg，含 config.json 的 llm 节）。这是关键修复——修前 inline 空 + 文件
            有节时会被错判 unconfigured，v2 pipeline 永不启动。
          * _channel_refresher / resolve_llm_channel_cached 按文件路径 mtime
            重读，不依赖 self._resolved_config，不受影响。
        """
        self._pipeline = None
        if not llm_pipe_cfg.get("enabled", True):
            return
        from ...core.ops.consolidator import _call_llm, resolve_llm_channel
        # ks_pipefix：用合并后的 cfg（含 config.json 的 llm 节）解析 channel，
        # 不要再用 self._config（仅含 inline）
        llm_channel = resolve_llm_channel(self._resolved_config)
        if not llm_channel.get("valid"):
            # 缺 base_url/model/api_key 之一 → 无 LLM 通道 → pipeline 不启动
            logger.warning(
                "keepsake: v2 pipeline disabled (llm channel unconfigured, source=%s); falls back to v1",
                llm_channel.get("source", "?"),
            )
            return

        # functools.partial 绑定 channel —— llm_fn 仍为 callable，测试 mock 不受影响
        import functools
        llm_fn = functools.partial(_call_llm, channel=llm_channel)

        # 热生效：每次 _drain_now 开头按 mtime 重读 config.json，重解析 channel，
        # 用新 channel 跑本窗。channel_refresher 返回 (channel_dict, llm_fn)。
        from ...core.ops.consolidator import resolve_llm_channel_cached

        def _channel_refresher():
            # 显式读磁盘 → 走 mtime 缓存路径
            fresh = resolve_llm_channel_cached()
            new_llm_fn = functools.partial(_call_llm, channel=fresh)
            return (fresh, new_llm_fn)

        # model 默认值：config.json llm.model 优先；管道配置项次之；空 → 不预设
        initial_model = (
            llm_pipe_cfg.get("model")
            or llm_channel.get("model")
            or ""
        )

        self._pipeline = Pipeline(
            storage=self._storage, llm_fn=llm_fn,
            model=initial_model,
            window_pairs=int(llm_pipe_cfg.get("window_pairs", 4)),
            window_seconds=float(llm_pipe_cfg.get("window_seconds", 30.0)),
            max_calls_per_window=int(llm_pipe_cfg.get("max_calls_per_window", 8)),
            update_top_k=int(llm_pipe_cfg.get("update_top_k", 5)),
            recent_context_size=int(llm_pipe_cfg.get("recent_context_size", 8)),
            gate_fallback=self._v1_fallback_store,
            channel_refresher=_channel_refresher,
        )
        self._pipeline.start()
        logger.info("keepsake: v2 pipeline started (channel=%s)", llm_channel.get("source", "?"))

    def _maybe_maintain(self) -> None:
        """检查是否该执行维护，执行 Forget（Consolidator 已退役）。"""
        import time as _time
        now = _time.time()
        if now - self._last_maintenance < self._maintenance_interval:
            return
        self._last_maintenance = now
        self.maintenance()

    def maintenance(self) -> Dict[str, Any]:
        """执行一轮完整维护：Forget（Consolidator 已退役）。

        Consolidator 退役（2026-09-09）：提纯职能由 v2 两相管线（pipeline.py）接管。
        这里仍保留 `consolidator` 字段以便 cron/health 探针可观测（status=retired），
        不再触发任何合并循环。

        返回:
            维护统计
        """
        stats: Dict[str, Any] = {
            # Consolidator 退役（2026-09-09）：保留字段供观测，无运行接线
            "consolidator": {"status": "retired", "reason": "v2 pipeline takeover"},
            "forgetter": {"status": "skipped"},
        }

        # Step 1: Selective Forgetting（Consolidator 已摘除，仅剩 Forget）
        if self._forgetter:
            try:
                result = self._forgetter.forget()
                stats["forgetter"] = result
                logger.info("keepsake: forgetting done — %s", result)
            except Exception as e:
                logger.warning("keepsake: forgetting error: %s", e)
                stats["forgetter"] = {"status": "error", "reason": str(e)}


        return stats

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FEEDBACK_SCHEMA, HOT_TOPICS_SCHEMA]

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        """Route tool calls to the appropriate handler."""
        import json as _json

        if tool_name == "keepsake_feedback":
            return self._handle_feedback(args, _json)
        elif tool_name == "keepsake_topics":
            return self._handle_hot_topics(args, _json)
        return tool_error(f"Unknown keepsake memory tool: '{tool_name}'")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_feedback(self, args: Dict[str, Any], _json) -> str:
        key = args.get("fragment_key", "")
        is_pos = bool(args.get("is_positive", True))
        if not key:
            return tool_error("fragment_key is required")
        if not self._storage:
            return tool_error("Memory storage not initialized")
        ok = self._storage.record_feedback(key, is_pos)
        if ok:
            action = "有用 👍" if is_pos else "没用 👎"
            return _json.dumps({"success": True, "action": action, "key": key})
        return tool_error("Failed to record feedback")

    def _handle_hot_topics(self, args: Dict[str, Any], _json) -> str:
        limit = min(int(args.get("limit", 10)), 30)
        period = args.get("period", "all")
        if not self._storage:
            return tool_error("Memory storage not initialized")
        topics = self._storage.get_hot_topics(limit=limit, period=period)
        return _json.dumps({"topics": topics, "count": len(topics)}, ensure_ascii=False)

    def shutdown(self) -> None:
        # v2（2026-09）：先排空管线，再关 storage；保证 shutdown 前队列内消息不丢
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None:
            try:
                pipeline.stop(drain=True, timeout=5.0)
            except Exception as e:
                logger.warning("keepsake: pipeline stop failed: %s", e)
        # 2026-10 ks_push_consumer：停掉推送线程（幂等；它是 daemon，停不掉
        # 也不影响进程退出 —— 留着只会白连一个不存在的服务端）。
        try:
            _stop_push_consumer()
        except Exception as e:
            logger.warning("keepsake: push consumer stop failed: %s", e)
        if self._storage:
            self._storage.close()
        logger.info("keepsake memory provider shutdown")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Hermes 常驻记忆（MEMORY.md / USER.md）写入时，**原文**存为 core 碎片。

        2026-10 keepsake_core_tier_v1：这是把常驻记忆收进 keepsake 的唯一入口 ——
        与本方法原先的「直接 return」（2026-09 ingest_gate R5 关闭双份入库）相反，
        现在两侧合并成同一套：常驻记忆存进碎片库并打上 `core` 标签，
        `system_prompt_block` 每轮原文全量注入，检索层不再重复召回。

        🔴 **原文逐字入库，绝不经 LLM 提炼 / 摘要改写**：常驻记忆是 agent 行为的
        硬约束（「端口是 8091」被改写成「用户偏好某端口」就是记忆损坏）。
        仍走 `scrub_secrets` 打码 —— 那是安全闸（R7），不是内容改写。

        🔴 **任何异常都吞掉 + warning，绝不影响 Hermes 主流程**：本方法由 Hermes
        在 memory 工具的成功路径上同步调用，抛出去会让用户的记忆写入失败。
        """
        try:
            self._core_write(action, target, content, metadata or {})
        except Exception as e:
            logger.warning("keepsake: on_memory_write(%s/%s) 失败（已吞掉）: %s",
                           action, target, e)

    def _core_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> None:
        """`on_memory_write` 的实际动作。**调用方保证异常已被吞掉。**

        action 语义（对齐 Hermes memory 工具）：
          * `add`                  → 入库为 core 碎片
          * `replace` / `update`    → 先删旧（metadata 的 key 或 old_text）再写新
          * `remove`                → 删掉 metadata 指的那条
        识别不了的 action 一律 warning + 跳过，绝不猜。
        """
        if not self._storage:
            return
        action = (action or "").strip().lower()
        # remove 不需要新内容：Hermes 对 remove 传的 content 是空串，在这里早退会让
        # memory(action=remove) 变成完全无效的操作（2026-10-12 实测：删除从未生效）。
        if action != "remove" and (not content or not content.strip()):
            return

        # R7：入库前凭据打码（与 _v1_store_after_decide 同一入口，同一口径）。
        # 只打码、不改写其余文字 —— 原文入库是硬要求。
        from ...core.ingest_gate import scrub_secrets
        try:
            if content:
                scrubbed, _n = scrub_secrets(content)
                if scrubbed and scrubbed.strip():
                    content = scrubbed
        except Exception as e:
            logger.debug("keepsake: scrub_secrets failed in _core_write: %s", e)

        tags = f"{_CORE_TAG},{target or 'memory'}"

        if action == "remove":
            self._core_delete(metadata, content or "")
        elif action in ("replace", "update"):
            # 先删旧再写新。找不到旧条目时退化成「新增」—— 用户重写一条记忆
            # 而 Hermes 没给 old_text/key 时，这是唯一不丢内容的做法。
            self._core_delete(metadata, metadata.get("old_text") or "")
            self._core_add(content, tags)
        elif action == "add":
            self._core_add(content, tags)
        else:
            logger.warning("keepsake: on_memory_write action=%r 无法识别，跳过", action)
            return

        # 写入/删除成功 ⇒ 核心层缓存立即失效（下一轮立刻看到新内容）。
        _CORE_CACHE["ts"] = 0.0
        _CORE_CACHE["frags"] = None

    def _core_add(self, content: str, tags: str) -> None:
        """原文入库为 core 碎片；同 content 已存在则不重复入库（幂等）。

        幂等判据用「现存 core 碎片里有逐字相同的一条」而不是靠 `store()` 的
        内容 hash 去重 —— 后者对 PG/SQLite 的版本化语义不保证，而常驻记忆
        被重复注入是要用户能看出来的故障。
        """
        for f in self._core_fragments():
            if f.get("content") == content:
                return
        self._storage.store(
            text=content,
            tags=tags,
            category="core",
            source="builtin_memory",
            fragment_type="core",
        )

    def _core_delete(self, metadata: Dict[str, Any], fallback_text: str) -> None:
        """删掉 metadata 指向的那条 core 碎片。

        解析顺序（Hermes 侧契约见 agent/memory_manager.notify_memory_tool_write）：

          1. `metadata["key"]` —— 明确指定，直接用；
          2. `metadata["previous_content"]` —— 镜像带过来的**旧条目全文**，精确匹配；
          3. `old_text` / fallback —— 先精确全文，再**唯一子串**匹配。

        🔴 第 3 步的子串匹配是必需的：Hermes memory 工具规定 `old_text` 是「一段能唯一定位
        条目的短子串」而不是全文。只做全文相等 ⇒ replace/remove 时旧条目删不掉、新条目另存
        一条，同一事实库里两份（2026-10-12 实测 core 里已有这样一对）。

        命中多条时**不猜**：记 warning 并跳过 —— 宁可不删，也不能删错记忆。
        完全没命中时同样只 warning：replace 会退化成新增（既有语义），remove 则等于没删，
        由 Hermes 那侧决定重试。
        """
        key = metadata.get("key")
        if not key:
            candidates = [t.strip() for t in (metadata.get("previous_content"),
                                              metadata.get("old_text"), fallback_text)
                          if t and t.strip()]
            if not candidates:
                return
            frags = self._core_fragments()
            key = self._resolve_core_key(frags, candidates)
            if not key:
                logger.warning(
                    "keepsake: core 删除未命中（old_text=%r，候选 %d 个，core %d 条）——"
                    "replace 会退化成新增，remove 等于没删",
                    candidates[-1][:80], len(candidates), len(frags))
        if key:
            self._storage.delete_fragments_batch([key])

    @staticmethod
    def _resolve_core_key(frags: List[Dict[str, Any]], candidates: List[str]) -> Optional[str]:
        """候选文本 → 唯一 core key：逐候选先全文精确，再唯一子串；多命中不猜（返回 None）。"""
        def _norm(value: Optional[str]) -> str:
            return (value or "").strip()

        for text in candidates:
            needle = _norm(text)
            for frag in frags:
                if _norm(frag.get("content")) == needle:
                    return frag.get("key")
        for text in candidates:
            needle = _norm(text)
            if not needle:
                continue
            hits = [f for f in frags if needle in (f.get("content") or "")]
            if len(hits) == 1:
                return hits[0].get("key")
            if len(hits) > 1:
                logger.warning("keepsake: core 删除文本命中 %d 条（%r）——歧义，跳过以免删错",
                               len(hits), needle[:60])
                return None
        return None

    # ------------------------------------------------------------------
    # 通用记忆契约（adapters/generic/protocol.py）—— 2026-10 keepsake_p0_layout_v1
    #
    # 下面这组方法**不参与 Hermes 的每轮对话路径**：Hermes 走的是
    # system_prompt_block / prefetch / sync_turn / on_memory_write。
    # 它们是把同一个存储能力按「与宿主无关的契约」再暴露一遍，供通用适配层
    # （别的宿主、非 Hermes 的调用方）使用。新增，不改动任何既有路径。
    #
    # 失败语义（protocol.py 里的三条铁律，此处逐条落地）：
    #   * 注入层 fail-open —— system_prompt_block / prefetch 见上文，本组不涉及
    #   * 检索层显式抛错 —— search / get **不 try/except**，后端异常原样抛出，
    #     绝不塌成空列表（静默空 = 记忆搜不到且无告警）
    #   * 写层 fail-closed —— add / update / remove 捕获后端异常并返回 falsy +
    #     warning，绝不谎报成功
    # ------------------------------------------------------------------

    def _require_storage(self):
        """取 storage；未 initialize 时抛错（读路径按铁律「显式抛错」处理）。"""
        if not self._storage:
            raise RuntimeError(
                "keepsake: storage 未初始化 —— 先调用 initialize(session_id=...)"
            )
        return self._storage

    @staticmethod
    def _fragment_key_for(text: str) -> str:
        """碎片 key = `memory:frag:{sha256(text)[:12]}`。

        三个后端的 `store()` 用的是同一套算法（Redis / SQLite 内联，
        PG 抽成了 `PgStorage._fragment_key_for`），这里为了让 `add()` 能把
        key 交回给调用方而复刻一行。⚠️ 改任一后端的算法必须同步改这里 ——
        `tests/contract/test_provider_protocol.py` 用真 SqliteStorage
        跑 add→get 做交叉校验，改歪了会红。
        """
        return f"memory:frag:{hashlib.sha256(text.encode()).hexdigest()[:12]}"

    # ---- 检索层（显式抛错）----

    def _filter_excluded(self, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """剔除带 `retrieval.exclude_tags` 噪音标签的命中（多代理治理的第三条规则）。

        `search()` / `prefetch()` 两个检索出口统一走这里 —— 命中形态是
        `{"content":…, "tags": "逗号串", …}`，标签判据复用 `_has_tag`
        （逗号边界的完整标签相等，与存储层 SQL/Python 判据同口径）。
        排除发生在截断**之前**：不让噪音挤掉本该进上下文的名额。
        """
        tags = self._exclude_tags
        if not tags:
            return hits
        return [h for h in hits
                if not any(_has_tag(h.get("tags", ""), t) for t in tags)]

    def search(self, query: str, *, limit: int = 10,
               caller_agent_id: str = "", caller_role: str = "") -> List[Dict[str, Any]]:
        """按查询检索，返回最多 `limit` 条命中。

        🔴 显式抛错：后端不可达 / 查询非法都让异常往外走，**绝不返回 []** ——
        空列表在契约里只有一个含义「确实没有匹配」。

        `caller_agent_id` / `caller_role`（2026-10 ks_share_memory）：**MCP 远端
        调用方**的身份（凭据反查得出）。缺省 = 本进程自己（主脑直连存储），
        此时可见范围与开关上线前逐字一致。
        """
        storage = self._require_storage()
        scope = self._visibility_scope(caller_agent_id, caller_role)
        hits = self._filter_excluded(
            list(storage.search(query.strip(), tag_filter=self._tag_filter,
                                 **self._scope_kwargs(scope))))
        self._touch("read")      # 活动计数：主脑每轮都在检索，这条路径必须记
        return hits[:limit]

    def get(self, key: str, *, caller_agent_id: str = "",
            caller_role: str = "") -> Optional[Dict[str, Any]]:
        """按 key 取单条碎片；**确实不存在**返回 None，后端故障抛错。

        🔴 **归属校验**（2026-10 ks_share_memory）：非 primary 取到「未共享代理」
        的条目 ⇒ 返回 **not-found 语义**（None）—— 存在性本身也不许泄露。
        无归属的条目（老数据 / 共享层）人人可见。
        """
        storage = self._require_storage()
        row = storage.get_fragment(key)
        self._touch("read")
        if not row:
            return None
        scope = self._visibility_scope(caller_agent_id, caller_role)
        if scope.visible is not None:
            owner = fragment_owner(row.get("tags", ""))
            if owner and owner not in scope.visible:
                return None
        return row

    # ---- 可见性（2026-10 ks_share_memory）------------------------------

    def _visibility_scope(self, caller_agent_id: str = "",
                          caller_role: str = "") -> _ReadScope:
        """这次读取**看得见什么** —— 唯一口径入口（判据全在 `govern.visibility`）。

        * `caller_agent_id` 空 ⇒ **本进程自己**（provider 路径：主脑直连存储）。
          角色沿用既有来源（config 的 `is_primary`）—— 读路径的 primary 判定
          **逐字不变**（primary 全量可见是硬回归线）。
        * `caller_agent_id` 非空 ⇒ **MCP 远端调用方**（身份由闸门按凭据反查），
          角色以闸门给的为准（谁是 primary 由注册表说了算）。
        * 注册表不可用 ⇒ 读路径照旧（不过滤）：治理面挂掉不该让记忆读不出来
          （写闸那边是 fail-closed，方向相反：写要保守，读要照旧）。
        """
        registry = self._registry
        if caller_agent_id:
            aid = str(caller_agent_id).strip()
            role = str(caller_role or "").strip()
            if not role and registry is not None:
                # 注册表读面残缺（替身 / 半成品）⇒ 判不出角色就按 sub（收窄）
                role_of = getattr(registry, "role_of", None)
                role = str(role_of(aid) or "") if callable(role_of) else ""
            is_primary = role == REGISTRY_ROLE_PRIMARY
            explicit = True
        else:
            aid = self._agent_id
            is_primary = bool(self._is_primary)
            explicit = False
        if registry is None:
            return _ReadScope(aid, is_primary, None, explicit)
        ids = visible_agent_ids(aid, registry, is_primary=is_primary)
        return _ReadScope(aid, is_primary,
                          tuple(sorted(ids)) if ids is not None else None, explicit)

    @staticmethod
    def _scope_kwargs(scope: _ReadScope) -> Dict[str, Any]:
        """把可见范围翻译成存储层的调用参数。

        🔴 **只在真需要时才带新参数**：`visible_agents` 为空就不传 ——
        既有替身（测试里的假存储）没有这个形参，传了会 TypeError；
        语义上也正好是「集合为空 ⇒ 与开关上线前逐字相同」。
        """
        kwargs: Dict[str, Any] = {}
        if scope.explicit:
            # 调用方是**别人**（MCP 远端凭据）：身份与角色按它传，不能沿用存储自己的
            kwargs["agent_id"] = scope.caller_id
            kwargs["is_primary"] = scope.is_primary
        if scope.visible:
            extra = [a for a in scope.visible if a != scope.caller_id]
            if extra:
                kwargs["visible_agents"] = extra
        return kwargs

    # ---- 写层（fail-closed）----

    # ------------------------------------------------------------------
    # 治理收口（2026-10 ks_agent_registry）
    # ------------------------------------------------------------------

    def _init_governance(self, cfg: dict) -> None:
        """装配代理注册表：自登记 → 本机 bootstrap → 迁移退役的 exclude 键。

        **绝不因治理失败而拦住对话**（与本文件其余注入路径同款 fail-open）：
        注册表不可用时 `self._registry` 留 None，写闸据此 fail-closed 按 sub
        （见 `_registry_is_primary`）—— 记忆写入降级，人还能用。
        """
        # 本进程自己的身份（注册表登记 / 未读私信提醒都要用）。
        # 🔴 放在最前面：注册表不可用时身份仍然成立（提醒是附加值，不该被注册表带崩）。
        self._agent_id = str(cfg.get("agent_id") or self._agent_id or "")
        # 信箱句柄（未读私信提醒用，2026-10 ks_dm_hint）：与注册表**同一个
        # storage** ⇒ 不另开连接、不另拼 DSN。装配失败也不拦对话（提醒是附加值）。
        try:
            self._keepsake_messaging = Messaging(self._storage)
            # 🔴 建表 DDL 放在**启动**这一步（不放在检索热路径上）：
            # 检索每轮都跑，DDL 不该混在里面（PG 上建索引会拿锁）。
            if not self._keepsake_messaging.ensure_schema():
                logger.warning("keepsake: 信箱 schema 未就绪（未读私信提醒可能不可用）")
        except Exception as e:      # noqa: BLE001 —— 提醒读不到就不提醒
            logger.warning("keepsake: 信箱句柄装配失败（未读提醒不可用）: %s", e)
        try:
            registry = Registry(self._storage)
            if not registry.ensure_schema():
                logger.warning("keepsake: 注册表 schema 未就绪，写闸将按 sub 处理（fail-closed）")
                return
            # 本机文件通道：批准自己；`is_primary` 为真且库里还没有主脑时才上位。
            # 🔴 这一行的 `is_primary` 只是**首次引导**的种子，之后角色一律由注册表
            #    说了算（控制台改一次即生效，写闸 5s 内收敛）。
            registry.bootstrap_local(self._agent_id, self._agent_id,
                                     as_primary=bool(cfg.get("is_primary", False)))
            registry.migrate_exclude_tags(cfg)
            self._registry = registry
            self._role_cache = None
            # 「是否进主脑上下文」现在是每个代理身上的开关：被排除的代理，
            # 它的 `agent:<id>` 标签进检索排除表（配置键退役后的新家）。
            extra = tuple(
                f"{AGENT_TAG_PREFIX}{a['agent_id']}"
                for a in registry.list_agents() if a["exclude_from_primary"]
            )
            if extra:
                self._exclude_tags = tuple(dict.fromkeys(self._exclude_tags + extra))
            logger.info("keepsake: 代理注册表已装配（agent_id=%s）", self._agent_id)
        except Exception as e:      # noqa: BLE001 —— 治理不拦对话（写闸 fail-closed 兜底）
            logger.warning("keepsake: 注册表装配失败（写闸按 sub 处理）: %s: %s",
                           type(e).__name__, e)

    def _registry_is_primary(self) -> bool:
        """**我是不是主脑** —— 判定源是注册表角色（5s 缓存），不是 config 自报。

        🔴 三条 fail-closed 纪律：
          * 没装配注册表（裸实例 / 单测）⇒ 回落 `_is_primary`（保持既有装配行为）；
          * 注册表里查不到 / 不是 primary ⇒ sub；
          * **注册表抛异常 ⇒ sub**（宁可多拦一个 core 写入，也不放进一个假 primary）。
        """
        registry = self._registry
        if registry is None:
            return bool(self._is_primary)
        import time as _time

        now = _time.monotonic()
        cache = self._role_cache
        if cache is not None and now - cache["ts"] < _ROLE_CACHE_TTL:
            return bool(cache["is_primary"])
        try:
            info = registry.verify_local(self._agent_id)
            is_primary = bool(info.get("ok")) and info.get("role") == REGISTRY_ROLE_PRIMARY
        except Exception as e:      # noqa: BLE001 —— fail-closed 按 sub
            logger.warning("keepsake: 注册表角色查询失败（按 sub 处理）: %s", type(e).__name__)
            is_primary = False
        self._role_cache = {"ts": now, "is_primary": is_primary}
        return is_primary

    def _touch(self, kind: str) -> None:
        """打活动计数（`reads` / `writes`）—— **本机 provider 侧的记账点**。

        🔴 为什么主脑路径必须自己打（2026-10 计数排查）：`Registry.touch()`
        原先只有一个调用点 —— MCP 闸门 `server.py:237` 的 `record_activity`。
        那是**子代理**经 MCP 连上来时的路径；主脑是同进程直接调 provider 的
        `search()` / `add()`，**从不经过 MCP 闸门**，于是它的 reads/writes 恒为 0
        （面板上「主代理 0/0」的由来）。挂点补在这里，语义与 MCP 那侧逐字一致。

        三条 fail-open 纪律（与本文件其余注入路径同款）：
          * 没装配注册表（裸实例 / 单测）⇒ 直接返回；
          * `touch()` 抛异常 ⇒ 吞掉记日志，**绝不让计数把记忆读写带崩**；
          * 限频 60s 在 `Registry.touch()` 内部，这里不重复实现。
        """
        registry = self._registry
        if registry is None or not self._agent_id:
            return
        try:
            registry.touch(self._agent_id, kind)
        except Exception as e:      # noqa: BLE001 —— 计数是遥测，不是账本
            logger.debug("keepsake: 活动计数失败（忽略）: %s: %s", type(e).__name__, e)

    def _apply_write_gate(self, tags: str, *, core_direct: bool = False,
                          caller_role: Optional[str] = None
                          ) -> Tuple[str, bool]:
        """写闸：写 `core` 一律**不直通**常驻层（多代理治理第二条 + 2026-10 常驻层入口改革）。

        🔴 2026-10 ks_agent_registry：判定源已从 config 自报的 `_is_primary`
        切到 **registry 的角色**（`self._registry_is_primary()`，5s 缓存；
        注册表不可达时 fail-closed 按 sub —— 见那处的三条纪律）。

        🔴 **谁在写**（2026-10 ks_share_tighten 收紧）：`caller_role` 是 **MCP 远端
        调用方的角色**（凭据反查得出）。它是**权威**的 —— 远端调用方的身份不能
        由「托管这个进程的进程」替它回答（否则一个外部代理拿着 core 标签就能
        借主脑的进程身份绕审核进常驻）：

        * `None` ⇒ 本进程自己（Hermes 同进程直连）⇒ 按 `_registry_is_primary()` 判
          （既有行为逐字不变）；
        * `"primary"` ⇒ 调用方是 primary；
        * 其它（含空串）⇒ 非 primary（**判不出角色就当 sub**，fail-closed）。

        三条分支（标签判据是**逗号边界的完整标签相等**，复用 `_has_tag`，
        `core_x` / `xcore` 不算命中；其余标签原样保留、顺序不变）：

        * **非 primary** 写 `core` ⇒ 降级为 `core_candidate` **+ 提名日戳**
          （2026-10 ks_perm_scope：**子代理自荐常驻 = 与主脑同流程** —— 此前
          「降级但不打戳」等于**死候选**，班车永远不审它；打上提名戳后与主脑
          候选**同一条审核/班车链**：人工批准、热度自动过，一视同仁）；
        * **primary** 写 `core` ⇒ **不再直通**：落 `core_candidate` + 提名日戳
          （2026-10 常驻层入口改革：常驻层从此只有「审核页人工批准」与
          「候选满观察期自动通过」两个入口）；
        * **人工显式直写**（`core_direct=True`，MCP `memory_add` 显式 core）
          ⇒ **只对 primary 生效**：primary 原样放行 —— 那是「人在批」的等价动作
          （控制台 promote / CoreView 新增同理，都不走这里）；非 primary 带这个
          标志**不生效**（照常降级成候选 —— 显式直写不能绕过治理）。

        返回 `(改写后的 tags, 是否提名)`：第二个值为 True 时，这条是**新提名的
        候选**（⇒ 写闸负责补召回基数，见 `core.tiering.backfill_nominate_hits`）。
        """
        if not _has_tag(tags, _CORE_TAG):
            return tags, False
        from ...core import tiering as tiering_mod

        role = None if caller_role is None else str(caller_role).strip()
        is_primary = (role == REGISTRY_ROLE_PRIMARY if role is not None
                      else self._registry_is_primary())
        if is_primary:
            if core_direct:
                return tags, False          # 人工显式直写：原样放行
            new_tags = tiering_mod.candidate_tags(tags, datetime.now(timezone.utc))
            logger.warning(
                "keepsake: primary 写 core 不再直通常驻层，落候选（tags: %r → %r）",
                tags, new_tags)
            return new_tags, True
        # 非 primary：候选 + **提名日戳照打**（与主脑候选同一条链）
        new_tags = tiering_mod.candidate_tags(tags, datetime.now(timezone.utc))
        logger.warning(
            "keepsake: 非 primary 写 core 降级为 %s（提名日戳照打，"
            "与主脑候选同一条审核链）（tags: %r → %r）",
            CORE_CANDIDATE_TAG, tags, new_tags)
        return new_tags, True

    def add(
        self,
        text: str,
        *,
        tags: str = "",
        meta: Optional[Dict[str, Any]] = None,
        core_direct: bool = False,
        caller_role: Optional[str] = None,
        caller_agent_id: str = "",
    ) -> str:
        """写入一条记忆，返回它的 key；写入失败返回空串（falsy）。

        `meta` 目前只承载 `source` / `category` / `fragment_type` 三个字段 ——
        它是契约里预留的扩展位，不认识的键直接忽略，不报错。

        🔴 `core_direct=True` = **人工显式直写**（MCP `memory_add` 显式写 core）：
        这是「人在批」的等价动作，**只对 primary 生效**（primary 写 `core` 时仍
        直通常驻层）。自动管线与通用协议路径**不传**这个标志 ⇒ 写 core 一律落候选
        （2026-10 常驻层入口改革，判据见 `_apply_write_gate`）。

        🔴 `caller_role` = **谁在写**（2026-10 ks_share_tighten 收紧）：MCP 远端
        调用方的角色（凭据反查得出）。`None` ⇒ 本进程自己（按本进程的身份判，
        既有行为逐字不变）；给了值就以它为准（判不出角色 ⇒ 按 sub，fail-closed）。

        🔴 `caller_agent_id` = **归属章盖谁的**（2026-10 ks_owner_stamp）：MCP 远端
        调用方的 agent_id（凭据反查得出）。非空 ⇒ 落库时归属打 `agent:<它>`
        （它自报的 `agent:xxx` 一律被剥掉 —— **不许自报归属**）；空 ⇒ 落
        **进程默认身份**（本机直写/提炼管线路径逐字不变）。
        """
        meta = meta or {}
        try:
            storage = self._require_storage()
            gated_tags, nominated = self._apply_write_gate(
                tags, core_direct=core_direct, caller_role=caller_role)
            key = self._fragment_key_for(text)
            # 同内容已存在 ⇒ 存储层会**版本化**（新条走 `key:epoch` 独立 key，
            # 旧条归档）。那种情况下本函数返回的 key 指向的是归档旧条 ⇒
            # 基数不能补在它身上（那会给一条归档条目凭空加热、还会被班车
            # 当成热门提名）。所以先问一句「这条内容是不是新的」。
            duplicated = False
            if nominated:
                try:
                    duplicated = bool(storage.fragment_exists(key))
                except Exception as e:      # noqa: BLE001 —— 查不到就当新的
                    logger.debug("keepsake: fragment_exists 失败（按新条目处理）: %s", e)
            ok = storage.store(
                text,
                tags=gated_tags,
                category=str(meta.get("category", "")),
                source=str(meta.get("source", "")),
                fragment_type=str(meta.get("fragment_type", "")),
                **_owner_kwargs(storage.store, caller_agent_id),
            )
        except Exception as e:
            logger.warning("keepsake: add 失败（fail-closed，返回空串）: %s", e)
            return ""
        if not ok:
            logger.warning("keepsake: add 未落库（fail-closed，返回空串）")
            return ""
        self._touch("write")     # 落库成功才记：写失败的尝试不是写入活动
        if nominated and not duplicated:
            # 提名即带基数（2026-10 常驻层入口改革）：这条刚进了候选队列 ⇒
            # 补上召回基数，让它「看起来够热」（补失败不影响这次写入）。
            from ...core import tiering as tiering_mod

            tiering_mod.backfill_nominate_hits(storage, key, _nominate_backfill_hits())
        return key

    def update(
        self,
        key: str,
        text: str,
        expected_rev: str = "",
        *,
        tags: str = "",
        meta: Optional[Dict[str, Any]] = None,
        caller_agent_id: str = "",
        caller_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """**乐观并发（CAS）改写**一条记忆（2026-10 ks_memory_rev_cas）。

        🔴 **为什么不是「原地改」**：记忆条目的 key 是**内容哈希** —— 改正文
        就意味着换 key（存储层的 `cas_update` 会建新条目 + 把旧条转链身，
        演化链完整可回溯）。这里负责的是**这一层之下的治理**：

          * **归属校验与 `add` 同尺**：非 owner 不能改别人的碎片；primary 全量。
            判据复用 `govern.visibility.can_remove_fragment`（**唯一一份**「谁能动
            这条」的尺子 —— 看得见 ≠ 能改）。
          * **core 内容写入照旧走候选审核队列**：`core_direct` 在这条路径上
            **恒为 False** ⇒ **不得因 update 开直通**（primary 写 core 也落候选，
            与 `add` 同一套闸门）。
          * **冲突时返回结构化结果**（`{"error": "conflict", …}`，**不抛裸异常**）：
            代理要能读懂「谁改了什么、现在是什么样」并据此合并重提。

        返回：
          * 成功 `{"ok": True, "key": <新 key>, "rev": <新 rev>, "previous_key": …}`
          * 冲突 `{"error": "conflict", "current_rev":…, "current_content":…,
            "updated_by":…, "current_key":…}`
          * 不存在 `{"error": "not_found"}`
          * 不归我 `{"error": "not_owner"}`
        """
        meta = meta or {}
        if not key:
            return {"error": "not_found"}
        if not (text or "").strip():
            return {"error": "invalid", "message": "text 不能为空"}
        try:
            storage = self._require_storage()
        except Exception as e:
            logger.warning("keepsake: update(%s) 失败（fail-closed）: %s", key, e)
            return {"error": "error", "message": str(e)}
        # 🔴 **后端不支持 CAS ⇒ 立刻显式失败**（不先读一遍再报 not_found）：
        #    那会把「这个后端做不了」说成「这条不存在」—— 调用方会一直往错的方向
        #    重试。Redis 是只读兜底后端，生产 = PG / SQLite。
        if not getattr(storage, "supports_rev_cas", False):
            raise BackendUnsupportedError(
                "backend_unsupported: rev CAS 需 PG/SQLite 后端"
            )

        # ---- 归属校验（与 add 同尺：非 owner 不能改别人的碎片；primary 全量）----
        row = storage.get_fragment(key)
        if not row:
            return {"error": "not_found"}
        if not self._may_write_fragment(row, caller_agent_id, caller_role):
            return {"error": "not_owner", "key": key}

        # ---- 写闸：core 照旧落候选队列（**不开直通**）----
        effective_tags = tags if tags else str(row.get("tags", "") or "")
        try:
            gated_tags, _nominated = self._apply_write_gate(
                effective_tags, core_direct=False, caller_role=caller_role)
        except Exception as e:      # noqa: BLE001 —— 闸门挂了就当没闸（fail-closed 写）
            logger.warning("keepsake: update(%s) 写闸异常（按原标签写）: %s", key, e)
            gated_tags = effective_tags

        fields: Dict[str, Any] = {"content": text, "tags": gated_tags}
        for name in ("category", "source", "fragment_type", "applicability"):
            if meta.get(name) is not None:
                fields[name] = meta[name]
        try:
            status, payload = storage.cas_update(key, fields, expected_rev)
        except Exception as e:      # noqa: BLE001
            # 🔴 `backend_unsupported`（Redis）**原样上抛**：那不是「失败」，
            #    是「这个后端做不了」—— 调用方必须显式知道，不许被吞成假成功。
            logger.warning("keepsake: update(%s) CAS 失败: %s: %s", key, type(e).__name__, e)
            raise
        if status == "ok":
            self._touch("write")
            return {
                "ok": True,
                "key": (payload or {}).get("new_key", ""),
                "rev": (payload or {}).get("new_rev", ""),
                "previous_key": key,
            }
        if status == "conflict":
            payload = payload or {}
            return {
                "error": "conflict",
                "current_key": payload.get("current_key", ""),
                "current_rev": payload.get("current_rev", ""),
                "current_content": payload.get("current_content", ""),
                "updated_by": payload.get("updated_owner", ""),
                "updated_at": payload.get("updated_at", ""),
            }
        return {"error": "not_found"}

    def _may_write_fragment(self, row: Dict[str, Any], caller_agent_id: str,
                            caller_role: Optional[str]) -> bool:
        """这个调用方**能不能改**这条（归属尺子 = `can_remove_fragment`，唯一一份）。

        🔴 为什么复用删除那把尺子：「看得见」≠「能改」。非 primary 只能改
        **自己的**条目与**无归属的老条目**；别人的条目 —— **含已共享代理的** ——
        一律不许（与 `memory_remove` 的归属闸逐条同款）。

        `caller_agent_id` 空 ⇒ **本进程自己**（主脑直连存储）⇒ 归属方 = 本进程
        身份 `self._agent_id`，primary 判定沿用 config 的 `self._is_primary`
        （与读路径 `_visibility_scope` 逐条同款，既有行为不变）。
        """
        if not caller_agent_id:
            caller = str(self._agent_id or "")
            is_primary = bool(self._is_primary)
        else:
            caller = str(caller_agent_id)
            is_primary = caller_role == REGISTRY_ROLE_PRIMARY
        owner = fragment_owner(row.get("tags", ""))
        return can_remove_fragment(owner, caller, is_primary=is_primary)

    def remove(self, target: str, expected_rev: Optional[str] = None) -> Any:
        """按 key 删除。

        * `expected_rev` 为 None（**默认**）⇒ **现行为逐字不变**：返回 bool。
        * `expected_rev` 给了 ⇒ **CAS 删除**：版本对不上返回**同款 conflict 结构**
          （与 `update()` 同形：带 `current_rev` / `current_content` / `updated_by`），
          且**行仍在**（不删）；删掉了返回 `{"ok": True, "removed": True, "key": …}`。
        """
        try:
            storage = self._require_storage()
        except Exception as e:
            logger.warning("keepsake: remove(%s) 失败（fail-closed，返回 False）: %s", target, e)
            return False
        if not expected_rev:
            # `expected_rev` 空 ⇒ **现行为逐字不变**（向后兼容已接入的老代理）
            try:
                ok = bool(storage.delete_fragments_batch([target]))
            except Exception as e:
                logger.warning("keepsake: remove(%s) 失败（fail-closed，返回 False）: %s",
                               target, e)
                return False
            if ok:
                self._touch("write")
            return ok
        try:
            status, payload = storage.cas_remove(target, expected_rev)
        except Exception as e:      # noqa: BLE001
            logger.warning("keepsake: remove(%s) CAS 失败: %s: %s", target, type(e).__name__, e)
            raise
        if status == "ok":
            self._touch("write")
            return {"ok": True, "removed": True, "key": target}
        if status == "conflict":
            payload = payload or {}
            return {
                "error": "conflict",
                "current_key": payload.get("current_key", ""),
                "current_rev": payload.get("current_rev", ""),
                "current_content": payload.get("current_content", ""),
                "updated_by": payload.get("updated_owner", ""),
                "updated_at": payload.get("updated_at", ""),
            }
        return {"error": "not_found"}

    # ---- 维护层 ----

    def merge(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """合并。Consolidator 的运行接线已于 2026-09-09 退役（v2 两相管线接管提纯），
        所以这里**恒定报告 retired**，既不扫描也不写库 —— 一个字都不动。
        """
        stats = {"status": "retired", "reason": "v2 pipeline takeover",
                 "dry_run": bool(dry_run), "merged": 0, "groups": 0}
        logger.info("keepsake: merge() 被调用 —— consolidator 已退役，不做任何合并")
        return stats

    def forget(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """遗忘。`dry_run=True`（默认）走 forgetter 自带的 dry_run 口径，
        只统计不删；`dry_run=False` 传 force=True 强制实际删除。"""
        if not self._forgetter:
            return {"status": "skipped", "reason": "forgetter not initialized"}
        return self._forgetter.forget(force=not dry_run)

    def capabilities(self) -> Capabilities:
        """能力声明。core 层与 KNN 是 keepsake 的既定能力，开箱即有。

        `push_consumer=True` 表示**能**接推送（实现了可选契约
        `PushEventConsumer`）—— 是否真的挂流由 `push_consumer.enabled` 决定
        （缺省关 ⇒ 行为与上线前逐字相同）。

        `notify_outlet=True` 表示**能**把未读私信提醒打到渠道 webhook
        （只含元数据，无正文）—— 是否真的发由 `push_consumer.notify_url`
        决定（缺省空 ⇒ 零外呼）。
        """
        return Capabilities(core_tier=True, knn=True, dry_run_merge=True,
                            push_consumer=True, notify_outlet=True, version="v1")
