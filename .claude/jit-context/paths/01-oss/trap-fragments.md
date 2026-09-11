---
title: "trap.d/: log it and move on, decide nothing"
description: "One file per trap, <issue>.<slug>.md, no frontmatter required. Do not choose a dimension, write a match pattern, or judge whether it is worth keeping -- a later curation pass does that, with every fragment visible at once."
match: (^|/)trap\.d/
---

**Log it and move on.** Hesitating because you are not sure it is worth recording is the exact
failure this directory removes. Write it; let a later pass throw it away.

- **Name:** `trap.d/<issue>.<slug>.md` -- the issue that was being worked on, and a slug so two
  fragments on one issue never collide on a path. Both halves are required; `42.md` does not parse.
- **No frontmatter.** No `title:`, no `match:`, no `keywords:`. Prose is fine.
- **Decide nothing.** Not the dimension, not the match pattern, not whether it earns a rule. A
  later curation pass does that, holding every fragment at once -- which is the only position
  from which "these three are one rule" is visible, and it is not the position you are in.

What helps whoever curates it:

| | |
| --- | --- |
| **what was observed** | the behaviour, not the theory |
| **where** | the file, the command, the error string, verbatim |
| **what it cost** | a CI round, a retracted conclusion, ten minutes |
| **how it was confirmed** | what you changed to make it go away, or what you measured |

Unsure whether it belongs upstream, or is even a real rule? **Say so in the fragment and log it
anyway** -- a guess at where it belongs is useful and costs you nothing.

**This rule is reinforcement, not the delivery mechanism.** It cannot fire before the touch that
would trigger it, so it has nothing to say at the exact moment a first fragment is written, and a
subagent sharing its parent's session sees it at most once for the whole session -- a rule an
earlier lane already triggered renders identically to one that never matched. The standing
invitation -- write a fragment when something costs you time -- has to live in whatever carries
your instructions on every turn (an agent or skill definition), not here.
