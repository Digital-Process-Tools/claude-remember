---
title: "What a sub-manager is"
description: "One tick, then it dies with its context. Holds every authority the loop needs except tag and publish."
keywords: sub-manager, submanager
mode: once
---

`agents/sub-manager.md` runs exactly one maintainer tick over the repo named by `.oss.json`, then
dies -- its context is thrown away at the end of the tick, which is the design's whole cost story: a
tick's price should track the tick it ran, not accumulate across every tick that ever ran.

**It re-derives the board itself rather than trusting a handoff.** The tick state file records what
was *believed* when it was written; the first call of a tick is the repo itself -- the last commit,
the open pull requests, the open issues.

**It holds every authority the loop needs except tag and publish.** It dispatches lanes, reviews
pull requests, merges on green -- all of it. Only `agents/releaser.md` runs the release phase. Of
the two, only publishing is code-enforced: `scripts/release_publish.py` reads a marker
(`scripts/agent_role.py`) and refuses to publish a GitHub Release the instant it sees
`sub-manager`. Tagging carries no such check -- `git tag` and `git push origin <tag>` are plain
shell commands, so withholding tagging from a sub-manager rests on this file's own prose, not on
anything a script enforces.

**It never runs a whole tick inline.** Since #1544 it spawns one throwaway agent per step --
`oss:tick-dispatch` (select, claim, render the lane call), `oss:tick-review` (wait for CI, review,
route findings), `oss:tick-merge` (one merge), `oss:tick-accounting` (state file + a drafted
handback) -- and stays the one live parent that receives each lane's own completion, rather than
holding every step's own procedure in its own long-lived context for the rest of the tick.
