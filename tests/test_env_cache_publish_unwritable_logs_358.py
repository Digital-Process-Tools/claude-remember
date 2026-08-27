"""#358: a cache publish on a store whose logs/ dir cannot be created bakes in
120/50 as though config had said so.

log.sh (scripts/log.sh) returns early — before it ever sets
REMEMBER_SAVE_COOLDOWN or REMEMBER_DELTA_THRESHOLD — on a store whose logs/
directory it cannot create. `_remember_env_cache_publish`
(scripts/lib-env-cache.sh) used to default both keys at the point of writing
(`${REMEMBER_SAVE_COOLDOWN:-120}`), so an unwritable store published exactly
the same digits a real 120-second / 50-line config would, and the load side's
validation (scripts/lib-env-cache.sh's digit check) cannot tell a default from
an answer — both are just digits.

user-prompt-hook.sh's slow path (the one every session's first prompt takes,
before anything is cached) sources log.sh with stderr suppressed and calls
`_remember_env_cache_publish` unconditionally, which is why this is
reproduced through that hook rather than by calling the shell function
directly — the settling experiment the issue itself proposes.

Every "must not publish a lie" assertion below is paired with a "must publish
the real answer" control in the same fixture: a store that CAN write its logs
and has a NON-default cooldown configured must have that cooldown reach the
cache, or "no cache was written" would be indistinguishable from "the hook
never ran".
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_post_tool_fast_path_350 import PROMPT_HOOK, _env, _project

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod-based unwritability and bash hook subprocess are not "
    "portable to Windows runners; POSIX permission bits do not model an "
    "ACL-unwritable directory there the way they do here.",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cache_files(tmpdir: Path):
    return sorted(Path(tmpdir).glob("remember-env-*"))


def _run_prompt_hook(env: dict):
    return subprocess.run(
        ["bash", str(PROMPT_HOOK)], capture_output=True, env=env, timeout=60,
        check=False,
    )


def test_an_unwritable_logs_dir_does_not_publish_a_default_cooldown_as_config(tmp_path):
    """The issue's own settling experiment: chmod the store so log.sh's
    `mkdir -p logs` fails, configure a cooldown that is NOT 120, fire
    user-prompt-hook.sh (the first-prompt / slow-path caller), and check what
    reached the published cache.

    Note the directory has to be ABSENT for chmod to matter here: log.sh's own
    `[ ! -d "$REMEMBER_LOG_DIR" ] &&` guard (added for #230) skips the `mkdir`
    call entirely when logs/ already exists, so chmod 000 on an
    already-created logs/ directory changes nothing (measured directly against
    this fixture before writing this test — the cache came back with the
    configured 999, not a default, because log.sh never even tried to
    mkdir). What #358 is about is the directory not existing yet and being
    impossible to create — the CI-relevant, first-run case — so this makes
    the PARENT unwritable and leaves logs/ absent, which is what actually
    fails `mkdir -p`.

    Root only: `chmod` on a directory you own does not stop root from
    creating entries inside it, so this assertion cannot hold under a root
    test runner. Skipped rather than silently passing there.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory write permission bits — chmod-based "
                    "unwritability does not reproduce as this user")

    home, project, remember = _project(
        tmp_path, cooldown_ts=int(time.time()),
        config={"cooldowns": {"save_seconds": 999}},
    )
    env = _env(tmp_path, home, project)

    logs = remember / "logs"
    assert not logs.exists(), "fixture invariant: logs/ must not exist yet"
    remember.chmod(0o555)  # readable + traversable, NOT writable: mkdir fails
    try:
        result = _run_prompt_hook(env)
    finally:
        remember.chmod(0o755)

    assert result.returncode == 0, (
        "documented EXIT CODES: 0 Always — got " + str(result.returncode)
        + ": " + repr(result.stderr[:600])
    )

    cache_files = _cache_files(Path(env["TMPDIR"]))
    assert not cache_files, (
        "a store that could not create logs/ still published an env cache: "
        + repr([f.read_text(encoding="utf-8") for f in cache_files])
        + " — REMEMBER_SAVE_COOLDOWN/REMEMBER_DELTA_THRESHOLD were never set "
        "by log.sh on this run, so any value written for them is a silent "
        "default masquerading as a configured answer"
    )

    # POSITIVE CONTROL, same fixture: once logs/ CAN be created, the same
    # project — same non-default 999 in config.json — publishes for real.
    result2 = _run_prompt_hook(env)
    assert result2.returncode == 0, repr(result2.stderr[:600])
    cache_files2 = _cache_files(Path(env["TMPDIR"]))
    assert cache_files2, (
        "no cache was published even once the store could write its logs — "
        "this fixture cannot tell a real fix from a hook that stopped "
        "publishing altogether"
    )
    body = cache_files2[0].read_text(encoding="utf-8")
    assert "REMEMBER_SAVE_COOLDOWN=999" in body, (
        "the healthy run did not publish the configured cooldown (999) — "
        "got: " + repr(body)
    )
