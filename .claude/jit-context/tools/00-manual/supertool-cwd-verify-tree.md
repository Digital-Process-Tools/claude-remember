---
title: "supertool's default cwd can silently point at the wrong tree -- verify with a known-content probe, not trust"
description: "In a lane given its own worktree, the first several supertool calls of a session resolved against the main clone instead, because the default cwd loaded from .supertool.json pointed there. Caught only by a grep that returned zero results for code known to exist."
tool: Bash
match: ~supertool
mode: remind
---

Observed on a follow-up lane working `worktree /path/to/repo-wt/<N>` (#705): supertool's default
cwd for ops loaded from `.supertool.json` silently pointed at the main clone rather than the
worktree the lane's task actually named, for the first several calls of the session. The mismatch
surfaced only because a `grep` for code known to exist in the worktree returned zero results;
prefixing `cwd:PATH` to the same call found it immediately.

**If a lane or task names a specific worktree path, prefix `cwd:<that path>` to every supertool
call rather than trusting the default** -- the default cwd loaded from `.supertool.json` does not
necessarily track the actual directory a task was given, and a silent wrong-tree read reads as a
normal empty result (a grep with no hits, a read of a file that "doesn't exist yet"), not an
error. If an early read in a fresh session returns nothing for something you expect to be there,
suspect the cwd before concluding the code is missing.

**This extends to write ops, not just reads (#804).** A write op run from whatever cwd the
harness defaulted to -- not cwd-prefixed the same way a preceding read/grep was -- applied
cleanly to the *main clone* instead of the worktree the task named: all four edit calls reported
"1 write" and passed their git-status validator normally, so nothing about the tool's own output
looked like a failure. It surfaced only because a later cwd-prefixed read in the worktree still
showed the unedited file, and `git diff --stat` in the worktree showed nothing, prompting a
git-status check in both trees that found the diff sitting in main instead. A wrong-tree write
does not read as an empty result the way a wrong-tree read does -- it reads as success -- so
prefixing `cwd:PATH` on reads/greps alone is not enough: prefix it on every supertool call in a
worktree task, edit and paste included, and if a write's success is ever in doubt, confirm with
`git status`/`git diff --stat` in *both* trees rather than trusting the op's own report.
