# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
