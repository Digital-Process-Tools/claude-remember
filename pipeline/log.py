import os
import sys
from ._tz import time_str, today_str
from .types import TokenUsage
def _log_path(log_dir: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"memory-{today_str()}.log")
def _timestamp() -> str:
    return time_str()
def log(component: str, message: str, log_dir: str) -> None:
    line = f"{_timestamp()} [{component}] {message}\n"
    try:
        with open(_log_path(log_dir), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        print(line, file=sys.stderr, end="")
def log_usage(component: str, usage: TokenUsage, log_dir: str) -> None:
    log(component, f"tokens: {usage}", log_dir)
def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins}m" if mins else f"{hours}h"
