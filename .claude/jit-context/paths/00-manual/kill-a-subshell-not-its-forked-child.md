---
title: "kill $subshell_pid does not reach the sleep it forked -- it can outlive the subshell for the rest of its budget"
description: "Collapsing a per-second kill -0 poll loop into one long sleep looked like a pure win and passed every targeted test; it orphaned the sleep binary past the parent's kill+wait, reparented to PID 1, holding an inherited fd open for up to the full remaining timeout."
match: scripts/.*\.sh$
---

Collapsing a watchdog's per-second `kill -0 "$pid"` poll loop into a single `sleep "$budget"`
(then one `sleep "$grace"` after `TERM`) looks like a pure win -- same wall-clock timeout, fewer
forks -- and can pass every test written specifically for the change, including a full suite of
targeted timeout tests (#679). It broke a *different*, unrelated test at the full-suite level
only: that test used to finish in ~1.4s and started timing out at 10s.

**Why:** the parent's cleanup is `kill "$_wpid"; wait "$_wpid"`, sending SIGTERM to the watchdog
*subshell's own PID*. The subshell dies almost instantly -- but the `sleep` binary it forked to
do the actual waiting is the subshell's CHILD, not the subshell itself, and `kill $_wpid` only
ever names one PID. That child is orphaned (reparented to PID 1) and keeps running for up to the
full remaining budget. If it inherited a file descriptor still connected to whatever is reading
the script's own stdout/stderr, that orphan holds the pipe open long after the script that owns
it has exited, and a test harness's `subprocess.communicate()` blocks until the orphan eventually
exits on its own. The OLD per-second loop never surfaced this: it self-terminates within about a
second of the parent finishing, on its own, via its own `kill -0` check inside the loop, long
before the external `kill` is ever needed.

**The lesson:** `kill $pid` (or `kill -0` to check liveness) only ever addresses the one PID you
named. A subshell that itself forks a long-running command (`sleep`, or anything else) is not
killed by killing the subshell -- its own children survive as orphans, for however long they were
told to run, and can hold open any fd they inherited well past the point their parent process
exited. Before collapsing a poll loop into one long sleep inside a subshell (or forking anything
else long-running from one), audit every fd that subshell -- and anything it forks -- inherits,
or use a process-group kill (`kill -TERM -- -$pgid`) instead of a single-PID one. This specific
collapse was reverted rather than shipped, under time budget; the general trap is not specific to
this codebase's own fd layout.
