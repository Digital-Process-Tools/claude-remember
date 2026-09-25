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
