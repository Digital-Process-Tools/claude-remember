"""Tests for the shell↔Python variable-bridge seam (issue #84).

Pre-fix, `pipeline.shell._shell_escape` single-quote-wrapped values per
POSIX `eval` convention, while `safe_eval` in `scripts/log.sh` assigned
verbatim via `printf -v` — no shell expansion. Mismatched halves: Linux
temp paths contained no shell-unsafe chars so the escaper returned them
unquoted and the bug was invisible; Windows backslash paths got quoted
and the literal quotes survived into the variable, breaking downstream
`open()` with `OSError: [Errno 22]`.

Also pre-fix, `log.sh`'s `safe_eval` did not strip CR — Python on
Windows emits `\\r\\n`, so values kept a trailing `\\r` and broke the
integer tests in `save-session.sh`.

Post-fix: `_shell_escape` is verbatim (raises on newline), `safe_eval`
strips CR, and the redundant override in `detect-tools.sh` is removed
(`log.sh` is the single source of truth).

These tests pin the contract both halves must satisfy, including a
parametrized Python→shell roundtrip across Linux/Windows path shapes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
DETECT_SH = REPO_ROOT / "scripts" / "detect-tools.sh"

sys.path.insert(0, str(REPO_ROOT))

BASH = shutil.which("bash") or ""
pytestmark = pytest.mark.skipif(not BASH, reason="bash not on PATH")


def _run_bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PROJECT_DIR": str(tmp_path),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(tmp_path / ".remember"),
    }
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


# ── log.sh standalone — pins the canonical safe_eval ────────────────────

def test_log_sh_safe_eval_strips_crlf(tmp_path):
    """safe_eval must strip trailing \\r from CRLF input.

    Without the strip, `EXCHANGE_COUNT=15\\r` fails integer tests in
    save-session.sh on Windows.
    """
    script = f'source "{LOG_SH}"; safe_eval < <(printf "FOO=bar\\r\\n"); printf "[%s]" "$FOO"'
    r = _run_bash(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "[bar]", f"got {r.stdout!r}"


def test_log_sh_safe_eval_crlf_value_is_integer(tmp_path):
    """safe_eval value must be usable in `[ -eq ]` after CRLF input."""
    script = (
        f'source "{LOG_SH}"; '
        'safe_eval < <(printf "NUM=42\\r\\n"); '
        'if [ "$NUM" -eq 42 ]; then echo OK; else echo FAIL; fi'
    )
    r = _run_bash(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK", f"got {r.stdout!r} / {r.stderr!r}"


def test_log_sh_safe_eval_passes_backslash_path_verbatim(tmp_path):
    """safe_eval stores Windows backslash paths verbatim (post-fix: emitter
    no longer quotes, so safe_eval sees the raw path)."""
    script = (
        f'source "{LOG_SH}"; '
        r"""safe_eval <<< "P=C:\Users\x.txt"; """
        'printf "[%s]" "$P"'
    )
    r = _run_bash(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == r"[C:\Users\x.txt]", f"got {r.stdout!r}"


# ── Real sourcing order — what orchestrators actually do ────────────────

def test_real_sourcing_order_safe_eval_handles_crlf(tmp_path):
    """detect-tools.sh THEN log.sh — the order used by save-session.sh."""
    script = (
        f'source "{DETECT_SH}"; source "{LOG_SH}"; '
        'safe_eval < <(printf "FOO=bar\\r\\n"); '
        'printf "[%s]" "$FOO"'
    )
    r = _run_bash(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "[bar]", f"got {r.stdout!r}"


# ── Python ↔ Bash roundtrip — the actual seam ───────────────────────────

@pytest.mark.parametrize("value", [
    "/tmp/remember-extract-abc.txt",                 # Linux temp path
    r"C:\Users\VANDER~1\AppData\Local\Temp\x.txt",   # Windows temp path
    "/path with spaces/file.txt",                    # spaces
    "value-with-'-quote",                            # single quote
    "plain",                                         # trivial
])
def test_shell_escape_safe_eval_roundtrip(value, tmp_path):
    """`_shell_escape` output piped through `safe_eval` must roundtrip exactly.

    This is the contract the bug violated: escape used POSIX
    single-quote semantics, parser used verbatim semantics.
    """
    from pipeline.shell import _shell_escape

    escaped = _shell_escape(value)
    script = (
        f'source "{LOG_SH}"; '
        f'safe_eval <<< "VAL={escaped}"; '
        'printf "%s" "$VAL"'
    )
    r = _run_bash(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == value, f"roundtrip broken: {r.stdout!r} != {value!r}"
