"""知识库的**共用层**：`ingest`（写判据）+ `service`（数据层与代理面服务）。

* `ingest` —— 控制台写端点、灌入器 CLI、代理面的 `kb_store` **同源**：
  「一篇文档怎么切块、怎么幂等落库、审核 status 链怎么走」只有这一份。
* `service` —— 连接 / 探列 / 写事务 + **代理面**的知识库服务（`kb_store` /
  `kb_search` / `kb_get` / `kb_remove` 的实现）。**FastAPI-free**：MCP 服务面
  不一定装了控制台的 fastapi（`pyproject.toml` 里 fastapi 压根不是依赖）。

控制台的 HTTP 端点在 `keepsake.api.routers.kb`（薄薄一层，转译 HTTP 语义）。
"""

from __future__ import annotations

from .ingest import (  # noqa: F401 —— 便捷出口：from keepsake.kb import split_chunks
    MANUAL_SOURCE_TYPE,
    content_hash,
    manual_source_key,
    replace_chunks,
    split_chunks,
    update_document,
    upsert_document,
)

__all__ = ["MANUAL_SOURCE_TYPE", "content_hash", "manual_source_key",
           "replace_chunks", "split_chunks", "update_document", "upsert_document"]
