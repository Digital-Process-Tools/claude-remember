---
title: "Declined traps — paths layer"
description: "Traps curated and deliberately not promoted here, with the reason. A trap named below has been decided, not overlooked."
---

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight.

- **`524.readme-call-site-count-stale`** — declined as a rule 2026-09-05, **filed as
  [#580](https://github.com/Digital-Process-Tools/claude-remember/issues/580) instead**.
  `docs/windows.md:16` claims 10 `_remember_forward_slash` call sites; the live `grep` count was 12
  when the trap was written and is 15 today. That is a wrong number in a document, which a fix
  corrects — not a situation a rule can warn anyone out of. Injecting "the count in this file may be
  stale" on every touch of `docs/` would be noise standing in for a one-line repair.

- **`695.emit-read-max-is-an-unvalidated-knob`** — declined 2026-09-25. `REMEMBER_EMIT_READ_MAX`
  was read with no numeric guard at `lib-memory-context.sh:198`, unlike its siblings in the
  same file; fixed in #758, which added the same `case (''|*[!0-9]*) ... ;; esac` guard the
  file's other numeric knobs already carry. Declined as a rule because the knob is
  undocumented and developer-only (not user-facing input), too narrow a single line to be worth
  injecting into every touch of `scripts/`. **Filed as
  [#758](https://github.com/Digital-Process-Tools/claude-remember/issues/758) instead.**
- **`719.silent-exclude-mkdir-failure`** — declined 2026-09-25. The `mkdir -p ... 2>/dev/null`
  guard with no `else` at `50-git-backup.sh:504` is still true at HEAD: a failed `mkdir -p` still
  silently skips writing the `config.json` exclusion, with the caller none the wiser. Declined as
  a rule (the fragment already names the exact fix — an `else` branch and a test that forces the
  `mkdir -p` to fail — this is a one-line-diff-shaped fix, not a lesson a standing rule would
  teach anyone away from). **Filed as
  [#759](https://github.com/Digital-Process-Tools/claude-remember/issues/759) instead.**
- **`721.git-dir-symlink-bypasses-tracked-check`** — declined 2026-09-25. Still true at HEAD:
  `_remember_handoff_is_tracked` still resolves `PROJECT_DIR/.git` the ordinary way, so a
  pre-planted symlink/`gitdir:` pointer would answer against a different repository. Declined as
  a rule because the fragment's own analysis holds up: the attacker already needs a strictly
  stronger local-write primitive than this plugin's own threat model assumes anywhere else, at
  which point the tracked-check is not the weakest link. **Filed as
  [#761](https://github.com/Digital-Process-Tools/claude-remember/issues/761) as a low-priority
  defense-in-depth item, not a blocking one.**
- **`660.promo-installed-key-array-plugins-shape`** — declined 2026-09-25. Still true at HEAD:
  `session-start-hook.sh:1494`'s `to_entries` probe still does not error on a non-object
  `.plugins`, unlike the per-key query it replaced. Declined as a rule because it is one jq call
  site's own defensive-coding gap, not established live (no evidence `installed_plugins.json`'s
  real writer ever emits a non-object `.plugins`), and narrower than a standing rule is worth.
  **Filed as [#762](https://github.com/Digital-Process-Tools/claude-remember/issues/762)
  instead.**
- **`743.doctor-pwd-fallback`** — declined 2026-09-26. Still true at HEAD: `scripts/doctor.sh:94-100`
  and `:223-229` still fall back to a raw `$PWD` for `CLAUDE_PROJECT_DIR`, the same shape #743 fixed
  in `write-handoff.sh` by preferring `git rev-parse --show-toplevel`. Declined as a rule because it
  is a one-line-per-site fix once `doctor.sh`'s own then-open PRs (#769/#770/#771) are no longer live,
  not a lesson a standing rule would teach anyone away from. **Filed as
  [#802](https://github.com/Digital-Process-Tools/claude-remember/issues/802) instead.**
- **`745.entrypoint-sniff-has-two-unfixed-edges`** — declined 2026-09-26. Still true at HEAD:
  `_transcript_is_pluginless_sdk()` (`scripts/session-start-hook.sh:910-926`) still has all three
  edges the #745 follow-up self-review found (no evidence check tying the exclusion to the
  candidate's own marker/save-record; a substring scan rather than a real JSON parse; the
  message-field skip that can itself mask the bug it exists to prevent). Declined as a rule because
  each edge is a scoped, single-function fix rather than a generalizable lesson. **Filed as
  [#803](https://github.com/Digital-Process-Tools/claude-remember/issues/803) instead.**
- **`748.marker-signal-dies-if-mktemp-fails`** — declined 2026-09-26. Still true at HEAD:
  `scripts/lib-memory-dir.sh:622`'s no-jq Python-fallback drop marker still has no signal if its own
  `mktemp` call fails alongside the untrusted-config load it is meant to disclose. Declined as a rule
  because the fix (a second, independent failure signal, or accepting the existing
  every-mktemp-failure-here-is-tolerated precedent) is a single-site design call, not a lesson.
  **Filed as [#804](https://github.com/Digital-Process-Tools/claude-remember/issues/804) instead.**
- **`777.rotated-slices-no-guard`** — declined 2026-09-26. Still true at HEAD:
  `_remember_render_memory_section`'s rotated-slices loop (`scripts/lib-memory-context.sh`,
  currently ~lines 972-983) still lists memory-file paths with no `_remember_may_inject` call,
  unlike the compact-mode deferred-file loop the same function already guards. Declined as a rule
  because the fix is the same one-shape guard call #790 already applied to the sibling loop, not a
  new lesson. `#791` was filed for this same finding and closed in favor of this very trap.d
  fragment being the record; since this curation pass consumes and deletes that fragment, **filed
  as [#805](https://github.com/Digital-Process-Tools/claude-remember/issues/805) instead**, so the
  finding still has a durable record once the fragment is gone.
- **`788.consolidate-hardcoded-timeout`** — declined 2026-09-26. Still true at HEAD:
  `pipeline/consolidate.py:324` still hardcodes `call_haiku(prompt, timeout=180)` with no config
  key, in the staging consolidation path sibling to the one #788/#792 fixed for the NDC path.
  Declined as a rule because the fix is the same `thresholds.*` config-key shape #792 already
  established as a template, not a new lesson. `#793` was filed for this same finding and closed in
  favor of this very trap.d fragment being the record; since this curation pass consumes and deletes
  that fragment, **filed as [#806](https://github.com/Digital-Process-Tools/claude-remember/issues/806)
  instead**, so the finding still has a durable record once the fragment is gone.
- **`799.windows-skip-reason-stale`** — declined 2026-09-26. The fragment's own worry (a blanket
  `win32` skip reads suspicious in this repo and needs triaging into either a rule or a tracked
  decision) is already fully covered by existing infrastructure: `docs/windows-skip-triage.md`
  already carries a per-module verdict for every file the fragment discusses, and
  `.claude/jit-context/tools/00-manual/win32-skip-triage-entry.md` already reminds a session adding
  a NEW blanket skip to add a triage-doc row in the same commit. Checked against `docs/windows-skip-
  triage.md` at HEAD: `tests/test_write_handoff_pwd_root_743.py`, `tests/test_write_handoff_tracked_
  target_750.py` and `tests/test_write_handoff_subdirectory_project_776.py` are each marked
  **convertible** ("reason names only the bash-subprocess dependency, no other blocker"), and
  `tests/test_compact_deferred_guard_777.py` is marked **unclear** (plants a real symlink via
  `os.symlink`, a POSIX-only primitive, so the auditor's own symlink-privilege theory in the
  fragment may be the real blocker there). The fragment itself named only one of these four files
  (`test_write_handoff_tracked_target_750.py`) -- the other three, spanning #743/#776/#777, carry
  the identical reason string and the same open question, corrected here for whoever reads this
  entry next. Declined as a new rule because the review-and-track mechanism this fragment asked for
  already exists in more complete form than the fragment itself; the residual work (actually
  converting the three convertible rows to `resolve_bash()`, and resolving the one unclear row) is
  visible directly in `docs/windows-skip-triage.md`'s own verdict column, the same way this repo
  already tracks backlog outside of milestones -- no new issue filed.
