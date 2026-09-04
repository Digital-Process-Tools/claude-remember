"""Regression tests for #517: 7 further REMEMBER_DIR/PROJECT_DIR glob and
pattern-match sites, beyond the one #487/#488 (PR #499) already fixed in
scripts/save-session.sh's own retention sweep, that silently match nothing
on msys/cygwin because bash's own glob (`ls`, `rm -rf ... *`, a bare
`for ... in`) and its parameter-expansion pattern matching (`%/*`, `##*/`,
a `[[ == ]]` glob) both recognise ONLY '/' as a path-component separator,
never a backslash -- while REMEMBER_DIR/PROJECT_DIR arrive backslash-
separated there via resolve-paths.sh's own `_remember_normalize_win_path`.

Each site below now routes through one shared fix, `_remember_forward_slash`
(scripts/resolve-paths.sh, #517) -- extracted, forward-slashes its operand
gated on $OSTYPE, otherwise passes it through unchanged. This file tests
that helper's own gating logic directly (TestForwardSlashHelper) plus 6 of
the 7 call sites the issue names. The 7th (session-start-hook.sh's #373
delivery-record pruner) is tested by extending its own existing regression
file, tests/test_delivery_record_pruning_373.py, instead of duplicating
that fixture here, per the issue's own instruction.

Every site's test uses the SAME extracted-block technique
tests/test_autonomous_log_retention_487.py demonstrates for #487: real code
pulled verbatim out of its owning script and run standalone in a bash
subprocess, with REMEMBER_DIR (and, where relevant, PROJECT_DIR) handed a
SYNTHETIC backslash-laden string directly, $OSTYPE shadowed with a `local`
inside a wrapping function (not an environment-variable override -- #487's
own test found a real windows-latest runner's Git Bash resets $OSTYPE to
its own compiled default regardless of the parent environment, so only the
`local` shadow is reliable there). See tests/_glob_backslash_517.py's own
module docstring for why the full end-to-end hook cannot reproduce this
shape on a POSIX CI runner at all.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from _glob_backslash_517 import extract_function, extract_lines, run_block

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

_FORWARD_SLASH_FN = extract_function(
    "scripts/resolve-paths.sh", "_remember_forward_slash"
)


class TestForwardSlashHelper:
    """The shared mechanism every site below relies on -- tested once here
    rather than once per site, since the gating logic (msys/cygwin only, a
    no-op everywhere and on every other value) is identical at every call
    site and only needs proving once."""

    def test_must_fire_backslashes_become_forward_slashes_under_msys(self):
        block = r"""printf '%s' "$(_remember_forward_slash 'C:\Users\x\.remember')" """
        result = run_block(block, ostype="msys", setup=_FORWARD_SLASH_FN)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "C:/Users/x/.remember", (
            f"msys/cygwin must forward-slash a backslash-laden operand\n"
            f"stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_fire_cygwin_is_gated_the_same_as_msys(self):
        block = r"""printf '%s' "$(_remember_forward_slash 'C:\Users\x')" """
        result = run_block(block, ostype="cygwin", setup=_FORWARD_SLASH_FN)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "C:/Users/x"

    def test_must_not_fire_control_a_posix_backslash_named_value_is_untouched(self):
        """The gate's own reason to exist: a literal backslash is an
        ordinary, legal filename character on POSIX, and this helper must
        leave one alone there rather than mangling a real path."""
        block = r"""printf '%s' "$(_remember_forward_slash 'weird\name')" """
        result = run_block(block, ostype="linux-gnu", setup=_FORWARD_SLASH_FN)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "weird\\name", (
            "a non-msys/cygwin $OSTYPE must leave a literal backslash "
            f"untouched -- stdout={result.stdout!r}"
        )

    def test_must_fire_forward_slash_value_is_a_no_op_everywhere(self):
        """Positive control: an ordinary POSIX path is unaffected by this
        helper on any platform -- the normalization is a no-op there, not a
        new requirement."""
        block = r"""printf '%s' "$(_remember_forward_slash '/home/x/.remember')" """
        result = run_block(block, ostype="linux-gnu", setup=_FORWARD_SLASH_FN)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "/home/x/.remember"


class TestRunConsolidationSnapshotSweep:
    """Site 1: scripts/run-consolidation.sh's stale-snapshot cleanup."""

    _BLOCK = extract_lines(
        "scripts/run-consolidation.sh",
        "_remember_consolidate_glob_dir=$(_remember_forward_slash",
        'rm -rf "${_remember_consolidate_glob_dir}"',
    )

    def test_must_fire_stale_snapshot_is_removed_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        stale = remember_dir / "tmp" / "consolidate-snapshot-abc123"
        stale.mkdir(parents=True)
        (stale / "recent.md").write_text("leftover\n")

        windows_style = str(remember_dir).replace("/", "\\")
        setup = _FORWARD_SLASH_FN + "\nREMEMBER_DIR=" + repr(windows_style).replace('"', "'")
        result = run_block(self._BLOCK, ostype="msys", setup=setup)

        assert result.returncode == 0, result.stderr
        assert not stale.exists(), (
            "a backslash-separated REMEMBER_DIR must not defeat the stale "
            f"consolidate-snapshot sweep\nstderr={result.stderr}"
        )

    def test_must_fire_forward_slash_remember_dir_is_swept_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still works."""
        remember_dir = tmp_path / ".remember"
        stale = remember_dir / "tmp" / "consolidate-snapshot-def456"
        stale.mkdir(parents=True)

        setup = _FORWARD_SLASH_FN + "\nREMEMBER_DIR=" + repr(str(remember_dir)).replace('"', "'")
        result = run_block(self._BLOCK, ostype="", setup=setup)

        assert result.returncode == 0, result.stderr
        assert not stale.exists()


class TestSessionStartStagingCount:
    """Site 2: session-start-hook.sh's STAGING_COUNT, which gates the
    "N day(s) of memory to compress" message and the background
    consolidation trigger."""

    _BLOCK = extract_lines(
        "scripts/session-start-hook.sh",
        "_remember_staging_glob_dir=$(_remember_forward_slash",
        'STAGING_COUNT=$(ls "$_remember_staging_glob_dir/today-"',
    )

    def _run(self, remember_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + '\nTODAY="2099-01-01"'
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after='printf "%s" "$STAGING_COUNT"')

    def test_must_fire_backslash_remember_dir_counts_staging_files(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "today-2020-01-01.md").write_text("a\n")
        (remember_dir / "today-2020-01-02.md").write_text("b\n")
        (remember_dir / "today-2020-01-03.md.done.md").write_text("c\n")
        (remember_dir / "today-2099-01-01.md").write_text("today\n")

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "2", (
            "a backslash-separated REMEMBER_DIR must not defeat the "
            f"STAGING_COUNT glob -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_remember_dir_counts_staging_files_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still counts."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "today-2020-01-01.md").write_text("a\n")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "1"


class TestSessionStartRotatedSlices:
    """Site 3: session-start-hook.sh's ROTATED_SLICES, feeding HAS_MEMORY
    -- an unnormalized REMEMBER_DIR silently omits the session banner's own
    "=== MEMORY ===" section even when rotated slices genuinely exist."""

    _BLOCK = extract_lines(
        "scripts/session-start-hook.sh",
        "_remember_rotated_glob_dir=$(_remember_forward_slash",
        'ROTATED_SLICES=$(ls "$_remember_rotated_glob_dir"',
    )
    _AFTER = 'if [ -n "$ROTATED_SLICES" ]; then printf HAS; else printf NONE; fi'

    def _run(self, remember_dir, ostype):
        import shlex
        setup = _FORWARD_SLASH_FN + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after=self._AFTER)

    def test_must_fire_rotated_slices_are_seen_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "archive-2020-01-01.md").write_text("a\n")
        (remember_dir / "recent-2020-01-01.md").write_text("b\n")

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "HAS", (
            "a backslash-separated REMEMBER_DIR must not hide rotated "
            f"slices that genuinely exist -- stdout={result.stdout!r} "
            f"stderr={result.stderr}"
        )

    def test_must_not_fire_control_no_rotated_slices_reports_none(self, tmp_path):
        """Positive control: with none on disk, the glob genuinely matches
        nothing -- must not be confused with the bug being reintroduced."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "NONE"

    def test_must_fire_forward_slash_remember_dir_sees_rotated_slices_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still works."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "archive-2020-01-01.md").write_text("a\n")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "HAS"


