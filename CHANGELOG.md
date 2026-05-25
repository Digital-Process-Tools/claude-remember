# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.1] — 2026-05-23

### Fixed

- **SessionStart hook libuv assertion on Windows** ([#39](https://github.com/Digital-Process-Tools/claude-remember/pull/39)) — backgrounded `save-session.sh` and `run-consolidation.sh` now fully detach via `</dev/null >/dev/null 2>&1 & disown`, preventing the `UV_HANDLE_CLOSING` assertion that surfaced as `SessionStart:startup hook error` on every fresh terminal. Community contribution by [@maxwellkemp10-ux](https://github.com/maxwellkemp10-ux).
- **Silent save failures on Windows + Git Bash** ([#44](https://github.com/Digital-Process-Tools/claude-remember/pull/44)) — Git Bash exposes `$CLAUDE_PROJECT_DIR` as a POSIX path (`/c/Users/...`), but Claude Code stores sessions under the Win32-form slug (`C--Users-...`). The post-tool hook silently exited because the slug never matched. `resolve-paths.sh` now normalizes the POSIX form to Win32 inside an `OSTYPE`-gated case (no-op on Linux/macOS). Community contribution by [@kanelavish-a11y](https://github.com/kanelavish-a11y).

### Tests

- 327 tests (up from 323).

## Older versions

Pre-0.7.1 release notes live inline in [`README.md`](README.md#changelog) and on the [GitHub Releases page](https://github.com/Digital-Process-Tools/claude-remember/releases). They will be migrated into this file incrementally.
