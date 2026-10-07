---
title: "Declined traps — paths layer"
description: "Traps curated and deliberately not promoted here, with the reason. A trap named below has been decided, not overlooked."
---

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight.

- **`725.recon-reads-dirty-sibling-clone-not-origin-main`** (duplicate, second copy) -- swept
  2026-09-28. This is a leftover untracked copy of a fragment already declined on 2026-09-25 (see
  the tools-layer entry filed as
  [claude-oss#1745](https://github.com/Digital-Process-Tools/claude-oss/issues/1745)) -- the file
  survived, unstaged, in the primary clone's working tree past that pass's own commit, and this
  pass's `--copy-stray-from` swept it in again as if new. Deleted with no new decision: nothing
  here has changed since the 2026-09-25 entry.
- **`816.make-env-fixture-threshold-merge-trap`** -- declined 2026-09-28. Still true at HEAD:
  `tests/test_consolidation_append_race.py`'s `_make_env(tmp_path)` still writes
  `config.json` as the hard-coded literal `{"cooldowns": {}, "thresholds": {}}`, with no
  parameter for a caller to merge a threshold value into. Declined as a rule because the fix (an
  optional `thresholds` dict merged into the literal) is a one-fixture-shaped change, not a
  generalizable lesson, and the gap is currently latent (no test in the file exercises a threshold
  value today). **Filed as
  [#833](https://github.com/Digital-Process-Tools/claude-remember/issues/833) instead.**
- **`816.six-more-silent-coercion-config-keys`** -- declined 2026-09-28. Partially stale: of the
  seven `config ".thresholds.*"` reads the fragment named as unguarded, five already carry
  #816/#821's malformed-value guard at HEAD (`min_human_messages`, `min_exchanges_without_human`,
  `staging_warn_bytes`, `memory_inject_max_bytes`, `autonomous_log_retention_days`). Two still
  lack it: `consolidate_max_bytes` (`scripts/run-consolidation.sh:132`) and `extract_max_bytes`
  (`scripts/save-session.sh:696`) -- still true, confirmed by reading both files at HEAD. Declined
  as a rule because the fix is the same scoped `case ... esac` guard shape #816/#821 already
  established, not a new lesson. **Filed as
  [#834](https://github.com/Digital-Process-Tools/claude-remember/issues/834) instead, scoped to
  the two remaining sites.**
- **`825.ci-skip-lets-username-leaks-reach-main`** -- declined 2026-09-28. Still true at HEAD:
  `_username_check_should_run()` in `tests/test_no_real_home_paths_467.py` still unconditionally
  skips the real-username-leak guard whenever `CI`/`GITHUB_ACTIONS` is set, by design (#472), with
  no other mechanism to catch this class of leak reaching `main` with green CI. Declined as a rule
  because the open question is a design decision (a genuinely CI-safe detection mechanism, or an
  accepted local-only gap), not a lesson a standing rule would teach anyone away from. **Filed as
  [#835](https://github.com/Digital-Process-Tools/claude-remember/issues/835) instead.**
- **`827.changelog-cited-a-trap-fragment-curate-already-deleted`** -- declined 2026-10-03.
  Duplicate of the existing rule `changelog-trap-citation-goes-stale.md` in this same layer, which
  already cites this exact #827 incident. Already fixed: the release that folded
  `changelog.d/827.fixed.md` (commit `a92a072`, "chore(release): 0.36.0") reworded the entry
  before it ever reached `CHANGELOG.md` -- the live #827 entry in `CHANGELOG.md` today names no
  `trap.d/` path at all. The fragment's own suggestion of a mechanical grep check is not acted on:
  the existing rule already states explicitly that this is a naming-convention fix, not a tooling
  gap to route around, and a second rule saying the same thing would only grow this layer for no
  new lesson.
- **`828.clones-badge-silent-schema-drift`** -- declined 2026-10-03. Still true at HEAD:
  `.github/workflows/clones-badge.yml`'s merge step still reads `(.[1].clones // []) + .[0])` with
  no check for whether the traffic/clones response actually carried a `clones` key, so a 200
  response with an unexpected schema would silently add nothing to `history.json` with no error
  anywhere. Declined as a rule because the fix (a loud, non-failing warning when `.[1] |
  has("clones")` is false) is a single-workflow design call, not a generalizable lesson. **Filed
  as [#877](https://github.com/Digital-Process-Tools/claude-remember/issues/877) instead.**
- **`842.session-start-budget-exhausted-not-logged`** -- declined 2026-10-03. Still true at HEAD:
  `_remember_apply_session_start_budget` (`scripts/lib-memory-context.sh`, ~lines 1341-1350) still
  returns after its drop loop with no check of whether the budget was actually met and no log line
  either way. Declined as a rule because the fix is a single-function comparison-and-log addition,
  not a lesson a standing rule would teach anyone away from. **Filed as
  [#878](https://github.com/Digital-Process-Tools/claude-remember/issues/878) instead.**
- **`859.release-tree-preflight-spelling-gaps`** -- declined 2026-10-03. Partially stale: the
  fragment's space-delimited `allowed-tools` case (`Read Bash`, no comma) is already fixed at HEAD
  -- `_check_front_matter` (`.github/scripts/check_release_tree.py`) now tokenizes on whitespace as
  well as commas (#866), confirmed by driving `_check_front_matter` directly against that exact
  spelling. The remaining gaps are still true, confirmed the same way: a `..` path segment inside
  a `Bash(${CLAUDE_PLUGIN_ROOT}/...)` grant, an unlisted `bash5` binary name, and nine wrapper
  commands (`xargs`, `sudo`, `eval`, `exec`, `source`, `find`, `awk`, `nohup`, `timeout`) absent
  from `_UNSCOPED_COMMANDS` all still pass the preflight silently. Declined as a rule because the
  fix is the same kind of list/check extension #866 already made, not a new lesson. **Filed as
  [#879](https://github.com/Digital-Process-Tools/claude-remember/issues/879), scoped to the
  remaining gaps only.**
- **`870.doctor-capture-is-working-arm-missing-144-guard`** -- declined 2026-10-03. Still true at
  HEAD: `scripts/doctor.sh`'s "capture is working" verdict arm still lacks the
  `{ [ -z "$_SESSION_DIR" ] || [ -d "$_SESSION_DIR" ]; }` guard the #870 arm immediately above it
  already carries for the identical masking reason, confirmed by reading both arms. Declined as a
  rule because the fix is copying one existing guard clause onto a sibling condition, not a new
  lesson. **Filed as [#880](https://github.com/Digital-Process-Tools/claude-remember/issues/880)
  instead.**
- **`870.log-py-no-control-char-flattening`** -- declined 2026-10-03. Still true at HEAD:
  `pipeline/log.py`'s `log()` still writes `message` with no control-character flattening, unlike
  the shell-side `report_error()` / `_dispatch_report_skip()` (#599/#618), confirmed by reading
  `log()` directly. Declined as a rule because the fix touches every caller of `log()` repo-wide in
  one shape (sanitize inside `log()` itself), which is a single-function change, not a
  generalizable lesson. **Filed as
  [#881](https://github.com/Digital-Process-Tools/claude-remember/issues/881) instead.**

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
- **`804.trusted-config-json-load-uncaught-misreports`** — declined 2026-09-27. At the time of
  this entry, `scripts/lib-memory-dir.sh:724-725`'s no-jq Python-fallback merge still loaded each
  TRUSTED config source (a corrupted `~/.remember/config.json` or `${REMEMBER_DIR}/config.json`)
  with a bare `with open(path) as f: data = json.load(f)`, no `try/except`. A malformed file there
  raised uncaught, exited nonzero-but-not-3, and the shell's own bundled-only fallback ran with no
  WARNING from #804's disclosure guard -- the identical silent behaviour as no config existing at
  all. Declined as a rule because the fix (a third exit code, or a stated well-formed-is-assumed
  boundary) is a single-site design call adjacent to #804, not a generalizable lesson. **Filed as
  [#815](https://github.com/Digital-Process-Tools/claude-remember/issues/815), which fixed it**
  (`lib-memory-dir.sh` now wraps the load in `try: ... except (OSError, ValueError):`) -- this
  entry's own "still true at HEAD" claim went stale the moment #815 merged, uncorrected until
  #827 (#815 and #817 landed close enough together that #817's curation pass captured this
  entry's text before #815's fix was visible in the same review).
- **806.timeout-guard-silent-substitution** -- declined 2026-09-27. At the time of this entry, both
  `scripts/run-consolidation.sh:137-138` (thresholds.consolidate_timeout_seconds) and its sibling
  `scripts/save-session.sh:1149-1150` (thresholds.ndc_timeout_seconds) still silently substituted
  the default 180 on an empty or non-digit configured value, with no log line naming the
  substitution. Declined as a rule because the fix (a log/report_error call on the fallback branch
  of both guards, plus a regression test) is a scoped fix to a known pair of sites, not a
  generalizable lesson. **Filed as
  [#816](https://github.com/Digital-Process-Tools/claude-remember/issues/816), which fixed it**
  (both guards now log the malformed value before substituting the default) -- the same stale
  "still true at HEAD" gap as the #804 entry above, corrected here by #827.
- **`860.codex-fallback-deprecation-wording`** -- declined 2026-10-06, already fixed. The
  fragment described `pipeline/haiku.py`'s deprecation message for the legacy
  `REMEMBER_OAUTH_TOKEN` / `haiku.oauth_token` fallback naming a Claude-Code-only remedy
  that did not exist for a Codex-hosted operator. Checked against HEAD: the warning this
  code path now emits (the branch gated on `_AUTH_FAILURE_MARKERS` after an un-isolated
  retry) reads "log in again with your coding agent's own CLI. This plugin reads no
  credential of its own any more -- there is no setting here to configure (#129/#131/#860)"
  -- generic across every host, naming no plugin-specific remedy at all, because the whole
  recovery-token mechanism this fragment's complaint was about was removed entirely in
  round 3/4 of #860. No new issue filed.
- **`860.stale-live-credential-wording-outside-diff`** -- declined 2026-10-06, still true at
  HEAD for a narrower set of files than the fragment originally named (several other sites
  it cited, e.g. `docs/configuration.md`, were independently fixed since). `hooks.d/
  after_save/50-git-backup.sh`, `hooks.d/before_session_start/50-git-restore.sh`,
  `scripts/lib-memory-dir.sh` and `scripts/log.sh` still describe `haiku.oauth_token` as "a
  live ... OAuth credential" even though #860 round 4 removed it as an auth source on every
  host (`docs/configuration.md`'s own current wording: "not read for anything ... there is
  nothing to migrate it to"). Declined as a rule because the fix is a one-time wording sweep
  across a known, small file set, not a generalizable lesson. **Filed as
  [#918](https://github.com/Digital-Process-Tools/claude-remember/issues/918) instead**
  (together with `894.stale-remember-oauth-token-docs-sweep-gap` below, same root cause).
- **`870.auth-failure-warning-misattributes-dead-credential`** -- declined 2026-10-06, already
  fixed. The fragment described the un-isolated-retry auth-failure warning
  (`pipeline/haiku.py`) blaming "the CLI's own saved login" even when the actually-dead
  credential was a configured userConfig token, a host-supplied `CLAUDE_CODE_OAUTH_TOKEN` or
  an ambient `ANTHROPIC_API_KEY`. Checked against HEAD: the warning no longer names or blames
  any specific credential source at all -- it says the CLI's saved login has expired and that
  "this plugin reads no credential of its own any more," because #860 round 3/4 removed the
  userConfig recovery-token mechanism the misattribution was about. **Issue
  [#890](https://github.com/Digital-Process-Tools/claude-remember/issues/890), filed for this
  same finding before this curation pass, appears to already be fixed by the same change --
  worth a maintainer look at closing it.** No new issue filed here.
- **`875.readme-cache-disclosure-names-old-filename`** -- declined 2026-10-06, **fixed by
  [#888](https://github.com/Digital-Process-Tools/claude-remember/issues/888), checked at the
  commit that landed it.** Originally: `README.md`'s `$TMPDIR/remember-*` disclosure named the
  pre-`v2` filename (`remember-config-cache-<key>`) while `scripts/log.sh` had written
  `remember-config-cache-v2-<key>` since #864 changed the cache format (#875 was the follow-up
  PR that landed #864's fix, not the format bump itself). README now names the real `v2`
  filename and the cache publisher best-effort removes a leftover pre-`v2` orphan on write.
  Declined as a rule at the time because the fix was a one-line README correction, not a
  generalizable lesson -- that stands; this entry is kept only as a closed record, not reopened.
- **`891.lib-lock-eval-new-hits`** -- declined 2026-10-06, already fixed. The fragment
  described `check_release_tree.py`'s widened `EVAL_OF_SUBSTITUTION` pattern newly flagging
  `scripts/lib-lock.sh`'s own `eval "_LOCK_TIMING_T0_${KEY}=..."` shape, untriaged. Checked
  against HEAD: `scripts/lib-lock.sh` no longer uses `eval` for this at all -- the lock-timing
  state is now kept in bash arrays (`_LOCK_TIMING_T0S`, `_LOCK_TIMING_WAITS`, etc.), with a
  comment reading "#898 round 10: no `eval`, no variable named at run time." `.github/
  release-branch.json`'s deny-list never needed the file. No new issue filed.
- **`894.doctor-notice-grep-is-history-not-presence`** -- declined 2026-10-06, already fixed.
  The fragment described `doctor.sh`'s "legacy recovery-token config in use" section deciding
  presence by grepping daily-log history for a `NOTICE:` substring rather than checking
  current state, with both a stale-warning-after-migration and a false-all-clear failure
  mode. Checked against HEAD: the whole section is gone -- `scripts/doctor.sh`'s own comment
  reads "#898, round 4: the 'Legacy recovery-token config (#860)' section that used to live
  here is removed entirely," consistent with the underlying mechanism (`REMEMBER_OAUTH_TOKEN`
  / `haiku.oauth_token`) itself being fully removed by the same round. No new issue filed.
- **`894.stale-remember-oauth-token-docs-sweep-gap`** -- declined 2026-10-06, still true at
  HEAD for two of the five files the fragment originally named (`docs/configuration.md`,
  `docs/verification.md` and `docs/diagnostics.md` were independently fixed since; the
  fragment's own `docs/diagnostics.md:16` concern is now resolved by an explicit "#898 round
  15" addendum right below it). `docs/git-backup-security.md` and
  `docs/external-storage-mode.md` still tell users to set `REMEMBER_OAUTH_TOKEN` as a working
  mitigation, when the var authenticates nothing on any host. Declined as a rule for the same
  reason as `860.stale-live-credential-wording-outside-diff` above. **Filed as
  [#918](https://github.com/Digital-Process-Tools/claude-remember/issues/918) instead**
  (same issue as that entry -- one root cause, one sweep).
- **`896.readme-overstates-token-isolation`** -- declined 2026-10-06, already fixed. The
  fragment described README.md overstating the userConfig recovery token's isolation
  ("only this plugin's own save can read it back") against `pipeline/haiku.py:_child_env`'s
  actual strip list. Checked against HEAD: that README sentence no longer exists, and
  `CLAUDE_PLUGIN_OPTION_OAUTH_TOKEN` is no longer read anywhere in `pipeline/` -- the whole
  userConfig recovery-token mechanism was removed in #860 round 4, so the isolation claim
  the fragment questioned has nothing left underneath it to overstate. No new issue filed.
- **`898.legacy-token-presence-check-swallows-read-errors`** -- declined 2026-10-06, already
  fixed, per the fragment's own addendum. `_legacy_recovery_token_configured()` and its
  caller were deleted entirely in #898 round 4 (commit `3b93c53`), not patched -- the
  underlying read-error-swallowing behaviour this fragment described has no code left to
  misreport from. No new issue filed.
- **`898.url-in-comment-guard-heredoc-body-false-positive`** -- declined 2026-10-06, still
  true at HEAD: `.github/scripts/check_release_tree.py`'s `_check_url_in_comment` still has
  no heredoc-boundary tracking, unlike the sibling `_in_code()` helper it could reuse the
  shape of. Re-confirmed not live today (a repo-wide grep for a `#`-prefixed line with a URL
  host inside any heredoc body still returns zero hits). Declined as a rule because the fix
  is a scoped addition to one checker function, not a generalizable lesson. **Filed as
  [#919](https://github.com/Digital-Process-Tools/claude-remember/issues/919) instead.**
- **`902.changelog-overclaims-mkdir-guard-coverage`**,
  **`902.remember-dir-guard-fatal-swallowed`** and
  **`902.remember-dir-guard-refuses-unc-store`** -- declined 2026-10-06, all three still true
  at HEAD. All three describe gaps in the same #902 `REMEMBER_DIR` safety guard:
  `scripts/bootstrap-dirs.sh` still builds the exact directory tree the guard exists to
  refuse, with no check of its own, before the guard ever runs on the slow path;
  `scripts/post-tool-hook.sh`/`scripts/log.sh` still route the guard's own FATAL through
  `log()`, which has already pointed `MEMORY_LOG_FILE` at `/dev/null` by the time it fires,
  so the refusal reaches nowhere a user can read it; and `scripts/resolve-paths.sh`'s
  `_remember_normalize_win_path` still only recognises drive-letter and `/c/`-style Windows
  paths, so a native UNC project directory is itself misclassified as unsafe and refused.
  Declined as a rule because the fix for each is a scoped change to one existing guard, not a
  generalizable lesson, and all three are different facets of the same shipped feature.
  **Filed as [#920](https://github.com/Digital-Process-Tools/claude-remember/issues/920)
  instead**, covering all three.
- **`879.wrapper-grant-missing-command-builtin`** and **`879.wrapper-grant-spellings-missed`** --
  declined 2026-10-07, both still true at HEAD. `.github/scripts/check_release_tree.py`'s
  `_UNSCOPED_COMMANDS` set (lines 840-851) still lacks `command`, `nice` and `stdbuf`, and the
  `..` path-escape check at line 876 still only splits on forward slash, missing a
  backslash-separated escape -- confirmed directly against `bash_grant_problem`. Declined as a
  rule because the fix is the same list/check-widening shape #866/#879 already established, not
  a new lesson. **Filed as
  [#950](https://github.com/Digital-Process-Tools/claude-remember/issues/950) instead,
  consolidating both.**
- **`913.windows-cygpath-test-skip`** -- declined 2026-10-07, already documented. The fragment's
  concern (`tests/test_session_dir_cache_913.py` skips on win32, so "CI passed on windows-latest"
  verifies nothing about the fix's actual cygpath-avoidance claim there) is already recorded
  verbatim in `docs/windows-skip-triage.md`'s own row for that file ("the fix's cygpath-avoidance
  claim is verified only against a POSIX stub cygpath, not a native one"). No new rule and no new
  issue: the existing per-module verdict list already carries this exact caveat.
- **`932.ndc-gen-not-bumped-on-conflict-path`** -- declined 2026-10-07, still true at HEAD:
  `hooks.d/after_save/60-git-reconcile.sh`'s conflict path still never calls
  `_grc_bump_ndc_gen`, unlike the fast-forward and successful-rebase paths. Declined as a rule
  because the fix is a single missing call site, not a generalizable lesson. **Filed as
  [#954](https://github.com/Digital-Process-Tools/claude-remember/issues/954) instead.**
- **`933.fast-path-test-does-not-confirm-fast-path-ran`** -- declined 2026-10-07, still true at
  HEAD: the fast-path test in `tests/test_autonomous_log_retention_487.py` still has no positive
  evidence `_remember_auto_ref` was actually built by `mktemp`, unlike its sibling fallback test.
  Declined as a rule because the fix is an assertion/marker addition across one test file's
  existing tests, not a new lesson -- the closest generalizable lesson here (confirming which
  code path a test exercises, not just that its output is correct) is adjacent to but distinct
  from this repo's existing "pair a must-not-fire assertion with a must-fire one" convention, and
  one incident is not enough to justify a second, overlapping rule. **Filed as
  [#951](https://github.com/Digital-Process-Tools/claude-remember/issues/951) instead.**
- **`939.reconcile-abort-exit-status-unchecked`** -- declined 2026-10-07, already fixed. The
  fragment described the old `git rebase --abort ... || true` (unchecked exit status) at the
  pre-#945 version of `60-git-reconcile.sh`. At current HEAD, the conflict path no longer calls
  `--abort` at all -- commit 84a6ea5 (#945) replaced it with `if git -C "$REPO_ROOT" rebase
  --quit >/dev/null 2>&1; then ...`, which does check the exit status via the `if` itself. No new
  issue.
- **`939.reconcile-foreign-commit-dropped-by-abort`** -- declined 2026-10-07, already fixed /
  superseded. The fragment's exact mechanism (`git rebase --abort` resetting the whole tracked
  tree and dropping a foreign commit with it) no longer applies: commit 84a6ea5 (#945) removed
  the `--abort` call from the live conflict path entirely, replacing it with `rebase --quit` (which
  does not reset the working tree) plus a scoped checkout. A related-but-different scoping gap in
  that replacement is already tracked separately as part of
  [#952](https://github.com/Digital-Process-Tools/claude-remember/issues/952) (the
  restore-scoped-to-slug finding below). No new issue filed for this fragment specifically.
- **`945.reconcile-autostash-not-disabled-for-rebase`**, **`945.reconcile-restore-scoped-to-slug-not-rebase-paths`**
  and **`945.reconcile-symbolic-ref-uses-configured-not-actual-branch`** -- declined 2026-10-07,
  all three still true at HEAD. All three are regressions introduced by commit 84a6ea5 (#945)'s
  `rebase --quit`-based rewrite of `60-git-reconcile.sh`'s conflict path: no `--no-autostash` at
  either rebase call site, the scoped restore assuming every replayed commit is confined to
  `$SLUG/`, and `git symbolic-ref HEAD refs/heads/$BRANCH_NAME` restoring to the configured
  branch rather than the rebase's own recorded head-name. Declined as a rule because each fix is
  a scoped change to one already-identified function, not a generalizable lesson (the
  generalizable half -- a notice claiming a tree state it never checked -- is promoted
  separately as `hook-notice-must-not-claim-unverified-tree-state.md` in this same layer). **Filed
  as [#952](https://github.com/Digital-Process-Tools/claude-remember/issues/952) instead,
  consolidating all three.**
- **`946.rebase-in-progress-collapses-could-not-tell-into-nothing-there`** -- declined 2026-10-07,
  still true at HEAD: `_grc_rebase_in_progress` still returns a plain false both when no rebase is
  in progress and when the underlying `git rev-parse --git-path` call itself fails, across all of
  its call sites. Declined as a rule because the open question is a design decision (whether the
  undetermined case should behave like "in progress" or surface its own log line), not a lesson a
  standing rule would teach anyone away from. **Filed as
  [#953](https://github.com/Digital-Process-Tools/claude-remember/issues/953) instead.**
