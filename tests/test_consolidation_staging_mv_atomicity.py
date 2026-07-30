"""The consolidation staging-tail mv must be an atomic rename, and must not lie
when it is not (#246).

`run-consolidation.sh`'s retire loop splits a staging file that grew past its
consolidated offset and keeps the unconsolidated tail:

    staging_tail=$(mktemp "${TMPDIR:-/tmp}"/remember-staging-tail-XXXXXX)   # 198
    if head -c "$staging_consumed" "$staging_path" > "$staging_done" &&
       tail -c +$(( staging_consumed + 1 )) "$staging_path" > "$staging_tail"; then
        mv "$staging_tail" "$staging_path"                                  # 201

This is #242's class, one file over: the temp lives under `$TMPDIR` while the
destination (`staging_path`, a `today-*.md` file) lives in `REMEMBER_DIR` — a
different filesystem in the ordinary cases (tmpfs `/tmp`, devcontainers, WSL
under `/mnt/c`, external `data_dir`). Across filesystems `mv` is
copy-then-unlink, not `rename(2)`, and a failure partway leaves `staging_path`
destroyed or truncated rather than "old or new". The #245 precedent
(`save-session.sh`'s NDC commit) fixed exactly this shape by moving the temp
beside the target and gating on the mv's result; this applies the same
pattern here.

Lines 205 and 208 (`mv "$staging_path" "$staging_done"`) are same-directory
renames and cannot half-complete, but were also unchecked — a failure there
used to be swallowed silently rather than logged, which the fix also closes
(see `TestConsolidationRetireMvLogged`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from .test_consolidation_append_race import _make_env, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

# Intercepts ONLY the mv whose destination is STUB_MV_TARGET. Modeled on
# test_ndc_commit_atomicity.py's stub — see that file for the xdev/fail
# semantics.
MV_STUB = r"""#!/bin/sh
_last=""
_prev=""
for _a in "$@"; do _prev="$_last"; _last="$_a"; done

if [ -n "$STUB_MV_TARGET" ] && [ "$_last" = "$STUB_MV_TARGET" ]; then
    printf '%s\t%s\n' "$_prev" "$_last" >> "$STUB_MV_LOG"
    [ -n "$STUB_MV_SNAP_BEFORE" ] && cp "$_last" "$STUB_MV_SNAP_BEFORE" 2>/dev/null
    [ -n "$STUB_MV_SNAP_SRC" ] && cp "$_prev" "$STUB_MV_SNAP_SRC" 2>/dev/null

    case "$STUB_MV_MODE" in
        fail)
            exit 1
            ;;
        xdev)
            if [ "$(dirname "$_prev")" != "$(dirname "$_last")" ]; then
                : > "$_last"
                head -c 8 "$_prev" >> "$_last" 2>/dev/null
                exit 1
            fi
            ;;
    esac
