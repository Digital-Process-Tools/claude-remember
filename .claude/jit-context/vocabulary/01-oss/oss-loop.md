---
title: "What `oss` is: the maintainer loop, its commands, and the decision boundary"
description: "A repository that maintains itself, on a loop, with no human in the merge path -- and what that autonomy does and does not cover."
keywords: oss, the loop, maintainer loop
mode: once
---

`oss` is a maintainer loop for an open-source repository, installed into that repository as a
Claude Code plugin. It triages the tracker, decides what is worth building, delegates
implementation, reviews hard, merges on green, and releases -- unattended, no human in the merge
path.

**Four commands**, in `commands/`:

| command | does |
| --- | --- |
| `/oss:run` | the scheduler -- sets up on first use, then triages, builds, reviews, merges and releases on a loop |
| `/oss:tick` | one maintainer tick -- read the board, decide, delegate, review, merge on green |
| `/oss:doctor` | diagnose this repo's oss setup |
| `/oss:release` | cut a release -- gates first, then version sites, tag, publish |

**The decision boundary**: the loop holds tag-and-publish authority when `.oss.json`'s
`release.authority` says `loop` -- the gates still bind (CI green at leg level, review passed, the
release delta clean), but no human approval is required to cross them. Everything upstream of a
release is already the loop's own decision at every step: which issues to work, how to fix them,
whether a finding blocks. Autonomous here does not mean unbounded, though -- the loop is shaped as
much around cost as around correctness, since an unattended run spends real quota on every turn of
every agent it spawns; see `CLAUDE.md`'s token-economy section for the numbers that constrain it.
