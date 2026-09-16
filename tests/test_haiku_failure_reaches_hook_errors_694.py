"""#694: a failed background save reported only `exited 1` in hook-errors.log
-- the file /remember:doctor tails and a reporter is asked to paste -- while
the real cause (auth expired, balance exhausted, rate limited, whatever the
nested `claude` CLI actually said) went only to the daily narrative log,
which nobody reads while diagnosing a live outage.

Surfaced in #693/#703: one installation failed every background summarize for
days, and `hook-errors.log` had nothing but `save-session.sh --force exited
1` the whole time, while `~/.remember/logs/memory-*.log` held the real Haiku
CLI error (an ANTHROPIC_API_KEY precedence issue) on every single attempt.

`pipeline.shell call-haiku`'s own failure path already writes the full detail
to STDERR (`call-haiku error: <_failure_detail() output>`,
pipeline/shell.py:304) -- this is a pure PROPAGATION bug, not a missing
diagnosis: scripts/save-session.sh's own `HAIKU_VARS=... || { ... }` handler
(around the `log "haiku" "ERROR: ..."` call) reads that same stderr capture
and passes it only to `log()` (the daily narrative log), never to
`report_error()` (which also appends the SAME line to hook-errors.log). This
test drives the real save-session.sh with a stub `pipeline.shell` (the same
harness test_save_session_gates.py's `STUB_HAIKU_FAIL` route already uses)
and checks the destination the operator actually reads.

A must-fire / must-not-fire pair, per this repo's own CLAUDE.md: a failing
call must land its detail in hook-errors.log, and an ordinary successful save
must write NOTHING there -- otherwise a broken harness that writes
everything, always, would pass the must-fire half for the wrong reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_save_session_gates import _make_env, _run

STUB_HAIKU_FAILURE_DETAIL = "stub: simulated haiku failure"


def _hook_errors_text(project: Path) -> str:
    f = project / ".remember" / "logs" / "hook-errors.log"
    return f.read_text(encoding="utf-8") if f.is_file() else ""


def test_the_real_failure_detail_reaches_hook_errors_log(tmp_path):
    env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    env["STUB_HAIKU_FAIL"] = "1"

    result = _run(plugin, env, sid)

    assert result.returncode == 1
    errors = _hook_errors_text(project)
    assert STUB_HAIKU_FAILURE_DETAIL in errors, (
        "the nested call's own failure detail must reach hook-errors.log, "
        f"not just an exit code. hook-errors.log held: {errors!r}"
    )
    assert "haiku" in errors


def test_an_ordinary_successful_save_writes_nothing_to_hook_errors_log(tmp_path):
    """Negative control paired with the must-fire case above: a healthy save
    (STUB_HAIKU_FAIL unset, the harness's own default) must not leave
    anything in hook-errors.log at all -- otherwise a harness that always
    writes something there would pass the must-fire assertion for a reason
    that has nothing to do with propagating a real failure."""
    env, project, plugin, _calls, sid = _make_env(tmp_path, exchanges=6, humans=5)

    result = _run(plugin, env, sid)

    assert result.returncode == 0
    assert _hook_errors_text(project) == "", (
        "an ordinary successful save must not write anything to hook-errors.log"
    )


def _empty_response_env(tmp_path: Path):
    """The `pipeline.shell` stub has no built-in way to make call-haiku
    return a genuinely empty summary (STUB_HAIKU_TEXT="" is falsy in Python
    and falls through to the SKIP branch), so this patches the stub's own
    copy of pipeline/shell.py -- written fresh per test by `_make_env` -- to
    add one more branch, rather than changing the shared stub every other
    test in test_save_session_gates.py also uses."""
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    shell_py = plugin / "pipeline" / "shell.py"
    text = shell_py.read_text(encoding="utf-8")
    marker = 'elif os.environ.get("STUB_HAIKU_TEXT"):'
    assert marker in text, "test_save_session_gates.py's STUB_SHELL shape changed"
    text = text.replace(
        marker,
        'elif os.environ.get("STUB_HAIKU_EMPTY"):\n            f.write("")\n        ' + marker,
        1,
    )
    shell_py.write_text(text, encoding="utf-8")
    env["STUB_HAIKU_EMPTY"] = "1"
    env["STUB_HAIKU_SKIP"] = "0"
    return env, project, plugin, calls, sid


def test_the_empty_response_failure_also_reaches_hook_errors_log(tmp_path):
    """Same #694 shape, one call site over: an empty summary is diagnosed
    correctly (there is nothing more to say than "empty response") but was
    reported only to the daily narrative log, never to hook-errors.log."""
    env, project, plugin, _calls, sid = _empty_response_env(tmp_path)

    result = _run(plugin, env, sid)

    assert result.returncode == 1
    errors = _hook_errors_text(project)
    assert "empty response" in errors, (
        f"an empty Haiku response must be reported in hook-errors.log too. "
        f"hook-errors.log held: {errors!r}"
    )
