---
title: "bash's fork-free $(<file) is a special-cased grammar production -- add any other token and it silently reads nothing"
description: "x=$(2>/dev/null <\"$f\") was written to avoid forking cat while also silencing a missing-file error, and returns EMPTY for a file that exists and has real content -- not just for a missing one."
match: scripts/.*\.sh$
---

`$(<file)` -- **exactly** that, nothing else inside the parens -- is a special-cased bash grammar
production that reads the file with no subprocess at all. The moment another token shares the
parentheses, even a bare redirection like `2>/dev/null`, bash instead parses it as an ordinary
command substitution running NO command with two redirections set up: no output, ever, file
present or missing, silently.

Confirmed directly (#679): `x=$(2>/dev/null <"/tmp/f")` on a file containing `sess-prev` printed
`[]`, `len=0`; `x=$(<"/tmp/f")` on the identical file printed `[sess-prev]`, `len=9`. The bug read
as correct in review because the missing-file case (tested first, by hand) genuinely returns
empty either way -- only the present-file case exposes the difference, and the targeted test
written for these lines never checked the read VALUE, only the absence of a `cat` spawn.

**If you want to read a file into a variable with no fork, and also want a clean empty result on
a missing file:** guard existence first (`[ -f "$f" ]`), then use the untouched `$(<"$f")` form
--- nothing else in the parens -- only once existence is known. Verify byte-identical to `cat
"$f" 2>/dev/null` for both the missing-file and present-file cases before trusting it. And more
generally: a test asserting a spawn's *absence* is not a substitute for asserting the *value*
that call was supposed to produce -- pair every "this fork must not happen" assertion with one
that checks the actual output landed correctly, per this repo's own must-fire/must-not-fire rule
for tests.
