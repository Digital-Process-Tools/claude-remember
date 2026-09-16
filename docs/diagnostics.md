# Diagnostics (`/remember:doctor`)

Prints resolved paths, detected tools, storage mode, whether the session directory Claude Code actually created matches the slug the plugin computes, when the last successful save happened, whether `PostToolUse` has ever fired for this project, and whether `SessionEnd` -- the last-chance flush -- has ever fired ([#370](https://github.com/Digital-Process-Tools/claude-remember/issues/370)). Each line is prefixed `OK` / `WARN` / `FAIL`, ending in a one-line verdict.

The `SessionEnd` check only counts a quiet transcript as evidence once it is newer than `.remember/.install-marker`'s own mtime -- written exactly once, the first time any hook bootstraps the store, and never rewritten by ordinary hook activity -- since a transcript quiet since before that moment cannot be proof `SessionEnd` failed to fire: the hook was never registered for it. Installing into (or upgrading in) a project with pre-existing Claude Code history therefore reads correctly as "nothing has had the chance to prove or disprove this yet", not as a false `problem`. This baseline is unavailable, and the check can only `WARN`, never `FAIL`, when the marker itself is missing or unreadable -- which should now only happen in the brief window before a store's first hook invocation writes it; an earlier version of this baseline (`.remember/.gitignore`'s mtime) was deleted, by design, the first time a legacy-to-external migration was backed up with git, permanently degrading the check for that store ([#401](https://github.com/Digital-Process-Tools/claude-remember/issues/401)) -- `.install-marker` is written unconditionally of storage mode and nothing else in this codebase has any reason to touch it again.

The VERDICT line's own ranking of `SessionEnd`'s silence against a `PostToolUse` cause already named above it changed as well ([#404](https://github.com/Digital-Process-Tools/claude-remember/issues/404)): "PostToolUse is wired and running, but has not serviced a session -- it is exiting early" now outranks "SessionEnd has never fired" when both are true on an aged store, since the exiting-early diagnosis already explains SessionEnd's own silence and is the more specific, actionable cause. SessionEnd's own priority over a healthy-looking "capture is working" line, and over "PostToolUse has never fired at all", is unchanged.

Available on plugin installs, which auto-discover `commands/`. If you set the plugin up manually into `<project>/.claude/remember/`, that discovery does not apply — copy `commands/doctor.md` into `.claude/commands/`, or just run the script directly: `bash .claude/remember/scripts/doctor.sh`.

Reach for it whenever memory is not appearing and nothing says why — the two silent failures it names outright are a slug mismatch ([#144](https://github.com/Digital-Process-Tools/claude-remember/issues/144)) and hooks that were never registered ([#200](https://github.com/Digital-Process-Tools/claude-remember/issues/200)).

## Machine-readable output (`doctor.sh --json`)

`bash .claude/remember/scripts/doctor.sh --json` (or the plugin-install path, `${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh --json`) prints one line of JSON instead of the human report, for another program that needs the resolved store directory and storage mode without reimplementing `session_dir_slug` -- the algorithm is UTF-8-aware, hashes over 200 characters, and folds Windows drive letters, so a caller vendoring it takes on logic that goes stale on its own schedule ([#408](https://github.com/Digital-Process-Tools/claude-remember/issues/408)).

**Provisional, not a stable contract yet.** Every payload carries `schema_version` (currently `1`); an incompatible change to these keys or their meaning bumps it, and a caller should check it rather than assume the shape below is permanent.

Three states, not two -- `"resolved"` when `CLAUDE_PROJECT_DIR` was given; `"resolved_assumed_project_dir"` when it was not and the current directory was guessed instead (the same gap the human report's `CLAUDE_PROJECT_DIR was not set` line documents, [#207](https://github.com/Digital-Process-Tools/claude-remember/issues/207)); `"could_not_resolve"` when path resolution itself failed. Only the first two carry `remember_dir`, `storage_mode` (`"legacy"` or `"external"`) and `project_dir`; `could_not_resolve` carries `reason` instead and never a directory it could not vouch for -- an absent key or an empty object here would read as "nothing to report", indistinguishable from a caller that never asked, which is the exact gap this issue exists to close.

```
$ bash .claude/remember/scripts/doctor.sh --json
{"schema_version":1,"state":"resolved","remember_dir":"/Users/you/.remember/-Users-you-proj","storage_mode":"external","project_dir":"/Users/you/proj"}
```

It also reports the **store's spelling** ([#298](https://github.com/Digital-Process-Tools/claude-remember/issues/298)): whether the store directory the plugin resolved is spelled the same way on disk, and the same way in the git repository that backs it up. Git's index is case-sensitive where NTFS and the default macOS filesystem are not, so a store can be `C--Users-you-proj` on disk and `c--Users-you-proj` in git. **On a case-insensitive filesystem those are the same directory and nothing is wrong** — memory is being read and written normally. It matters on a restore: checked out onto a case-sensitive filesystem the two spellings become two directories, each holding part of the memory, and the plugin uses one of them. Four answers rather than two — they agree / a second spelling exists / **could not check** (no git, not a repository, nothing committed) / not applicable, for a store whose directory is not named by the slug — and "could not check" is never rendered as "they agree". Nothing is renamed, merged or migrated for you.

## A store over the consolidation cap

Consolidation refuses to build a prompt larger than `thresholds.consolidate_max_bytes` (default 600000), measured across staging + `recent.md` + `archive.md` together. A store past that number skips every round, and until [#348](https://github.com/Digital-Process-Tools/claude-remember/issues/348) it skipped **forever**: `recent.md` is part of the sum the cap is measured on, so a file that grew past it disabled the only mechanism that could shrink it. The reporter of [#346](https://github.com/Digital-Process-Tools/claude-remember/issues/346) reached 6.4 GB and the only recovery available was `mv recent.md recent.md.bak && touch recent.md`, which discards the history.

**It now rotates its way out, and nothing is deleted.** Whichever file is measurably the reason the round will not fit is renamed to a dated sibling — `archive-YYYY-MM-DD.md`, `recent-YYYY-MM-DD.md`, with a `-2` suffix if it happens twice in one day — a fresh empty one is started, and consolidation resumes on the next round. The rotated bytes stay on disk, stay greppable, and are named at session start so recall can still reach them.

**Which file moves is decided by arithmetic, not by guessing.** Dropping `archive.md` is tried first; `recent.md` is rotated only when dropping it is what brings the round under the cap. If the past-day staging files are over the cap *on their own*, nothing is rotated at all — no rotation available would change the next round, so moving `recent.md` would split an unconsolidated span for nothing. `/remember:doctor` distinguishes the two: the self-healing shape is a `WARN` that tells you to do nothing, and the shape that needs you is a `FAIL` that reaches the verdict line.

## Profiling a hook: where your trace goes

`bootstrap-dirs.sh` redirects fd 2 into `logs/hook-errors.log` so a hook's stderr never leaks into the host's transcript. Bash's own `xtrace` stream is on fd 2 too, so `bash -x scripts/session-start-hook.sh` used to produce a trace that covered only the part of the run before that line — silently, and from partway through ([#690](https://github.com/Digital-Process-Tools/claude-remember/issues/690)). Measured on macOS: `wall 0.71s`, traced span `0.07s`. A truncated trace does not look truncated; it reads as a complete profile of a fast hook, and every fork count and per-step attribution derived from it describes a fraction of the run.

Two ways to profile, and the first is the better one:

```bash
# Best: trace on its own fd, stderr still captured in the log
exec 9>/tmp/trace.txt
BASH_XTRACEFD=9 PS4='+$EPOCHREALTIME ' bash -x scripts/session-start-hook.sh < payload.json

# Also fine: trace on fd 2, which the hook now leaves alone
PS4='+$EPOCHREALTIME ' bash -x scripts/session-start-hook.sh < payload.json
```

In the second shape the hook prints one line saying it is **not** redirecting stderr, so the absence of `hook-errors.log` entries during a profiling run is stated rather than discovered. `REMEMBER_TRACE=1` asks for the same thing without `bash -x`, for a profiler that is not bash's own xtrace.

**`BASH_XTRACEFD` needs bash 4.1 or newer.** Stock macOS ships bash 3.2 as `/bin/bash`, which silently ignores the variable -- xtrace stays on fd 2 no matter what it names. The redirect knows this and stands down on that floor too, exactly as it does for a bare fd-2 trace, so a profile taken with the "best" shape above still survives on an old bash; it just lands on fd 2 like the plain case rather than in its own file.

Its "Recent errors" section tails **`<your memory store>/logs/hook-errors.log`**. That file is where a hook's own stderr goes: `bootstrap-dirs.sh` points every coding agent hook's stderr at it, and a hook that exits non-zero is reported there with its exit status and its own first lines ([#277](https://github.com/Digital-Process-Tools/claude-remember/issues/277)). It is the single most useful thing to attach to a bug report — most of what makes a plugin failure hard to diagnose from the outside is already written in it, and a report that includes it usually skips a whole round of questions.

