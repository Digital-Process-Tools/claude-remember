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
