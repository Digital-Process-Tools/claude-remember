import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
def _resolve_tz_from_env():
    tz_name = os.environ.get("REMEMBER_TZ", "").strip()
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None
def now() -> datetime:
    tz = _resolve_tz_from_env()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()
def today_str() -> str:
    return now().strftime("%Y-%m-%d")
def time_str() -> str:
    return now().strftime("%H:%M:%S")
