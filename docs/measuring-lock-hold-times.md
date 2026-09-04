## Measuring lock hold times

The NDC commit waits up to `REMEMBER_NDC_COMMIT_LOCK_TIMEOUT` (default 30s) for `save.lock`. [#226](https://github.com/Digital-Process-Tools/claude-remember/issues/226) filed 30 as reasoned but never measured; the **hold** has since been measured and the **default** is now defended by it rather than by intuition.

`save-session.sh` holds that lock for the *whole* save, including its own summarize `claude -p` call, so the hold is roughly `1.2s + summarizer latency` (non-model floor p50 1.22s, n=10). 30s therefore covers the common case comfortably. It cannot cover the tail by construction — the summarizer's own wall is 120s — but a save that far out is already failing at its own bound, and the NDC skip is a second-order symptom of that rather than the problem. Raising the default is not free either: `lock_acquire` busy-spins at roughly 21% of a core while it waits, on the `PostToolUse` path.

What #226 leaves open is structural, not a constant: taking the model call out from under `save.lock` at all. That is a design change to the save path, because the lock also serialises the summarizers themselves.

This is how to reproduce those numbers on a real machine, and how to answer the one question holds alone cannot — how often the wait actually runs out.

```bash
export REMEMBER_LOCK_TIMING=1        # in the shell Claude Code launches hooks from
# ...work normally for a day...
scripts/lock-timing-report.sh
```

```
lock-timing: ok  file=/Users/you/.remember/<slug>/logs/lock-timing.tsv  records=418

lock            prec     n  held_p50  held_p90  held_p99  held_max  wait_p50  wait_p90  wait_p99  wait_max timeouts
save.lock         us   197      4210      9840     21030     24118         0         1      2004     30001        1
staging.lock      us   210        31        44        88       201         0         0         1        12        0
```

- **`held_*`** is acquire-to-release. `save.lock`'s tail is what the 30s has to cover.
- **`timeouts`** counts waits that ran out. For `save.lock` each one is an NDC commit that skipped and duplicated a span into `today-*.md` — the outcome the bounded wait was chosen to avoid. A non-zero count here is the direct answer to #226.
- **Turning it on cannot change what it measures.** If the log cannot be written — read-only directory, read-only file, `REMEMBER_DIR` unset — the lock use completes normally and one line names the file that could not be written, in the pipeline log or on stderr, once per process. A hold that was not timed is **missing** from the distribution rather than present in it as a `0ms` row; those two give different `p50`s, and only one of them is honest.
- **`prec`** is the clock resolution the rows were taken at, and it is not the same everywhere: `us` on bash ≥ 5 (`EPOCHREALTIME`, no spawn), `ms` with GNU `date`, `s` on macOS's `/bin/bash` 3.2 with BSD `date`. Do not read sub-second structure out of an `s` file — reading a number at a finer resolution than it was taken at is the false confidence this issue was filed about. One second is coarse for `staging.lock` and adequate for `save.lock`.

The raw file is TSV, one row per lock use, so anything the report does not show is one `awk` away:

```
# ts_ms  lock  event  outcome  wait_ms  held_ms  precision  pid
```

The report says **`skipped`** (exit 2), with the reason, when there is no file or no records — an empty table on a file that was never written reads exactly like one taken on an idle machine, and those are the two answers worth telling apart.

