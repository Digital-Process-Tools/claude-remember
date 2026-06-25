# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
