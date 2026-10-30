"""通用记忆接口契约（`MemoryProvider` Protocol）。

本模块**不许 import 任何 Hermes 的东西** —— 它描述的是「一个记忆系统对外
应该长什么样」，与宿主是谁无关。Keepsake 自带的 `KeepsakeProvider`
（`adapters/hermes/provider.py`）顺带满足它，但那是实现方向的事：
依赖方向只准 `adapters.hermes → adapters.generic`，反向绝不允许。

--------------------------------------------------------------------------
失败语义（与现网口径逐字一致，写进来是为了让新实现不自己发明一套）
--------------------------------------------------------------------------
🔴 **注入层 fail-open**
    `system_prompt_block()` / `prefetch()` 位于每轮组装上下文的**关键路径**上。
    这里抛异常等于整轮对话失败 —— 记忆是增强项，不该有能力让对话瘫掉。
    实现必须吞掉后端异常并降级成静态文案 / 空串（至多 logger.warning）。

🔴 **检索层显式抛错，绝不静默返回空**
    `search()` / `get()` 的失败形态是**抛异常**。静默返回 `[]` 是最危险的
    失败形态：记忆静默搜不到、没有告警，用户只会以为「AI 忘了」。

🔴 **写层 fail-closed**
    `add()` / `update()` / `remove()` 的失败形态是**明确报告失败**
    （返回 falsy / 抛错 / 返回带 `status=error` 的统计），绝不假装成功。
    写失败若被当成成功，调用方会继续往下走并向用户确认「已保存」—— 那是在撒谎。

--------------------------------------------------------------------------
自检
--------------------------------------------------------------------------
`tests/contract/test_provider_protocol.py` 做双向契约测试：
正向拿 Fake 与真 `SqliteStorage` 各跑一遍，反向故意违反必须被检出。

--------------------------------------------------------------------------
可选扩展
--------------------------------------------------------------------------
`PushEventConsumer` 是**另立的一份可选契约**（推送事件回调注册）——
基础契约 `MemoryProvider` 一个成员都不许为它让路（那会让所有第三方实现
跟着改）。宿主想接推送，就额外实现这一份；不实现，一切照旧。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from ...domain.types import Capabilities, Fragment, Hit, PushEvent


@runtime_checkable
class MemoryProvider(Protocol):
    """记忆系统对外契约。

    @runtime_checkable：只校验**方法是否齐全**（非数据协议），
    不校验签名 —— 签名由 `tests/contract/test_provider_protocol.py`
    用 `inspect.signature` 单独钉。
    """

    # ---- 注入层（fail-open）----
    def system_prompt_block(self) -> str:
        """常驻提示块（每轮注入）。后端不可用时返回静态文案，绝不抛。"""
        ...

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """按查询检索并渲染成待注入文本。后端不可用时返回空串，绝不抛。"""
        ...

    # ---- 检索层（显式抛错）----
    def search(self, query: str, *, limit: int = 10) -> List[Hit]:
        """检索。后端不可用 / 查询非法时**抛异常**，绝不返回空列表冒充「没找到」。"""
        ...

    def get(self, key: str) -> Optional[Fragment]:
        """按 key 取单条；key 不存在返回 None（这是「确实没有」，不是「查不到」）。"""
        ...

    # ---- 写层（fail-closed）----
    def add(self, text: str, *, tags: str = "", meta: Optional[Dict[str, Any]] = None) -> str:
        """写入一条记忆，返回其 key。失败必须抛错或返回空串，绝不谎报成功。"""
        ...

    def update(
        self,
        key: str,
        text: str,
        expected_rev: str = "",
        *,
        tags: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """**乐观并发（CAS）改写**一条记忆（2026-10 ks_memory_rev_cas）。

        🔴 **为什么要 CAS**：共享记忆被多个代理同时读写。两个代理基于同一版
        各自改写 ⇒ 各存一条（key=内容哈希）⇒ **互相看不见对方**（内容分叉，
        检索时两条都命中，代理却以为自己改成功了）。乐观并发（HTTP ETag /
        If-Match、CouchDB _rev）把「我读到的还是不是最新版」变成一次可判定的
        比对：**读带 rev，写声明 expected_rev，对不上就拒并回最新内容**。

        `expected_rev` 空串 = **不做版本校验**（老调用方的兼容路径；
        但并发场景下**必须**传，否则照样分叉）。

        返回结构（**结构化，不抛裸异常** —— 代理要能读懂并重试）：

          * 成功：`{"ok": True, "key": <新 key>, "rev": <新 rev>, "previous_key": …}`
          * 冲突：`{"error": "conflict", "current_rev":…, "current_content":…,
            "updated_by":…, "current_key":…}` ← **带对方最新内容**，据此合并重提
          * 不存在：`{"error": "not_found"}`

        🔴 **后端不支持 CAS 时必须抛错**（不许静默降级成「无条件覆盖」——
        那会让调用方拿到「成功」回执，而并发写照样分叉）。
        """
        ...

    def remove(self, target: str, expected_rev: str = "") -> Any:
        """删除；key 不存在或后端失败返回 False。

        `expected_rev` 给了 ⇒ **CAS 删除**：版本对不上返回**同款 conflict 结构**
        （与 `update()` 同形），且**行仍在**（不删）；不给 ⇒ **现行为逐字不变**。
        """
        ...

    # ---- 维护层 ----
    def merge(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """合并。`dry_run=True` 只统计不写（默认）；能力声明见 `Capabilities.dry_run_merge`。"""
        ...

    def forget(self, *, dry_run: bool = True) -> Dict[str, Any]:
        """遗忘。`dry_run=True` 只统计不删（默认，安全模式）。"""
        ...

    def capabilities(self) -> Capabilities:
        """能力声明。调用方据此决定能不能问后端要某项能力。"""
        ...


@runtime_checkable
class PushEventConsumer(Protocol):
    """**可选**扩展：推送事件回调的注册口（2026-10 ks_push_consumer）。

    🔴 **刻意不进 `MemoryProvider` 基础契约**：基础契约只许长出「每个实现
    都必须实现」的东西 —— 塞一个可选方法进来，第三方实现与
    `tests/contract/test_provider_protocol.py` 里的 Fake 会当场翻红。
    这里另立一份**可选**契约：宿主实现了它就能收推送摘要，没实现一切照旧
    （推送层只是少一个出口，记忆读写一行都不变）。

    事件摘要的形状见 `domain.types.PushEvent`：`{event, seq, from, unread}`
    —— **没有正文**（私信正文永远走信箱本体）。

    🔴 回调在消费者线程里被调用 ⇒ 实现必须快、必须自己吞异常。
    """

    def on_push_event(self, fn: Callable[[PushEvent], None]) -> None:
        """注册推送事件回调（同一个回调重复注册只算一次）。"""
        ...


__all__ = ["MemoryProvider", "PushEventConsumer", "Capabilities", "Fragment", "Hit", "PushEvent"]