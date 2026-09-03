---
title: "Before gh-pr-merge: gates, cleanup, and the check that comes after"
description: "Never-failing checks are not green -- count the legs. gh-pr-merge writes nothing without |force. Cleanup and branch deletion are the op's own |cleanup token, not a second call."
tool: Bash
match: ~gh-pr-merge
mode: remind
---

Merge only when: CI fully green **at leg level** (the state counts must sum to the number of
legs; name any leg not `SUCCESS` before merging), the review passed, and the change is a
bugfix / docs / test / chore. Never auto-merge feature scope, a public API or behaviour
rename, an external-contributor PR, or anything irreversible.

- **`gh-pr-merge` writes nothing without `|force`.** One call:
  `supertool 'gh-pr-merge:N:squash|force|cleanup'`, never merge and cleanup as two calls.
- **Cleanup is gated on the op's own verified `MERGED` read-back**, not a second call. On a
  fleet-running loop its worktree half reports `skipped: reason` when more than one tree is
  idle -- correct, not a failure; reap the rest via `git-worktrees`, whose two columns
  (`cannot tell`, `merge unknown`) are never a yes.
- **One `Closes #N` per issue, the keyword repeated.** `Closes #A #B` links both and closes
  only `#A`. Read the whole line; a fragment match cannot audit either failing case.
- **After merge, check the default branch's own run with `gh-branch`** (GREEN / NOT GREEN /
  NO RUN / UNKNOWN) -- a green PR is a statement about its merge-base, not about `main` after
  the squash.
- **Do not route around a denied merge.** Say the call was denied, name it exactly, and let
  the maintainer run or permit it.

Full argument, the `|force` opt-outs and their blast radii, the rerun-vs-moved-base trap, and
the branch-deletion rules: `skills/manager/phases/merge.md`.
