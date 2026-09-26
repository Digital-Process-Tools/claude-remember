---
title: "Generalizing a narrow security check needs a whole-suite grep for callers, not just the issue's own test files"
description: "The first #754/#755/#756 draft made _remember_may_inject's git-tracked check unconditional -- exactly what those issues asked for, and it passed all 10 new test cases plus the existing #721/#747 regression suite. It broke an unrelated-looking pre-existing test, test_delivery_record_per_machine_285.py, found only by a full-suite run."
match: scripts/.*\.sh$
---

`_remember_handoff_is_tracked` (#721/#747) had a precondition most readers would skim past: "True
only in legacy mode (`REMEMBER_ROOT == MEMORY_PROJECT_DIR`)". That equality is not incidental -- it
is the entire thing that kept the original guard from firing on external-storage mode, where
`REMEMBER_DIR` can be (and, in one real fixture, genuinely is) a subdirectory of the user's OWN
private git repository: the `git_backup` feature (`hooks.d/after_save/50-git-backup.sh`, #253)
commits memory files into it on purpose, to sync a store across machines. A generic "ask git about
the file" check cannot tell that apart from a hostile cloned project shipping a poisoned
`.remember/*.md` committed into ITS repo -- both are, mechanically, "a file some git repository
tracks." Generalizing "how do I know if this file is tracked" (the #754 ask) without also carrying
forward "should I even ask" (the original scope) silently widens the blast radius of a security fix
into a feature it was never meant to touch.

**Before generalizing (or reviewing a generalization of) an existing narrow security/trust check:**
grep for every USE of the thing being generalized, across the whole test suite -- not only the test
file the issue itself names. `test_delivery_record_per_machine_285.py` never mentions #721, #747,
#754, #755 or #756 anywhere in its own text, and there was no way to find it from the issue bodies
alone; only a full test-suite run, not the four targeted files the task was scoped to, caught the
regression.

**Passing every test the issue's own test files name proves nothing about scope creep.** The
generalized check passed all 10 new cases and the full #721/#747 regression suite on the first
try -- the break was in a file with no textual link to any of those issue numbers.
