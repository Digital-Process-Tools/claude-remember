---
title: "New blanket win32 skip needs a docs/windows-skip-triage.md entry, same commit"
description: "#585 and #588 each hit this only on a Windows CI leg, 20+ minutes into the run. A module-level pytestmark = pytest.mark.skipif(sys.platform == \"win32\", ...) needs a docs/windows-skip-triage.md row before it is pushed, not after."
tool: Bash
match: ~pytestmark[^)]*skipif\([^)]*win32
mode: remind
---

You are writing (via `supertool 'paste:@-'` or `'edit:@-'`) a module-level

    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason=...,
    )

or its list-form (`pytestmark = [pytest.mark.skipif(...win32...), ...]`). This skips **every
test in the file** on the `windows-latest` CI leg, unconditionally. `docs/windows-skip-triage.md`
is the recorded per-module verdict list #497/#507 asked for, and a new row belongs in the
**same commit**, not a follow-up once Windows CI fails.

**Before you push:**

1. Read `docs/windows-skip-triage.md`'s own header for the convention and what a
   compliant row looks like.
2. Add this module's row now.
3. Prefer `tests/_bash_runner.py`'s `resolve_bash()` route instead, if the reason is
   "no bash on this platform" -- that is the route #432 already converted four modules
   onto, and it needs no triage-doc row at all because it is not a blanket skip.

**This deliberately does not fire on a per-test `@pytest.mark.skipif(...)` decorator** --
the `pytestmark[^)]*` anchor requires the module-level assignment form. This repo's own
declined trap `487.ostype-immutable-on-windows-gitbash` (`.claude/jit-context/tools/00-manual/00-README.md`)
already reasoned that a rule firing on every per-test Windows-conditional skip would restate
a skip that is already visible right there in the decorator; only the blanket, whole-file form
needs a doc row at all.

**Cost of skipping this:** #585 and #588 each cost 20+ minutes of CI round-trip for the
identical omission, in the same session -- caught only by the `windows-latest` legs, never at
authoring time. `tests/test_windows_skip_triage_497.py` already catches the same drift on
every CI leg (it is AST-based, not platform-conditional) -- this rule's only independent value
is firing before push, with no CI round-trip at all.
