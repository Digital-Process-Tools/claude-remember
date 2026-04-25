"""Timezone-aware date helpers for the memory pipeline.

Reads the ``REMEMBER_TZ`` environment variable (set by ``scripts/log.sh``
from ``config.json``'s ``.timezone`` field) and produces dates/times in
that zone. Empty, unset, or invalid values fall back to system local
time — NOT UTC — so that shell and Python agree on "today" regardless
of how the plugin is installed or invoked.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _resolve_tz_from_env():
    """Return a ``ZoneInfo`` from ``REMEMBER_TZ`` env, or ``None`` for local."""
    tz_name = os.environ.get("REMEMBER_TZ", "").strip()
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None


def now() -> datetime:
    """Current datetime in ``REMEMBER_TZ`` if set, else naive system local."""
    tz = _resolve_tz_from_env()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


def today_str() -> str:
    """Today's date as ``YYYY-MM-DD`` in the resolved timezone."""
    return now().strftime("%Y-%m-%d")


def time_str() -> str:
    """Current time as ``HH:MM:SS`` in the resolved timezone."""
    return now().strftime("%H:%M:%S")
