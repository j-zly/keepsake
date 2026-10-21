"""默认 provider 装配 —— 从 keepsake **现有入口**取，不自建存储。

默认实现是 `adapters/hermes/provider.KeepsakeProvider` 的一个薄子类：
存储、检索、写入、遗忘全部原样复用（约束：绝不复制粘贴 storage / 检索实现）。
这里只做两件事：

  1. 补上 `MemoryProvider` 契约之外的两个**可选**扩展
     （`list_core` / `count_fragments`，见 `tools.StorageIntrospection`）；
  2. 关掉两个**宿主侧**副作用 —— 定时任务自注册、v2 后台提纯管线。
     它们属于「记忆挂在某个对话宿主里」的场景；MCP 进程不是那个宿主，
     让它去改宿主的 cron 表 / 起常驻线程是越界。

⚠️ 这里**没有**改动 `adapters/hermes/provider.py` 本体 —— Hermes 插件那条路逐字不变。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from ..adapters.hermes.provider import KeepsakeProvider, _load_json_config
from ..govern.visibility import visible_core_fragments

__all__ = ["KeepsakeMCPProvider", "default_provider", "DEFAULT_AGENT_ID", "SCAN_PAGE"]

#: 配置里没有 agent_id 时的兜底值。MCP 是独立进程，没有天然宿主 id；
#: 用固定值而不是空串 —— 空串会让后端的 agent 隔离把条目全部滤掉。
#:
#: 🔴 2026-10 ks_agent_registry：这个 id **只用于去注册表登记/判身份**，
#: 不再是「你就是主脑」的通行证 —— 闸门在 `server.AgentGate`，读的是
#: `KEEPSAKE_AGENT_ID` + `KEEPSAKE_CREDENTIAL`（凭据由控制台批准时签发）。
#: 新代理接入默认 pending，读写全拒。
DEFAULT_AGENT_ID = "mcp"

#: count_fragments 的 keyset 分页大小
SCAN_PAGE = 1000


class KeepsakeMCPProvider(KeepsakeProvider):
    """`KeepsakeProvider` + MCP 需要的两个可选扩展。Hermes 侧行为逐字不变。"""

    # ---- 可选扩展（MemoryProvider 契约之外）----

    def list_core(self, caller_agent_id: str = "",
                  caller_role: str = "") -> List[Dict[str, Any]]:
        """常驻层全量条目。走 `storage.list_by_tag("core")` —— 与控制台
        `GET /api/v1/core` 同一个原语，所以两边条数永远一致。

        刻意**不走** `KeepsakeProvider._core_fragments()`：那个带 60s 进程级缓存
        且读失败就返回空，MCP 是显式取数接口，缓存与预算兜底都会让调用方
        拿到「与库里不一致」的结果。

        🔴 **可见性同口径**（2026-10 ks_share_memory）：非 primary 只拿得到
        自己的 core + 已共享代理的 core（判据在 `govern.visibility`；primary 的
        core 也要 primary 自己开共享才外露 —— 对称规则）。

        🔴 **这里不按注入预算截**（2026-10 ks_agent_budget）：截断由**工具面**
        （`memory_core_list`）做 —— 那边能顺带给出「截了多少」的可见说明，
        而这里保持「与库里一致」（显式取数接口的既有语义）。
        """
        frags = list(self._require_storage().list_by_tag("core"))
        scope = self._visibility_scope(caller_agent_id, caller_role)
        if scope.is_primary or self._registry is None:
            return frags
        return visible_core_fragments(frags, self._registry, scope.caller_id)

    def count_fragments(self) -> int:
        """当前记忆条目总数（`memory_forget` 的 confirm 凭据）。

        `ponytail:` 存储层没有 count 原语，只能 keyset 扫描 —— 本库几千行，
        一次工具调用扫一遍完全可接受。上限 `_count_fragments` 侧会显式拒绝，
        不在这里悄悄截断（截断了 confirm 就成了假凭据）。
        """
        storage = self._require_storage()
        total, cursor = 0, ""
        while True:
            cursor, keys = storage.scan_fragment_keys(cursor, SCAN_PAGE)
            total += len(keys)
            if not cursor:
                return total

    # ---- 宿主侧副作用：MCP 进程里关掉 ----

    def _ensure_cron_jobs(self) -> None:
        """不注册 cron。MCP 不是 Hermes 宿主，不许去改宿主的 `~/.hermes/cron`。"""

    def _init_pipeline(self, llm_pipe_cfg: dict) -> None:
        """不起 v2 后台提纯管线。它是「对话轮次入队 → 提纯」的服务端组件，
        由宿主驱动；MCP 是被动的请求-响应端点，起常驻线程只会泄漏。
        写侧退回 v1（provider 内部本就有 `gate_fallback`）。"""


def default_provider(**overrides: Any) -> KeepsakeMCPProvider:
    """按 keepsake 现有配置装配默认 provider。

    配置来源与 provider 自身一致：`KEEPSAKE_CONFIG` → `~/.config/keepsake/config.json`
    （后端由 `storage.backend` 决定），外加 `KEEPSAKE_AGENT_ID` 等环境变量。
    不连生产库这件事由调用方保证（本包不读 `KEEPSAKE_PG_DSN` 以外的任何数据源）。
    """
    if "agent_id" not in overrides and not os.environ.get("KEEPSAKE_AGENT_ID"):
        overrides["agent_id"] = _load_json_config().get("agent_id") or DEFAULT_AGENT_ID
    provider = KeepsakeMCPProvider(**overrides)
    provider.initialize(session_id="mcp")
    if not provider._initialized:
        raise RuntimeError(
            "keepsake MCP：存储后端未就绪 —— 检查 KEEPSAKE_CONFIG 指向的配置与后端可达性"
        )
    return provider