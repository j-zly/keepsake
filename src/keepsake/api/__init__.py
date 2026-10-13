"""Keepsake 只读控制台后端（FastAPI）。

P1 可视化后台的后端一半：**只挂 GET 路由**。前端在 `web/`（Vue3 + Element Plus）。
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app():
    """惰性导出 `keepsake.api.app.create_app`（避免 import 本包就拉起 FastAPI）。"""
    from .app import create_app as _create_app

    return _create_app()
