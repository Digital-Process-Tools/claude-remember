"""#843: `_remember_env_cache_load`'s `-nt` loop (scripts/lib-env-cache.sh,
originally ~274) is true against a config layer that does not exist, which is
correct for a layer that NEVER existed -- but it reads exactly the same for a
layer that existed when the cache was published and was since DELETED,
because deleting a file changes no mtime a `-nt` check looks at.

The sibling config-flatten cache (scripts/log.sh, #668) carries the identical
bug, pinned by tests/test_config_flatten_cache_668.py's own #843 cases. This
file is the "verified it elsewhere too" half #843 itself asks for:
log.sh's `config_into REMEMBER_TZ ".timezone" ""` feeds straight into the
value lib-env-cache.sh's `_remember_env_cache_publish` writes and
`_remember_env_cache_load` later replays, so a deleted `timezone` layer is
exactly as live a staleness bug here as it is for the flattened-config cache.

Driven through the real SessionStart/UserPromptSubmit entrypoint
(scripts/user-prompt-hook.sh), the same way tests/test_env_cache_publish_
unwritable_logs_358.py already does, rather than sourcing lib-env-cache.sh
directly -- that is what actually publishes and replays this cache in
production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.env_cache import CACHE_GLOB
from tests.test_post_tool_fast_path_350 import PROMPT_HOOK, _env, _project, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)


def _cache_files(tmpdir: Path):
    return sorted(Path(tmpdir).glob(CACHE_GLOB))


def _run_prompt_hook(env: dict):
    return _run(env, script=PROMPT_HOOK)


def test_deleting_a_config_layer_invalidates_the_env_cache(tmp_path):
    """#843's own repro, generalised to this cache: publish with a
    non-default timezone configured, delete that config layer, and confirm
    the stale timezone does not survive into the next resolution."""
    home, project, remember = _project(
        tmp_path, config={"timezone": "America/New_York"},
    )
    env = _env(tmp_path, home, project)

    result1 = _run_prompt_hook(env)
    assert result1.returncode == 0, repr(result1.stderr[:600])
    caches1 = _cache_files(Path(env["TMPDIR"]))
    assert caches1, "no env cache was published on the first (cold) run"
    text1 = caches1[0].read_text(encoding="utf-8")
    assert "REMEMBER_TZ=America/New_York" in text1, (
        "positive control failed: the configured timezone never reached the "
        f"published cache at all: {text1!r}"
    )

    cfg = remember / "config.json"
    cfg.unlink()

    result2 = _run_prompt_hook(env)
    assert result2.returncode == 0, repr(result2.stderr[:600])
    caches2 = _cache_files(Path(env["TMPDIR"]))
    assert caches2, "the env cache vanished entirely after the layer was deleted"
    text2 = caches2[0].read_text(encoding="utf-8")
    assert "REMEMBER_TZ=America/New_York" not in text2, (
        "a DELETED config layer must make the env cache miss -- the stale "
        f"timezone stayed in effect instead: {text2!r}"
    )


def test_env_cache_is_reused_when_nothing_changes_843(tmp_path):
    """Positive control, paired with the deletion test above: with the SAME
    config layer present and unchanged on both runs, the cache must still be
    replayed rather than republished -- proving this fixture can tell a real
    cache hit from a harness that always re-resolves."""
    home, project, _remember = _project(
        tmp_path, config={"timezone": "America/New_York"},
    )
    env = _env(tmp_path, home, project)

    result1 = _run_prompt_hook(env)
    assert result1.returncode == 0, repr(result1.stderr[:600])
    caches1 = _cache_files(Path(env["TMPDIR"]))
    assert caches1
    before = caches1[0].stat()

    result2 = _run_prompt_hook(env)
    assert result2.returncode == 0, repr(result2.stderr[:600])
    caches2 = _cache_files(Path(env["TMPDIR"]))
    assert caches2
    after = caches2[0].stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns), (
        "the env cache was republished even though nothing changed between "
        "the two runs -- this fixture cannot distinguish a real hit from a "
        "cache that is always cold"
    )
    text2 = caches2[0].read_text(encoding="utf-8")
    assert "REMEMBER_TZ=America/New_York" in text2


def test_env_cache_layer_that_never_existed_stays_a_hit_843(tmp_path):
    """Third case: the HOME config layer (home/.remember/config.json), which
    this fixture never creates on either run, must not by itself force a
    miss -- only a layer that EXISTED and then vanished should."""
    home, project, _remember = _project(
        tmp_path, config={"timezone": "America/New_York"},
    )
    env = _env(tmp_path, home, project)
    assert not (home / ".remember" / "config.json").exists()

    result1 = _run_prompt_hook(env)
    assert result1.returncode == 0, repr(result1.stderr[:600])
    caches1 = _cache_files(Path(env["TMPDIR"]))
    assert caches1
    before = caches1[0].stat()

    assert not (home / ".remember" / "config.json").exists()
    result2 = _run_prompt_hook(env)
    assert result2.returncode == 0, repr(result2.stderr[:600])
    caches2 = _cache_files(Path(env["TMPDIR"]))
    assert caches2
    after = caches2[0].stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns), (
        "a HOME config layer that never existed on either run must not by "
        "itself force the env cache to miss"
    )
