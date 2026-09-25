---
title: "A value computed per-session, published through one shared file, is owned by whichever session wrote it last"
description: "session-start-hook.sh resolved a correct per-session handoff path, then published it through a single project-wide file with no session identity -- 'correct' is a property of (value, reader) pairs, and a channel with no notion of 'which session' drops the second half."
match: scripts/.*\.sh$
---

`#738`: `session-start-hook.sh` resolved a per-session handoff target (`remember.<session_id>.md`,
only under `handoff_mode: "per_session"`) correctly, and published it to a SINGLE file,
`$REMEMBER_DIR/tmp/handoff-path`, rewritten on every SessionStart regardless of which session
started it. `write-handoff.sh` then trusted that one file with no idea which session was actually
calling it. Neither resolution step was wrong in isolation -- the per-session path was computed
correctly, the shared file was written correctly -- the bug was that a value whose correctness
depends on WHICH SESSION is asking was handed off through a channel with no notion of "which
session" at all: one path, last writer wins, readable by anyone. Two sessions running
concurrently (the ordinary case for `per_session` mode, not an edge one) is enough to make the
LAST session to start own the pointer, so an EARLIER session's later `/remember` silently wrote
into a DIFFERENT session's file.

**The general shape, worth watching for anywhere in this codebase:** a value computed
per-request/per-session/per-caller, published through a fixed, shared location for a downstream
reader to pick up out-of-band later. The value can be perfectly correct at write time and still be
wrong by the time it's read, because "correct" here is a property of (value, reader) pairs, and
the shared channel drops the second half. The fix that generalizes: key the published artifact by
whatever identity the channel was silently assuming was unique (here, the session id via
`CLAUDE_CODE_SESSION_ID`, a live env var Claude Code sets for the Bash tool) rather than trying to
make the channel itself more careful about ordering. This repo already has one worked example of
doing it right -- `remember.delivered.<session_id>` (#373) -- worth reaching for as the template
before reasoning a new per-session channel out from scratch, pruning included (same
GRACE_MIN/#393-coupled sweep, one extra loop).
