"""FastAPI 依赖：Bearer 认证 + keepsake 存储后端单例。

**本单只读**：这里只提供「读」依赖（认证 / 取后端），不提供任何写依赖。
"""

from __future__ import annotations

import hmac
import threading
from typing import Any, Optional

from fastapi import Header, HTTPException

from . import config

# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """校验 `Authorization: Bearer <token>`；缺失 / 格式错 / 值不对 ⇒ 401。

    比较用 `hmac.compare_digest`（常数时间，避免逐字节计时侧信道）。
    **token 本身不出现在任何异常消息里** —— 401 的 detail 是固定文案。
    """
    expected = config.console_token()
    if not expected:
        # 未配置 token ⇒ 一律拒绝。配置缺失绝不能退化成「无认证放行」。
        raise HTTPException(
            status_code=401,
            detail={"error": "token_not_configured",
                    "message": "控制台 token 未配置（CONSOLE_TOKEN 或 token 文件）"},
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "缺少 Authorization: Bearer <token>"},
        )
    got = authorization[len("Bearer "):].strip()
    # 🔴 比对前先 encode 成 bytes：`hmac.compare_digest` 对非 ASCII 的 str 直接抛
    # TypeError，那会让「随便一个畸形 Authorization 头」变成 500 —— 信任边界上的输入
    # 校验不许把 401 降级成服务端错误。
    if not got or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": "token 不匹配"},
        )


# --------------------------------------------------------------------------
# 存储后端（只读用）
# --------------------------------------------------------------------------

_STORAGE: Any = None
_STORAGE_LOCK = threading.Lock()


def get_storage() -> Any:
    """返回进程级 keepsake 存储后端单例（走 `storage_from_config`，不自己拼连接）。

    🔴 **只经存储层取数**：本控制台不自建 SQL、不绕过 `storage.*` 原语
    （知识库那两条只读查询是唯一例外，见 routers/kb.py，且只读）。
    构造失败直接抛 —— 由全局异常处理器转成 500，不吞成空列表。
    """
    global _STORAGE
    if _STORAGE is None:
        with _STORAGE_LOCK:
            if _STORAGE is None:
                from keepsake.storage import storage_from_config

                _STORAGE = storage_from_config(config.storage_config_path())
    return _STORAGE


def reset_storage() -> None:
    """清掉进程级单例（测试用：换配置后重新构造）。"""
    global _STORAGE
    with _STORAGE_LOCK:
        _STORAGE = None
