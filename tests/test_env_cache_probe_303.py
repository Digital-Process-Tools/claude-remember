"""A spawn-count test must be able to say which path it measured (#303).

`lib-env-cache.sh` refuses a cache that is not `-nt` every config layer, and
bash's `-nt` compares whole SECONDS. So a test that writes a config and then
counts process spawns on the "warm" run is measuring one of two different
things depending on which side of a second boundary the two writes landed on:

* different seconds → cache rejected → the run is **cold** → the count is high;
* same second → cache honoured → the run is **warm** → the count is low.

Both are correct product behaviour. Only one is what such a test claims to
measure, and when it passes there is nothing in the output distinguishing "the
warm path is cheap" from "the run happened to be warm".

Backdating the config removes the coin flip. It does not remove the defect,
because a backdated config still produces a number with no statement of which
path produced it — a later edit to the hook that quietly stops the cache from
loading at all leaves a green budget test measuring the cold path and saying
nothing. What removes the defect is a measurement that reports its own path:
`tests/env_cache.py` gives one, with the same three answers this repo insists
on everywhere else — `warm`, `cold`, and *cannot tell*.

This file pins the probe itself. The budget tests that use it live in
`tests/test_prompt_hook_spawns.py` and `tests/test_prompt_stamp_301.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

from tests.env_cache import (  # noqa: E402
    COLD,
    OLDER_BY_SECONDS,
    UNKNOWN,
    WARM,
    EnvCacheProbe,
    nt_granularity,
    write_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project


def _env(home: Path, project: Path, extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(project.parent / "tmp"),
    }
    (project.parent / "tmp").mkdir(exist_ok=True)
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_PROMPT_STAMP"):
        env.pop(stale, None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(USER_PROMPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )


# ── The three answers ────────────────────────────────────────────────────────

def test_the_first_run_in_a_project_is_reported_cold(tmp_path):
    """Nothing has been published yet, so the chain must actually run.

    A probe that cannot see this says `warm` about a resolution that cost 19
    processes — which is the failure being retired, wearing the probe's badge.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    probe = EnvCacheProbe(env["TMPDIR"])

    probe.snapshot()
    assert _run(env).returncode == 0
    verdict = probe.verdict()

    assert verdict.verdict == COLD, (
        f"the very first invocation in a project cannot be warm — nothing has "
        f"published a resolution for it to replay. Got {verdict.verdict}: {verdict.detail}"
    )


