"""兼容层：`keepsake.forgetter` → 真实实现已搬到 `keepsake.core.ops.forgetter`。

Forgetter（选择性遗忘算子）。搬家是为了分层，本文件保证旧导入路径**逐字不变**。

用 sys.modules 模块别名而不是 `from .core.ops.forgetter import *`：别名与实现是**同一个
module 对象**，所以模块级私有名（`_SNAPSHOTS` / `_expand_terms` 之类）与
monkeypatch 落点都还在原处；`import *` 会拷一份快照，patch 就会打空 ——
那才是真正的行为变化。

⚠️ 只搬文件、零行为变化：本文件不含任何实现代码。
"""

from __future__ import annotations

import sys as _sys
import importlib as _importlib

_impl = _importlib.import_module("keepsake.core.ops.forgetter")
_sys.modules[__name__] = _impl
