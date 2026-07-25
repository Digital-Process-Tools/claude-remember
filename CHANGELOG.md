# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A non-ASCII character anywhere in the project path disabled memory entirely, in silence** ([#144](https://github.com/Digital-Process-Tools/claude-remember/issues/144)) — `session_dir_slug()` has to reproduce the name Claude Code gave `~/.claude/projects/<slug>/`, and Claude Code slugs with a JS regex that replaces one *character* per dash. `sed`'s idea of a character follows the locale, and on Git Bash/MSYS it stayed byte-wise even under `LC_CTYPE=C.UTF-8`: a CJK path got three dashes per character, the slug pointed at a directory that does not exist, the hook found no transcript and exited 0 — every tool call, for the life of the session. No `now.md`, no error, nothing in `hook-errors.log`. Pinning a locale does not fix it either, in both directions: `C.utf8` is absent on macOS and a missing locale falls back to byte-wise C, while `en_US.UTF-8` exists there but makes `[a-z]` follow collation, so `café` keeps its `é` and misses the directory just as thoroughly. The slug now forces byte semantics deliberately and collapses each UTF-8 sequence by hand. Note the shipped regex carries no `/u` flag, so it counts UTF-16 *code units*, not characters: a 2- or 3-byte sequence is one BMP character and costs one dash, but a 4-byte one is a surrogate pair and costs two — which is what an emoji or one of the Extension-B kanji found in Japanese name registries actually produces. The lead-byte ranges follow the UTF-8 well-formedness table rather than one blanket class, because a surrogate or overlong form is not a character and the decoder emits a replacement per byte for it; each lead takes exactly its own continuations, or a valid sequence swallows the stray bytes after it; and an embedded newline — legal in a POSIX filename, and `sed`'s own record separator, so it reaches no rule at all — is converted before the pipeline rather than surviving into the slug. Differential-fuzzed against that regex under node: identical for all well-formed UTF-8 and for surrogate, overlong and out-of-range forms, under every hostile locale. A truncated sequence still differs, which needs the decoder's maximal-subpart state machine and cannot come from a path a filesystem hands us; it is documented in place, and the new warning below covers it if it ever does. The sed program is assembled once when the script is sourced rather than per call, since the post-tool hook slugs on every single tool call and building it inline forked a subshell per byte constant: 2.4 ms/call before this change, 1.8 ms after. And the silent half is fixed too: a slug that matches no directory now says so in the log, hourly while it lasts, instead of exiting 0 with nothing to show for it — once-ever would have gone quiet again from the second session on, and the cause here is environmental, so it persists. Reported with a complete root-cause trace by [@shenkang11](https://github.com/shenkang11).

