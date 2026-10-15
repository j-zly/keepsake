"""知识库**写入**的共用层（`ingest`）：控制台写端点与灌入器 CLI 同源。

只读检索在 `keepsake.api.routers.kb`；这里管的是「一篇文档怎么切块、
怎么幂等落库」——两处必须共用同一份判据，否则 API 写进去的文档会被
灌入器切成另一种粒度，检索命中与页面显示对不上。
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
