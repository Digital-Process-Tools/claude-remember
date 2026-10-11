"""#1021: the #353 sidecar-trust fast path used to extract the "sessions"
object out of `_LAST_SAVE_CONTENT` (the whole `last-save.json` file) with
`${_LAST_SAVE_CONTENT#*"sessions"}` then `${_SESSIONS_SCOPE%%\\}*}` -- a pair
of bash parameter expansions that are QUADRATIC in bash's `#`/`%` glob-match
engine, the same class #1017 fixed for `_pt_scan_lines`'s newline count.

Measured on real `/bin/bash` 3.2.57 (this repo's documented floor shell,
macOS's shipped default), with the "sessions" key positioned worst-case at
the END of the file (the shape a long-lived store's own insertion order
would eventually produce if the writer did not reorder on every save):
0.02s at 100 sessions, 1.93s at 1,000, 179s at 10,000. Each test below pins
a hard wall-clock bound at a size the auditor already measured directly
against the pre-fix code, so a failure here is not a guess about where the
line sits -- it is the same measurement, re-run.

`pipeline.shell.cmd_save_position` is the only real writer of this file and
caps it at `_POSITION_SLOTS` (32) sessions with "sessions" always the FIRST
key -- so the worst case here is synthetic, not something this repo's own
writer produces. The fix is defense-in-depth; the fixture below constructs
the pathological shape on purpose, matching the issue's own measurement.

The fix went through one self-review round: the first draft replaced the
two expansions with a single regex PLUS an `elif` fallback that kept the
old quadratic prefix-strip alive for a truncated/corrupted file (one with
"sessions" but no closing `}` anywhere after it). The final form drops the
trailing `}` requirement from the regex instead, so no fallback exists at
all -- `test_a_truncated_sessions_object_at_scale_is_still_bounded` below
pins that the dropped shape stays linear too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.test_post_tool_fast_path_350 import HOOK, _env, _prime, _project

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def _bash32():
    """/bin/bash on a real bash-3.2 host (macOS's shipped default, and this
    repo's own documented floor). None if /bin/bash is absent or is not
    actually 3.2 (e.g. a Linux CI image where /bin/bash is already 5.x) --
    the caller skips rather than silently testing the wrong shell."""
    path = "/bin/bash"
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                              timeout=5, text=True, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if "version 3.2" in out else None


def _run_with(bash_path, env, *, timeout=60):
    return subprocess.run([bash_path, str(HOOK)], capture_output=True, env=env,
                           timeout=timeout, input=b"", check=False)


def _run(env, *, timeout=60):
    return _run_with(shutil.which("bash") or "bash", env, timeout=timeout)


def _write_last_save_worst_case(remember, session_id, position, filler_count):
    """Build a last-save.json with "sessions" as the LAST top-level key
    (json.dumps preserves dict insertion order) and `filler_count` other
    sessions ahead of the one under test -- the worst-case shape the
    issue's own measurement used, and the shape neither parameter
    expansion this fixes can skip past cheaply."""
    sessions = {}
    for i in range(filler_count):
        sessions["filler-" + str(i)] = i
    sessions[session_id] = position
    payload = {"session": session_id, "line": position, "sessions": sessions}
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_sidecar(remember, session_id, value):
    (remember / "tmp" / ("position." + session_id)).write_text(
        value, encoding="utf-8",
    )


def _write_last_save_truncated(remember, filler_count):
    """A last-save.json with "sessions" present but NO closing `}` anywhere
    after it -- a truncated/corrupted write the atomic temp-file-then-rename
    writer should never produce, but the one shape self-review's first draft
    needed an `elif` quadratic-prefix-strip fallback for. The final fix drops
    the fallback entirely (`([^}]*)` with no trailing `}` requirement), so
    this fixture exists to prove that shape stays linear too, not just the
    well-formed one."""
    sessions = {}
    for i in range(filler_count):
        sessions["filler-" + str(i)] = i
    body = (
        '{"session":"x","line":1,"sessions":{'
        + ",".join(f'"{k}":{v}' for k, v in sessions.items())
    )
    (remember / "tmp" / "last-save.json").write_text(body, encoding="utf-8")


# -- bash 3.2.57, the repo's documented floor: the issue's own measurement --

def test_sidecar_trust_scope_on_bash32_floor_is_bounded(tmp_path):
    """#1021's own repro, run for real: 10,000 filler sessions (~128KB) with
    "sessions" worst-cased at the end of the file, on /bin/bash 3.2.57
    measured at ~2s pre-fix for this exact shape. A linear fix (one builtin
    regex match, no quadratic glob-match term) must land comfortably under
    that."""
    bash32 = _bash32()
    if bash32 is None:
        pytest.skip("no real /bin/bash 3.2 found on this host -- cannot "
                     "exercise this repo's documented floor shell")

    home, project, remember = _project(tmp_path, jsonl_lines=5,
                                        session_id=SESSION_A)
    _write_last_save_worst_case(remember, SESSION_A, 3, filler_count=10_000)
    _write_sidecar(remember, SESSION_A, "3")
    env = _env(tmp_path, home, project)
    _prime(env)

    start = time.monotonic()
    warm = _run_with(bash32, env, timeout=30)
    elapsed = time.monotonic() - start

    assert warm.returncode == 0, warm.stderr[:400]
    assert elapsed < 1.0, (
        f"sidecar-trust scope extraction over a 10,000-session worst-cased "
        f"last-save.json took {elapsed:.2f}s on /bin/bash 3.2 -- #1021's "
        "own measurement for this exact shape was ~2s on the quadratic "
        "code this bound exists to catch"
    )
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "a fast-but-wrong rewrite (e.g. one that always falls back, or "
        "silently truncates BASH_REMATCH at this scale) must not pass this "
        "timing-only test by being fast: delta is 3 against a threshold of "
        "50, so a fork here means the sidecar was NOT trusted at 10,000 "
        "filler sessions, even though the timing bound above passed"
    )


# -- whatever bash resolves off PATH (bash 5.x on most CI images) -----------

def test_sidecar_trust_scope_of_a_20000_session_file_is_bounded(tmp_path):
    """#1021's own measurement: a 20,000-filler-session worst-cased file
    cost ~9.7s on bash 3.2 with the quadratic expansion pair. A linear fix
    stays well under that on whatever bash resolves off PATH too."""
    home, project, remember = _project(tmp_path, jsonl_lines=5,
                                        session_id=SESSION_A)
    _write_last_save_worst_case(remember, SESSION_A, 3, filler_count=20_000)
    _write_sidecar(remember, SESSION_A, "3")
    env = _env(tmp_path, home, project)
    _prime(env)

    start = time.monotonic()
    warm = _run(env, timeout=60)
    elapsed = time.monotonic() - start

    assert warm.returncode == 0, warm.stderr[:400]
    assert elapsed < 5.0, (
        f"sidecar-trust scope extraction over a 20,000-session worst-cased "
        f"last-save.json took {elapsed:.2f}s -- #1021's own measurement for "
        "this exact shape was ~9.7s on the quadratic code this bound exists "
        "to catch"
    )
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "a fast-but-wrong rewrite must not pass this timing-only test by "
        "being fast: delta is 3 against a threshold of 50, so a fork here "
        "means the sidecar was NOT trusted at 20,000 filler sessions, even "
        "though the timing bound above passed"
    )


# -- correctness: the linear rewrite must still trust exactly what the ------
# -- quadratic original trusted, on the worst-cased shape too ---------------

def test_a_worst_cased_sessions_key_is_still_trusted_correctly(tmp_path):
    """Positive control for the two timing tests above: with "sessions"
    worst-cased at the end of the file and thousands of filler entries
    ahead of it, the real session id must still be found and trusted --
    without this, a fix that always fell back (never finding anything)
    would also pass the timing bound trivially."""
    home, project, remember = _project(tmp_path, jsonl_lines=60,
                                        session_id=SESSION_A)
    _write_last_save_worst_case(remember, SESSION_A, 55, filler_count=500)
    _write_sidecar(remember, SESSION_A, "55")
    env = _env(tmp_path, home, project)
    _prime(env)

    result = _run(env, timeout=30)

    assert result.returncode == 0, result.stderr[:400]
    assert not (remember / "tmp" / "save-session.pid").exists(), (
        "delta should be 5 against a threshold of 50 -- no save should "
        "have been forked. If this fired, the sidecar was not trusted, "
        "meaning the rewritten scope extraction failed to find the real "
        "session id among the filler entries"
    )


# -- the shape the dropped `elif` fallback used to cover: truncated input --

def test_a_truncated_sessions_object_at_scale_is_still_bounded(tmp_path):
    """The final fix drops the `elif` fallback that kept the old quadratic
    prefix-strip alive for a "sessions" key with no closing `}` anywhere
    after it. Without this test, nothing would catch a regression back to
    that fallback (or an equivalent quadratic path) on exactly the
    corrupted-file shape the fix exists to also cover -- 20,000 filler
    sessions, no closing brace anywhere in the file after "sessions"."""
    home, project, remember = _project(tmp_path, jsonl_lines=5,
                                        session_id=SESSION_A)
    _write_last_save_truncated(remember, filler_count=20_000)
    # No sidecar for SESSION_A: this fixture's "session" field is "x", not
    # SESSION_A, so there is nothing for the sidecar-trust path to trust --
    # the only thing under test here is that reading this truncated file
    # does not cost the old quadratic time, not what it then does with it.
    env = _env(tmp_path, home, project)
    _prime(env)

    start = time.monotonic()
    result = _run(env, timeout=30)
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr[:400]
    assert elapsed < 5.0, (
        f"reading a truncated (no closing brace) 20,000-session "
        f"last-save.json took {elapsed:.2f}s -- the dropped `elif` "
        "fallback would have cost ~9.7s here, the same as the well-formed "
        "case; this shape must cost the well-formed case's ~0.02s, not "
        "the fallback's quadratic one"
    )
