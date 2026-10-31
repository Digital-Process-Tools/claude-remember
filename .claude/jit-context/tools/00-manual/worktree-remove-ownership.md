---
title: "Never git worktree remove a path you did not yourself git worktree add -- a concurrent spawn may still be using it"
description: "oss:recon, pointed at a worktree path the calling lane had already named and was actively using, ran git worktree add then git worktree remove against that same path on its own initiative -- deleting the lane's own checked-out branch out from under it mid-session, with no error at the time."
tool: Bash
match: ~worktree (remove|add)
mode: remind
requires: supertool
---

**Interim copy of knowledge that belongs upstream** (claude-oss, not this repository's code) --
delete this once oss:recon itself is fixed. Filed as
[claude-oss#1813](https://github.com/Digital-Process-Tools/claude-oss/issues/1813).

A lane's spawn payload named a worktree path that already existed on disk when the lane's turn
started -- the lane used it directly (`git-status`, then `git checkout -b fix/991`) while
concurrently spawning `oss:recon` in the same tool-call batch, pointed at that same path with the
instruction "locate, do not design." The recon agent ran `git worktree add` against that same
path (treating it as not-yet-existing from its own perspective, or re-deriving it
independently), read from it, and then ran `git worktree remove` on it when done -- on the stated
reasoning that the task named that exact path as the one "to use," so it assumed cleanup was its
job.

**Result:** the lane's own checked-out branch and worktree directory vanished out from under it
mid-session, with no error at the moment it happened -- the next `cd`/`git-status` call in that
same path simply failed with "no such file or directory." The branch metadata survived in the
main clone, but the checked-out directory did not, and recreating it needed a second
`git worktree add` naming the existing branch rather than `-b` (which then failed: "a branch
named 'fix/991' already exists").

**Not catastrophic in the one observed case** -- no uncommitted edits existed yet when the
directory disappeared -- but if edits had already landed before the removal, this is a silent
loss of real work, the delete-side analog of the write-side hazard
`supertool-cwd-verify-tree.md` (this same layer) documents for a wrong-tree *write*.

**The rule, regardless of which side of this eventually gets fixed upstream:** never run
`git worktree remove` against a path you did not yourself `git worktree add` in the same
invocation, and never remove a worktree at all when the caller's own task payload named that
exact path (as opposed to a scratch path your own spawn derived and owns alone). If a task names
a worktree path explicitly, treat it as someone else's resource to read from, never to tear down
-- cleanup is the caller's job, not the reader's.
