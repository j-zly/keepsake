"""治理层（governance）—— 代理身份收口到中心注册表。

2026-10 keepsake_agent_registry：此前 `is_primary` / `agent_id` 全凭各代理
**自报 config**（`provider._resolve_config` 读 config、`factory.DEFAULT_AGENT_ID`
兜底）—— 伪造一份 config 就等于冒充主代理。本包把身份判定挪进一个中心注册表：

* 新代理接入默认 **pending**（读写全拒），管理员在控制台批准才放行；
* 角色（primary / sub）在注册表定，**全库唯一 primary**；
* 「是否进主脑上下文」从退役的 `retrieval.exclude_tags` 变成每个代理身上的开关；
* 凭据只在批准时生成一次（库里只有 sha256），人工转交。

本包只依赖标准库（不 import `core.*` / `api.*`），所以 `core/pg.py` /
`core/sqlite.py` 反向 import 它的 `KS_AGENT_DDL` 建表不会成环 —— 那段 DDL 的
**唯一定义处就在这里**，两个后端各写一份必然漂。
"""

from __future__ import annotations

from .registry import (  # noqa: F401 —— 门面转出，调用方一律 `from keepsake.govern import Registry`
    AGENT_TAG_PREFIX,
    CREDENTIAL_PREFIX,
    ROLE_PRIMARY,
    ROLE_SUB,
    ROLES,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REVOKED,
    STATUSES,
    Registry,
    new_credential,
)

__all__ = [
    "Registry",
    "new_credential",
    "ROLE_PRIMARY", "ROLE_SUB", "ROLES",
    "STATUS_PENDING", "STATUS_APPROVED", "STATUS_REVOKED", "STATUSES",
    "CREDENTIAL_PREFIX", "AGENT_TAG_PREFIX",
]
