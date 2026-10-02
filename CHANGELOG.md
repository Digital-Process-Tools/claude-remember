# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-remember/blob/main/CHANGELOG.md).

## [0.38.0] - 2026-10-02 — a SessionStart total byte budget with a handoff redelivery cap, and /remember:doctor's shell grant scoped to its own script

### Added

- Added `thresholds.session_start_max_bytes` (default 9000) and `thresholds.handoff_max_redeliveries` (default 3): SessionStart's total body (handoff + the `=== REMEMBER ===` legend + the `=== MEMORY ===` section) can sum past Claude Code's own ~10,000-character preview/persist threshold even when every individual memory file is healthy, at which point the model never sees the `=== MEMORY ===` section at all. The budget fills in priority order handoff -> now.md -> recent.md -> today-*.md -> archive.md and lists whatever does not fit by name and size, never dropping it silently. A handoff delivered unchanged `handoff_max_redeliveries` times in a row stops being re-injected in full and is listed by path instead, since re-sending the same ~2KB note forever was most of what was pushing stores over the new budget (#842).

### Fixed

- Fixed: the over-cap notice for `thresholds.memory_inject_max_bytes` recommended running `/remember:doctor` even when the cap had been deliberately lowered below the bundled 200000 default (e.g. to stay under the new `thresholds.session_start_max_bytes` budget). It now says the file was "capped by config" and drops the `/remember:doctor` suggestion in that case -- but only while every over-cap file is within the bundled default; a file larger than 200000 bytes keeps the original "this store looks broken" wording and the doctor advice whatever the cap, as does a cap at or above the bundled default (#842).

### Security

- Security: `/remember:doctor`'s front matter granted bare `Bash` --
  unrestricted shell access for the full run of the command -- when the command
  body only ever needs one script. `allowed-tools` is now scoped to
  `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh:*)`, mirroring
  `skills/remember/SKILL.md`'s existing narrow grant for `write-handoff.sh`, and
  `scripts/doctor.sh` is invoked directly rather than through `bash "..."` (a
  scoped pattern cannot match a `bash` prefix). `scripts/doctor.sh` is now
  executable in git (mode 100755), the same as `write-handoff.sh` already is.
  `.github/scripts/check_release_tree.py`'s pre-submission preflight now fails
  on any shipped skill, command or agent whose `allowed-tools` grants
  unrestricted shell, matching the directory's own `ALLOWED_TOOLS_BROAD`
  wording: bare `Bash`, `Bash(*)`, `Bash(:*)`, a wildcard right after a shell,
  interpreter, package manager or runner, or `curl`/`wget` (`Bash(bash:*)`,
  `Bash(pwsh:*)`, `Bash(python3:*)`, `Bash(npx:*)`, `Bash(curl:*)` ...), a relative path, or a
  wildcard inside the path -- in both the string and the YAML-list form, so this
  class is caught before a release tree ships rather than by the directory's own
  scan. The portal's own accepted examples (`Bash(git status:*)`,
  `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run.py:*)`) still pass (#859).
- Two comments that described code the plugin does not run were reworded: the
  `pipeline/shell.py` docstring said scripts consume its output with an
  `eval` of a command substitution, but the real consumer is `safe_eval`, which
  never runs the text; and a `scripts/log.sh` comment used `curl` as an example
  of a stalled child. The directory's `RUNTIME_FETCH_EXEC` warning was raised on
  exactly these two files of the `release` tree, which contain no download (#859).

[0.38.0]: https://github.com/Digital-Process-Tools/claude-remember/releases/tag/v0.38.0
