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

--------------------------------------------------------------------------
分层（2026-10 keepsake_p0_layout_v1）
--------------------------------------------------------------------------
实现本体已按三层搬走，本文件**只做对外门面**：

    domain/    纯逻辑、零 IO（emotion / splitter / types）
    core/      存储与检索（redis / pg / sqlite / shared / pipeline / ops）
    adapters/  hermes/provider.py（实现）、generic/（与宿主无关的契约）

兼容层：`storage` / `storage_pg` / `storage_sqlite` / `storage_shared` /
`embedder` / `consolidator` / `splitter` / `attention` / `pipeline` 等
**旧导入路径一个都没断**，cron 与 scripts 里 30+ 处调用逐字可用。

本文件不做普通的 `from .adapters.hermes.provider import *` re-export，
而是把 `sys.modules["keepsake"]` **指向 provider 模块本身**（模块别名）：
别名共享同一份 globals，于是 `keepsake.RedisStorage`、`keepsake._CORE_CACHE`、
`keepsake._has_tag` 这些名字原封不动，而 monkeypatch("keepsake.X") 也仍然
打在 provider 真正使用的那份 globals 上 —— 拷贝式 re-export 会让 patch 打空，
那才是真正的行为变化。`__path__` 显式带过来、子包属性补挂、模块级 `__getattr__`
兜住懒解析，子模块路径与搬家前一致。
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys

from .adapters.hermes import provider as _provider

# 子模块（keepsake.core.* / keepsake.adapters.* / keepsake.storage* 等）靠它定位
_provider.__path__ = __path__  # type: ignore[attr-defined]

# 上面那次 import 是在本文件**执行中**发生的，导入器把子包属性挂到了即将被丢弃的
# 原 __init__ module 上。把它们补挂到 provider 上，`import keepsake.adapters.generic`
# / `from keepsake import core` 这类写法才不会扑空。
for _sub in ("core", "domain", "adapters"):
    _mod = _sys.modules.get(f"{__name__}.{_sub}")
    if _mod is not None:
        setattr(_provider, _sub, _mod)


def _compat_getattr(name: str):
    """子模块懒解析 —— 保住搬家前的 `from keepsake import splitter` 这类写法。

    模块别名的代价：`sys.modules["keepsake"]` 指向 provider 模块后，IMPORT_FROM
    找不到属性时按 `keepsake.<name>` 回退 sys.modules，而本模块的 `__name__` 已不是
    "keepsake"，那条回退路径会扑空。这里补一个 PEP 562 的模块级 `__getattr__`，
    按名字 import 子模块 —— 与搬家前「导入器把子模块挂成包属性」的效果一致，
    且保持懒加载（不会为了兼容把 psycopg 依赖的 storage_pg 提前拉起来）。
    """
    if not name.startswith("_"):
        try:
            return _importlib.import_module(f"{__name__}.{name}")
        except ImportError:
            pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_provider.__getattr__ = _compat_getattr  # type: ignore[attr-defined]

_sys.modules[__name__] = _provider