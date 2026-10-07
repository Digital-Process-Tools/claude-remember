---
title: "A curate PR title's parenthetical counts must be derived from the enumerated list, not restated by hand"
description: "#921: the PR title read 9 already fixed + 4 filed = 13, but the body's own enumeration was 6 already-fixed + 1 matching an existing issue + 6 filed across 3 tracking issues = 13 -- the title's arithmetic matched neither grouping, because it was typed from memory instead of counted."
match: (commands/run/curate\.md|scripts/trap_curate\.py)$
---

Observed on PR #921 (curate pass, 2026-10-06): the title read "13 declined (9 already fixed, 4
filed as 3 tracking issues)". The body's own per-fragment breakdown enumerated 6 "already fixed"
+ 1 "matches an existing issue" (#888) + 6 fragments filed across 3 new tracking issues (#918,
#919, #920) = 13, matching the diff's 13 non-promoted deletions. The title's own arithmetic
(9+4=13) reconciled with neither grouping in the body -- it undercounted "filed" by 2 and
overcounted "already fixed" by 2-3, apparently by folding the "matches existing issue" case into
neither bucket consistently with the body text. The substance (diff, file list, issues filed) all
checked out; only the one-line title summary's arithmetic was off, caught during tick-review
rather than before the PR opened.

**When drafting this pass's own PR title, count directly from the per-fragment disposition list
you are about to write into the body -- never restate the counts from memory of how the pass
felt while running.** Tally promote / merge / decline / defer from the actual list of fragments
and their final outcomes, after every fragment has a disposition. The same "quote, don't
restate" principle `skills/manager/phases/review.md` already applies to issue bodies applies here
to a pass's own title.
