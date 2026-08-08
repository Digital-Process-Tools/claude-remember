"""The digits-only guard feeds `$(( ))` without `10#`, and hand-fixing recurs (#332).

Four issues — #321, #325, #329, #331 — each added `10#` to a correct subset of
call sites and each looked complete. None was. #327 was filed with an inventory
("`10#` appears in save-session.sh and lib-lock.sh and nowhere else") that was
already stale on the day it was written, because #329 had added two more.

So the defect this file pins is not any one site. It is that the sweep was being
done by hand.

THE SHAPE
=========
    case "$X" in ''|*[!0-9]*) X=0 ;; esac      # rejects non-digits
    ...
    n=$(( now - X ))                           # reads "08" as octal, and dies

`08` and `09` are all digits, so they clear the guard; `$(( ))` then reads a
leading zero as octal and refuses with "value too great for base". Bash abandons
the whole enclosing command list at that point, so what is skipped is not the
arithmetic — it is every remaining statement in the branch, which in this repo
has repeatedly been the throttle, the ERROR log, or the backup.

WHY THIS IS ENUMERABLE RATHER THAN OPEN-ENDED
=============================================
`$(( ))` is the only sink. `[ "$x" -lt "$y" ]` parses base 10 without
complaint — measured, `[ 08 -lt 9 ]` is true and silent — so a guarded value
used only in `test` is NOT a finding and is not reported as one. That is what
keeps this check finite and false-positive-free enough to be a gate.

WHY A LINT AND NOT A SHARED HELPER
==================================
A `_remember_epoch_or_zero` in log.sh would cover most sites, and #332 argues
for it as its own change rather than bundled here. It also would not prevent
recurrence: the failure mode is a NEW call site written next month by someone
who has read none of these five issues, and a helper they do not know about
cannot help them. ShellCheck does not flag this shape either
(koalaman/shellcheck#2679, open since 2023), so CI catches nothing today.

A check in the test suite fails in the one place people already look.

THIS FILE IS ALSO A TEST OF ITSELF
==================================
`test_the_detector_finds_a_planted_instance` and its clean twin exist because a
linter that silently matches nothing is the exact defect class this repo keeps
shipping — an absence produced by the tool, read as an absence in the world. A
green here has to mean "looked, found none", never "did not look".
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SHELL_DIRS = ("scripts", "hooks.d")

# The guard's signature: any `case` arm rejecting a non-digit.
_DIGIT_ARM = re.compile(r"\*\[!0-9\]\*")
# `case "$VAR" in`, `case $VAR in`, `case "${VAR}" in`.
_CASE_HEAD = re.compile(r"\bcase\s+\"?\$\{?(\w+)\}?\"?\s+in\b")

# How far above a digit-rejecting arm the `case` head may sit. The single-line
# form puts them on the same line; the block form in this repo spans two.
_CASE_LOOKBACK = 4


def _guarded_names(text: str) -> set:
    """Every variable this file passes through a digits-only `case`."""
    lines = text.splitlines()
    names = set()
    for i, line in enumerate(lines):
        if not _DIGIT_ARM.search(line):
            continue
        for j in range(i, max(-1, i - _CASE_LOOKBACK) - 1, -1):
            head = _CASE_HEAD.search(lines[j])
            if head:
                names.add(head.group(1))
                break
    return names


def _arith_spans(text: str):
    """Yield (offset, source) for every `$(( ... ))`, nesting included.

    Hand-matched rather than regexed: every real site in this repo contains a
    nested `$(date +%s)`, and a non-greedy match stops at that inner `))` —
    which would silently exclude exactly the lines that matter.
    """
    i = 0
    while True:
        start = text.find("$((", i)
        if start < 0:
            return
        depth = 0
        j = start + 1
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield start, text[start:j + 1]
        i = start + 3


def _unbased_uses(text: str, names):
    """Guarded names read inside `$(( ))` without a `10#` radix prefix."""
    findings = []
    for offset, span in _arith_spans(text):
        for name in sorted(names):
            if name.isdigit():
                # A positional parameter: `case "$1" in` captures the name "1".
                # Inside `$(( ))` a bare `1` is the integer one, not a reference
                # to $1, so the sigil is REQUIRED here. Without it every
                # `$(( x + 1 ))` in a file that guards a positional becomes a
                # finding — and lib-lock.sh guards two of them today, so this
                # gate would have started failing the build on the next
                # ordinary line of arithmetic anyone added there.
                pattern = re.compile(r"\$\{?" + re.escape(name) + r"\}?")
            else:
                pattern = re.compile(r"(?:\$\{?)?\b" + re.escape(name) + r"\b")
            for m in pattern.finditer(span):
                head = m.start()
                while head > 0 and span[head - 1] in "${":
                    head -= 1
                if span[:head].endswith("10#"):
                    continue
                line = text.count("\n", 0, offset + m.start()) + 1
                findings.append((line, name, span.strip()))
    return findings


def _shell_files():
    for directory in SHELL_DIRS:
        yield from sorted((REPO_ROOT / directory).rglob("*.sh"))


def test_no_case_guarded_value_reaches_arithmetic_without_a_radix():
    """The sweep. One finding fails the build and names file, line and variable.

    If you are reading this because it went red on a line you wrote: the fix is
    `10#$VAR` inside the `$(( ))`, AFTER the `case`, never instead of it. `10#`
    on an empty string is itself an error on bash 5, and the `case` is also what
    rejects a space-padded value that arithmetic would otherwise accept.
    """
    offenders = []
    for path in _shell_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        names = _guarded_names(text)
        if not names:
            continue
        for line, name, span in _unbased_uses(text, names):
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"  {rel}:{line}  ${name}  in  {span}")

    assert not offenders, (
        "a value guarded by a digits-only `case` reaches `$(( ))` with no "
        "`10#`, so 08/09 clear the guard and are then read as octal — bash "
        "abandons the rest of the enclosing command list, which is where the "
        "throttle, the ERROR log or the backup lives "
        "(#321/#325/#327/#329/#331/#332):\n" + "\n".join(offenders)
    )


# Double-quoted, with the inner quotes escaped, and NOT single-quoted: `''`
# inside a single-quoted Python literal closes it and opens another, so the
# empty-string case arm vanishes into implicit concatenation with no error.
# test_the_planted_guard_is_actually_the_shape_it_claims pins that.
PLANTED_GUARD = (
    "LAST=$(cat \"$f\")\n"
    "case \"$LAST\" in ''|*[!0-9]*) LAST=0 ;; esac\n"
)


def test_the_detector_finds_a_planted_instance():
    """A linter that matches nothing must not be able to read as a clean repo."""
    planted = PLANTED_GUARD + "ELAPSED=$(( $(date +%s) - LAST ))\n"
    names = _guarded_names(planted)
    assert names == {"LAST"}, names
    found = _unbased_uses(planted, names)
    assert [f[1] for f in found] == ["LAST"], found


def test_the_detector_finds_it_through_the_dollar_form_too():
    planted = PLANTED_GUARD + "ELAPSED=$(( $(date +%s) - $LAST ))\n"
    assert [f[1] for f in _unbased_uses(planted, _guarded_names(planted))] == ["LAST"]


def test_the_detector_accepts_the_fixed_form():
    fixed = PLANTED_GUARD + "ELAPSED=$(( $(date +%s) - 10#$LAST ))\n"
    assert _unbased_uses(fixed, _guarded_names(fixed)) == []


def test_the_detector_does_not_flag_a_test_builtin_comparison():
    """`[ 08 -lt 9 ]` is true and silent, so `test` is not a sink. Flagging it
    would advertise a gap that is not there and train people to ignore this."""
    only_test = (
        "case \"$COOLDOWN\" in ''|*[!0-9]*) COOLDOWN=120 ;; esac\n"
        "[ \"$ELAPSED\" -lt \"$COOLDOWN\" ] && echo throttled\n"
    )
    assert _unbased_uses(only_test, _guarded_names(only_test)) == []


def test_the_sweep_actually_reads_the_shell_files():
    """`_guarded_names` returning nothing everywhere would make the sweep vacuous
    and green. Pin that the corpus is non-empty and that the guard is found in
    it, so a rename of scripts/ or hooks.d/ fails here instead of going quiet."""
    files = list(_shell_files())
    assert len(files) > 10, f"only {len(files)} shell files found — corpus is wrong"
    guarded = {
        p.relative_to(REPO_ROOT).as_posix(): _guarded_names(
            p.read_text(encoding="utf-8", errors="replace"))
        for p in files
    }
    with_guards = {k: v for k, v in guarded.items() if v}
    assert len(with_guards) >= 5, (
        "the digits-only guard was found in almost no file — the detector has "
        f"stopped recognising it: {with_guards}"
    )

# ── Two ways this detector can lie, both found reviewing #336 ────────────────

def test_the_planted_guard_is_actually_the_shape_it_claims():
    """The fixtures above must contain a real empty-string case arm.

    `''` inside a SINGLE-quoted Python string is not two apostrophes — it closes
    the literal and opens another, and the two characters vanish into implicit
    concatenation with no error. The planted guard silently became
    `case "$LAST" in |*[!0-9]*)`, which still matched only because the detector
    keys off the `*[!0-9]*` arm.

    A self-test whose fixture is not the shape it claims still goes green, and a
    green here is supposed to mean "looked, found none".
    """
    assert "in ''|" in PLANTED_GUARD, (
        f"the empty-string arm was eaten by Python string concatenation: "
        f"{PLANTED_GUARD!r}"
    )


POSITIONAL_GUARD = "case \"$1\" in ''|*[!0-9]*) return 1 ;; esac\n"


def test_a_positional_parameter_guard_does_not_flag_the_integer_literal():
    """`case "$1" in` guards a positional parameter, so the captured name is the
    bare string "1" — and a bare `1` inside `$(( ))` is the integer one, not a
    reference to `$1`.

    Matching the name without its sigil turns every `$(( x + 1 ))` in the file
    into a finding. scripts/lib-lock.sh already carries two of these guards
    (_lock_timing_ns_to_ms, _lock_timing_s_to_ms), so the next ordinary
    arithmetic added to that file would have failed the build for no reason.

    A lint gate that cries wolf gets switched off, and then it is not a gate.
    """
    src = POSITIONAL_GUARD + "MS=$(( x + 1 ))\n"
    assert _guarded_names(src) == {"1"}
    assert _unbased_uses(src, _guarded_names(src)) == [], (
        "the integer literal 1 was mistaken for a reference to $1"
    )


def test_a_positional_parameter_is_still_flagged_in_its_dollar_form():
    """The other side: narrowing to the sigil must not make $1 invisible."""
    src = POSITIONAL_GUARD + "MS=$(( $1 * 1000 ))\n"
    assert [f[1] for f in _unbased_uses(src, _guarded_names(src))] == ["1"]


def test_a_positional_parameter_accepts_the_fixed_form():
    src = POSITIONAL_GUARD + "MS=$(( 10#$1 * 1000 ))\n"
    assert _unbased_uses(src, _guarded_names(src)) == []
