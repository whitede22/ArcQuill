"""故障可见性：一个轮转文件、一个 excepthook。

## 为什么要有这个文件

打包后的程序没有控制台。在此之前全仓没有 `import logging`，也没有
`sys.excepthook`——任何未处理异常、任何被 `except` 吞掉的失败，都只表现成
「点了没反应」。用户说不清发生了什么，开发者手上一个字都没有。

## 写什么，不写什么

日志**只记事件，不记内容**。词条正文、备注、标签、密钥、接口返回片段一律不落
文件：日志会被用户随手发到 issue 里，而他发的时候不会逐行检查。

两道保险：

1. 调用方只传事件名与结构化字段（`_write_failed`、`field=notes`），不传词条；
2. 不得不带异常原文时（`log_exception`）先过 `scrub()`：引号内的词、形如密钥
   的长串都会被抹成占位符。

这不是「有个开关所以安全」的设计——没有开关。要么不写内容，要么写的时候
已经抹过。
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 单文件 512 KB，留三份。够装下几十次崩溃的栈，也不会把用户盘占出可见的量。
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3

LOGGER_NAME = "arcquill"
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "app.log"

# 抹除规则。顺序不重要，各自独立：
#   1. `sk-` 开头的密钥；2. 任意 32 位以上的长串（多数平台密钥的形态）；
#   3. 中文引号里的内容——报错文案里唯一会带用户输入的位置就是那里
#      （「接口没有返回「ubiquitous」的释义」）。
_REDACTIONS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "***"),
    (re.compile(r"「[^」]{1,120}」"), "「…」"),
)

_configured = False
_log_path: Path | None = None
_hook_installed = False
# excepthook 里弹过一次框就不再弹：一次崩溃常常连着触发多次（每个槽函数一次），
# 叠一摞一模一样的对话框比崩溃本身更让人没法处理。
_reported = False


def scrub(text: str) -> str:
    """把可能属于用户内容或凭据的片段抹掉。"""
    out = str(text or "")
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def log_path() -> Path | None:
    """当前日志文件路径；未初始化时为 None。"""
    return _log_path


def setup_logging(app_dir: Path | str) -> Path:
    """在 `app_dir/logs/app.log` 上装一个轮转处理器，返回日志路径。

    可重复调用：第二次起直接返回同一路径，不叠加处理器（叠加的表现是同一条
    记录出现两遍，看起来像「程序跑了两次」）。
    """
    global _configured, _log_path

    log_dir = Path(app_dir) / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / LOG_FILE_NAME
    if _configured:
        return _log_path or target

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = RotatingFileHandler(
        target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)

    _configured = True
    _log_path = target
    return target


def get_logger(name: str) -> logging.Logger:
    """子记录器。未初始化时写不出去，但也不会抛错。"""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_exception(logger: logging.Logger, event: str, exc: BaseException) -> None:
    """记一条「这里失败了」，带上抹过的异常类型与栈。

    栈本身要留：出事的行号是唯一能定位问题的东西，而它不含用户内容。
    异常原文要抹：文案里可能嵌着用户查的词。
    """
    detail = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    logger.error("%s | %s\n%s", event, type(exc).__name__, scrub(detail))


def install_excepthook() -> None:
    """把未处理异常写进日志，并当面告诉用户日志在哪。

    弹窗只弹一次：`_reported` 一旦置位就不再打扰。用户此刻需要的是「知道
    去哪看」，不是被同一段话拦三次。
    """
    global _hook_installed
    if _hook_installed:
        return
    _hook_installed = True

    logger = get_logger("crash")

    def _hook(exc_type, exc, tb) -> None:
        global _reported
        if exc_type is KeyboardInterrupt:
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("未处理异常 | %s\n%s", exc_type.__name__, scrub(detail))
        if _reported:
            return
        _reported = True
        _announce(exc_type, exc)

    sys.excepthook = _hook


def _announce(exc_type, exc) -> None:
    """一条「出错了」的说明。没有 Qt 就只留日志。"""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:  # noqa: BLE001 - 无界面环境（脚本、自检）只记日志
        return
    try:
        if QApplication.instance() is None:
            return
        where = log_path()
        tail = f"\n\n日志：{where}" if where else ""
        QMessageBox.critical(
            None,
            "出错了",
            f"程序遇到一个未处理的错误，已经记进日志。{tail}\n\n"
            f"若反复出现，把这份日志发给作者即可，里面不含你查过的词。\n\n"
            f"{exc_type.__name__}: {scrub(str(exc))[:300]}",
        )
    except Exception:  # noqa: BLE001 - 报错路径本身出错就只能放弃
        return