class TestCaseDivergenceSplit:
    """Site 5: lib-case-divergence.sh's own REMEMBER_DIR%/*` / `##*/`
    split, plus the two guard comparisons #517 also had to fix alongside
    it (the "did the split find anything" self-check, and the #138
    in-project refusal) -- both now compare against a forward-slashed
    form on both sides, or they would silently misfire the same way the
    split itself did."""

    _BLOCK = extract_lines(
        "scripts/lib-case-divergence.sh",
        "local _remember_case_div_dir _root _name",
        '[ "$_root" != "$(_remember_forward_slash "${PROJECT_DIR:-}")" ] || return 0',
    )
    _AFTER = 'printf "REACHED:%s:%s" "$_root" "$_name"'

    def _run(self, remember_dir, project_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + f"\nPROJECT_DIR={shlex.quote(project_dir)}"
            + '\nREMEMBER_STORE_ROOT="non-empty"'
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after=self._AFTER)

    def test_must_fire_split_and_guards_work_under_backslash_paths(self):
        remember_dir = r"C:\Users\x\.claude\remember\proj-slug"
        project_dir = r"C:\Users\x\proj"  # NOT the store's parent
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REACHED:C:/Users/x/.claude/remember:proj-slug", (
            "a backslash-separated REMEMBER_DIR must still split into "
            "root/name, and the store's parent must still compare "
            f"correctly against PROJECT_DIR -- stdout={result.stdout!r} "
            f"stderr={result.stderr}"
        )

    def test_must_not_fire_control_in_project_store_is_still_refused_on_windows(self):
        """Positive control for the #138 refusal itself: when the store's
        parent genuinely IS the project (both backslash-laden, msys), the
        function must still bail rather than go looking inside the user's
        own repository."""
        remember_dir = r"C:\Users\x\.claude\remember\proj-slug"
        project_dir = r"C:\Users\x\.claude\remember"  # IS the store's parent
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", (
            "the #138 in-project refusal must still fire on a "
            f"backslash-separated pair -- stdout={result.stdout!r}"
        )

    def test_must_fire_forward_slash_paths_still_work(self):
        """Positive control: ordinary POSIX paths are unaffected."""
        remember_dir = "/home/x/.claude/remember/proj-slug"
        project_dir = "/home/x/proj"
        result = self._run(remember_dir, project_dir, "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REACHED:/home/x/.claude/remember:proj-slug"


class TestDoctorStorageModeJSON:
    """Site 6a: doctor.sh's JSON-mode storage-mode detection -- misreports
    an in-project store as "external" for a legacy store with a non-default
    relative data_dir, on a backslash-separated REMEMBER_DIR/PROJECT_DIR
    pair."""

    _BLOCK = extract_lines(
        "scripts/doctor.sh",
        'if [ "$REMEMBER_DIR" = "${PROJECT_DIR}/.remember" ]',
        "fi",
    )
    _AFTER = 'printf "%s" "$_JSON_STORAGE_MODE"'

    def _run(self, remember_dir, project_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + f"\nPROJECT_DIR={shlex.quote(project_dir)}"
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after=self._AFTER)

    def test_must_fire_in_project_store_reports_legacy_under_backslash_paths(self):
        # A store one directory below PROJECT_DIR but NOT the literal
        # default `${PROJECT_DIR}/.remember` -- e.g. a configured
        # relative data_dir -- so only the `[[ == ]]`-glob arm can catch
        # it, never the literal-equality arm beside it.
        project_dir = r"C:\Users\x\proj"
        remember_dir = r"C:\Users\x\proj\custom-store"
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "legacy", (
            "a backslash-separated in-project store must still report "
            f"legacy -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_not_fire_control_external_store_reports_external(self):
        """Positive control: a store outside PROJECT_DIR genuinely is
        external and must not be misreported the other way either."""
        project_dir = r"C:\Users\x\proj"
        remember_dir = r"C:\Users\x\elsewhere\store"
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "external"

    def test_must_fire_forward_slash_paths_still_report_legacy(self):
        """Positive control: ordinary POSIX paths are unaffected."""
        project_dir = "/home/x/proj"
        remember_dir = "/home/x/proj/custom-store"
        result = self._run(remember_dir, project_dir, "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "legacy"


class TestDoctorStorageModeHumanReadable:
    """Site 6b: doctor.sh's human-readable "-- Storage --" section -- same
    conditional as 6a, one call site over, printed directly to the
    operator."""

    _BLOCK = extract_lines(
        "scripts/doctor.sh",
        'if [ "$REMEMBER_DIR" = "${PROJECT_DIR}/.remember" ]',
        "fi",
        after_marker="the JSON-mode branch above for why",
    )

    def _run(self, remember_dir, project_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + f"\nPROJECT_DIR={shlex.quote(project_dir)}"
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup)

    def test_must_fire_in_project_store_reports_legacy_under_backslash_paths(self):
        project_dir = r"C:\Users\x\proj"
        remember_dir = r"C:\Users\x\proj\custom-store"
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert "Storage mode: legacy" in result.stdout, (
            "a backslash-separated in-project store must still report "
            f"legacy to the operator -- stdout={result.stdout!r} "
            f"stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_paths_still_report_legacy(self):
        """Positive control: ordinary POSIX paths are unaffected."""
        project_dir = "/home/x/proj"
        remember_dir = "/home/x/proj/custom-store"
        result = self._run(remember_dir, project_dir, "")

        assert result.returncode == 0, result.stderr
        assert "Storage mode: legacy" in result.stdout


class TestDoctorStagingBytes:
    """Site 7a: doctor.sh's _STAGING_BYTES -- silently undercounts to 0 on
    a backslash-separated REMEMBER_DIR, a number printed directly to the
    operator."""

    _SIZE_OF_FN = extract_function("scripts/doctor.sh", "_size_of")
    _BLOCK = extract_lines(
        "scripts/doctor.sh",
        "_remember_staging_bytes_glob_dir=$(_remember_forward_slash",
        "    done",  # the loop's own closing keyword, NOT the "*.done.md" case arm
    )

    def _run(self, remember_dir, ostype):
        import shlex
        setup = (
            _FORWARD_SLASH_FN
            + "\n"
            + self._SIZE_OF_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + '\n_DOCTOR_TODAY="2099-01-01"'
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after='printf "%s" "$_STAGING_BYTES"')

    def test_must_fire_staging_bytes_are_counted_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "today-2020-01-01.md").write_text("12345")  # 5 bytes
        (remember_dir / "today-2020-01-02.md").write_text("123")  # 3 bytes
        (remember_dir / "today-2020-01-03.md.done.md").write_text("ignored-done")
        (remember_dir / "today-2099-01-01.md").write_text("ignored-today")

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "8", (
            "a backslash-separated REMEMBER_DIR must not undercount "
            f"_STAGING_BYTES -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_remember_dir_is_counted_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still counts."""
        remember_dir = tmp_path / ".remember"
        remember_dir.mkdir()
        (remember_dir / "today-2020-01-01.md").write_text("12345")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "5"


class TestDoctorRotationPendingCount:
    """Site 7b: doctor.sh's $_RT_PENDING -- same undercount-to-0 shape as
    _STAGING_BYTES, one call site over, also printed directly to the
    operator (as part of the log-rotation-failure warning)."""

    _BLOCK = extract_lines(
        "scripts/doctor.sh",
        "_remember_rt_pending_glob_dir=$(_remember_forward_slash",
        "done",
    )

    def _run(self, remember_dir, ostype):
        import shlex
        setup = _FORWARD_SLASH_FN + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after='printf "%s" "$_RT_PENDING"')

    def test_must_fire_pending_logs_are_counted_under_backslash_remember_dir(self, tmp_path):
        remember_dir = tmp_path / ".remember"
        logs = remember_dir / "logs"
        logs.mkdir(parents=True)
        (logs / "memory-2020-01-01.log").write_text("a\n")
        (logs / "memory-2020-01-02.log").write_text("b\n")

        windows_style = str(remember_dir).replace("/", "\\")
        result = self._run(windows_style, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "2", (
            "a backslash-separated REMEMBER_DIR must not undercount "
            f"$_RT_PENDING -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_remember_dir_is_counted_too(self, tmp_path):
        """Positive control: an ordinary POSIX REMEMBER_DIR still counts."""
        remember_dir = tmp_path / ".remember"
        logs = remember_dir / "logs"
        logs.mkdir(parents=True)
        (logs / "memory-2020-01-01.log").write_text("a\n")

        result = self._run(str(remember_dir), "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "1"