fi
command -p mv "$@"
"""


def _with_mv_stub(env, tmp_path, mode, target):
    bindir = tmp_path / "mv-stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "mv"
    stub.write_text(MV_STUB, encoding="utf-8")
    stub.chmod(0o755)

    # Isolated TMPDIR so "did the fallback leak into TMPDIR" is answerable at
    # all — the shared system temp accumulates other runs' files.
    isolated_tmp = tmp_path / "isolated-tmp"
    isolated_tmp.mkdir(exist_ok=True)

    env = dict(env)
    env["TMPDIR"] = str(isolated_tmp)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_MV_MODE"] = mode
    env["STUB_MV_TARGET"] = str(target)
    env["STUB_MV_LOG"] = str(tmp_path / "mv-calls.tsv")
    env["STUB_MV_SNAP_BEFORE"] = str(tmp_path / "snap-before.md")
    env["STUB_MV_SNAP_SRC"] = str(tmp_path / "snap-src.md")
    return env


def _mv_calls(tmp_path):
    log = tmp_path / "mv-calls.tsv"
    if not log.is_file():
        return []
    return [line.split("\t") for line in log.read_text().splitlines() if "\t" in line]


def _logs(project):
    files = list((project / ".remember" / "logs").glob("*.log"))
    return "\n".join(f.read_text() for f in files)


APPENDED = "\n## 23:59 | main\n\n- landed during consolidation\n"


class TestConsolidationStagingMvAtomicity:

    def test_tail_temp_is_a_sibling_of_staging_path(self, tmp_path):
        """#246: the tail temp must live beside staging_path so the mv is a
        rename(2), not a copy-then-unlink."""
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED
        env = _with_mv_stub(env, tmp_path, "passthrough", staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        calls = _mv_calls(tmp_path)
        assert calls, (
            "the staging-tail mv never ran — no mv targeted the staging file, "
            "so this test proves nothing about where the temp lives"
        )
        src, dst = calls[-1]
        assert os.path.dirname(src) == os.path.dirname(dst), (
            "the staging-tail temp must be a sibling of staging_path so the mv "
            "is a rename(2), not a copy-then-unlink. #242's safety argument is "
            "'mv-over is atomic on the same filesystem'; a temp under $TMPDIR "
            f"makes that false wherever $TMPDIR is a different filesystem. src={src!r} dst={dst!r}"
        )

    def test_staging_path_is_never_partial_when_the_mv_fails(self, tmp_path):
        """#246: a failed tail-mv must leave staging_path wholly old or wholly new."""
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED
        env = _with_mv_stub(env, tmp_path, "xdev", staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        snap_before = tmp_path / "snap-before.md"
        snap_src = tmp_path / "snap-src.md"
        assert snap_before.is_file(), (
            "the mv stub never saw a commit targeting the staging file — the "
            "run did not reach the tail mv, so this test proves nothing"
        )
        old = snap_before.read_bytes()
        new = snap_src.read_bytes() if snap_src.is_file() else None

        assert staging.exists(), (
            "the staging file does not exist after a failed tail mv. A "
            "cross-filesystem mv opens the destination with O_TRUNC and "
            "copies; BSD mv then unlinks the partial destination outright — "
            "the entry appended during consolidation is gone entirely (#242's "
            "reproduction, one file over)."
        )
        after = staging.read_bytes()
        assert after in (old, new), (
            "staging_path is neither the content it held before the tail mv "
            "nor the tail content the mv was installing — it is a fragment of "
            "a copy that failed partway. This is memory appended during a live "
            "consolidation, silently lost. "
            f"len(before)={len(old)} len(intended)={len(new) if new is not None else '?'} "
            f"len(after)={len(after)} after={after[:120]!r}"
        )

    def test_failed_mv_is_logged_and_not_reported_as_kept(self, tmp_path):
        """A silent failure here is worse than the failure — the log must not
        claim the tail was kept when the mv never landed."""
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED
        env = _with_mv_stub(env, tmp_path, "fail", staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        log_text = _logs(project)
        assert "ERROR" in log_text and "today-2026-07-24.md" in log_text, (
            f"expected an error log naming the failed tail mv, got: {log_text!r}"
        )
        assert "appended to today-2026-07-24.md during consolidation" not in log_text, (
            "the run logged the success line for a tail mv that failed — "
            f"memory misfiled, log reads clean. got: {log_text!r}"
        )

    def test_failed_mv_leaves_the_file_for_the_next_run_to_retry(self, tmp_path):
        """#246 judgment: on failure, do not retire — leave staging_path so the
        next consolidation run picks it up again instead of losing the tail or
        wrongly marking it done."""
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED
        env = _with_mv_stub(env, tmp_path, "fail", staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        assert staging.exists(), (
            "staging_path must survive a failed tail mv so the next run can "
            "retry the split instead of silently losing the appended entry"
        )
        assert "landed during consolidation" in staging.read_text(encoding="utf-8"), (
            "the appended entry must still be present in staging_path after a "
            "failed mv — it was never actually moved anywhere"
        )
        done = remember / "today-2026-07-24.done.md"
        assert not done.exists() or "landed during consolidation" not in done.read_text(encoding="utf-8"), (
            "the appended entry must not appear inside .done.md — that would "
            "mean it was wrongly sealed away despite the mv having failed"
        )

    def test_failed_mv_leaves_no_stray_temp(self, tmp_path):
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED
        env = _with_mv_stub(env, tmp_path, "fail", staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        beside = sorted(p.name for p in remember.glob(f"{staging.name}.tail-*"))
        in_tmpdir = sorted(
            p.name for p in Path(env.get("TMPDIR", "/tmp")).glob("remember-staging-tail-*")
        )
        assert not beside and not in_tmpdir, (
            f"a failed tail mv orphaned its temp. beside={beside} in_tmpdir={in_tmpdir}"
        )


class TestConsolidationRetireMvLogged:
    """#246: the same-directory retire renames (lines 205, 208) were unchecked.
    They cannot half-complete, but a failure used to be silently swallowed and
    the run reported it as though the file had been retired."""

    def test_a_failed_retire_is_logged_not_silently_swallowed(self, tmp_path):
        env, project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
        # No STUB_APPEND_DURING_CONSOLIDATION — nothing appended, so the plain
        # retire (`mv "$staging_path" "$staging_done"`, line 208) runs rather
        # than the tail-keep branch.
        done_target = remember / "today-2026-07-24.done.md"
        env = _with_mv_stub(env, tmp_path, "fail", done_target)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        log_text = _logs(project)
        assert "ERROR" in log_text and "today-2026-07-24.md" in log_text, (
            f"a failed retire mv must be logged as an error, got: {log_text!r}"
        )
        assert staging.exists(), (
            "staging_path must still exist after a failed retire — a "
            "same-directory rename that fails leaves the source untouched, "
            "and the next run should see it and retry"
        )
