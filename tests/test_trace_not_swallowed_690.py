"""`bash -x` over the hooks must not have its trace swallowed (#690).

`scripts/bootstrap-dirs.sh` redirects fd 2 into `hook-errors.log` so a hook's
stderr never leaks into the host's transcript. An `xtrace` stream is on fd 2 by
default, so that one line also swallowed every profiler that pointed a trace
there -- silently, and from partway through the run. Measured while building
the #660 diagnostic: `wall 0.71s | traced span 0.07s`, 90% of the run missing
and nothing saying so, while the result reads exactly like a complete profile
of a fast hook.

The shape of the fix these tests pin: the redirect stands, EXCEPT when doing it
would swallow a trace the operator deliberately started. Both directions are
asserted here, because each alone is satisfied by a broken implementation --
"never redirect" passes every trace assertion, and "always redirect" passes
every stderr-containment assertion.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

# Not a blanket win32 skip (#432/#497): the thing under test is fd 2 and
# BASH_XTRACEFD, which Git Bash has exactly as macOS and Linux do -- so the
# only real precondition is a usable bash, and Windows is not excluded from
# a defect that was measured while profiling a Windows hook.
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash on this runner")

MARKER = "MARKER-690-ON-FD-2"


def _store(tmp_path):
    """A store bootstrap-dirs.sh will accept, with its logs dir already there.

    The redirect is guarded on `logs/` existing, so a test that did not create
    it would pass by never reaching the line under test.
    """
    remember = tmp_path / "project" / ".remember"
    (remember / "logs").mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return remember


def _run_bootstrap(remember, tmp_path, *, bash_args=(), extra_env=None):
    """Source bootstrap-dirs.sh, then write MARKER to fd 2 and report where it went.

    Everything after the source is the probe: whichever destination holds the
    marker is where this hook's own stderr would have gone.
    """
    script = f'source "{BOOTSTRAP}"; echo "{MARKER}" >&2'
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    env.pop("REMEMBER_TRACE", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [BASH, *bash_args, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    log = remember / "logs" / "hook-errors.log"
    logged = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    return proc, logged


def test_stderr_still_goes_to_the_log_when_nothing_is_tracing(tmp_path):
    """The must-fire control. Without this, "do not swallow the trace" is
    satisfied by never redirecting at all -- which puts every hook's stderr
    back into the host's transcript, the leak the redirect exists to stop."""
    remember = _store(tmp_path)

    proc, logged = _run_bootstrap(remember, tmp_path)

    assert MARKER in logged, (
        "an untraced hook's stderr must still land in hook-errors.log"
    )
    assert MARKER not in proc.stderr, (
        "and must NOT reach the terminal, where the host would transcribe it"
    )


def test_an_xtrace_on_fd_2_survives_the_bootstrap(tmp_path):
    """The reported defect: `bash -x` puts the trace on fd 2, and the redirect
    sent the rest of it into a log file the profiler never reads (#690)."""
    remember = _store(tmp_path)

    proc, logged = _run_bootstrap(remember, tmp_path, bash_args=("-x",))

    assert MARKER in proc.stderr, (
        "with xtrace on fd 2, fd 2 must be left where the profiler put it -- "
        "otherwise the trace stops partway through the run, silently"
    )
    assert MARKER not in logged, (
        "and the trace must not be poured into hook-errors.log instead"
    )


def test_the_skipped_redirect_says_so_on_the_stream_it_kept(tmp_path):
    """A trace that survives silently is better than one that vanishes, but the
    operator still has to know this hook's stderr is now in their terminal
    rather than in the log they would otherwise go read."""
    remember = _store(tmp_path)

    proc, _ = _run_bootstrap(remember, tmp_path, bash_args=("-x",))

    assert "hook-errors.log" in proc.stderr, (
        "the notice must name the log that is NOT being written"
    )
    assert "BASH_XTRACEFD" in proc.stderr, (
        "and the way to get both: a trace on its own fd, stderr in the log"
    )


