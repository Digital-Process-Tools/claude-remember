"""#864 -- the Anthropic directory's scan flags `eval "..."` in a shipped file as
RUNTIME_FETCH_EXEC ("Contains a download-and-run command"), even though the only
things being `eval`'d here were the two lines inside the old
`_remember_cfg_flatten_cache_load` (scripts/log.sh) that turned a `%q`-quoted
word back into a plain shell value.

Course correction mid-fix (scheduler review): the first version of this fix kept
`%q` as the on-disk format and replaced `eval` with a hand-written, byte-by-byte
decoder that inverted %q's own escaping rules -- correct, but a fragile
character-by-character bash loop on a hot path, and more code than the `eval`
it replaced. Since this cache's publisher and loader are both owned by this
same file, there is no reason to keep %q's shape at all: the format was
changed to a trivial one instead -- one `NAME` TAB `VALUE` record per line,
where VALUE escapes only three bytes (backslash, newline, tab) -- decoded with
a single `printf -v NAME '%b' VALUE` call. `%b` is a pure byte-level format
directive, never a re-parse of VALUE as shell source, so there is no
metacharacter here that ever needed defusing for SAFETY; the whitelist in
`_remember_cfg_flatten_cache_valid_value` exists for CORRECTNESS (rejecting an
escape %b would read differently from how this file's own encoder meant it,
such as `\\c`, `\\xHH` or octal). The cache's on-disk path itself was bumped
(`remember-config-cache-v2-...`) so a cache an older build wrote in the old
%q-based format is simply never opened, rather than needing a migration path.

These tests pin the new encode/decode pair
(`_remember_cfg_flatten_q_encode` / `_remember_cfg_flatten_q_decode`) for every
awkward value named in review: empty, `~`, `a:~`, spaces, quotes, `;|&`,
newlines, tabs, backslashes and non-ASCII -- and the two real call sites,
the REMEMBER_DIR identity line and the full publish/load round trip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

sys.path.insert(0, str(REPO_ROOT))

BASH = shutil.which("bash") or ""
pytestmark = pytest.mark.skipif(not BASH, reason="bash not on PATH")


# Every awkward value shape named in review, plus the three bytes the new
# format actually escapes (backslash, newline, tab) in combination.
_VALUE_SHAPES = [
    "",
    "hello",
    "a b",
    "a;b",
    "a|b&c",
    "a~b",
    "~foo",
    "a:~",
    "quote's",
    "double\"quote",
    "back\\slash",
    "double\\\\slash",
    "tab\there",
    "nl\nline2\nline3",
    "cr\rhere",
    "semi;pipe|amp&paren(close)lt<gt>",
    "dollar$var`backtick`",
    "日本語",
    "héllo wörld",
    "mixed \t\n;|&~end",
    "trailing-backslash\\",
    "tab-then-backslash\t\\",
]


def _run_bash(script: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        timeout=15,
        check=False,
        env={**os.environ, **(env_extra or {})},
    )


@pytest.mark.parametrize("value", _VALUE_SHAPES)
def test_encode_decode_roundtrips_every_awkward_value_byte_identical(value):
    """Red before the fix: neither `_remember_cfg_flatten_q_encode` nor
    `_remember_cfg_flatten_q_decode` exist yet in the old %q-based design, so
    this fails with 'command not found' until the new pair lands. Green
    after: every awkward value named in review round-trips byte-identical,
    with no `eval` anywhere in the path. Binary mode (not text=True):
    Python's universal-newline translation would turn a lone `\\r` into
    `\\n` on the way out of the pipe, masking a real decoder bug as a
    harness artifact."""
    script = f"""
set -eu
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
_remember_cfg_flatten_q_encode _enc "$TEST_VALUE"
_remember_cfg_flatten_q_decode _dec "$_enc"
printf '<<<START>>>%s<<<END>>>' "$_dec"
"""
    result = _run_bash(script, {"TEST_VALUE": value, "PROJECT_DIR": "/tmp"})
    stdout = result.stdout.decode("utf-8", errors="surrogateescape")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, (
        f"round trip of {value!r} failed (exit {result.returncode}): "
        f"stdout={stdout!r} stderr={stderr!r}"
    )
    marker_start = "<<<START>>>"
    marker_end = "<<<END>>>"
    assert marker_start in stdout and marker_end in stdout, (
        f"did not print the expected sentinel wrapper: {stdout!r}"
    )
    decoded = stdout.split(marker_start, 1)[1].split(marker_end, 1)[0]
    assert decoded == value, (
        f"round trip of {value!r} produced {decoded!r} instead of the "
        f"original value -- not byte-identical"
    )


def test_decode_executes_nothing_for_shell_metacharacter_payloads():
    """Positive control for the 'must not execute' claim: a value shaped
    like a command substitution or a semicolon-chained command must
    survive the encode/decode round trip as INERT TEXT. Without this
    control, a harness that never actually ran the functions (e.g. a typo
    in the sourced path) would also report 'canary not created' and look
    identical to a real pass."""
    canary = "/tmp/test_config_flatten_cache_864_canary"
    malicious = f"safe$(touch {canary})value; touch {canary}"
    script = f"""
