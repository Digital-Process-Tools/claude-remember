# How memory files are written

The locking and atomic-rename rules every writer of a memory file follows. Moved verbatim from the README; read it before touching any script that writes to the store.

Writers of `now.md` take `save.lock`. **Readers do not, by design** — the `SessionStart` hook that injects memory into a new session sources only what it needs (`resolve-paths.sh`, `detect-tools.sh`, `bootstrap-dirs.sh`, `log.sh`, `lib-env-cache.sh`) and never `lib-lock.sh`, so it *cannot* lock even if it wanted to. That is deliberate: it runs before your first prompt, and `save.lock` is held for the whole of a save including its `claude -p` call ([#227](https://github.com/Digital-Process-Tools/claude-remember/issues/227), [#230](https://github.com/Digital-Process-Tools/claude-remember/issues/230), [#204](https://github.com/Digital-Process-Tools/claude-remember/issues/204)). A hook that blocks your prompt behind a model call is a worse outcome than anything it would be protecting you from.

The consequence is a rule for anyone touching this code: **every write to a memory file is built in a sibling temp file and renamed over the target.** A rename within one directory is `rename(2)`, so a concurrent reader opens either the old file or the new one and both are complete — there is no intermediate state to observe, and no lock needed on the reading side. Two things follow from "sibling":

- The temp must be **in the same directory as the target**, not in `$TMPDIR`. Across filesystems `mv` is copy-then-unlink, not a rename, and a failure partway destroys or truncates the destination ([#242](https://github.com/Digital-Process-Tools/claude-remember/issues/242)). `$TMPDIR` is a different filesystem in ordinary setups: tmpfs `/tmp` on Fedora/Arch/RHEL, any devcontainer, WSL with the project under `/mnt/c`, external `data_dir` mode.
- The `mv`'s **result must be checked**, and a failure must leave the file and the saved position alone so the next run retries ([#243](https://github.com/Digital-Process-Tools/claude-remember/issues/243)).

Appending is not an exception to this. `>>` is not atomic for a reader at any size — the entry arrives one `write(2)` chunk at a time — so an appended entry is staged as `old + separator + entry` in a sibling temp and committed by rename like everything else ([#247](https://github.com/Digital-Process-Tools/claude-remember/issues/247)).


## What a compaction refire does and does not do

No manual prompting, no "read this file" instructions. The agent begins every session with its memory already loaded. It just remembers.

**Except after a compaction.** `SessionStart` fires again with `source=compact`, and a compaction is not a new session: the store has not changed and the same bytes were already delivered, once, to the context the compaction has just replaced ([#339](https://github.com/Digital-Process-Tools/claude-remember/issues/339)). There the hook still injects `identity.md` — a path to it does not make the agent behave as that persona — and names the rest with their sizes instead of injecting them, so they stay greppable. `startup`, `resume`, `clear` and `fork` are unchanged, and so is any payload whose `source` this hook does not recognise.

The same `source=compact` check keeps two other things from firing a second time for the same session: the handoff delivery counter (below) does not increment on a compaction refire, and a pending day of staging does not re-spawn background consolidation — both are provably the same session continuing, not a new one starting ([#341](https://github.com/Digital-Process-Tools/claude-remember/issues/341), [#342](https://github.com/Digital-Process-Tools/claude-remember/issues/342)).