def test_a_trace_on_its_own_fd_does_not_disable_the_redirect(tmp_path):
    """`BASH_XTRACEFD=9` is the recommended shape, and it is exactly the case
    where the redirect is harmless: the trace is not on fd 2, so sending fd 2
    to the log costs the profiler nothing. Disabling the redirect here would
    trade a solved problem for the stderr leak (#643)."""
    remember = _store(tmp_path)
    trace = tmp_path / "trace.txt"

    script = (
        f'exec 9>"{trace}"; export BASH_XTRACEFD=9; set -x; '
        f'source "{BOOTSTRAP}"; echo "{MARKER}" >&2'
    )
    proc = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
    )
    log = remember / "logs" / "hook-errors.log"
    logged = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""

    assert MARKER in logged, (
        "a trace on its own fd is untouched by the redirect, so the redirect stands"
    )
    assert MARKER not in proc.stderr
    traced = trace.read_text(encoding="utf-8", errors="replace")
    assert f"echo {MARKER}" in traced, (
        "and the trace itself must still cover the commands AFTER the redirect"
    )


def test_remember_trace_opts_out_without_bash_x(tmp_path):
    """The explicit opt-out, for a profiler that is not bash's own xtrace --
    anything that expects a hook's stderr in its own pipe."""
    remember = _store(tmp_path)

    proc, logged = _run_bootstrap(
        remember, tmp_path, extra_env={"REMEMBER_TRACE": "1"}
    )

    assert MARKER in proc.stderr
    assert MARKER not in logged


def test_remember_trace_unset_is_not_the_opt_out(tmp_path):
    """The twin of the opt-out: only the documented value turns it on. An
    implementation testing `-n "${REMEMBER_TRACE:-}"` against `0` would read
    an explicit "no" as a yes."""
    remember = _store(tmp_path)

    proc, logged = _run_bootstrap(
        remember, tmp_path, extra_env={"REMEMBER_TRACE": "0"}
    )

    assert MARKER in logged, "REMEMBER_TRACE=0 is not an opt-out"
    assert MARKER not in proc.stderr


def test_the_real_hook_traces_past_the_bootstrap(tmp_path):
    """End to end, on the hook the issue was filed about.

    The unit tests above pin the mechanism; this pins the thing an operator
    actually does -- `bash -x scripts/session-start-hook.sh` -- and fails if
    the trace stops at the redirect the way it did when #660 was being
    measured. `_remember_render_memory_section` is defined in
    lib-memory-context.sh and runs well after bootstrap-dirs.sh is sourced, so
    its presence in the trace is a statement about the span, not about the
    hook merely having started.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)
    (remember / "now.md").write_text("NOW-BODY-690\n", encoding="utf-8")

    payload = (
        '{"session_id": "eeeeeeee-0000-4000-8000-000000000690", '
        '"transcript_path": "/does/not/matter/x.jsonl", '
        '"hook_event_name": "SessionStart", "source": "startup", '
        '"cwd": "/does/not/matter"}'
    )
    # Bytes, not text=True: an xtrace echoes the commands verbatim, and one of
    # them carries the promo's emoji -- which bash's own line-oriented trace
    # can split mid-character, making the stream undecodable as strict UTF-8.
    # A test that dies decoding its evidence proves nothing about the trace.
    raw = subprocess.run(
        [BASH, "-x", str(SESSION_START)],
        input=payload.encode("utf-8"),
        capture_output=True,
        timeout=120,
        env={
            **os.environ,
            "HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(project),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            "REMEMBER_DIR": str(remember),
            "_LIB_MEMORY_DIR_LOADED": "1",
        },
    )
    stdout = raw.stdout.decode("utf-8", errors="replace")
    stderr = raw.stderr.decode("utf-8", errors="replace")

    assert raw.returncode == 0, stderr[-2000:]
    assert "NOW-BODY-690" in stdout, "the hook itself must still work traced"
    assert "_remember_render_memory_section" in stderr, (
        "the trace must reach the memory render, which runs long after "
        "bootstrap-dirs.sh redirects fd 2 -- a trace that stops there reads "
        "exactly like a complete profile of a fast hook (#690)"
    )
