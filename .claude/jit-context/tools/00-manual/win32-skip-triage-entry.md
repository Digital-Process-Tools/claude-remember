---
title: "New blanket win32 skip needs a docs/windows-skip-triage.md entry, same commit"
description: "#585 and #588 each hit this only on a Windows CI leg, 20+ minutes into the run. A module-level pytestmark = pytest.mark.skipif(sys.platform == \"win32\", ...) needs a docs/windows-skip-triage.md row before it is pushed, not after."
tool: Bash
match: ~pytest\.mark\.skipif\(sys\.platform *== *.win32.
mode: remind
---

You are writing (via `supertool 'paste:@-'` or `'edit:@-'`) a module-level

    pytestmark = pytest.mark.skipif(sys.platform == "win32", reason=...)

This skips **every test in the file** on the `windows-latest` CI leg, unconditionally.
`docs/windows-skip-triage.md` is the recorded per-module verdict list #497/#507 asked
for, and a new row belongs in the **same commit**, not a follow-up once Windows CI fails.

**Before you push:**

1. Read `docs/windows-skip-triage.md`'s own header for the convention and what a
   compliant row looks like.
2. Add this module's row now.
3. Prefer `tests/_bash_runner.py`'s `resolve_bash()` route instead, if the reason is
   "no bash on this platform" -- that is the route #432 already converted four modules
   onto, and it needs no triage-doc row at all because it is not a blanket skip.

**Cost of skipping this:** #585 and #588 each cost 20+ minutes of CI round-trip for the
identical omission, in the same session -- caught only by the `windows-latest` legs,
never at authoring time.
