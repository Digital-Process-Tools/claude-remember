"""Locate the #668 flattened-config cache without hardcoding its filename.

#682 moved this cache out of the project tree (`$REMEMBER_DIR/tmp/config.rcfg`,
committable and clonable) to a per-project file under the system temp dir,
keyed by mangling `REMEMBER_DIR` itself (same convention as
`_remember_env_cache_path` in lib-env-cache.sh). A test that hardcodes the
mangled filename duplicates the mangling logic and drifts from it silently;
globbing the fixed prefix `_remember_cfg_flatten_cache_path` always writes
finds the real file regardless of the exact key shape, exactly the way
`tests/env_cache.py`'\''s own `CACHE_GLOB` does for the sibling env cache.
"""

from __future__ import annotations

from pathlib import Path

# The fixed prefix `_remember_cfg_flatten_cache_path` (scripts/log.sh) always
# writes; only the mangled-REMEMBER_DIR suffix varies.
CACHE_GLOB = "remember-config-cache-*"


def cache_files(tmpdir) -> list[Path]:
    """Every published flattened-config cache file under `tmpdir`."""
    return sorted(Path(tmpdir).glob(CACHE_GLOB))
