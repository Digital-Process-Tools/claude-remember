"""Logging utilities for the memory pipeline.

Writes timestamped log lines to daily log files in the same format as
the shell-based ``log.sh``, ensuring Python and shell log entries are
interleaved cleanly in the same file.

Log format::

    HH:MM:SS [component] message

Log files are named ``memory-YYYY-MM-DD.log`` and created in the
configured log directory (typically ``.remember/logs/``).
"""

import os
import sys

from ._tz import time_str, today_str
from .types import TokenUsage


def _log_path(log_dir: str) -> str:
    """Return today's log file path, creating the directory if needed."""
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"memory-{today_str()}.log")


def _timestamp() -> str:
    """Return current time as HH:MM:SS string."""
    return time_str()


def log(component: str, message: str, log_dir: str) -> None:
    """Append a timestamped log line to today's log file.

    Falls back to stderr if the log file cannot be written.

    Args:
        component: Pipeline stage identifier (e.g., "save", "consolidate").
        message: Free-form log message text.
        log_dir: Directory where daily log files are stored.
    """
    line = f"{_timestamp()} [{component}] {message}\n"
    try:
        with open(_log_path(log_dir), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        print(line, file=sys.stderr, end="")


def log_tokens(component: str, usage: TokenUsage, log_dir: str) -> None:
    """Log token usage and cost for a Haiku call.

    Delegates to ``log()`` with the TokenUsage string representation,
    matching the format used by the shell ``log_tokens`` function.

    Args:
        component: Pipeline stage identifier.
        usage: Token counts and cost from the Haiku call.
        log_dir: Directory where daily log files are stored.
    """
    log(component, f"tokens: {usage}", log_dir)


def format_duration(seconds: int) -> str:
    """Format a duration in seconds as a compact human-readable string.

    Args:
        seconds: Duration in whole seconds.

    Returns:
        Compact string like "42s", "3m12s", "1h30m", or "2h".
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins}m" if mins else f"{hours}h"
