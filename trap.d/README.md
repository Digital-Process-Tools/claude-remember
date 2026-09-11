# trap.d/ — one fragment per lesson, no ceremony

Managed by the oss plugin. This file, `trap.d/README.md`, is OVERWRITTEN every time
`/oss:scaffold` runs. Everything else in this directory -- every fragment an agent or a
person writes here -- is yours; the plugin never reads it, never replaces it, and never
deletes it.

## What this directory is for

A lesson that costs an agent time -- a call that swallowed the error, a fixture that
violated one platform's limit, a tool whose payload form is not what it looks like -- dies
with the session that paid for it unless it is written down. This directory is where it
goes.

**Log it and move on.** Hesitating over whether it is worth recording is the exact failure
this directory removes. Write it; a later pass decides whether it becomes a permanent rule.

## Writing one

- **Name:** `trap.d/<issue>.<slug>.md` -- the issue that was being worked on, and a slug so
  two fragments on one issue never collide on a path. Both halves are required; `42.md`
  does not parse.
- **No frontmatter.** No `title:`, no `match:`, no `keywords:`. Prose is fine.
- **Decide nothing.** Not the dimension, not the match pattern, not whether it earns a
  rule. Promotion is a separate pass, taken later with every fragment visible at once --
  which is the only position from which "these three are one rule" is visible.

What helps whoever promotes it later:

| | |
| --- | --- |
| **what was observed** | the behaviour, not the theory |
| **where** | the file, the command, the error string, verbatim |
| **what it cost** | a CI round, a retracted conclusion, ten minutes |
| **how it was confirmed** | what changed to make it go away, or what was measured |

Unsure whether it belongs elsewhere, or is even a real rule? Say so in the fragment and log
it anyway -- a guess at where it belongs is useful and costs nothing.

## Fragments are inert

Nothing loads them into a session automatically. Writing one costs nothing and reads
nothing back; a separate pass is what turns a fragment into a rule a session actually sees.
This repository also ships a matching rule under `.claude/jit-context/paths/`, but that rule
is reinforcement, not the delivery mechanism -- it cannot fire before the first touch that
would trigger it, and it fires at most once per session even across several agents in the
same one. Nothing injects this file into a session either; the standing invitation to log a
trap has to live in whatever carries an agent's instructions on every turn, not in a file on
disk that is read only when something chooses to open it.
