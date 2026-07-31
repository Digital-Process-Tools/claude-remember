"""A failed read of the last entry must not be handed to the summarizer as
"(no previous entry)" (#251).

`save-session.sh` Step 2 built the dedup context for the Haiku prompt with

    [ -n "$LAST_LINE" ] && tail -n +"$LAST_LINE" "$MEMORY_FILE" > "$TMP" \\
        || echo "(no previous entry)" > "$TMP"

which is the `A && B || C` trap: when `A` holds but `B` *fails*, `C` runs as
well, and `C`'s `>` overwrites whatever `B` managed to write. A read failure is
therefore rendered as a factual claim about the memory file — the summarizer is
told there is no previous entry when there is one, loses its dedup anchor and
its SKIP anchor, and can write the previous span back into now.md. That is the
damage class #142/#224/#250 have been closing from the other end.

Three states, not two: (1) genuinely no `## ` header yet, (2) now.md exists but
cannot be read, (3) the read itself failed. Only (1) is "(no previous entry)".
(2) and (3) must say so — same shape as the NDC tail arm at save-session.sh:782,
which reports rather than acting on a read it could not make.

The save itself still succeeds: losing the save to protect the prompt would
trade a loud bug for a quiet one, and a span that never reaches memory is not
recoverable while a possibly-duplicated entry is.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_save_session_gates import _make_env, _run, _suppress_ndc  # shared harness
from subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

PREVIOUS_ENTRY = "## 09:00 | main\n\n- real previous entry, issue #251\n"

# Fails only the exact invocation Step 2 uses (`tail -n +N MEMORY_FILE`), so the
# `tail -1` inside the header pipeline and every other tail in the script still
# run through the real binary and the run reaches Step 2 normally.
#
# `command -p` resolves against the shell's default PATH, not the directory this
# stub was prepended to, so it cannot recurse into itself. It is deliberately
# NOT prefixed with `exec`: Debian's dash (/bin/sh on every Ubuntu runner)
# implements `exec` as a PATH search for an executable with no builtin
# fallback, so `exec command -p tail` dies with exit 127 before any tail runs
# (see test_ndc_tail_failure_no_truncate.py, where that cost a CI leg).
TAIL_STUB = """#!/bin/sh
if [ "$1" = "-n" ] && [ "$3" = "$STUB_TAIL_FAIL_FILE" ]; then
    if [ -n "$STUB_TAIL_PARTIAL" ]; then
        printf '%s' "$STUB_TAIL_PARTIAL"
    fi
    echo "stub tail: simulated read failure" >&2
    exit 1
fi
command -p tail "$@"
"""


def _env_with_previous_entry(tmp_path: Path):
    env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
    _suppress_ndc(project)
    memory_file = project / ".remember" / "now.md"
    memory_file.write_text(PREVIOUS_ENTRY, encoding="utf-8")
    snapshot = tmp_path / "last-entry-as-sent.md"
    env = dict(env)
    env["STUB_LAST_ENTRY_SNAPSHOT"] = str(snapshot)
    return env, project, plugin, calls, sid, memory_file, snapshot


def _with_failing_tail(env: dict, tmp_path: Path, memory_file: Path, partial: str = ""):
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "tail"
    stub.write_text(TAIL_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(env)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_TAIL_FAIL_FILE"] = str(memory_file)
    if partial:
        env["STUB_TAIL_PARTIAL"] = partial
    return env


def _log_text(project: Path) -> str:
    log_dir = project / ".remember" / "logs"
    return "\n".join(f.read_text(errors="replace") for f in log_dir.glob("*.log"))


class TestTailFailureIsNotAnEmptyBuffer:

    def test_failed_tail_is_not_reported_as_no_previous_entry(self, tmp_path):
        """The #251 case: a read failure must not read as a claim about now.md."""
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        env = _with_failing_tail(env, tmp_path, memory_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        assert snapshot.is_file(), (
            "build-prompt was never called, so the run did not reach Step 2 and "
            "this test proves nothing"
        )
        sent = snapshot.read_text(encoding="utf-8", errors="replace")
        assert "(no previous entry)" not in sent, (
            "now.md holds a real entry and tail failed to read it — telling the "
            "summarizer there is no previous entry is a factual claim the script "
            f"cannot make. sent={sent!r}"
        )
        assert "previous entry unavailable" in sent, (
            "the summarizer must be told the context is missing rather than "
            f"absent, so it does not treat this as a fresh buffer. sent={sent!r}"
        )

    def test_failed_tail_is_logged(self, tmp_path):
        """Silent is how this survived: rc 0, nothing logged, no marker."""
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        env = _with_failing_tail(env, tmp_path, memory_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        log_text = _log_text(project)
        assert "last entry" in log_text and "ERROR" in log_text, (
            f"expected an error log naming the failed last-entry read, got: {log_text!r}"
        )

    def test_partial_tail_output_is_not_sent_as_the_entry(self, tmp_path):
        """A half-written read is not a previous entry either."""
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        env = _with_failing_tail(env, tmp_path, memory_file, partial="## 09:00 | ma")

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        sent = snapshot.read_text(encoding="utf-8", errors="replace")
        assert "previous entry unavailable" in sent, (
            "a truncated read must be replaced by the unavailable marker, not "
            f"passed off as the entry. sent={sent!r}"
        )
        assert "(no previous entry)" not in sent

    def test_save_still_completes_when_the_read_fails(self, tmp_path):
        """Chosen failure: keep the save, mark the prompt — not the reverse."""
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        env = _with_failing_tail(env, tmp_path, memory_file)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert "call-haiku" in calls.read_text(), (
            "the summarization must still run — losing the save to protect the "
            "prompt trades a loud bug for a quiet one"
        )


class TestUnreadableMemoryFile:

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores the permission bits this test relies on",
    )
    def test_unreadable_now_md_is_not_reported_as_empty(self, tmp_path):
        """`grep | tail | cut` swallows grep's error: rc comes from cut, always 0."""
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        memory_file.chmod(0o000)
        try:
            result = _run(plugin, env, sid)
            assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

            sent = snapshot.read_text(encoding="utf-8", errors="replace")
            assert "(no previous entry)" not in sent, (
                "an unreadable now.md is not an empty one — the header search "
                f"failed, it did not come back empty. sent={sent!r}"
            )
            assert "previous entry unavailable" in sent
        finally:
            memory_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


class TestGenuinelyEmptyBufferStillReportsAbsence:
    """The state that is legitimately "(no previous entry)" must stay that way —
    otherwise the fix would just move the ambiguity to the other side."""

    def test_no_memory_file_reports_no_previous_entry(self, tmp_path):
        env, project, plugin, calls, sid = _make_env(tmp_path, exchanges=6, humans=5)
        _suppress_ndc(project)
        snapshot = tmp_path / "last-entry-as-sent.md"
        env = dict(env)
        env["STUB_LAST_ENTRY_SNAPSHOT"] = str(snapshot)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert snapshot.read_text(encoding="utf-8").strip() == "(no previous entry)"

    def test_memory_file_without_headers_reports_no_previous_entry(self, tmp_path):
        env, project, plugin, calls, sid, memory_file, snapshot = _env_with_previous_entry(tmp_path)
        memory_file.write_text("some prose, no header yet\n", encoding="utf-8")

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")
        assert snapshot.read_text(encoding="utf-8").strip() == "(no previous entry)"
