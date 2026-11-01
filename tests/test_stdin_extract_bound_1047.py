"""#1047: `_stdin_json_string_into`'s `${var#*pattern}` is quadratic in bash --
O(n^2) on the RAW input's length. Measured by the reporter on bash 5.2.15:
0.28s at 12.5 KB, 15.2s at 100 KB, 4x per doubling. A host that pipes a whole
transcript in as SessionStart's stdin (legal, not something the hook can
refuse) hands this function a multi-megabyte string; the reporter observed
27 `session-start-hook.sh` processes alive 21 hours each, immune to SIGTERM
(a single builtin runs for hours with no signal check).

Fixed by bounding the RAW argument to a fixed prefix (4096 bytes) before the
quadratic match runs, inside `_stdin_json_string_into` itself -- not by
truncating `HOOK_STDIN`'s own accumulation, which the
before_session_start/after_session_start listener-publish block (#266,
further down in this hook) still needs byte-identical up to its own 32 KiB
cap. The four fields this hook ever extracts (cwd, session_id,
transcript_path, source) are documented by the issue itself to sit in the
first few hundred bytes of a legitimate payload, so this bound is a no-op
for every real payload and only ever discards an adversarial tail.

Reuses `_extract_function` from test_stdin_extractor_top_level_wins_447.py
(this file's own convention for pulling a function verbatim out of a real
hook) rather than re-deriving the extraction, so this test exercises the
actual shipped code, not a hand-copied stand-in."""

from __future__ import annotations

import time

import pytest

from ._bash_runner import resolve_bash
from .test_stdin_extractor_top_level_wins_447 import _extract_function

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found (see tests/_bash_runner.py)",
)

SESSION_START = "scripts/session-start-hook.sh"

# A function body plus a thin driver: $1 is the field name, $2 is a PATH to
# the raw payload -- a multi-megabyte payload cannot travel as an argv word
# (the OS's own ARG_MAX refuses it, observed: "Argument list too long"), so
# it goes through a temp file and bash's own `$(<file)` fast-path command
# substitution instead (the same construct capture-gap-check.sh's own
# SEEN_ID read already uses, for the same "no fork" reason).
_DRIVER = """
%s
_RAW=$(<"$2")
_stdin_json_string_into OUT "$1" "$_RAW" 2>/dev/null
printf '%%s' "$OUT"
"""


def _run(raw: str, field: str, timeout: float, tmp_path) -> tuple[str, float]:
    import subprocess

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(raw, encoding="utf-8")
    body = _extract_function(SESSION_START, "_stdin_json_string_into")
    script = _DRIVER % body
    start = time.monotonic()
    result = subprocess.run(
        [BASH, "-s", "--", field, str(payload_file)],
        input=script, capture_output=True, text=True, timeout=timeout, check=False,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    return result.stdout, elapsed


def test_a_huge_payload_with_the_target_field_near_the_start_stays_fast(tmp_path):
    """Positive control: `${var#*pattern}`'s cost tracks how far into the
    string the match succeeds, not the string's total length (consistent
    with the issue's own table, which put `cwd` at the END of its synthetic
    string to show the cost at all) -- so a huge payload with the target
    field near the front, exactly where a real SessionStart payload puts
    it, was already fast before #1047 too. This is NOT a regression pin by
    itself (see the must-fire timing case below for that); it confirms the
    fix is a no-op for the realistic shape every legitimate payload has.
    """
    huge = '{"cwd":"/some/project","padding":"' + ("x" * (2 * 1024 * 1024)) + '"}'
    out, elapsed = _run(huge, "cwd", timeout=20, tmp_path=tmp_path)
    assert out == "/some/project", f"expected the field extracted, got {out!r}"
    assert elapsed < 3.0, (
        f"extraction against a 2 MB payload took {elapsed:.2f}s -- the "
        f"quadratic match is back if this scales with payload size"
    )


def test_a_payload_with_the_target_field_far_from_the_start_no_longer_hangs(tmp_path):
    """Must-fire case, the issue's own repro shape and its own measurement
    methodology: the issue's table places the target field at the END of
    its synthetic string specifically because that is where
    `${var#*pattern}`'s cost shows up (0.95s at 25 KB, 15.2s at 100 KB on
    the reporter's bash 5.2.15). RED before #1047: this 150 KB payload,
    field at the very end, took multiple seconds per that table; bounded to
    the first 4096 bytes, the match never reaches that far at all and
    returns (missing the field, the documented degradation -- see the
    correctness pin below) in a small fraction of a second regardless of
    payload size."""
    padding = "x" * (150 * 1024)
    far = '{"padding":"' + padding + '","session_id":"far-value"}'
    _out, elapsed = _run(far, "session_id", timeout=30, tmp_path=tmp_path)
    assert elapsed < 3.0, (
        f"extraction against a 150 KB payload with the target field at the "
        f"end took {elapsed:.2f}s -- the quadratic match is back"
    )


def test_small_payload_extraction_is_byte_identical(tmp_path):
    """Positive control, paired with the must-fire case above: an ordinary
    small payload (well under the 4096-byte bound) must extract exactly as
    before -- the bound must be a no-op for every real-world payload, not
    just avoid hanging on an adversarial one."""
    small = '{"session_id":"abc-123","cwd":"/a/b/c","source":"startup"}'
    out, _ = _run(small, "session_id", timeout=10, tmp_path=tmp_path)
    assert out == "abc-123"
    out, _ = _run(small, "cwd", timeout=10, tmp_path=tmp_path)
    assert out == "/a/b/c"
    out, _ = _run(small, "source", timeout=10, tmp_path=tmp_path)
    assert out == "startup"


def test_a_field_entirely_past_the_bound_is_missed_not_hung(tmp_path):
    """A field that genuinely starts past the 4096-byte bound is missed --
    the documented degradation, same shape #745's own pluginless-SDK sniff
    cap already accepts -- rather than causing the match to scan the whole
    (still huge) string. Confirms the bound actually engages rather than
    silently extracting through the full payload regardless."""
    late = (
        '{"padding":"' + ("x" * 8192) + '","session_id":"late-value"}'
    )
    out, elapsed = _run(late, "session_id", timeout=10, tmp_path=tmp_path)
    assert out == "", (
        f"expected the far field to be missed once past the bound, got "
        f"{out!r} -- the 4096-byte cap may not be engaging at all"
    )
    assert elapsed < 3.0


def test_a_value_straddling_the_bound_fails_closed_not_corrupted(tmp_path):
    """Self-review finding: the field's opening quote can survive the
    4096-byte bound while its closing quote does not -- the field name and
    value-opening quote sit inside the bound, but the value itself is long
    enough that its closing quote falls past the cut. Before this fix, the
    function returned SUCCESS with the entire unterminated truncated tail
    as the "value" instead of failing closed: a corrupted-but-non-empty
    result, not the documented "missed" degradation every other case in
    this file pins. `cwd` is the one call site most exposed to this (no
    character allowlist, no enum, no openability check at the point of
    extraction -- see session-start-hook.sh's own comment on
    REMEMBER_HOOK_CWD), so that is the field name used here."""
    straddling_value = "y" * 8192  # long enough that its closing quote cannot survive the bound
    straddling = '{"cwd":"' + straddling_value + '"}'
    out, elapsed = _run(straddling, "cwd", timeout=10, tmp_path=tmp_path)
    assert out == "", (
        f"expected extraction to fail closed (empty) when the value's own "
        f"closing quote falls past the bound, got a "
        f"{len(out)}-byte corrupted value instead: {out[:80]!r}..."
    )
    assert elapsed < 3.0
