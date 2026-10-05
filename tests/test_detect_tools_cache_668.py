"""#668: PYTHON/JQ detection is cached across every hook invocation that
sources detect-tools.sh, keyed on the exact $PATH string -- the verdict
cannot change unless PATH does.

Sourced directly (not via a full hook run) so the cache's own load/publish
functions are pinned independently of anything else session-start-hook.sh or
post-tool-hook.sh does with PYTHON/JQ afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT = REPO_ROOT / "scripts" / "detect-tools.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash found")


def _fake_python_dir(tmp_path: Path) -> Path:
    """A PATH entry with a `python3` that actually runs `-V` (exit 0) and a
    counter file so the test can assert whether it was invoked."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    counter = tmp_path / "python3-calls"
    counter.write_text("", encoding="utf-8")
    stub = bindir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo x >> "{counter}"\n'
        'exit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bindir, counter


def _run(env, cachefile: Path):
    return subprocess.run(
        [BASH, "-c", f'source "{DETECT.as_posix()}"; echo "PYTHON=$PYTHON"; echo "JQ=$JQ"'],
        env={**env, "REMEMBER_TOOLS_CACHE": "1", "TMPDIR": str(cachefile)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_fresh_path_is_a_miss_and_probes_for_real(tmp_path):
    """Positive control: on a PATH that has never been cached, detection must
    actually run -- the fake python3 stub's call counter must show at least
    one invocation, so a "cache hit" here would be caught rather than a
    green run that never exercised the probe at all."""
    bindir, counter = _fake_python_dir(tmp_path)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    result = _run(env, cache_tmpdir)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PYTHON=python3" in result.stdout
    assert counter.read_text().count("x") >= 1, (
        "positive control failed: the fake python3 was never actually "
        "invoked, so this proves nothing about whether the probe ran"
    )


def test_the_same_path_hits_the_cache_and_skips_the_probe(tmp_path):
    """Core case: run once (cold), then run again with the IDENTICAL PATH --
    the second run must NOT invoke python3 -V again, because the cached
    verdict is served instead."""
    bindir, counter = _fake_python_dir(tmp_path)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    first = _run(env, cache_tmpdir)
    assert first.returncode == 0, (first.stdout, first.stderr)
    calls_after_first = counter.read_text().count("x")
    assert calls_after_first >= 1

    second = _run(env, cache_tmpdir)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert second.stdout == first.stdout
    calls_after_second = counter.read_text().count("x")
    assert calls_after_second == calls_after_first, (
        "cache hit must skip the python3 -V probe entirely -- the call "
        f"counter grew from {calls_after_first} to {calls_after_second}"
    )


def test_a_different_path_is_never_served_the_other_paths_cache(tmp_path):
    """Negative-fires-must-not-serve-stale case: changing PATH (a different
    python3 entirely) must never reuse a verdict cached for the old PATH,
    even though both runs share the same cache file location."""
    bindir_a, counter_a = _fake_python_dir(tmp_path / "a")
    bindir_b, counter_b = _fake_python_dir(tmp_path / "b")
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()

    env_a = {**os.environ, "PATH": f"{bindir_a}{os.pathsep}{os.environ['PATH']}"}
    env_b = {**os.environ, "PATH": f"{bindir_b}{os.pathsep}{os.environ['PATH']}"}

    first = _run(env_a, cache_tmpdir)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert counter_a.read_text().count("x") >= 1

    second = _run(env_b, cache_tmpdir)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert counter_b.read_text().count("x") >= 1, (
        "a PATH change must invalidate the cache -- the second run's own "
        "python3 stub was never invoked, meaning a's cached verdict leaked "
        "into b's process"
    )


def test_a_symlinked_tools_cache_is_refused_not_followed(tmp_path):
    """Positive control for the -L/-O checks: a planted symlink at the
    tools-cache path must never be trusted -- the next probe must ignore it
    and re-detect for real rather than adopting a forged verdict."""
    bindir, _counter = _fake_python_dir(tmp_path)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    first = _run(env, cache_tmpdir)
    assert first.returncode == 0, (first.stdout, first.stderr)
    cache_file = cache_tmpdir / "remember-detect-tools-cache"
    assert cache_file.is_file()

    victim = tmp_path / "attacker-controlled-cache"
    victim.write_text(
        f"CACHE_PATH={env['PATH']}\nPYTHON=/bin/rm\nJQ=jq\n", encoding="utf-8"
    )
    cache_file.unlink()
    cache_file.symlink_to(victim)

    second = _run(env, cache_tmpdir)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "PYTHON=/bin/rm" not in second.stdout, (
        "a symlinked tool-verdict cache was trusted instead of refused -- "
        f"stdout={second.stdout!r}"
    )
    assert "PYTHON=python3" in second.stdout


# ── Eager and lazy share one probe (#898) ────────────────────────────────────


def _cache_fields(cache_tmpdir: Path) -> dict:
    cache_file = cache_tmpdir / "remember-detect-tools-cache"
    if not cache_file.is_file():
        return {}
    out = {}
    for line in cache_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_an_eager_miss_publishes_a_complete_verdict(tmp_path):
    """Eager mode publishes once JQ is known as well as PYTHON: a cache
    written before JQ was resolved would carry an empty JQ field, which the
    loader refuses, so every later run would silently re-probe."""
    bindir, _counter = _fake_python_dir(tmp_path)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    result = _run(env, cache_tmpdir)
    assert result.returncode == 0, (result.stdout, result.stderr)
    fields = _cache_fields(cache_tmpdir)
    assert fields.get("PYTHON") == "python3", fields
    assert fields.get("JQ") in ("jq", "_jq_fallback"), fields
    assert f"JQ={fields['JQ']}" in result.stdout


def _run_lazy(env, cache_tmpdir: Path, body: str):
    return subprocess.run(
        [BASH, "-c",
         f'_REMEMBER_LAZY_PYTHON=1; source "{DETECT.as_posix()}"; ' + body],
        env={**env, "REMEMBER_TOOLS_CACHE": "1", "TMPDIR": str(cache_tmpdir)},
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_lazy_mode_probes_only_on_first_use_and_then_publishes(tmp_path):
    """Lazy mode (#662): sourcing probes nothing and publishes nothing; the
    first _remember_python call probes, sets PYTHON, and publishes a
    complete verdict; a second call probes nothing more."""
    bindir, counter = _fake_python_dir(tmp_path)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    result = _run_lazy(env, cache_tmpdir, (
        'echo "before=$PYTHON"; '
        f'echo "calls=$(wc -l < "{counter.as_posix()}" | tr -d " ")"; '
        f'[ -f "{(cache_tmpdir / "remember-detect-tools-cache").as_posix()}" ] && echo cached-early; '
        '_remember_python && echo "after=$PYTHON"; '
        f'n1=$(wc -l < "{counter.as_posix()}"); _remember_python; '
        f'n2=$(wc -l < "{counter.as_posix()}"); [ "$n1" = "$n2" ] && echo second-free'))
    assert result.returncode == 0, (result.stdout, result.stderr)
    out = result.stdout.splitlines()
    assert "before=" in out and "calls=0" in out, out
    assert "cached-early" not in out, out
    assert "after=python3" in out, out
    assert "second-free" in out, out
    fields = _cache_fields(cache_tmpdir)
    assert fields.get("PYTHON") == "python3", fields
    assert fields.get("JQ") in ("jq", "_jq_fallback"), fields


def test_lazy_mode_failure_returns_instead_of_exiting(tmp_path):
    """No usable interpreter in lazy mode: _remember_python reports the same
    FATAL diagnostics but RETURNS 1, so the jq-less caller's own fallback
    still runs (eager mode exits; see test_detect_tools_fatal_diagnostics_650)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text("#!/bin/sh\nexit 49\n", encoding="utf-8")
    stub.chmod(0o755)
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir()
    env = {"PATH": str(bindir), "HOME": str(tmp_path)}

    result = _run_lazy(env, cache_tmpdir, '_remember_python || echo "returned=$?"; echo alive')
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "returned=1" in result.stdout and "alive" in result.stdout, result.stdout
    assert "FATAL: No working Python found" in result.stderr, result.stderr
    assert "exit 49" in result.stderr, result.stderr
    assert _cache_fields(cache_tmpdir) == {}
