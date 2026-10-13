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
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ...domain.types import Capabilities, Fragment, Hit


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
        *,
        content: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> bool:
        """局部更新；key 不存在或后端失败返回 False。"""
        ...

    def remove(self, target: str) -> bool:
        """删除；key 不存在或后端失败返回 False。"""
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


__all__ = ["MemoryProvider", "Capabilities", "Fragment", "Hit"]