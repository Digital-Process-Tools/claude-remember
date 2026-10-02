# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-remember/blob/main/CHANGELOG.md).

## [0.37.0] - 2026-10-02 — a slim `release` branch for the Anthropic plugin directory, a config-cache invalidation fix for deleted layers, and README disclosures

### Added

- Added (#851): a slim `release` branch for the Anthropic plugin directory, which was holding every version since v0.29.1 because the full repository (474 files, 9.2 MB, a 611 KiB CHANGELOG.md) broke its file rules. A tag push now runs `.github/workflows/release-branch.yml`: it builds the tree from the tag without `tests/`, `docs/` and the maintainer tooling (73 files, 1.3 MB for v0.36.0), cuts CHANGELOG.md to the latest release, points the README's `docs/` links and logo at `main`, checks the result against the directory's pre-submission checklist, runs `claude plugin validate --strict` and every hook once in isolation, and only then pushes one commit to `release`. Nothing changes for installs from `main`. The release sequence and the one manual portal step are in `docs/releasing.md`.

### Changed

- Changed: README.md now discloses everything the plugin runs, sends and writes outside the memory store -- the `codex exec` summarizer path and `REMEMBER_SUMMARIZER`/`REMEMBER_SUMMARIZER_FALLBACK`; the opt-in `git fetch` in `hooks.d/before_session_start/50-git-restore.sh` (`git_restore.enabled`); `~/.remember/tmp/promo-notice`, `~/.remember/run/summarizers/`, and the `$TMPDIR/remember-*` temp files (some holding transcript text, all removed by an `EXIT` trap when the save finishes); and credential handling for `CLAUDE_CODE_OAUTH_TOKEN`, `REMEMBER_OAUTH_TOKEN`/`haiku.oauth_token`, `ANTHROPIC_API_KEY`/`haiku.anthropic_api_key`, and `CODEX_API_KEY`. Also fixed: the git backup section wrongly said the push "If you enable it" -- `hooks.d/after_save/50-git-backup.sh` has no enable flag; it runs whenever the external store's parent directory is itself a git repository with an upstream (#854).

### Fixed

- Fixed a config-cache staleness bug (#843, split out of #842, reported by @books-around-trees): deleting a config layer (`config.json` in the project, `~/.remember/`, or the plugin root) left its values in effect indefinitely. Both the flattened-config cache (`scripts/log.sh`) and the resolved-environment cache (`scripts/lib-env-cache.sh`) invalidated themselves by comparing mtimes with `-nt`, which reads a missing file as "unchanged" -- correct for a layer that never existed, but wrong for one that existed when the cache was published and was since deleted, since deleting a file changes no mtime a `-nt` check looks at. Both caches now also record which layers existed at publish time and treat any layer appearing or disappearing as a cache miss, regardless of mtime.

[0.37.0]: https://github.com/Digital-Process-Tools/claude-remember/releases/tag/v0.37.0
