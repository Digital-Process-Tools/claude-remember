# Windows blanket-skip triage (#497)

#497 measured the `windows-latest` CI legs reporting green while most of the
suite skips: modules that carry a module-level

    pytestmark = pytest.mark.skipif(sys.platform == "win32", reason=...)

skip every test in the file, unconditionally, on that leg. #507 made the
resulting skip *ratio* visible on every run (`scripts/report_windows_skip_floor.py`,
wired into `conftest.py`). This file is the other half #497 asked for: not a
mass conversion of these modules, but "a recorded list with a per-module
verdict" against `tests/_bash_runner.py`'s `resolve_bash()` route -- the
thing that already replaced this exact pattern in the four modules #432
converted (`tests/test_hook_cwd_leak_417.py`, `tests/test_transcript_path_leak_424.py`,
`tests/test_session_end_log_names_488.py`, `tests/test_stdin_extractor_top_level_wins_447.py`),
plus a fifth, `tests/test_autonomous_log_retention_487.py`, which adopted the
same route for its own #487 rather than being converted by #432 itself.

## The count: 98, not 107 (and not the issue's original 92)

The issue's own re-verified count -- `grep -rl "pytestmark = pytest.mark.skipif"
tests | xargs grep -l "win32"` -- returns 107 on this tree. That command only
requires both substrings to appear *somewhere* in the file; it does not
require them to form one real module-level skip. Two files fail that test
outright: `tests/test_bash_runner_432.py` and
`tests/test_sanctioned_divergence_state_440.py` only *quote* the pattern in a
docstring, describing modules that used to carry it. Eleven more already
carry the `resolve_bash()`-style route -- a local `_find_bash()` whose own
probe order happens to mention `sys.platform == "win32"`, with a skip reason
of `"bash not available"` or `"Git Bash not found (...)"`, never a blanket
win32 skip -- so the grep's `win32` hit lands inside that probe, not inside
an actual `pytestmark` skip condition.

`scripts/windows_skip_triage_497.py` recomputes the set precisely instead: it
parses every test module with `ast`, and only counts a file that has an
actual module-level `pytestmark = pytest.mark.skipif(<expr containing
"win32">, reason=...)` assignment -- a bare call, or one arm of a
list/tuple of marks (pytest ORs a list, so any one arm mentioning "win32"
is a real blanket skip). That finds **98** modules on this tree at this
commit. `tests/test_windows_skip_triage_497.py` recomputes this same set
on every run and diffs it against the table below, so this list cannot
silently drift out of sync with the tree the way the issue itself describes
(92 -> 107 with nothing announcing the drift) -- though see the note at the
end of the Verdicts section: that guard has its own blind spot, caught
once already during review.

## Verdicts

- **convertible** -- the skip reason is plausibly about a POSIX-only *tool*
  (bash itself, a POSIX-only shell construct) that `resolve_bash()` could
  supply on a Windows runner via Git Bash. 8 modules.
- **not-convertible** -- the reason names something Git-Bash discovery
  cannot fix: POSIX file permissions/mode bits, process signals (`kill -0`,
  `fork`), `flock`, `umask`, NTFS/ACL semantics, or an explicit POSIX-vs-Windows
  path-format incompatibility. 12 modules.
- **unclear** -- the reason string alone does not say enough to tell; reading
  it is not the same as reading the test body, and this list is deliberately
  not that read. 78 modules.

**A blind spot in the scanner itself, caught once already.** An earlier
version of `scripts/windows_skip_triage_497.py` only matched a bare
`pytestmark = pytest.mark.skipif(...)` call, not the list-of-marks form
(`pytestmark = [pytest.mark.skipif(...), pytest.mark.skipif(...)]`), and so
silently missed `tests/test_slug_vectors_294.py` -- which the doc and its
own drift-detection test then agreed was absent, because the test recomputes
its "live" baseline from that same scanner function and cannot see a module
class the function never looked for. A second, independent reviewer's read
of the diff caught it; the scanner now walks both forms. The fix is
mechanical, but the lesson generalizes: this list's accuracy is bounded by
what the AST matcher was written to recognize, not by an exhaustive read of
every skip expression in `tests/`.

**The `unclear` majority is the honest result, not a shortcut.** The great
bulk of these 98 reasons is one of a handful of near-identical templated
strings -- `"bash subprocess + POSIX layout -- not portable to Windows
runners"`, `"bash hook subprocess + POSIX semantics -- not portable to
Windows runners"`, and near-variants -- that bundle the one thing
`resolve_bash()` fixes (no bash on PATH) with an unnamed "POSIX
semantics"/"layout" claim. Spot-reading a handful of these modules
(`tests/test_now_md_append_atomicity.py`, `tests/test_windows_drive_slug_263.py`)
confirmed the ambiguity is real, not just a property of the template: the
former genuinely depends on POSIX `rename(2)` atomicity semantics a bash
substitute would not by itself supply; the latter turned out to depend on
nothing but a bash subprocess running two of this repo's own `.sh` scripts.
Both share the same templated reason string. Telling them apart needs a
per-module read of the test body against the shell script(s) it drives --
exactly the work this list is scoped not to do (see #497's own "what would
settle it": "not a mass rewrite ... a recorded list with a per-module
verdict is the useful artifact").

## The table

| module | skip reason | verdict | basis |
| --- | --- | --- | --- |
| `tests/test_agy_hooks_563.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate -- same templated reason and same verdict as test_session_end_hook_345.py, test_post_tool_hook_spawns.py and test_prompt_hook_spawns.py, which this module's own three subprocess tests are modelled on |
| `tests/test_agy_hooks_malformed_payload_568.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate -- same templated reason and same verdict as test_agy_hooks_563.py, which this module's own three-way malformed/empty/valid subprocess tests are modelled on |
| `tests/test_agy_stop_hook_576_578_579.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate -- same templated reason and same verdict as test_agy_hooks_563.py/test_agy_hooks_malformed_payload_568.py, which this module's own agy-stop-hook.sh subprocess tests are modelled on; one test in this module (the #579 CRLF-python3 integration test) stubs the platform-specific piece and could in principle run on win32 too, but the module-level skip still applies because every other test in the file drives the real bash script directly |
| `tests/test_bootstrap_readonly_root.py` | POSIX mode bits + bash — read-only enforcement is not portable to NTFS (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_capture_gap_notice.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_case_divergence_298.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_codex_stdin_session_id_468.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_codex_transcript_path_459.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_config_key_injection_539.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; the test body sources scripts/log.sh's config() through a bash subprocess against tmp_path-derived PROJECT_DIR/PIPELINE_DIR/HOME and separately exercises an LC_ALL=C-scoped bracket-range regex under a UTF-8 locale -- real bash/locale-collation behavior, not merely the interpreter's presence, so this is not "reason names only bash" (convertible); but nothing here names a specific POSIX-only primitive either (not-convertible) -- reading the body narrows this to the templated majority rather than settling it |
| `tests/test_consolidate_read_race.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_consolidation_append_race.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_consolidation_retire_reopened_day_509.py` | bash subprocess + POSIX layout -- not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_consolidation_staging_mv_atomicity.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_consolidation_trigger_source_342.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_cross_host_lock_contract_491.py` | bash subprocess + POSIX process semantics (kill -0, mkdir races) -- not portable to Windows runners (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_delivery_record_per_machine_285.py` | bash hook subprocess + POSIX git semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_delivery_record_pruning_373.py` | bash subprocess + POSIX session-start hook -- not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_dispatch_timeout_286.py` | drives the real hooks through a bash subprocess — not portable to Windows (see tests/test_path_resolution.py) | not-convertible | cross-references test_path_resolution.py, whose own reason names an explicit POSIX-vs-Windows path-format incompatibility (/c/Users vs C:\Users) |
| `tests/test_doctor.py` | bash subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_cap_disabled_360.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_json_408.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_oversized_store_348.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_session_end_370.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_session_end_survives_backup_cleanup_401.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_timezone_357.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_verdict_ordering_359.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_doctor_verdict_ordering_404.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_env_cache_hook_cwd_key_469.py` | bash subprocess assertions -- not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_env_cache_probe_303.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_env_cache_publish_unwritable_logs_358.py` | chmod-based unwritability and bash hook subprocess are not portable to Windows runners; POSIX permission bits do not model an ACL-unwritable directory there the way they do here. | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_external_data_dir.py` | bash subprocess + POSIX session-start hook — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_git_backup_hook.py` | bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_git_backup_push_rejected_253.py` | bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_git_backup_silent_stops_257.py` | bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_git_restore_hook_253.py` | bash hook subprocess + POSIX flock/git semantics — not portable to Windows runners (#79) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_handoff_delivery_count_source_341.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_handoff_per_session_363.py` | bash subprocess + POSIX session-start hook — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_handoff_preservation.py` | bash subprocess + POSIX session-start hook — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_home_dir_migration.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_hook_cwd_end_to_end_411.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_hook_cwd_fallback_444.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_hook_stdout_labelling_280.py` | drives the real hooks through a bash subprocess — not portable to Windows (see tests/test_path_resolution.py) | not-convertible | cross-references test_path_resolution.py, whose own reason names an explicit POSIX-vs-Windows path-format incompatibility (/c/Users vs C:\Users) |
| `tests/test_host_shell_parity_407.py` | bash subprocess assertions -- not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_hot_path_cost_pin_330.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_jq_free_config.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_last_entry_read_failure_251.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_layered_config.py` | bash subprocess + POSIX lib-memory-dir.sh — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_legacy_logs_untracking_288.py` | bash hook subprocess + POSIX git semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_lock_primitive.py` | POSIX process semantics (kill -0, fork races) — the lock is exercised on ubuntu/macos runners | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_lock_timing.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_log_rotation.py` | bash subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_marker_range_guard_326.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_commit_atomicity.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_commit_lock.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_day_boundary.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_reject_gate.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_tail_failure_no_truncate.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_ndc_truncate_race.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_nested_summarizer.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_non_ascii_paths.py` | bash subprocess assertions — not portable to Windows runners (#79) | unclear | reason names only bash, but the test body runs the real sed function under several hostile locales (`LC_CTYPE=C.UTF-8`, `LC_ALL=C.utf8`, `LC_ALL=en_US.UTF-8`) whose availability and byte/character semantics under Git Bash/MSYS are not established here -- read on inspection, downgraded from the reason-string-only rule below |
| `tests/test_now_md_append_atomicity.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_path_resolution.py` | POSIX path layouts (/c/Users vs C:\Users) + bash subprocess assertions — not portable to Windows | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_plugin_root_validated_471.py` | bash subprocess assertions -- not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_position_sidecar_353.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_cooldown.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_fast_path_350.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_hook_spawns.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_hook_stdin_size.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_save_log_swept_527.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_post_tool_session_id.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_project_root_cwd_fallback_411.py` | bash subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_prompt_hook_spawns.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_prompt_stamp_301.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_provider_logging_461.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_recovery_transcript_leak_407.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_reject_gate_visibility.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_resolve_paths.py` | bash subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_rotated_archive_recall.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_save_session_branch_override.py` | bash dispatch + git command form -- not portable to Windows Git Bash without fixtures | convertible | reason itself frames the gap as missing fixtures under Git Bash, not an unfixable POSIX primitive |
| `tests/test_save_session_force_lock_race.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_save_session_gates.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_save_session_lock_ownership.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_save_session_marker_arithmetic_322.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_session_end_hook_345.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_session_end_log_swept_483.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_session_slug_record_294.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_session_start_compact_recap_339.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_session_start_prev_session_270.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_slug_index_297.py` | bash hook subprocess + POSIX semantics — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_slug_parity.py` | bash subprocess assertions — not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_slug_vectors_294.py` | bash subprocess assertions — not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker (same reason string as test_slug_parity.py above); module-level pytestmark here is a list of two skipif marks rather than a bare call, which an earlier version of the AST scanner missed entirely -- see scripts/windows_skip_triage_497.py's own docstring |
| `tests/test_staging_growth_warning_349.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_staging_lock.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_staging_lock_config_unread_399.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_staging_lock_missing_log_dependency_394.py` | bash subprocess + POSIX layout — not portable to Windows runners (#79) | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_stdin_source_top_level_wins_344.py` | bash hook subprocess + POSIX semantics -- not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
| `tests/test_umask.py` | POSIX umask + mode bits don't apply to NTFS (umask is a no-op on Windows) | not-convertible | names a specific POSIX-only primitive (permissions/signals/fork/flock/umask/NTFS/path-format) that Git-Bash discovery cannot supply |
| `tests/test_windows_drive_slug_263.py` | bash subprocess assertions — not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_windows_long_path_slug_294.py` | bash subprocess assertions — not portable to Windows runners (#79) | convertible | reason names only the bash-subprocess dependency, no other blocker |
| `tests/test_worktree_memory.py` | bash subprocess + POSIX git worktree layout — not portable to Windows runners | unclear | reason bundles the bash-subprocess dependency with an unspecified "POSIX semantics/layout" claim; cannot tell from the string alone whether that names a real non-bash blocker or is boilerplate |
