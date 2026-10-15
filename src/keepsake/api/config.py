"""控制台（只读）运行配置 —— 全部来自环境变量或文件，**零硬编码密钥**。

本单（keepsake_p1_console_ro_v1）后端严格只读：只挂 GET 路由，
所有凭据靠环境变量注入，代码里不出现任何 token / 连接串 / 口令字面量。

取值优先级：**环境变量 > 文件 > 既有 keepsake 默认值**。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------

#: 环境变量名（有它就不读文件）
TOKEN_ENV = "CONSOLE_TOKEN"

#: token 文件兜底路径（环境变量 KEEPSAKE_CONSOLE_TOKEN_FILE 可改）
DEFAULT_TOKEN_FILE = "/root/.hermes/secrets/keepsake_console.token"

# --------------------------------------------------------------------------
# 管理员凭据 / 会话（P3 登录）
# --------------------------------------------------------------------------

#: 管理员凭据文件（存 username/salt/hash/iters，**只有哈希，没有明文口令**）
ADMIN_FILE_ENV = "KEEPSAKE_CONSOLE_ADMIN_FILE"
DEFAULT_ADMIN_FILE = "/root/.hermes/secrets/keepsake_console_admin.json"

#: 会话文件（存 token 的 sha256、username、expires_at —— **不存 token 原值**）
SESSION_FILE_ENV = "KEEPSAKE_CONSOLE_SESSION_FILE"
DEFAULT_SESSION_FILE = "/root/.hermes/secrets/keepsake_console_sessions.json"

#: 会话默认存活秒数；``remember=true`` 时用 REMEMBER_SESSION_TTL（30 天）
SESSION_TTL_ENV = "KEEPSAKE_CONSOLE_SESSION_TTL"
DEFAULT_SESSION_TTL = 12 * 3600
REMEMBER_SESSION_TTL = 30 * 86400

# --------------------------------------------------------------------------
# 存储 / 知识库 / 定时任务
# --------------------------------------------------------------------------

#: keepsake 自身配置（config.json）路径 —— 透传给 `storage_from_config`
STORAGE_CONFIG_ENV = "KEEPSAKE_CONFIG"

#: 知识库 PG 连接串 —— **只从环境读**，代码里绝不出现连接串与口令
PG_DSN_ENV = "KEEPSAKE_PG_DSN"

#: cron jobs.json 路径（任务书给的是 /root/.hermes/cron/jobs.json；
#: 这里按既有 provider 的 `~/.hermes/cron` 口径取，env 可改）
CRON_JOBS_ENV = "KEEPSAKE_CRON_JOBS"
DEFAULT_CRON_JOBS = str(Path.home() / ".hermes" / "cron" / "jobs.json")

#: 知识库 schema（表名固定为 skill_documents/documents/chunks —— 任务书口径）
PG_SCHEMA_ENV = "KEEPSAKE_PG_SCHEMA"
DEFAULT_PG_SCHEMA = "public"

#: 知识库三表名（任务书给定）
KB_TABLE_SKILL_DOCS = "skill_docs"
KB_TABLE_DOCUMENTS = "documents"
KB_TABLE_CHUNKS = "chunks"

# --------------------------------------------------------------------------
# 静态资源 / 分页
# --------------------------------------------------------------------------

#: 前端构建产物目录；不存在时只挂 API（后端仍可单独跑）
STATIC_DIR_ENV = "CONSOLE_STATIC_DIR"
DEFAULT_STATIC_DIR = str(Path(__file__).resolve().parents[3] / "web" / "dist")

#: 分页硬上限（防止 ?page_size=999999 一次拉全表）
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: 列表里 content 的截断长度（单条详情接口返回全文）
CONTENT_PREVIEW_CHARS = 200

#: 检索层计数上限：存储层没有 count/offset 原语，全库总数只能 keyset 扫描
#: （本库几千行，可接受）。扫到上限就 `count=null` + `count_reason=scan_capped`，
#: **不谎报总数**。stats 总览与 fragments 列表共用这一份口径。
RETRIEVAL_SCAN_CAP = 5000


def _env(name: str) -> str:
    """读环境变量并 strip；空值视作未设置（避免 " " 被当真 token）。"""
    return (os.environ.get(name) or "").strip()


def console_token() -> str:
    """取控制台 token：`CONSOLE_TOKEN` 优先，其次 token 文件。

    两处都没有 ⇒ 返回空串（调用方按「未配置」拒绝一切 /api/* 请求，
    401 而不是放行 —— 配置缺失绝不能退化成无认证）。

    🔴 本函数**只把 token 交给比较方**，任何返回体 / 日志 / 异常消息里
    都不许出现 token 本身。
    """
    tok = _env(TOKEN_ENV)
    if tok:
        return tok
    path = Path(_env("KEEPSAKE_CONSOLE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def admin_file() -> Path:
    """管理员凭据文件路径（测试与 88 环境用环境变量指到临时目录）。"""
    return Path(_env(ADMIN_FILE_ENV) or DEFAULT_ADMIN_FILE)


def session_file() -> Path:
    """会话文件路径。"""
    return Path(_env(SESSION_FILE_ENV) or DEFAULT_SESSION_FILE)


def session_ttl() -> int:
    """默认会话存活秒数（`remember=true` 走 30 天那个常量，不受此变量影响）。

    配了非法值（负数 / 非数字）⇒ 回落默认值 —— 配置坏了不该把会话变成永不过期。
    """
    try:
        n = int(_env(SESSION_TTL_ENV) or DEFAULT_SESSION_TTL)
    except ValueError:
        return DEFAULT_SESSION_TTL
    return n if n > 0 else DEFAULT_SESSION_TTL


def storage_config_path() -> Optional[str]:
    """keepsake config.json 路径；未设则回 None（让 storage_from_config 用其默认）。"""
    return _env(STORAGE_CONFIG_ENV) or None


def pg_dsn() -> str:
    """知识库 PG 连接串（仅环境变量）。空串 = 未配置，kb 接口按数据源不可用报 500。"""
    return _env(PG_DSN_ENV)


def pg_schema() -> str:
    """知识库 schema 名（标识符，不是密钥）。"""
    return _env(PG_SCHEMA_ENV) or DEFAULT_PG_SCHEMA


def cron_jobs_path() -> str:
    """cron jobs.json 路径。"""
    return _env(CRON_JOBS_ENV) or DEFAULT_CRON_JOBS


def static_dir() -> Path:
    """前端构建产物目录。"""
    return Path(_env(STATIC_DIR_ENV) or DEFAULT_STATIC_DIR)


def core_char_budget() -> int:
    """常驻层注入预算（字符数）—— **只转发 provider 的 `_core_budget()`**。

    唯一真值源是 `retrieval.core_char_budget`（provider 侧按 config.json mtime
    缓存热载）。控制台再读一遍 config 或再存一份常量，都会让「页面显示的
    占用率」和「注入时真正截断的阈值」在某次改配置后悄悄对不上 —— 口径只有
    一份，两处调同一个函数。
    """
    try:
        from keepsake.adapters.hermes.provider import _core_budget

        return int(_core_budget())
    except Exception:       # noqa: BLE001 —— provider 不可导入时用兜底常量
        return 60000


def keepsake_config_file() -> Path:
    """provider 注入路径实际读的 config.json 完整路径（写配置的端点用这个）。

    必须走 provider 的 `config_path()`：读取端解析 env 的规则若与写入端各写
    一份，就会出现「PUT 返回成功、注入却读的还是另一个文件」。
    """
    try:
        from keepsake.adapters.hermes.provider import config_path

        return config_path()
    except Exception:       # noqa: BLE001 —— provider 不可导入时退回默认路径
        return Path(_env(STORAGE_CONFIG_ENV)
                    or "~/.config/keepsake/config.json").expanduser()


def forget_core_budget_cache() -> None:
    """改完 config.json 后丢弃 provider 侧的预算缓存（写完必须调，见下）。

    为什么不能只靠 (mtime_ns, size) 指纹：**同尺寸改值**（20000→30000）在
    mtime 粒度粗的文件系统上指纹一字不变 ⇒ 缓存命中 ⇒ 界面说「改成功了」，
    注入却还按老预算截断。写完主动失效是唯一不依赖文件系统精度的做法。
    """
    try:
        from keepsake.adapters.hermes.provider import invalidate_core_budget_cache

        invalidate_core_budget_cache()
    except Exception:       # noqa: BLE001 —— provider 不可导入时没有缓存可失效
        pass
