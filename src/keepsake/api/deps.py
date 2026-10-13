"""FastAPI 依赖：Bearer 认证 + keepsake 存储后端单例。

认证从 P1 的「一把静态 token」升级成 P3 的「会话 token（登录换来的），
**旧静态 token 仍然放行** —— 我的脚本与后续 MCP 都在用它，不许因为加了登录
就把它们全打死。
"""

from __future__ import annotations

import hmac
import threading
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

from . import auth as auth_mod
from . import config

# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


def require_session(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """校验 `Authorization: Bearer <token>`，返回当前会话信息。

    两条放行路径（任一即可）：

    * **会话 token**（P3 登录换来的）—— 服务端只存其 sha256，比对常数时间；
    * **旧静态 token**（`CONSOLE_TOKEN` / token 文件）—— 等价超管令牌。

    缺失 / 格式错 / 值不对 ⇒ 401，比较用 `hmac.compare_digest`（常数时间，避免
    逐字节计时侧信道）。**token 本身不出现在任何异常消息里** —— 401 的 detail
    是固定文案。返回体里的 `token` 只在进程内流转给 `logout` 撤销用，不落日志。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "缺少 Authorization: Bearer <token>"},
        )
    got = authorization[len("Bearer "):].strip()
    if not got:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "缺少 Authorization: Bearer <token>"},
        )

    legacy = config.console_token()
    # 🔴 比对前先 encode 成 bytes：`hmac.compare_digest` 对非 ASCII 的 str 直接抛
    # TypeError，那会让「随便一个畸形 Authorization 头」变成 500 —— 信任边界上的输入
    # 校验不许把 401 降级成服务端错误。
    if legacy and hmac.compare_digest(got.encode("utf-8"), legacy.encode("utf-8")):
        return {"username": "static-token", "expires_at": "", "token": got, "legacy": True}

    session = auth_mod.verify_session(got)
    if session is not None:
        session["legacy"] = False
        return session

    # 静态 token 没配 **且** 从来没签发过会话 ⇒ 报「未配置」（P1 的既有文案，
    # 沿用同一套排障口径）；配了静态 token 却没对上 ⇒ 报「不匹配」。
    if not legacy and not auth_mod.admin_exists():
        raise HTTPException(
            status_code=401,
            detail={"error": "token_not_configured",
                    "message": "控制台 token 未配置（CONSOLE_TOKEN 或 token 文件）"},
        )
    raise HTTPException(
        status_code=401,
        detail={"error": "invalid_token", "message": "token 不匹配或已过期"},
    )


#: 旧名字保留（P1/P2 的路由与外部脚本按名字引用过）
require_token = require_session


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
