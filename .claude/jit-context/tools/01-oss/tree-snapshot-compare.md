---
title: "tree_snapshot compare: the recorded root, the snapshot's home, and the third verdict"
description: "compare already defaults to the before-snapshot's own recorded root, not the live cwd -- but only once it says that root actually resolved. A before-snapshot in the shared scratchpad can vanish mid-run. could-not-compare is never clean."
tool: Bash
match: ~tree_snapshot
mode: remind
---

The review phase's mutation check has three things worth knowing, and each has produced an
answer that read exactly like a genuine one.

**`compare` already defaults to the before-snapshot's own recorded root, not the live cwd -- a
same-call `cd` chaining `snapshot` to `compare` is not needed for the ordinary case.** An
earlier version of this tool re-snapshotted whatever directory the later Bash call happened to
be standing in, so a `compare --before -` issued after cwd had reset between calls read the
wrong tree and reported a false `mutated` verdict about the wrong repository. `compare` now
reuses the root `snapshot` recorded by default, provided the before-snapshot itself says that
root actually resolved -- pass `--root` explicitly only to compare against a different directory
on purpose, or if this pair ever lands on the wrong sibling worktree, in which case say so in the
report rather than trusting the default silently.

**Write the before-snapshot inside the worktree, not a shared scratchpad.** A snapshot written to
a shared scratchpad location, verified readable immediately after the write, can simply be gone
several calls later while every neighbouring file written in the same window survives, with no
error at any point. Root cause unconfirmed -- scratchpad GC racing a long-running background
agent is a guess, not a finding -- so the mitigation is positional, not a fix: a path inside the
worktree is not subject to whatever collected the missing file.

**`could-not-compare` is a third verdict and it is not `clean`.** When the snapshot is missing,
the recorded root could not be resolved, or the compare cannot run, say so in the report.
`git status --porcelain` coming back empty is weaker indirect evidence, not a substitute -- it
cannot see an index/worktree split.

**The check earns its cost -- it has caught a real mutation.** A reviewer told explicitly not to
mutate the tree can still leave the working tree at HEAD's blob and the **index** at a different
commit's. `git status` then shows a false "modified" nobody made, HEAD never moved, and there is
no admission to catch it by if the reviewer's own final message claims it worked only in an
isolated scratch copy -- only the blob hashes show it. Left alone, the next `git add -A` commits a
silent revert of the fix. Cost to repair, once the snapshot names the file and the direction: one
`git restore --staged <path>`.