set -eu
rm -f {canary}
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
_remember_cfg_flatten_q_encode _enc "$TEST_VALUE"
_remember_cfg_flatten_q_decode _dec "$_enc"
printf '<<<START>>>%s<<<END>>>' "$_dec"
"""
    result = _run_bash(script, {"TEST_VALUE": malicious, "PROJECT_DIR": "/tmp"})
    stdout = result.stdout.decode("utf-8", errors="surrogateescape")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, f"round trip failed (exit {result.returncode}): {stderr!r}"
    canary_path = Path(canary)
    try:
        assert not canary_path.exists(), (
            "encode/decode EXECUTED the command-substitution/semicolon payload "
            "instead of treating it as inert text -- canary file was created"
        )
        decoded = stdout.split("<<<START>>>", 1)[1].split("<<<END>>>", 1)[0]
        assert decoded == malicious, (
            f"round trip of a malicious-shaped value produced {decoded!r} "
            f"instead of the literal original {malicious!r}"
        )
    finally:
        canary_path.unlink(missing_ok=True)


def test_decode_positive_control_actually_runs():
    """A 'must fire' pair for the control above: a payload that SHOULD
    leave a trace (a plain value, no shell metacharacters) decodes to
    exactly that value, proving the harness itself is alive and not
    silently skipping."""
    script = f"""
set -eu
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
_remember_cfg_flatten_q_encode _enc "plain-value-no-metachars"
_remember_cfg_flatten_q_decode _dec "$_enc"
printf '<<<START>>>%s<<<END>>>' "$_dec"
"""
    result = _run_bash(script, {"PROJECT_DIR": "/tmp"})
    assert result.returncode == 0
    stdout = result.stdout.decode("utf-8")
    decoded = stdout.split("<<<START>>>", 1)[1].split("<<<END>>>", 1)[0]
    assert decoded == "plain-value-no-metachars"


@pytest.mark.parametrize(
    "path",
    [REPO_ROOT / "scripts" / "log.sh", REPO_ROOT / "pipeline" / "shell.py"],
)
def test_no_eval_curl_wget_substring(path):
    """Structural guard matching the issue's own goal state VERBATIM (#864):
    `grep -rniE 'eval|curl|wget'` over these files must return nothing -- a
    plain substring search, deliberately not word-boundary-limited, because
    the portal's own matcher is a text heuristic and `safe_eval` (the old
    function name) contains the substring `eval` just as much as a bare
    `eval "$x"` call does. A rename that keeps mentioning the OLD name in a
    comment reintroduces exactly the string this issue exists to remove."""
    text = path.read_text()
    hits = [
        (i + 1, line)
        for i, line in enumerate(text.splitlines())
        if re.search(r"eval|curl|wget", line, re.IGNORECASE)
    ]
    assert not hits, f"{path} still contains eval/curl/wget: {hits}"


def test_identity_check_at_l518_rejects_mismatched_remember_dir(tmp_path):
    """Integration-level positive control for the identity-line call site
    specifically: the publish/load round trip for the REMEMBER_DIR identity
    line must still reject a cache written for a DIFFERENT REMEMBER_DIR,
    now that the identity is assigned via the new encode/decode pair
    instead of `eval "_identity=$_identity_raw"`."""
    remember_dir_a = tmp_path / "project-a" / ".remember"
    remember_dir_b = tmp_path / "project-b" / ".remember"
    remember_dir_a.mkdir(parents=True)
    remember_dir_b.mkdir(parents=True)
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_CONFIG=$(mktemp "${{TMPDIR}}/remember-config-XXXXXX")
export REMEMBER_DIR="{remember_dir_a.as_posix()}"
_remember_cfg_flatten_cache_publish "$(printf 'FOO\\tbar')"
export REMEMBER_DIR="{remember_dir_b.as_posix()}"
if _remember_cfg_flatten_cache_load; then
    echo "LOADED-WRONGLY"
else
    echo "REJECTED"
fi
"""
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PROJECT_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    assert "REJECTED" in result.stdout, (
        f"identity check did not reject a cache from a different "
        f"REMEMBER_DIR: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_identity_check_at_l518_accepts_matching_remember_dir(tmp_path):
    """'Must fire' pair for the rejection test above: the SAME REMEMBER_DIR
    must load successfully, proving the rejection above is a real identity
    mismatch check and not a decoder that rejects everything."""
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True)
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_CONFIG=$(mktemp "${{TMPDIR}}/remember-config-XXXXXX")
export REMEMBER_DIR="{remember_dir.as_posix()}"
_remember_cfg_flatten_cache_publish "$(printf 'FOO\\tbar')"
if _remember_cfg_flatten_cache_load; then
    printf 'LOADED:%s' "$_RCFG_FOO"
else
    echo "REJECTED-WRONGLY"
fi
"""
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PROJECT_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    assert "LOADED:bar" in result.stdout, (
        f"matching REMEMBER_DIR failed to load: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_cache_path_bumped_so_old_format_cache_is_ignored(tmp_path):
    """A cache file written at the OLD (pre-#864, %q-based) path must never
    be read by the new loader -- the path itself changed
    (`remember-config-cache-` -> `remember-config-cache-v2-`), so an old
    file sits at a name this build never even looks at, rather than needing
    a version marker inside the file."""
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True)
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_DIR="{remember_dir.as_posix()}"
_remember_cfg_flatten_cache_path
"""
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PROJECT_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    assert "remember-config-cache-v2-" in result.stdout, (
        f"cache path was not bumped to the v2 scheme: {result.stdout!r}"
    )
