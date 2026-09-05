---
title: "tree_snapshot compare: the cd, the snapshot's home, and the third verdict"
description: "A compare run from the wrong cwd reports `mutated` about the wrong repo. A before-snapshot in the shared scratchpad can vanish mid-run. could-not-compare is never clean."
tool: Bash
match: ~tree_snapshot
mode: remind
---

The review phase's mutation check has three failure modes, all observed in this repo, and each
produces an answer that reads exactly like a real one.

**Put the `cd` on the same Bash call as the compare.** The Bash tool's cwd resets between calls,
so a `compare --before -` issued in a later call than the `cd` reads the **main clone's** git
state, not the worktree's -- and reports `VERDICT: mutated -- HEAD moved from <worktree HEAD> to
<clone HEAD>`. Observed (#456): a lane spent several seconds believing a spawned reviewer had
mutated its tree; re-running as `cd <worktree> && cat before.json | ... compare --before -` in one
call returned `clean`. The developer brief already says this for the three write ops (note, report,
PR payload). It applies to read-only diagnostics too -- that is the gap this rule closes, because
nothing about a diagnostic looks like it needs the same care.

**Write the before-snapshot inside the worktree, not the shared scratchpad.** Observed (#539): a
snapshot written to `<scratchpad>/before_snapshot.json`, verified readable immediately after the
write, was simply gone several calls later while every neighbouring file written in the same window
survived. No error at any point. The reviewers had run 3-4 minutes concurrently in between. Cause
was never established -- scratchpad GC racing a long-running background agent is a guess, not a
finding -- so the mitigation is positional, not a fix: a path inside the worktree is not subject to
whatever collected that file.

**`could-not-compare` is a third verdict and it is not `clean`.** When the snapshot is missing, the
cwd is wrong, or the compare cannot run, say so in the report. `git status --porcelain` coming back
empty is weaker indirect evidence, not a substitute -- it cannot see an index/worktree split.

**The check earns its cost -- it has caught a real one.** Observed (#527): an `Explore` reviewer,
told explicitly not to mutate the tree, left the working tree at HEAD's blob and the **index** at
`HEAD~1`'s. `git status` showed `MM scripts/post-tool-hook.sh` -- a false "modified" nobody made.
HEAD never moved, and the reviewer's own final message claimed it had worked in an isolated scratch
copy, so there was no admission to catch it by. Only the blob hashes showed it. Left alone, the next
`git add -A` would have committed a silent revert of the fix. Cost to repair, once the snapshot
named the file and the direction: one `git restore --staged <path>`.
