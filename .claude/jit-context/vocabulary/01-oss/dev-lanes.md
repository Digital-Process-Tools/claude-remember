---
title: "How many developer lanes to run, and what one carries"
description: "The recurring question is really one question asked many ways: how many lanes, when to start them, what a lane needs, and how many issues it carries."
keywords: dev, devs, developer, developers, lane, lanes
mode: once
---

A developer lane is one spawn of `agents/developer.md`: one worktree, TDD, self-review, a commit
handed back. It never pushes, never opens a pull request, never merges -- the maintainer loop owns
the push, the pull request, the merge and the release.

**A lane carries up to three issues, bounded by file disjointness, not by ambition.** One issue per
lane is the under-filled state: the lane pays its own context floor either way, so a second and
third issue close to that same floor are close to free against a denominator that triples. Two
issues that touch the same file are not one lane's problem to hold -- split them across lanes
instead.

**How many lanes run at once is a dispatch decision, not a number picked in advance.**
`skills/manager/phases/dispatch.md` ranks the open, unclaimed board and claims what is claimable
each tick; the count that comes out is however many file-disjoint groups the ranked issues resolve
into that tick. "Start more devs" and "run as much dev as possible" are the same request answered
the same way: let the ranked claim produce as many lanes as the board supports this tick, never a
target chosen ahead of reading it.

**What a lane needs to start**: an issue number (or up to three, via `--claim-also`) and a
worktree path -- `lane_setup.py --claim` writes the GitHub assignee for the issue(s), which is the
claim, not a local record of the worktree; `git worktree` state is the only authority on which
worktrees are live. Beyond those, `agents/developer.md` re-derives everything else (config, guards)
rather than trusting what its own spawn payload restates.
