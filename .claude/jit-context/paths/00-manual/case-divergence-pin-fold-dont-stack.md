---
title: "Editing a site test_case_divergence_298.py already pins: fold the pair, don't stack a new one beside it"
description: "Twice now (#734, #746), a second PR added a new old->new pair on top of a site an earlier PR had already pinned, and the earlier pair's old AND new forms both vanished from origin/main once the second PR merged -- 8 CI legs red, invisible to the second PR's own CI."
match: tests/test_case_divergence_298\.py$
---

`test_case_divergence_298.py` pins old->new code-shape substitutions against `origin/main`, one
pair per site. Twice, a second PR has landed on top of a first PR's already-pinned pair at the
SAME site and stranded it: #662's pair, stacked under #726, went red (fixed by #734/#735); #726's
own pair, stacked under #740/#744, went red again the same way -- 8 CI legs on `main` at
`ba2a592` (fixed by #746: "the pre-#726 -> #726 pair's old AND new code are both gone from
origin/main once #744 landed on top of it -- the same 'neither old nor new' state #734 fixed for
#662's pair").

**Why CI cannot catch this on the PR that causes it.** The break only appears once the second PR
actually lands on `main` -- its own CI diffed its branch against the *pre-merge* `origin/main*,
where the first pair's old and new forms were both still present. Nothing in that PR's own review
or its own CI run can see the pair going stale; only `main` itself, after the merge, goes red.

**Before adding or changing anything at a site this file already pins:** read the file's existing
pairs for that site first. If one already exists, fold the transition into ONE pair from the
*current* `origin/main` form to the new form, and delete the older pair in the SAME PR -- never
add a new pair alongside the old one. A stacked pair is not additive: once your PR merges, the
older pair's "old" form no longer exists anywhere in history the test can reach, and its "new"
form no longer exists in the tree either, so it can never pass again. This has to be checked by
reading the test file itself, not by trusting your own branch's CI green.
