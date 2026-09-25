---
title: "A targeted pytest run proves the new test passes -- it does not prove the change ships"
description: "#739 shipped with a Python 3.9 str | None annotation and shell code with a new bracket range, after an 18/18 local run of the new file plus adjacent suites; only the full CI matrix caught either one."
tool: Bash
match: ~(^|[;&|])[[:space:]]*(python3?[[:space:]]+-m[[:space:]]+)?pytest[[:space:]]+.*(tests/|\.py|::)
mode: remind
---

PR #739 was developed and tested locally against a targeted subset -- the new test file plus the
suites judged adjacent, 18/18 green -- and still shipped a change the first full CI round
rejected on every leg: `str | None` written directly in a function signature, which Python 3.9
evaluates at *definition* time, so collection itself failed before any test in the file could run.
Fixed by adding `from __future__ import annotations` to the new module (checked against 3.9
grammar with `ast.parse(feature_version=(3, 9))`, since no local 3.9 interpreter was available to
run it directly). The same lane's diff also added a new shell `[...]` bracket-range pattern
(a stale-hint sweep and a session-id allowlist) -- exactly the shape
`tests/test_locale_ranges_695.py` exists to catch an unguarded instance of -- which the targeted
run never exercised because it is a repo-wide scanner, not a suite "adjacent" to the new file.

**Two blind spots a targeted run cannot see, and both are cheap to check without running the full
suite:**

- **Python-version syntax.** `python3 -c "import ast; ast.parse(open('FILE').read(),
  feature_version=(3, 9))"` against any new or changed `.py` file catches a 3.9-incompatible
  annotation with no 3.9 interpreter needed.
- **A repo-wide scanner test.** If your diff adds a new shell bracket-range, a new spawn-counting
  call, or anything else a scanner test (`test_locale_ranges_695.py`,
  `tests/spawn_counting.py`-based tests, `test_case_divergence_298.py`) exists to catch, run that
  specific file -- it is fast and its whole job is to catch a pattern introduced anywhere in the
  tree, not just near files a "related suites" judgment call included.

A subset run is a legitimate speed choice; it just proves the new behavior works, not that it
ships. CLAUDE.md already keeps the full `pytest` run out of a pre-push hook for an unrelated
reason (transport timing) -- CI is still the only place the whole matrix runs, so a class only the
matrix catches costs a full round-trip whenever it slips past here.
