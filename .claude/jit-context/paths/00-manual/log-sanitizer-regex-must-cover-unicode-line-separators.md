---
title: "An ASCII-only control-char regex for anti-log-injection flattening misses three Unicode line separators Python's str.splitlines() still breaks on"
description: "#881's fix flattens C0/DEL (\x00-\x1f, \x7f) so an untrusted message cannot forge a second log line, but U+0085 (NEL), U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR) still split a Python str as two lines after the fix -- confirmed: python3 -c print(len(chr(0x2028).join(['a','b']).splitlines())) prints 2."
match: (pipeline/log\.py|scripts/log\.sh)$
---

`pipeline/log.py`'s `_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")` (and the shell-side
precedent it mirrors, `tr '[:cntrl:]' ' '` under `LC_ALL=C` in `scripts/log.sh`) flattens exactly
ASCII C0 plus DEL before writing an untrusted message to a log line, so the message cannot forge a
line break there. Python's `str.splitlines()` -- the exact method a line-oriented consumer, or a
test asserting "no forged second line", is likely to use -- also treats U+0085 (NEL), U+2028
(LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR) as line breaks, and none of those three
codepoints contain a C0/DEL byte in UTF-8, so the regex above does not touch them. A message
containing one of the three still reads as two lines to any `splitlines()`-based consumer, even
after the fix.

This is not a regression the Python port introduced -- the shell precedent has the identical
byte-wise blind spot, since `tr '[:cntrl:]' ' '` under `LC_ALL=C` is also ASCII-only. The two
sides are *meant* to match exactly; widening one without the other reopens the drift #881 closed.

**Before touching either side's control-char flattening, decide whether the class needs to cover
these three Unicode separators too** (extending the regex, or sanitizing against
`str.splitlines()`'s own boundary set directly rather than a fixed codepoint range) -- and if you
widen one side, widen the other the same way, or state in the commit why they now deliberately
differ. Today this is a rendering-seam gap, not an exploitable chokepoint: grepping `pipeline/` and
`scripts/` for `splitlines()` found no in-repo consumer that parses this log file line-by-line (the
only call sites, `pipeline/consolidate.py:216` and `pipeline/spawn_guard.py:156`, each parse an
unrelated string). It would matter the moment a future consumer reads this log line-by-line in
Python over the same untrusted-error-string surface (`pipeline/haiku.py`) the original fix exists
for.
