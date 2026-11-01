---
title: "The manual tools layer — what lives here, and what was declined"
description: "Human-written tool rules, safe from the generated 01-oss layer's wholesale replacement. Declined traps are recorded below rather than left silent."
---

**Why this layer exists at all.** `01-oss/` is generated and replaced wholesale on every plugin
install (`oss_rules.py`: "the layer is replaced wholesale on every install, because nothing a human
wrote lives in it"). An append there is destroyed silently by the next update — no error, no diff,
the knowledge simply gone. Anything a human writes goes here instead.

Two rules here are **interim copies of knowledge that belongs upstream**, because the situations
they describe belong to a plugin rather than to this repository, and every repo that plugin manages
hits them. They are filed upstream and should be deleted here once they ship in the generated layer:

- `tree-snapshot-compare.md` → [claude-oss#1105](https://github.com/Digital-Process-Tools/claude-oss/issues/1105)
- `release-publish-denied.md` → [claude-oss#1106](https://github.com/Digital-Process-Tools/claude-oss/issues/1106)
- `supertool-literal-backslashes.md` → [claude-supertool#2327](https://github.com/Digital-Process-Tools/claude-supertool/issues/2327)
- `raw-append-write-unguarded.md` → [claude-supertool#2699](https://github.com/Digital-Process-Tools/claude-supertool/issues/2699)

## Declined traps

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight.

- **`1002.detach-windows-silent-skip-after-launch`** -- declined 2026-10-31 as a new rule: already
  fixed. scripts/lib-detach.sh:223-233 now propagates wscript.exe's real exit status (`wscript_rc`)
  through `_remember_detach_windows` instead of unconditionally returning 0, confirmed by reading
  the current file. Round 3 of PR #1010 observed this fixed on real `windows-latest` CI legs (run
  #38049234152).
- **`1002.wsh-strips-quotes-in-hidden-launch-c-script`** -- declined 2026-10-31 as a new rule:
  already fixed. scripts/lib-detach.sh:345-363 no longer embeds a literal quote in any
  `wscript.exe` argv entry (OUTFILE is its own separate argument now), confirmed by reading the
  current file. PR #1010 round 3 observed this fixed on real `windows-latest` CI legs.
- **`1002.session-start-hook-comment-overstates-coverage`** -- declined 2026-10-31 as a new rule:
  already fixed. scripts/session-start-hook.sh:2326-2338's comment now correctly cites #1006 for
  the two deferred call sites rather than claiming they already carry the fix, confirmed by reading
  the current file.
- **`1002.supertool-edit-complexity-and-classifier-backreference`** -- partially promoted, partially
  declined, 2026-10-31. The Bash-tool complexity-refusal finding is folded into the new
  `worktree-isolation-guard-surface-text-false-positive.md` rule in this same layer. The separate
  `review_return.py` `--framed` back-reference classifier finding is declined as a rule in *this*
  repository -- it is not this repository's code to fix, filed instead as
  [claude-oss#1814](https://github.com/Digital-Process-Tools/claude-oss/issues/1814).
- **`1002.round-3-self-review-gitattributes-and-citation`** -- declined 2026-10-31 as a new rule:
  both self-review findings (missing `.gitattributes` pin for `tests/fixtures/v0_42_3/`; a stale CI
  citation mixing a contaminated run with a clean one) checked against HEAD and confirmed STILL
  TRUE. Filed as
  [claude-remember#1070](https://github.com/Digital-Process-Tools/claude-remember/issues/1070)
  rather than left as a one-off code defect with no tracking issue.
- **`1003.doctor-consolidation-health-unreadable-log-reads-as-absent`** -- declined 2026-10-31 as a
  new rule: a one-off code defect (scripts/doctor.sh's Consolidation health section still folds
  "unreadable" into "absent"), checked against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1071](https://github.com/Digital-Process-Tools/claude-remember/issues/1071).
- **`1003.python-floor-ladder-stops-at-3.13`** -- declined 2026-10-31 as a new rule: a one-off code
  defect (scripts/detect-tools.sh's versioned fallback ladder still stops at python3.13), checked
  against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1072](https://github.com/Digital-Process-Tools/claude-remember/issues/1072).
- **`1011.below-threshold-path-regressed-and-process-count-misreported`** -- declined 2026-10-31 as
  a new rule: a one-off code defect in a specific audit measurement (process-count regression on
  the below-threshold save path). Checked -- could not tell: re-running the auditor's own PID-delta
  strace measurement across tagged trees was outside this pass's budget, and the specific
  command-substitution shape the fragment names (`_pt_scan_lines` inside `$(...)`) already does not
  match what scripts/post-tool-hook.sh:960/1126 does today (a plain call setting globals), so the
  fragment's own suggested fix may already be partly applied -- not confirmed either way.
- **`1011.lib-detach-same-stray-sleep-shape-unfixed`** -- declined 2026-10-31 as a new rule: already
  promoted in this same layer as `kill-a-subshell-not-its-forked-child.md` under `paths/00-manual/`
  (matches `scripts/.*\.sh$`, so lib-detach.sh is already covered). The underlying code defect this
  fragment names (scripts/lib-detach.sh:356's watchdog still kills the subshell, not the `sleep`
  child it forked) was checked against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1073](https://github.com/Digital-Process-Tools/claude-remember/issues/1073).
- **`1028.test-fetch-tag-shallows-dev-clone`** -- declined 2026-10-31 as a new rule: a one-off code
  defect in a test helper (`_materialize_tree` in
  tests/test_post_tool_total_processes_1011.py still fetches a missing baseline tag into the
  developer's own `REPO_ROOT`), checked against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1074](https://github.com/Digital-Process-Tools/claude-remember/issues/1074).
- **`350.fast-path-warm-positive-control-satisfied-by-cold-run`** -- declined 2026-10-31 as a new
  rule: a one-off code defect in a test's own positive control (the warm run's `capture-alive`
  marker check in tests/test_post_tool_fast_path_350.py cannot fail because the cold run already
  wrote that marker), checked against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1075](https://github.com/Digital-Process-Tools/claude-remember/issues/1075).
- **`906.mask-bracket-tests-depth-carries-across-lines`** -- declined 2026-10-31 as a new rule: a
  one-off code defect (.github/scripts/compile_hooks.py's `_mask_bracket_tests` still carries an
  unbalanced bracket's depth across physical lines unconditionally, with no check for a trailing
  `&&`/`||`/`|`), checked against HEAD and confirmed STILL TRUE. Filed as
  [claude-remember#1076](https://github.com/Digital-Process-Tools/claude-remember/issues/1076).
- **`913.tail-exit-status-not-checked-in-scan-fallback`** -- declined 2026-10-31 as a new rule: a
  one-off code defect. The specific code shape the fragment cites (`tail`/`printf 'X'` sentinel in a
  command substitution) was replaced by #1036 with a process-substitution + one-byte `read`
  (scripts/post-tool-hook.sh lines 1015-1024, 1047-1049), but the same gap persists in the new
  shape -- `tail`'s own exit status is still never observed -- checked against HEAD and confirmed
  STILL TRUE under the new mechanism. Filed as
  [claude-remember#1077](https://github.com/Digital-Process-Tools/claude-remember/issues/1077).

- **`705.edit-toml-literal-double-escape`** -- declined 2026-09-28 as a new rule: already
  promoted. `supertool-literal-backslashes.md` in this same layer already documents exactly this
  incident by number (#705) -- an embedded literal double quote needing \" written through
  `edit:@-`'s TOML payload with `literal_backslashes = true`, and the fix (use a single-quoted
  Python string literal instead of juggling the backslash count). This fragment is a duplicate
  record of an already-shipped rule, not a new lesson.
- **`705.supertool-cwd-default-wrong-tree`** -- declined 2026-09-28 as a new rule: already
  promoted as `supertool-cwd-verify-tree.md` in this same layer, which documents this exact
  incident (#705) -- supertool's default cwd silently resolving against the main clone instead of
  the lane's own worktree. Duplicate record, not a new lesson.
- **`706.raw-append-write-unguarded`** -- declined 2026-09-28 as a new rule: already promoted as
  `raw-append-write-unguarded.md` in this same layer, which documents this exact incident and is
  filed upstream as
  [claude-supertool#2699](https://github.com/Digital-Process-Tools/claude-supertool/issues/2699).
  Duplicate record, not a new lesson.
- **`819.exclusion-cap-give-up-indistinguishable-from-no-previous-session`** -- declined
  2026-09-28. Already fixed: `scripts/session-start-hook.sh`'s `previous_transcript()` and
  `_second_newest_jsonl()` both now log a WARNING on the cap-hit give-up branch (`#823`), naming
  the exclusion count and that a real previous session may exist beyond the cap -- exactly the
  auditor's suggested fix. Checked against HEAD (2026-09-28).

- **`487.ostype-immutable-on-windows-gitbash`** — declined 2026-09-05. `$OSTYPE` could not be
  forced away from its real value inside bash under real Windows Git Bash: neither
  `subprocess.run(env={"OSTYPE": ...})` nor `local OSTYPE=...` inside a function had any effect,
  across two separate CI runs (jobs 100892436094 and 100895310415, PR #499), costing two full CI
  round-trips. `readonly -p` shows `OSTYPE` is not readonly, so the mechanism was never explained.
  Well-evidenced and genuinely surprising — declined anyway because the lesson is already encoded
  where it bites: the test carries `@pytest.mark.skipif(sys.platform == "win32", ...)`, so the next
  lane meets the answer at the point of failure rather than needing a warning beforehand. A rule
  firing on every Windows-conditional bash test would restate a skip that is already there.
  **Reopen this if someone tries the same override a second time** — a repeat would mean the skip
  is not visible enough, and that is a different finding from the one declined here.

- **`722.edit-tool-cannot-write-raw-unicode-literal`** — declined 2026-09-25. A literal
  zero-width-space character written through `edit:@-`'s TOML payload landed on disk, then
  ruff's PLE2515 correctly flagged it (a legitimate lint, not a supertool bug) — costing one
  round-trip until the escape-sequence text (`\u200b`) was written instead of the literal
  character. Declined as a rule: this is not a code defect (nothing in this repo needs fixing),
  and the workaround is a single, already-obvious habit ("write the escape sequence text, not
  the invisible character") not worth injecting into every `edit:@-` call — most payloads never
  carry an invisible/ambiguous unicode character at all.
- **`725.recon-reads-dirty-sibling-clone-not-origin-main`** — declined 2026-09-25 as a rule in
  *this* repository: the defect is in `oss:recon` (agents/recon.md, Digital-Process-Tools/
  claude-oss), which has no instruction pinning its reads to `origin/<default-branch>` rather
  than whatever tree its own invoking process's cwd resolves to — still true as of claude-oss
  0.42.0, confirmed by reading `agents/recon.md` directly. Not this repository's code to fix, and
  not a lesson a rule injected into *this* repo's own sessions would ever reach (recon runs as
  its own spawn, elsewhere). **Filed as
  [claude-oss#1745](https://github.com/Digital-Process-Tools/claude-oss/issues/1745) instead,**
  naming `commands/run/curate.md`'s own worktree-first fix (#1670) for the identical class as
  the template recon could adopt.
