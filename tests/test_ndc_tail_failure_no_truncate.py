"""NDC must not erase now.md when the tail-forward step fails (#173).

`save-session.sh` snapshots `now.md`'s byte length before handing it to a
Haiku call that can run up to 180s, then keeps everything past that offset
with `tail -c +N`. #142 fixed the success path so a *newer* save's entry
survives. But the failure path — `tail` erroring on disk pressure, a
permissions problem, or a full `$TMPDIR` — used to fall back to
`: > "$MEMORY_FILE"`: the exact blind truncate #142 removed from the success
arm, reintroduced as the one error case where the whole span, including
anything appended after the snapshot, is destroyed with nothing to recover it.

Reported in PR #173 (Jutiphan) against `flock`-based locking that this repo
does not use (see lib-lock.sh and CHANGELOG); the analysis of the defect was
correct even though that fix's mechanism was not portable here.

The fix: on tail failure, now.md is left completely untouched and an error is
logged. The known trade is a duplicated entry in today-*.md (the span was
already appended there before the tail step runs) — visible and recoverable,
unlike the erased-and-unlogged alternative.
"""

import os
import sys
from pathlib import Path

import pytest

from .test_save_session_gates import _make_env, _run  # shared harness
from .test_ndc_truncate_race import _ndc_env, _wait_for_background_ndc
from .subprocess_helpers import subprocess_failure_detail

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

# Fails only the exact invocation NDC uses (`tail -c +N MEMORY_FILE`), so
# every other `tail` call in the script (header extraction, entry
# normalization) still runs through the real binary and the run reaches the
# NDC step normally. `command -p` resolves against the shell's default PATH,
# not the one this stub directory was prepended to, so it cannot recurse into
# itself.
#
# `command -p` is deliberately NOT prefixed with `exec`. Debian's dash — which
# is `/bin/sh` on every Ubuntu runner — implements `exec` as a PATH search for
# an *executable*, with no fallback to builtins, so `exec command -p tail` dies
# with "exec: command: not found" and exit 127 before any tail runs. Bash (macOS
# `/bin/sh`) and Apple's dash both accept it, which is why this only ever showed
# up on Linux CI. Without `exec` the builtin runs normally and the stub exits
# with tail's own status; the only cost is one extra live process.
#
# Before failing, it snapshots the exact bytes of $3 (MEMORY_FILE) into
# $STUB_TAIL_SNAPSHOT if set. That snapshot — taken synchronously, inside the
# call the fix must not mutate around — is the only race-free ground truth
# for "what now.md held right before tail ran". Reading now.md from the test
# process at any point around the backgrounded NDC subshell cannot be trusted
# for that: the subshell has no artificial delay, so it may already have
# finished (successfully or not) before or after any read the test attempts.
TAIL_STUB = """#!/bin/sh
case "$1" in
    -c)
        if [ -n "$STUB_TAIL_SNAPSHOT" ]; then
            cp "$3" "$STUB_TAIL_SNAPSHOT" 2>/dev/null || true
        fi
        exit 1
        ;;
esac
command -p tail "$@"
"""


def _with_failing_tail(env: dict, tmp_path: Path, snapshot: Path) -> dict:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "tail"
    stub.write_text(TAIL_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(env)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_TAIL_SNAPSHOT"] = str(snapshot)
    return env


class TestNdcTailFailureNoTruncate:

    def test_now_md_untouched_when_tail_fails(self, tmp_path):
        """The #173 case: tail erroring must not blank now.md."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        memory_file = project / ".remember" / "now.md"
        snapshot = tmp_path / "tail-snapshot.md"
        env = _with_failing_tail(env, tmp_path, snapshot)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        # No tail-forward can succeed, so now.md never changes again once the
        # NDC subshell reaches (and fails) the tail step. Settling just means
        # waiting out the subshell's own lifetime.
        _wait_for_background_ndc(memory_file)
        after = memory_file.read_bytes()

        assert snapshot.is_file(), (
            "the tail stub never ran (-c branch) — the run did not reach the "
            "NDC tail step at all, so this test proves nothing"
        )
        before = snapshot.read_bytes()
        assert before, "the tail stub's snapshot of now.md was empty"

        assert after == before, (
            "tail failing must leave now.md byte-for-byte identical to what "
            "it held the instant tail was invoked — any diff means the "
            "failure path truncated or otherwise mutated a file it could not "
            f"safely rewrite; before={before!r} after={after!r}"
        )

    def test_tail_failure_is_logged(self, tmp_path):
        """A silent failure here is as bad as the truncate itself."""
        env, project, plugin, calls, sid = _ndc_env(tmp_path)
        snapshot = tmp_path / "tail-snapshot.md"
        env = _with_failing_tail(env, tmp_path, snapshot)

        result = _run(plugin, env, sid)
        assert result.returncode == 0, subprocess_failure_detail(result, project / ".remember")

        memory_file = project / ".remember" / "now.md"
        _wait_for_background_ndc(memory_file)

        log_files = list((project / ".remember" / "logs").glob("*.log"))
        assert log_files, "expected a log file to exist"
        log_text = "\n".join(f.read_text() for f in log_files)
        assert "tail failed" in log_text, (
            f"expected an error log naming the tail failure, got: {log_text!r}"
        )
