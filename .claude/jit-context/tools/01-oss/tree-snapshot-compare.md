---
title: "tree_snapshot compare: the recorded root, the snapshot's home, and the third verdict"
description: "compare already defaults to the before-snapshot's own recorded root, not the live cwd -- but only once it says that root actually resolved. A before-snapshot in the shared scratchpad can vanish mid-run. could-not-compare is never clean."
tool: Bash
match: ~tree_snapshot
mode: once
---

**`compare` defaults to the before-snapshot's own recorded root, not the live cwd -- no
same-call `cd` chaining needed -- but only once the before-snapshot itself says that root actually
resolved.** An earlier version re-snapshotted whatever directory the later call happened to stand
in, so a cwd reset between calls read the wrong tree and reported a false `mutated` verdict about
the wrong repository. Pass `--root` explicitly only to compare against a different directory on
purpose, or say so in the report if the pair ever lands on the wrong sibling worktree.

**Write the before-snapshot inside the worktree, never a shared scratchpad.** A snapshot written
to a shared scratchpad, verified readable right after the write, has been observed gone several
calls later with no error at any point (root cause unconfirmed -- GC racing a long-running
background agent is a guess). A worktree-local path is not subject to whatever collected it.

**`could-not-compare` is a third verdict and it is not `clean`.** Missing snapshot, unresolved
root, or a compare that cannot run -- say so in the report. `git status --porcelain` coming back
empty is weaker indirect evidence, not a substitute; it cannot see an index/worktree split.

**The check earns its cost -- it has caught a real mutation.** A reviewer told not to mutate the
tree left the working tree at HEAD's blob but the **index** at a different commit's -- `git status`
showed a false "modified" nobody made, HEAD never moved, and the reviewer's own final message
claimed it worked only in an isolated scratch copy. Only the blob hashes showed it. Left alone, the
next `git add -A` commits a silent revert. Repair, once the snapshot names the file and direction:
one `git restore --staged <path>`.
