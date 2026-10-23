"""`python -m keepsake.jobs` —— 定时任务统一入口（2026-10 ks_perf_stdlib）。

宿主 cron 原本散着三个各写各的入口（`memory_distill_cron.sh` / `dedup-memory.sh` /
`tiering_cron.sh`），配置来源、路径、sys.path hack 各一份。本包把入口收进包内：

    python -m keepsake.jobs distill --dry-run
    python -m keepsake.jobs dedup   --dry-run
    python -m keepsake.jobs tiering --dry-run

三条纪律：

  * **唯一配置源**：默认读 `~/.config/keepsake/config.json`（与线上服务**同一份**），
    `--config PATH` 可指另一份；**不新增任何环境变量开关**（`KEEPSAKE_CONFIG` 是
    既有的、provider 也在用的那个）。
  * **薄壳**：只做「读整份 config → 调**既有**的 distill/dedup/tiering 入口函数 →
    打印与现有 shell 脚本等价的汇总行」。**既有函数一行不改**（distill/dedup 的
    入口脚本在仓库的 `cron/` 与 `scripts/` 下，按文件路径加载，不搬家不重写）。
  * **不裸 traceback**：配置坏了 / 任务跑不动 ⇒ 一行人话错误 + 明确退出码
    （2 = 配置/用法错，1 = 任务跑失败）。

🔴 错误信息打到 **stdout**（不是 stderr）：cron 的日志常常只收 stdout，
错误走 stdout 才不会「静默消失」；click 8.2+ 的 `CliRunner.output` 也是
stdout+stderr 混合，两边都看得到。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import typer

__all__ = ["app"]

#: 默认配置路径 —— 与线上服务同一份（`provider.config_path()` 的缺省值）。
DEFAULT_CONFIG_PATH = "~/.config/keepsake/config.json"

#: 既有入口脚本的位置（相对仓库根）—— **只加载，不搬家、不改写**。
_ENTRY_SCRIPTS = {
    "distill": ("cron", "memory_distill.py"),
    "dedup": ("scripts", "dedup_memory.py"),
}

app = typer.Typer(
    help="keepsake 定时任务统一入口（distill / dedup / tiering）",
    no_args_is_help=True,
)


class _ConfigError(Exception):
    """配置文件读不了 / 坏了 —— 一律转成「一行人话错误 + 退出码 2」。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _resolve_config_path(config: Optional[str]) -> Path:
    """配置路径：`--config` > `KEEPSAKE_CONFIG`（既有） > `~/.config/keepsake/config.json`。"""
    if config:
        return Path(config).expanduser()
    return Path(os.environ.get("KEEPSAKE_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def _load_config(path: Path) -> Dict[str, Any]:
    """读**整份** config.json。

    坏文件（不存在 / 读不了 / 非法 JSON / 根节点不是对象）⇒ 抛 `_ConfigError`
    —— 调用方转成明确错误，**绝不裸 traceback**。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _ConfigError(f"配置文件不存在: {path}")
    except OSError as e:
        raise _ConfigError(f"配置文件读不了: {path}（{type(e).__name__}: {e}）")
    try:
        cfg = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise _ConfigError(f"配置文件不是合法 JSON: {path}（{e}）")
    if not isinstance(cfg, dict):
        raise _ConfigError(f"配置文件根节点必须是 JSON 对象: {path}")
    return cfg


@contextmanager
def _config_env(path: Path, cfg: Dict[str, Any]) -> Iterator[None]:
    """把「这一轮用哪份配置」告诉**所有**内部读者（收口在一处）。

    * `KEEPSAKE_CONFIG` —— `provider.config_path()` 与 consolidator 的通道解析
      都读它 ⇒ 全进程指向同一份文件（唯一配置源纪律）。
    * `KEEPSAKE_REDIS_*` —— `scripts/dedup_memory.py` 在 **import 时**读一次连接
      配置（它自己的老口径）⇒ 这里把 config.json 的 redis 节喂给它，
      否则 `--config` 指了别的文件、它还在连默认那份。

    🔴 已知边界：那个脚本在 host == "127.0.0.1" 时**还会**回落读
    `~/.config/keepsake/config.json`，那份会盖掉这里的 env 值（脚本本单不改）。
    本单能做的都做了（把 `--config` 的 redis 节喂成 env）；真要统一口径，
    得改那个脚本 —— 留给主脑收口。

    退出时**原样恢复**环境变量（不许污染调用方的进程环境）。
    """
    keys = ("KEEPSAKE_CONFIG", "KEEPSAKE_REDIS_HOST", "KEEPSAKE_REDIS_PORT",
            "KEEPSAKE_REDIS_PASSWORD")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["KEEPSAKE_CONFIG"] = str(path)
        for env_key, cfg_key, cast in (
            ("KEEPSAKE_REDIS_HOST", "redis_host", str),
            ("KEEPSAKE_REDIS_PORT", "redis_port", int),
            ("KEEPSAKE_REDIS_PASSWORD", "redis_password", str),
        ):
            value = cfg.get(cfg_key)
            if value in (None, ""):
                continue
            try:
                os.environ[env_key] = cast(value)
            except (TypeError, ValueError):
                pass                      # 脏配置不连累任务（连接按老口径回落）
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# ---------------------------------------------------------------------------
# 既有入口脚本（按路径加载，零新依赖）
# ---------------------------------------------------------------------------

def _find_entry_script(kind: str) -> Optional[Path]:
    """找既有入口脚本的**真实路径**。

    打包安装（`pip install .`）时脚本不在 site-packages 里 ⇒ 先从**包所在位置**
    向上找，再从**当前工作目录**向上找；都找不到 ⇒ 返回 None（调用方明确报错）。
    """
    sub, name = _ENTRY_SCRIPTS[kind]
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            path = candidate / sub / name
            if path.is_file():
                return path
    return None


def _load_entry_module(kind: str):
    """按文件路径加载既有入口脚本（标准库 importlib —— 不新增依赖）。

    加载的是**原脚本本身**（不是复制品）⇒ 它的行为一个字都没变。
    """
    path = _find_entry_script(kind)
    if path is None:
        raise RuntimeError(
            f"找不到既有入口脚本 {_ENTRY_SCRIPTS[kind][0]}/{_ENTRY_SCRIPTS[kind][1]}"
            "（它不在包内）——请在 keepsake 仓库目录内运行，"
            "或继续用宿主原来的 cron 脚本"
        )
    mod_name = f"keepsake_jobs_{kind}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"既有入口脚本加载不了: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 三个子命令
# ---------------------------------------------------------------------------

@app.command()
def distill(
    config: Optional[str] = typer.Option(
        None, "--config", help="config.json 路径（缺省 ~/.config/keepsake/config.json）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只算不写（预演）"),
) -> None:
    """自动记忆提炼：扫最近会话 → LLM 提炼 → 写入 Keepsake。

    壳：读整份 config → 调既有入口 `cron/memory_distill.py:main()` →
    打印既有汇总行（**既有函数一行未改**）。
    """
    path = _resolve_config_path(config)
    try:
        cfg = _load_config(path)
    except _ConfigError as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(2)

    with _config_env(path, cfg):
        try:
            module = _load_entry_module("distill")
        except RuntimeError as e:
            typer.echo(f"错误: {e}")
            raise typer.Exit(2)
        argv = sys.argv
        sys.argv = ["memory_distill.py"] + (["--dry-run"] if dry_run else [])
        try:
            module.main()          # 既有入口（它自己打既有汇总行）
        except SystemExit:
            raise
        except Exception as e:
            typer.echo(f"错误: distill 失败（{type(e).__name__}: {e}）")
            raise typer.Exit(1)
        finally:
            sys.argv = argv
    typer.echo(f"distill: done (dry_run={dry_run})")


@app.command()
def dedup(
    config: Optional[str] = typer.Option(
        None, "--config", help="config.json 路径（缺省 ~/.config/keepsake/config.json）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只标记不写（预演）"),
    threshold: float = typer.Option(0.65, "--threshold", help="融合阈值（默认 0.65）"),
    batch_size: int = typer.Option(500, "--batch-size", help="单批扫描条数（默认 500）"),
) -> None:
    """碎片去重：多信号融合判重 → 把旧碎片标记过期。

    壳：读整份 config → 调既有入口 `scripts/dedup_memory.py:run_dedup()` →
    打印与既有脚本同格式的汇总行（**既有函数一行未改**）。
    """
    path = _resolve_config_path(config)
    try:
        cfg = _load_config(path)
    except _ConfigError as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(2)

    with _config_env(path, cfg):
        try:
            module = _load_entry_module("dedup")
        except RuntimeError as e:
            typer.echo(f"错误: {e}")
            raise typer.Exit(2)
        try:
            stats = module.run_dedup(threshold=threshold, batch_size=batch_size,
                                     dry_run=dry_run)
        except Exception as e:
            # 后端不可达（Redis 挂了 / 连不上）：预演不报红（cron 绿），
            # 实跑报错（cron 红）—— 与既有脚本「连不上就抛」的语义对齐，
            # 只是把裸 traceback 换成一行人话。
            if dry_run:
                typer.echo(f"dedup: 跳过（{type(e).__name__}: {e}）")
                typer.echo("Done: checked=0, matched=0, marked=0, skipped=0")
                return
            typer.echo(f"错误: dedup 失败（{type(e).__name__}: {e}）")
            raise typer.Exit(1)

    typer.echo(
        "Done: checked={checked}, matched={matched}, marked={marked}, "
        "skipped={skipped}".format(
            checked=int(stats.get("checked", 0)),
            matched=int(stats.get("matched", 0)),
            marked=int(stats.get("marked", 0)),
            skipped=int(stats.get("skipped", 0)),
        )
    )


@app.command()
def tiering(
    config: Optional[str] = typer.Option(
        None, "--config", help="config.json 路径（缺省 ~/.config/keepsake/config.json）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只算不写（预演）"),
) -> None:
    """常驻层分级班车：提名 / 自动升 / 压力降。

    壳：读整份 config → 调既有入口 `keepsake.core.tiering.run_tiering()` →
    打印与既有脚本同格式的 stats JSON（**既有函数一行未改**）。
    """
    path = _resolve_config_path(config)
    try:
        cfg = _load_config(path)
    except _ConfigError as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(2)

    from keepsake.core.tiering import resolve_tiering, run_tiering

    # 缺省关死（tiering.enabled=false）⇒ 干净退出，cron 不报红 —— 与既有
    # `scripts/tiering_cron.py` 逐字一致（连存储都不构造）。
    if not resolve_tiering(cfg)["enabled"]:
        typer.echo(json.dumps(
            {"skipped": "disabled", "dry_run": dry_run,
             "reason": "tiering.enabled=false"},
            ensure_ascii=False))
        return

    try:
        from keepsake.storage import storage_from_config
        # config_path 一并给上：只是让存储层的告警/日志指向**本轮这份**配置
        # （配置内容仍以 cfg 为准）。
        storage = storage_from_config(config_path=str(path), config=cfg)
        try:
            stats = run_tiering(storage, cfg, dry_run=dry_run)
        finally:
            # 🔴 必须显式关：共享池实例的 close 不回收注册表（那是生产语义，
            # 防网关泄漏用的），CLI 一次性进程里池活到解释器退出时
            # ConnectionPool.__del__ 会在 finalize 阶段 join 线程 ⇒
            # PythonFinalizationError 脏 stderr（2026-10-23 主脑实跑发现）。
            # 正解=与 tests/conftest.py::_reset_shared_pg_pools 同一口径：
            # 关池 + 清注册表。生产进程不走这条路，语义不受影响。
            try:
                storage.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                from keepsake.storage_pg import _SHARED_POOLS, _SHARED_POOL_SRC
                for _pool in _SHARED_POOLS.values():
                    try:
                        _pool.close()
                    except Exception:  # noqa: BLE001
                        pass
                _SHARED_POOLS.clear()
                _SHARED_POOL_SRC.clear()
            except Exception:  # noqa: BLE001 —— 非 PG 后端没这俩符号，正常
                pass
    except Exception as e:
        typer.echo(f"错误: tiering 失败（{type(e).__name__}: {e}）")
        raise typer.Exit(1)
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))
