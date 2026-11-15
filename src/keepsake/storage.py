"""兼容层：`keepsake.storage` → 真实实现已搬到 `keepsake.core.factory`。

历史：本模块曾是 Redis 后端的正身，后改为 `core/redis.py` 的模块别名；
2026-11 Redis 后端整体移除，工厂/后端判定/权重键口径独立成 `core/factory.py`，
别名随之改指它 —— cron/脚本里 30+ 处 `from keepsake.storage import
storage_from_config` 逐字可用。

用 sys.modules 模块别名而不是 `from .core.factory import *`：别名与实现是**同一个
module 对象**，共享同一份 globals —— monkeypatch("keepsake.storage.X") 才会打在
实现真正使用的名字上（拷贝式 re-export 会让 patch 打空）。

注意：`RedisStorage` 名字随 Redis 后端一起消失（不再转口）。需要构造存储一律走
`storage_from_config()`；`storage.backend` 必须显式 `postgres` / `sqlite`。
"""

import importlib as _importlib
import sys as _sys

_impl = _importlib.import_module("keepsake.core.factory")
_sys.modules[__name__] = _impl
