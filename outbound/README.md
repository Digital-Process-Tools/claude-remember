# outbound/ — drafted public acts, waiting to be sent

Managed by the oss plugin. This file, `outbound/README.md`, is OVERWRITTEN every
time `/oss:scaffold` runs. Everything else in this directory -- every draft an
agent writes here -- is yours; the plugin never reads it, never replaces it, and
never deletes it.

## What this directory is for

`trap.d/` is for a lesson that dies with the session that paid for it unless it
is written down. This directory is the same idea for a *public* act: a refusal
with its reason, a reply to a comment, a review on an outside pull request.
Writing the file is not the act -- it is a draft, and drafting needs no
permission. Posting it does, because it happens in the maintainer's name, on a
public tracker, from an unattended loop.

**The queue waits. The loop does not.** A draft sitting here is never a reason
a tick pauses, blocks or waits on a human. It is written, recorded, and the
loop moves on.

## Naming

```
outbound/<issue>.<state>.<slug>.md
```

`<state>` is one of `pending` (written, not sent), `sent` (posted, its receipt
recorded in the body), `dropped` (read and declined) or `stale` (what it was
written against has moved -- it needs rewriting, not sending). Both `<issue>`
and `<slug>` are required, the same reason `trap.d/`'s own fragments require
both: a name missing either is a path two acts on one issue would collide on.

## Drafts are inert

Nothing here posts on its own. A draft is read, and the state it moves to is
decided, by a separate pass -- the same relationship `/oss:curate` has to
`trap.d/`'s fragments.