- **`build-prompt` failed with `FileNotFoundError` on a file that was right there on disk** ([#145](https://github.com/Digital-Process-Tools/claude-remember/issues/145)) — the same boundary class as [#91](https://github.com/Digital-Process-Tools/claude-remember/issues/91)/[#104](https://github.com/Digital-Process-Tools/claude-remember/pull/104), on the side that audit did not cover. Every `cmd_*` in `pipeline/shell.py` prints `KEY=value` lines that bash captures by command substitution and hands to the *next* Python call as argv. Those `print()` calls encoded with the console's ANSI codepage, not UTF-8, so under a non-ASCII Windows profile the temp path in `EXTRACT_FILE` came back mojibake and the following step could not open it. `stdout` and `stderr` are now reconfigured to UTF-8 in `main()`, the one place all command output funnels through, mirroring the `stdin` reconfigure already there. Diagnosed down to the cp932 byte level by [@shenkang11](https://github.com/shenkang11).

- **The 0.8.6 fence fix could unclose a code block, and still orphaned a fence in `archive.md`** — post-release review of [#126](https://github.com/Digital-Process-Tools/claude-remember/issues/126) found the fix incomplete in both directions. It stripped a leading fence and then *any* trailing fence, so a summary whose last line closed a ```` ```bash ```` sample lost that terminator and the block never ended; a response that was simply a code block had its fences deleted outright. And when the model wrapped the **whole** response, the closing fence landed inside the archive section — which has no opening fence of its own, so the per-section strip could not see it and the orphan ``` still reached `archive.md`, the exact artifact originally reported. A fence is now only treated as a wrapper when the structure says so. The closer must use the same fence character with a run at least as long, and it must land on the last line at nesting depth zero — the body in between is walked tracking that depth, because a bare ``` opening an inner block is textually identical to a closer and anything less mistakes one for the other. When no closer arrives, what is left OPEN decides: a dangling fence that could have closed the wrapper (bare, same character, long enough) means the leading fence enclosed part of the content, while a dangling ```` ```bash ```` or ``~~~`` could never have closed it and so cannot keep the wrapper alive. The info string is evidence rather than a gate — ```` ```markdown ```` is taken at its word, any other tag has to earn it by closing cleanly around a body that reads as a section, which is also what separates a truncated wrap from a pasted log when the two are grammatically identical. A whole-response wrapper is stripped before the sections are split.

## [0.8.6] — Memory actually saves: agentic sessions, auth, channels, and an NDC data race

### Fixed

- **Channel-delivered messages were dropped, so those sessions never saved** ([#128](https://github.com/Digital-Process-Tools/claude-remember/issues/128)) — input arriving through a channel integration (e.g. the Telegram plugin) is wrapped by the transport and carries `isMeta`, the same flag Claude Code puts on genuine meta records. `extract_messages()` filtered on it before counting, so every real human turn in such a session vanished: the human count stayed at 0, the min-human gate never cleared, and memory was never written for the entire class of channel-driven users. The wrapped text is now recovered and counted as the human turn it is. Reported by [@ondomru](https://github.com/ondomru).

- **A wrapping code fence produced a doubled `# Recent` header** ([#126](https://github.com/Digital-Process-Tools/claude-remember/issues/126)) — when Haiku wrapped its consolidation output in a ``` fence, `parse_consolidation_response()` stripped whitespace but not the fence line, so the `startswith("# Recent")` check missed and a second header was prepended. `recent.md` then carried two `# Recent` headers plus a stray fence, which the SessionStart banner and `/resume` both parse. The wrapping fence is now stripped before the header check. Reported by [@merpuya](https://github.com/merpuya).

- **NDC compression erased entries written while it was running** ([#142](https://github.com/Digital-Process-Tools/claude-remember/issues/142)) — the compression step snapshots `now.md`, hands it to a Haiku call allowed up to 180s, then truncated the file with `: >`. By the time that landed, the parent had released the save lock and exited, so a newer save could legitimately have appended an entry — and the truncate erased it. Worse, that entry's position had already been advanced, so the content was unrecoverable and nothing was logged: the memory simply wasn't there. Compression now drops exactly the bytes it snapshotted and keeps everything appended after that offset, logging the number of bytes preserved. Reported by [@bonyohana](https://github.com/bonyohana).

- **Nothing ever saved for `setup-token` users, and the reason was invisible** ([#131](https://github.com/Digital-Process-Tools/claude-remember/issues/131), [#129](https://github.com/Digital-Process-Tools/claude-remember/issues/129)) — two bugs 70 lines apart in `pipeline/haiku.py`, one hiding the other. `_child_env()` strips the `CLAUDE_CODE_*` family as parent-session identity ([#95](https://github.com/Digital-Process-Tools/claude-remember/issues/95)), but that prefix is a proxy, not a definition: `CLAUDE_CODE_OAUTH_TOKEN` is the *child's credentials*, so anyone who authenticated with `claude setup-token` (or runs under a hosted Agent SDK) had every nested summarization call go out unauthenticated and fail. It is now kept while the rest of the family is still stripped. And the failure was undiagnosable because `--output-format json` makes the CLI report errors as JSON on **stdout** with stderr empty, while the error path read only stderr — so the log said `claude exited 1:` and stopped. The real message is now recovered from stdout (structured field first, raw text as fallback, capped at 500 chars), and "no output on stdout or stderr" is stated outright instead of trailing off. Reported by [@socialmenteagency](https://github.com/socialmenteagency) and [@bigtopmultimedia](https://github.com/bigtopmultimedia) — who could only identify the first bug after patching the second.

- **Agentic sessions never saved anything, silently** ([#147](https://github.com/Digital-Process-Tools/claude-remember/issues/147), split out of [#125](https://github.com/Digital-Process-Tools/claude-remember/issues/125)) — two early exits in `save-session.sh` sat *upstream* of `save-position`, while the cooldown marker was written *before* them. So neither exit advanced the read cursor, and the next run re-extracted the identical span and exited identically — once per cooldown window, for the life of the session. Two distinct failures came out of that one asymmetry. A session with **0 new exchanges** now advances the position (there is nothing to summarize, so nothing is lost, and the loop breaks). A session with **many exchanges but few human turns** — the shape of every agentic session — no longer falls through the `min_human_messages` gate forever: a span of at least `thresholds.min_exchanges_without_human` (default 30) is treated as substantive and saved, because work is not only measured in human turns. The under-threshold skip deliberately still does *not* advance the cursor: those exchanges are real content and get summarized together with the turns that follow. Reported by [@KyleUnlock](https://github.com/KyleUnlock), with the month-long macOS trace and the root-cause generalization from [@VictorVvdl](https://github.com/VictorVvdl).

- **A failing summarizer could wedge memory permanently** ([#147](https://github.com/Digital-Process-Tools/claude-remember/issues/147)) — the third face of the same asymmetry, and the one confirmed in the wild for a month: when the Haiku call itself failed, the position was never written, so the next run re-extracted the identical span and failed identically, forever — no later span could ever be saved. Keeping the position is still right for a *transient* failure (rate limit, network blip): the span is simply retried. But consecutive failures against the same span are now counted, and past `thresholds.max_summary_failures` (default 3, `0` retries forever) that span is dropped — loudly, with a WARNING naming the threshold — so everything after it can still be recorded. Losing one span is bad; losing every future span is worse. A successful save or SKIP resets the count.

- **A nested `claude -p` session crashed on every hook** ([#137](https://github.com/Digital-Process-Tools/claude-remember/pull/137)) — `scripts/resolve-paths.sh` is always *sourced*, but signalled failure with a bare `exit 1`, which terminates the **caller's** whole process. The three Claude Code hooks are documented "EXIT CODES: 0 Always", and the one caller most likely to fail resolution is the plugin's own nested Haiku session (it runs with `cwd` in a temp dir and no `CLAUDE_PROJECT_DIR`, so it is not a project at all) — so the plugin crashed the very session it spawned. Resolution failure now stays **loud by default** (`exit 1`, unchanged for every worker script and for any caller that forgets to check), while a caller that must never take its host process down opts in with `REMEMBER_PATHS_SOFT_FAIL=1` and gets `return 1` to handle itself. Only the three hooks opt in, and they still report FATAL on stderr, which `hooks.json` redirects into `hook-errors.log` — a failed hook is silent to the session, never silent to the logs. Diagnosed and fixed by [@lucasrodriggs-tech](https://github.com/lucasrodriggs-tech).

## [0.8.5] — Ship the 0.8.4-era fixes: manifest bump, worktree safety, fork storm

### Fixed

- **Marketplace installs never received any fix shipped after 0.8.3** ([#133](https://github.com/Digital-Process-Tools/claude-remember/issues/133)) — the 0.8.4 release went out without bumping `.claude-plugin/plugin.json`, which still declared `0.8.3`. Claude Code's updater compares *manifest versions*, not source SHAs, so both background auto-update and an explicit `claude plugin update` reported `already at the latest version (0.8.3)` and did nothing — leaving every marketplace user on pre-0.8.4 code with no signal they were stale, including on [#123](https://github.com/Digital-Process-Tools/claude-remember/issues/123), whose failure mode is silent and self-reinforcing. The manifest is bumped to `0.8.5` and `tests/test_version_manifest.py` now fails the build whenever it drifts from the newest released `CHANGELOG.md` heading, so a release can no longer be cut without it. Reported with a full live diagnosis by [@jqit-ricky](https://github.com/jqit-ricky).

- **Worktree sessions pushed private memory into the project repo** ([#138](https://github.com/Digital-Process-Tools/claude-remember/issues/138)) — after [#127](https://github.com/Digital-Process-Tools/claude-remember/issues/127) keyed memory to the main checkout, a session running in a linked worktree left `PROJECT_DIR` on the worktree while `REMEMBER_DIR` pointed into the main checkout. The git-backup hook's legacy guard compares those two paths, so it no longer matched: the hook mistook the *project repo* for a dedicated backup repo, deleted the protective `.remember/.gitignore` (`*`), committed the whole memory tree onto whatever branch the main checkout had out, and pushed it to the project's origin — session notes landing on a shared remote, on an unrelated feature branch, needing a history rewrite to clean up. The hook now compares git *common dirs* instead of paths, which covers the plain, worktree and subdirectory layouts at once while still activating for a repo genuinely dedicated to memory backup. Reported with an exact root-cause trace by [@jaco2716](https://github.com/jaco2716).

- **The save hook forked a doomed `save-session.sh` on every tool call** ([#125](https://github.com/Digital-Process-Tools/claude-remember/issues/125)) — `post-tool-hook.sh` throttled its background fork on save *position* (`last-save.json`), which is only written after a save succeeds. Until the first save lands, `LAST_LINE` is `0`, so the delta is the entire transcript and always clears the threshold — and in an agentic session (many tool calls, few human turns) the min-human gate keeps that first save from ever landing. The hook therefore forked once per tool call for the life of the session, each fork dying milliseconds later on `save-session.sh`'s own cooldown: pure waste (orphaned process pairs, one empty log per tool call, pid churn). The hook now consults the same `last-save-ts` cooldown marker before forking, bounding saves to one fork per cooldown window. **This fixes the fork storm only** — #125 stays open for the deeper cause it also identifies: `save-session.sh` writes `last-save-ts` *before* the min-human gate but never advances the saved *position* past an early exit or failure, so a low-human-turn agentic session still re-extracts and bails once per cooldown window, and memory still never lands. Reported by [@KyleUnlock](https://github.com/KyleUnlock), with a corroborating month-long macOS repro from [@VictorVvdl](https://github.com/VictorVvdl).

- **The `UserPromptSubmit` hook was never registered for plugin installs** — `scripts/user-prompt-hook.sh` ships with the plugin and the README documents it ("The plugin registers three Claude Code hooks"), but `hooks/hooks.json` only wired `SessionStart` and `PostToolUse`. The per-prompt timestamp injection (so the agent knows the current time) and the `after_user_prompt` dispatch to `hooks.d/` listeners were dead code for everyone installing via the plugin marketplace; only manual `.claude/settings.json` installs (which the README snippet wires correctly) got them. The hook is now registered in the manifest, and a new lint test fails if any shipped `*-hook.sh` is left unwired.

- **Worktree sessions built a throwaway memory that vanished on cleanup** ([#56](https://github.com/Digital-Process-Tools/claude-remember/issues/56)) — the plugin derives `REMEMBER_DIR` from `CLAUDE_PROJECT_DIR`, which Claude Code sets to the *worktree* path for worktree sessions. In the default (legacy) layout that put memory at `<worktree>/.remember` — physically inside the worktree, and gitignored with `*`, so a plain `git worktree remove` (no `--force`, no warning) deleted every `now.md` / `today-*.md` / `remember.md` built up during the session, and none of it was ever migrated to the main checkout. In external mode the `{slug}` was computed from the worktree path, producing an orphaned `~/.remember/<slug-of-worktree>` subtree the main checkout's sessions never loaded. `REMEMBER_DIR` resolution now routes through git's *common dir*: when `PROJECT_DIR` is a linked worktree, memory is keyed to the main checkout, so it survives `worktree remove` and is shared across all worktrees of the repo. `PROJECT_DIR` itself is left untouched (session recovery still resolves transcripts under the worktree slug), and non-worktree / non-git projects behave exactly as before. Reported and diagnosed by [@KrzysztofKasprowicz](https://github.com/Digital-Process-Tools/claude-remember/issues/56) and [@dewet22](https://github.com/Digital-Process-Tools/claude-remember/issues/56).

## [0.8.4] — Bound consolidation prompt size so a huge archive can't stall saves

### Fixed

- **An oversized consolidation prompt could halt daily rotation** ([#122](https://github.com/Digital-Process-Tools/claude-remember/issues/122)) — [#96](https://github.com/Digital-Process-Tools/claude-remember/issues/96) (0.8.2) capped the *save* path, but the *consolidation* path still inlined the full staging set + `recent.md` + `archive.md` into one Haiku call with no size check. A large input overflowed the model window (`Prompt is too long`), so `run-consolidation.sh` logged `ERROR` and exited 1 — and it was self-reinforcing, since staging was never retired and re-fed identically on the next run (the #96 failure mode, one path over). The assembled prompt is now capped at `thresholds.consolidate_max_bytes` (default 600 KB, `0` disables). Unlike the save path it **skips** rather than truncates, because consolidation rewrites `recent.md`/`archive.md` and a truncated input would permanently drop archived memory.

### Added

- **Archive rotation keeps consolidation progressing** ([#122](https://github.com/Digital-Process-Tools/claude-remember/issues/122)) — when `archive.md` is the oversized bulk, `cmd_consolidate` rotates it to a dated sibling (`archive-YYYY-MM-DD.md`, cold storage — no memory lost) and retries once with a fresh archive. If there is nothing to rotate, the retry still overflows, or the retry's Haiku call errors, the rotation is undone and the original state is left intact. Follow-up [#124](https://github.com/Digital-Process-Tools/claude-remember/issues/124) tracks teaching recall to read the rotated siblings. Thanks to [@presempathy-awb](https://github.com/presempathy-awb) for the fix and thorough tests.

## [0.8.3] — Windows: resolve the claude.cmd shim before spawning

### Fixed

- **Every auto-save silently failed on Windows** ([#120](https://github.com/Digital-Process-Tools/claude-remember/issues/120)) — `pipeline/haiku.py` spawned the CLI in list-form as `subprocess.run(["claude", ...])`. The npm global install ships the CLI only as a `claude.cmd` shim (no `claude.exe`), and Python's `subprocess` goes through `CreateProcess`, which resolves only `.exe` from a bare name — so every spawn raised `FileNotFoundError: [WinError 2]`. The pipeline aborted right after `[haiku] calling`, so `now.md` / `today-*.md` / `recent.md` were never generated (the SessionStart hook and `/remember` skill kept working since they don't spawn `claude`). The binary is now resolved with `shutil.which("claude")`, which honours `PATHEXT` and returns the full `claude.cmd` path that `subprocess` launches fine — no `shell=True`, no argv-length regression, and cross-platform safe (returns the plain path on Linux/macOS). Override via `REMEMBER_CLAUDE_BIN`. Reported with a precise diagnosis and tested patch by the issue author.

## [0.8.2] — Oversized-extract guard keeps long sessions saving

### Fixed

- **A very long session could silently halt all memory saves** ([#96](https://github.com/Digital-Process-Tools/claude-remember/issues/96)) — a single long-lived session can grow an extract larger than Haiku's context window. `build-prompt` embedded the full extract with no size cap, so the Haiku call failed, the save aborted, and daily rotation stopped. Worse, it was self-reinforcing: a failed save never advanced the saved position, so the same session re-extracted the full transcript and failed identically on every subsequent save. The extract is now capped at `thresholds.extract_max_bytes` (default 300 KB), keeping the most-recent tail with a truncation note so the summary still reflects current work. Set to `0` to disable. Thanks to [@selvi5006-commits](https://github.com/selvi5006-commits) for the precise diagnosis and a tested patch.

## [0.8.1] — Handoff survives context-preview truncation

### Fixed

- **Last-session handoff was lost on every session start** — the session-start hook emits a large block (identity + tiered memory + handoff), but the harness may deliver only a leading preview to the agent. The handoff was dumped inside the memory loop, landing well past the preview cutoff, so it never reached the model. The previous session's handoff is now emitted **first**, before identity/memory, under a `=== LAST HANDOFF ===` header, so it always lands in context. Read-once-then-clear semantics are preserved (the file is truncated immediately after emission).

## [0.8.0] — CC 2.x save fix, Windows reliability, unified Haiku call

### Added

- **`REMEMBER_BRANCH` env var override** — `scripts/save-session.sh` now honors `$REMEMBER_BRANCH` when computing the `## HH:MM | <branch>` identity slot of each daily-log entry. Falls back to the existing `git branch --show-current` lookup, then the literal `"unknown"` if no git repo is present. Use case: running Claude Code from `$HOME` (or any non-git directory) collapses the identity slot to `unknown` on every entry, which makes log entries indistinguishable across instances. Export `REMEMBER_BRANCH=laptop` / `cloud` / `staging` / `$HOSTNAME` in your shell rc and the slot becomes a useful per-instance tag. Documented in `README.md` Configuration → Environment variables.

### Fixed

- **`--max-turns 1` broke the save on Claude Code 2.1.x** ([#98](https://github.com/Digital-Process-Tools/claude-remember/issues/98), [#100](https://github.com/Digital-Process-Tools/claude-remember/issues/100)) — CC 2.x counts prompt-delivery as turn 1, so the nested `claude -p` summarizer exited `error_max_turns` before the model replied; `save-session.sh` treated the non-zero exit as fatal and never wrote memory (and re-fired on nearly every tool call). `--max-turns` is now configurable via `REMEMBER_MAX_TURNS` (default 4, validated to `[1, 20]`); a user Stop hook eats an extra turn, hence the margin. Reported by [@davidomisi](https://github.com/davidomisi) and [@NORSAIN-AI](https://github.com/NORSAIN-AI).
- **Single `claude -p` call site** ([#94](https://github.com/Digital-Process-Tools/claude-remember/issues/94)) — the summarizer invocation lived in two drifted places (`save-session.sh` inlined it twice; `pipeline/haiku.py` had `call_haiku`). Unified on `pipeline/haiku.py` via a new `pipeline.shell call-haiku` subcommand; `save-session.sh` delegates both calls. Closes the drift where `haiku.py` was missing `--mcp-config` / `--strict-mcp-config`.
- **Summarizer subprocess flooded `~/.claude/projects/`** ([#87](https://github.com/Digital-Process-Tools/claude-remember/issues/87)) — the nested `claude -p` now runs with `--no-session-persistence` and `--exclude-dynamic-system-prompt-sections`, so it no longer writes a resumable session record per call (hundreds/day on busy sessions). Community contribution by [@sergeclaesen](https://github.com/sergeclaesen).
- **Consolidation wrote conversational replies as memory** ([#89](https://github.com/Digital-Process-Tools/claude-remember/issues/89)) — a SKIP or non-conforming Haiku response is now rejected (`ConsolidationSkipped`) instead of being written to `recent.md`/`archive.md` and irreversibly retiring the staging files. Community contribution by [@Buzzwoo-Ecom-Team](https://github.com/Buzzwoo-Ecom-Team).
- **Empty timezone resolved to UTC instead of system-local** ([#99](https://github.com/Digital-Process-Tools/claude-remember/pull/99)) — date calls now route through the `_remember_date` helper, so an unset `REMEMBER_TZ` falls back to system-local rather than a bare `TZ=""` (UTC) for users west of UTC. Community contribution by [@kristian-presso](https://github.com/kristian-presso).
- **Windows: mojibake and lone-surrogate save crash** ([#91](https://github.com/Digital-Process-Tools/claude-remember/issues/91), [#97](https://github.com/Digital-Process-Tools/claude-remember/issues/97)) — the stdin pipe and the `claude` subprocess decoded with the locale codec (cp1252) instead of UTF-8, corrupting `→`/`—` into mojibake and crashing every autosave on lone surrogates. Audited **every** byte↔str boundary: explicit `encoding="utf-8"` on the stdin pipe and subprocess; `errors="replace"` on text writes and user-editable memory-file/transcript reads (never crash a save on a hand-edited byte); `surrogatepass` on the staging-paths filename encode; machine-written JSON (`last-save.json`) kept strict. Reported by [@marketechniks](https://github.com/marketechniks) and [@DogmaLabsTech](https://github.com/DogmaLabsTech).

- **Windows external-mode `data_dir` path doubling** ([#79](https://github.com/Digital-Process-Tools/claude-remember/issues/79)) — `lib-memory-dir.sh` only recognized `/…` and `~…` as absolute when resolving `REMEMBER_DIR` from a `data_dir`, so a Windows drive path (`C:/Users/…/mem/{slug}`) fell through to the relative branch and was prepended to `PROJECT_DIR` — `REMEMBER_DIR` became `…/proj/C:/…` and `{slug}` was never substituted (substitution lives only in the absolute branch). Drive-letter forms (`C:/…` and `C:\…`) are now recognized as absolute. Surfaced by re-enabling the Windows shell tests (#79).

### Security

- **Nested `claude -p` leaked the parent Claude Code session env** ([#95](https://github.com/Digital-Process-Tools/claude-remember/issues/95)) — the subprocess stripped only `CLAUDECODE`, so `CLAUDE_JOB_DIR` and the `CLAUDE_CODE_*` family (e.g. `CLAUDE_CODE_SESSION_ID`) were inherited, making the child look like the parent's resumable session to anything keying off those vars. `_child_env()` now strips `CLAUDECODE`, `CLAUDE_JOB_DIR`, and all `CLAUDE_CODE_*`. Reported by [@FrankLedo](https://github.com/FrankLedo).

### Tests

- New `tests/test_save_session_branch_override.py` — pins the four-case truth table for the `BRANCH=` line in `save-session.sh`: env-set + git-repo (env wins), env-unset + git-repo (git wins), env-unset + no-git (`unknown` fallback), env-set-to-empty + no-git (`:-` treats empty as unset, falls back to `unknown`). Snapshots the line out of the live `save-session.sh` rather than re-asserting a copy, so the test fails loudly if the line is ever edited without updating the test.
- New `tests/test_encoding_boundaries.py` — exercises the real byte↔str boundaries under a forced non-UTF-8 locale (`PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 LC_ALL=C`) so the mojibake/surrogate bugs reproduce on the Linux/macOS CI legs too — the boundary-blindness (every test mocked `StringIO` stdin / `MagicMock` subprocess) is why the green Windows matrix never caught them.
- **Re-enabled Windows shell-subprocess coverage** ([#79](https://github.com/Digital-Process-Tools/claude-remember/issues/79)) — `test_log_sh`, `test_migration`, and `test_security_fixes` were `skipif(win32)`. Three layers: (1) tests invoke bash by its explicit Git-for-Windows path — `subprocess.run(["bash", …])` on Windows hits `System32\bash.exe` (the WSL launcher) first because `CreateProcess` searches System32 before PATH, so no PATH trick works; (2) Windows paths injected into bash scripts are normalized to forward-slash drive form (`C:\x` → `C:/x`) and quoted — forward-slash works for both Git Bash and the Windows `python3` the scripts invoke, where the MSYS `/c/x` form does not; (3) the real bug those tests caught (see Fixed → `lib-memory-dir.sh`). `TestDispatchOwnershipChecks` stays skipped on Windows (POSIX ownership/world-writable bits don't map to NTFS).

## [0.7.3] — Windows save pipeline shell↔Python bridge

### Fixed

- **Save pipeline broken on Windows / Git Bash** ([#84](https://github.com/Digital-Process-Tools/claude-remember/issues/84)) — the shell↔Python bridge had two mismatched halves: `pipeline.shell._shell_escape` single-quote-wrapped values per POSIX `eval` convention, but `safe_eval` in `scripts/log.sh` assigned verbatim via `printf -v` (no shell expansion). On Linux, temp paths contain no shell-unsafe chars so the escaper returned them unquoted — invisible. On Windows, backslash paths got quoted, then stored with literal quotes, then `open()` failed with `OSError: [Errno 22]`. Plus `safe_eval` did not strip CR, so Python's `\r\n` line endings on Windows corrupted integer values and broke `[ -eq ]` tests in `save-session.sh`. Fix: `_shell_escape` now emits verbatim (raises on newline); `safe_eval` strips trailing `\r`; redundant override in `detect-tools.sh` removed (`log.sh` is single source of truth). Issue reported by [@qzftsh7f44-design](https://github.com/qzftsh7f44-design).

### Tests

- New `tests/test_safe_eval_seam.py` pins the Python↔bash roundtrip contract — parametrized across Linux paths, Windows backslash paths, spaces, single quotes. Closes the seam gap CI was blind to (both sides were unit-tested in isolation, never together).
- 391 tests, 99% coverage.

## [0.7.1] — Windows portability fixes

### Fixed

- **SessionStart hook libuv assertion on Windows** ([#39](https://github.com/Digital-Process-Tools/claude-remember/pull/39)) — backgrounded `save-session.sh` and `run-consolidation.sh` now fully detach via `</dev/null >/dev/null 2>&1 & disown`, preventing the `UV_HANDLE_CLOSING` assertion that surfaced as `SessionStart:startup hook error` on every fresh terminal. Community contribution by [@maxwellkemp10-ux](https://github.com/maxwellkemp10-ux).
- **Silent save failures on Windows + Git Bash** ([#44](https://github.com/Digital-Process-Tools/claude-remember/pull/44)) — Git Bash exposes `$CLAUDE_PROJECT_DIR` as a POSIX path (`/c/Users/...`), but Claude Code stores sessions under the Win32-form slug (`C--Users-...`). The post-tool hook silently exited because the slug never matched. `resolve-paths.sh` now normalizes the POSIX form to Win32 inside an `OSTYPE`-gated case (no-op on Linux/macOS). Community contribution by [@kanelavish-a11y](https://github.com/kanelavish-a11y).

### Tests

- 327 tests (up from 323).

## [0.7.0] — Unified config reader, marketplace path fix

### Fixed

- **Unified config reader across all scripts** ([#38](https://github.com/Digital-Process-Tools/claude-remember/pull/38)) — all scripts now use `config()` from `log.sh` instead of separate readers; `PIPELINE_DIR` set with fallback for both marketplace and local installs. Issue reported by [@josemoreno801-netizen](https://github.com/josemoreno801-netizen).
- **`user-prompt-hook.sh` sources `resolve-paths.sh`** — was the root cause of marketplace config path failures.
- **Removed redundant `REMEMBER_TZ` re-reads** — timezone is now set once in `log.sh`, inherited by all scripts.
- **Removed duplicate `cfg()` from `session-start-hook.sh`** — uses shared `config()` instead.

### Tests

- 323 tests (up from 256), 99% coverage.

## [0.6.0] — Timezone fix, cross-platform, community contribution

### Fixed

- **Log filename date used UTC instead of configured timezone** ([#26](https://github.com/Digital-Process-Tools/claude-remember/pull/26)) — `MEMORY_LOG_DATE` was computed before `REMEMBER_TZ` was defined; `TZ=""` silently falls back to UTC on macOS/BSD. Community contribution by [@josemoreno801-netizen](https://github.com/josemoreno801-netizen).
- **Marketplace path resolution in `log.sh`** — `PIPELINE_DIR` now used for `config.json` and `hooks.d` paths.
- **BSD `mktemp` compatibility** — no file extensions after `XXXXXX` template.
- **Windows / Git Bash portability** — centralized `SYS_TMPDIR`, `py` launcher fallback, session-dir slug matching.
- **Haiku header guard** — prevents invented `unknown` headers in summarization output.

### Added

- **`pipeline/_tz.py`** — shared timezone-aware date helpers for Python, reading `REMEMBER_TZ` with fallback to system local (never UTC).
- **`time_format` config option** — `24h` (default) or `12h` for AM/PM timestamps in log files.

### Tests

- 256 tests (up from 224), 99% coverage, `_tz.py` at 100%.

## [0.5.0] — Bug fixes, Python 3.9 support, DPT marketplace

### Added

- **DPT marketplace** — install from our own marketplace for reliable updates (`/plugin marketplace add Digital-Process-Tools/claude-marketplace`).
- **Python 3.9 support** — `from __future__ import annotations` in all pipeline modules (macOS ships 3.9 via CommandLineTools).

### Fixed

- **NDC subshell killed by `set -e`** ([#14](https://github.com/Digital-Process-Tools/claude-remember/issues/14)) — background compression no longer dies silently when `claude -p` returns non-zero.
- **`.gitignore` created too late** ([#17](https://github.com/Digital-Process-Tools/claude-remember/issues/17)) — now created in `session-start-hook.sh` before any save triggers.

### Tests

- 186 tests (up from 162), 99% coverage.

## [0.4.0] — Version tagging & marketplace update docs

### Added

- First release with proper git tags.

### Documentation

- Documented known marketplace update bugs with workarounds ([anthropics/claude-code#37252](https://github.com/anthropics/claude-code/issues/37252), [anthropics/claude-code#38271](https://github.com/anthropics/claude-code/issues/38271)).

## [0.3.0] — Path resolution overhaul

Fixes [#9](https://github.com/Digital-Process-Tools/claude-remember/issues/9), addresses [#10](https://github.com/Digital-Process-Tools/claude-remember/issues/10).

### Added

- **`resolve-paths.sh`** — single source of truth for all path resolution across local and marketplace installs.
- All hooks log their resolved paths to `.remember/logs/` on every invocation.
- Hook stderr captured to `.remember/logs/hook-errors.log` via `hooks.json` redirect.

### Changed

- Marketplace installs without `CLAUDE_PROJECT_DIR` now **fail with a clear FATAL error** instead of silently computing wrong paths.

### Tests

- 162 tests (up from 122), including realistic plugin simulation tests for both install layouts.

## [0.2.0] — Windows compatibility, CLI v2.1.86+ support

### Fixed

- Path slugging for Windows backslashes and colons.
- UTF-8 encoding added to all Python file operations.
- Handle CLI v2+ JSON array response format in `haiku.py`.

## [0.1.0] — Initial release