def test_a_repeat_run_with_no_config_at_all_is_reported_warm(tmp_path):
    """The uncontested case: no config layer exists, so none can be newer.

    `-nt` against a file that does not exist is true, so the cache published by
    the first run survives, and the second run replays it.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    assert _run(env).returncode == 0

    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    assert _run(env).returncode == 0
    verdict = probe.verdict()

    assert verdict.verdict == WARM, (
        f"a second invocation with no config to invalidate anything must replay "
        f"the published resolution. Got {verdict.verdict}: {verdict.detail}"
    )


def test_a_run_that_publishes_nothing_is_reported_unknown_rather_than_warm(tmp_path):
    """With caching switched off there is no cache to compare, either way.

    The tempting shortcut is "no republish, therefore warm". That is exactly
    backwards here: nothing was replayed, nothing was recorded, and the honest
    answer is that this measurement cannot say which path it took. A probe that
    guessed `warm` would certify every disabled-cache run as the fast path.
    """
    home, project = _project(tmp_path)
    env = _env(home, project, {"REMEMBER_ENV_CACHE": "0"})
    probe = EnvCacheProbe(env["TMPDIR"])

    probe.snapshot()
    assert _run(env).returncode == 0
    verdict = probe.verdict()

    assert verdict.verdict == UNKNOWN, (
        f"no resolution was published and none was replayed — the probe has "
        f"nothing to attribute and must say so. Got {verdict.verdict}: {verdict.detail}"
    )


# ── The trap the issue is about ──────────────────────────────────────────────

def test_a_config_older_in_real_time_still_loses_to_whole_second_granularity(tmp_path):
    """The coin flip, made deterministic.

    The config is genuinely written *before* the cache — 0.1s into a second
    against the cache's 0.9s — and `-nt` still refuses it, because it compares
    whole seconds. That is the race a spawn-budget test loses at random, and
    the probe has to be able to see it, or pinning the fixture is pinning
    nothing.
    """
    granularity = nt_granularity()
    if granularity != "second":
        pytest.skip(
            f"bash's -nt resolves finer than whole seconds here ({granularity}), so "
            f"the same-second cache rejection this test reproduces cannot occur on "
            f"this platform — the config/cache race and everything downstream of it "
            f"went unchecked"
        )

    home, project = _project(tmp_path)
    env = _env(home, project)
    cfg = home / ".remember" / "config.json"
    cfg.write_text(json.dumps({"timezone": "UTC"}), encoding="utf-8")
    assert _run(env).returncode == 0

    caches = sorted(Path(env["TMPDIR"]).glob("remember-env-*"))
    assert caches, "precondition: the first run must publish a resolution"
    cache = caches[0]
    second = int(cache.stat().st_mtime)
    os.utime(cfg, ns=((second * 1_000_000_000) + 100_000_000,) * 2)
    os.utime(cache, ns=((second * 1_000_000_000) + 900_000_000,) * 2)

    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    assert _run(env).returncode == 0
    verdict = probe.verdict()

    assert verdict.verdict == COLD, (
        f"the config is 0.8s older than the cache and lands in the same whole "
        f"second, so `-nt` refuses the cache and the run re-resolves. A probe "
        f"reporting {verdict.verdict} here would certify a cold run as a warm "
        f"measurement: {verdict.detail}"
    )


def test_the_shared_config_writer_puts_the_config_a_whole_second_behind(tmp_path):
    """The writer's contract, asserted with the comparison that consumes it.

    Not "the mtime is lower" — `-nt` is the thing that decides, so `-nt` is what
    is asked. Written without the backdate this is red whenever the two writes
    land in one second, which is almost always; asserted through bash it is
    green only because the backdate is real.

    The end-to-end test below cannot pin this on its own: the cold run it waits
    on takes longer than a second, so the cache lands in a later second than the
    config no matter how the config was written. That accidental dodge is
    precisely what #303 says the existing suite relies on.
    """
    cfg = write_config(tmp_path / "config.json", {"timezone": "UTC"})
    cache = tmp_path / "remember-env-later"
    cache.write_text("", encoding="utf-8")

    newer = subprocess.run(["bash", "-c", f'[ "{cache}" -nt "{cfg}" ]'], timeout=15)
    assert newer.returncode == 0, (
        f"bash does not consider a cache written now newer than a config written "
        f"through write_config — so lib-env-cache.sh would refuse it and every "
        f"measurement taken afterwards is a cold-path measurement. "
        f"config mtime {cfg.stat().st_mtime}, cache mtime {cache.stat().st_mtime}"
    )


def test_the_shared_config_writer_wins_that_race_every_time(tmp_path):
    """The same scenario, written through the fixture instead of by hand.

    `write_config` backdates far enough that no second boundary can put the
    config on the wrong side of the cache, so the run after it is warm — and
    the probe, which just called an identical setup cold, says so.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    write_config(home / ".remember" / "config.json", {"timezone": "UTC"})
    assert _run(env).returncode == 0

    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    result = _run(env)
    assert result.returncode == 0
    verdict = probe.verdict()

    assert verdict.verdict == WARM, (
        f"a config backdated by {OLDER_BY_SECONDS}s cannot be newer than a cache "
        f"published after it, so the second run must replay. "
        f"Got {verdict.verdict}: {verdict.detail}"
    )
    assert "UTC" in result.stdout, (
        f"warm is only the right answer if the replayed resolution is the right "
        f"one — the configured timezone has to survive it. Got {result.stdout!r}"
    )


# ── The assertion helper ─────────────────────────────────────────────────────

def test_assert_warm_names_the_path_it_actually_measured(tmp_path):
    """A budget test's failure has to say *why* the number is not comparable.

    "3 spawns, expected 2" sends the reader looking for a new fork in the hook.
    "this run was cold" sends them to the two lines of setup that caused it.
    """
    home, project = _project(tmp_path)
    env = _env(home, project)
    probe = EnvCacheProbe(env["TMPDIR"])

    probe.snapshot()
    assert _run(env).returncode == 0

    with pytest.raises(AssertionError) as raised:
        probe.assert_warm("the spawn budget")
    message = str(raised.value)
    assert "the spawn budget" in message, message
    assert COLD in message, message


def test_assert_warm_distinguishes_cannot_tell_from_cold(tmp_path):
    """`unknown` is not a softer `cold` — it is a different repair.

    A cold run means the setup raced. An unknown one means the probe was
    pointed somewhere with no cache in it, and the number is unattributable
    rather than wrong.
    """
    home, project = _project(tmp_path)
    env = _env(home, project, {"REMEMBER_ENV_CACHE": "0"})
    probe = EnvCacheProbe(env["TMPDIR"])

    probe.snapshot()
    assert _run(env).returncode == 0

    with pytest.raises(AssertionError) as raised:
        probe.assert_warm("the spawn budget")
    message = str(raised.value)
    assert UNKNOWN in message, message
    assert COLD not in message, message


def test_a_verdict_without_a_snapshot_is_refused(tmp_path):
    """Silently treating "no baseline" as "nothing changed" would report warm
    for a run nobody bracketed — a green measurement of nothing at all."""
    probe = EnvCacheProbe(tmp_path)
    with pytest.raises(RuntimeError):
        probe.verdict()
