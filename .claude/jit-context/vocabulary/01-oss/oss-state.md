---
title: "The tick state file"
description: "Written every tick, read first every tick. A corrupt file raises rather than resetting; an over-long decision is refused rather than truncated."
keywords: state file, oss-watch, tick state, handoff, oss state
---

Written every tick, read first every tick. The decision and the one reason for it -- reasoning that
only matters to a pull request belongs in that pull request.

**The handoff is not the repo.** This file records what was *believed* when it was written. The
first call of a session is the repo itself: the last commit, the open pull requests, the open issues.

Two refusals worth knowing before editing the file by hand:

- **A corrupt file raises rather than starting fresh.** A silent reset destroys the history the file
  exists for, and the tick that did it looks exactly like a first tick.
- **An over-long decision is refused, not truncated.** A truncation drops the half that mattered and
  leaves something that still reads as a record.
