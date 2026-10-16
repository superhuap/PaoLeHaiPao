"""日志。

只输出到控制台（stderr），**不写任何文件**。cron 下直接重定向即可留档：

    python3 runner.py > /dev/null 2>> run.log

需要多个落点时由 shell 负责，而不是在这里堆 handler。

凭据在写入前脱敏 —— DEBUG 级会打印解密后的明文，而终端回滚缓冲、tmux、
录屏都可能把它带出去。
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from datetime import datetime

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()

# 需要脱敏的字段：值可能出现在报文追踪里
_SECRET_KEYS = re.compile(
    r'"(token|access_token|refresh_token|run_face_verify_id|face_verify_id|sign)"'
    r'\s*:\s*"([^"]*)"',
    re.IGNORECASE)


def _abbreviate(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def mask(text: str) -> str:
    """把报文里的凭据替换成首尾片段。"""
    return _SECRET_KEYS.sub(
        lambda m: f'"{m.group(1)}":"{_abbreviate(m.group(2))}"' if m.group(2)
        else f'"{m.group(1)}":""',
        text)


class Formatter(logging.Formatter):
    """时间 + 级别 + 账号。调度器自身的记录显示为「调度」。"""

    def format(self, record: logging.LogRecord) -> str:
        account = getattr(record, "account", None) or "调度"
        return (f"{self.formatTime(record, DATE_FORMAT)}  "
                f"{record.levelname:<5}  [{account}] {record.getMessage()}")


class ConsoleHandler(logging.Handler):
    """串行化的 stderr 输出。多账号并行时线程会交错，必须加锁。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            with _lock:
                sys.stderr.write(line)
                sys.stderr.flush()
        except Exception:                                  # noqa: BLE001
            self.handleError(record)


def configure(level: str = "INFO", quiet: bool = False) -> logging.Logger:
    """初始化根 logger。只挂一个控制台 handler，重复调用只生效一次。"""
    logger = logging.getLogger("runner")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    handler = ConsoleHandler()
    handler.setFormatter(Formatter())
    # 静默模式下只留警告以上，避免刷屏
    handler.setLevel(logging.WARNING if quiet else logging.NOTSET)
    logger.addHandler(handler)
    return logger


def run_id() -> str:
    """本次运行的标识，只用于关联同一次运行的多条记录（纯内存，不落盘）。"""
    return datetime.now().strftime("%H%M%S")