"""`python -m keepsake.jobs` 的进程入口 —— 见 `keepsake.jobs` 的模块文档。

用法:
    python -m keepsake.jobs distill|dedup|tiering [--config PATH] [--dry-run]
"""

from __future__ import annotations

from . import app

if __name__ == "__main__":
    app()
