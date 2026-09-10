"""Five more `> FILE 2>/dev/null`-after-a-failing-`>` sites leak bash's raw
diagnostic, unfixed by #635 (#643).

#635 fixed exactly two of these writes (COOLDOWN_MARKER's and NDC_MARKER's
guarded write, both now wrapped `{ CMD > FILE; } 2>/dev/null`), leaving five
more statements in scripts/save-session.sh with the identical shape:
`2>/dev/null` placed AFTER a `>` redirection only takes effect once that
redirection has already succeeded, so a FAILING `>` (target is a directory,
a permission is denied, etc.) is reported by bash to the real stderr before
`2>/dev/null` is ever applied -- leaking bash's own raw diagnostic rather
than being silenced by it. This is the same defect #635 fixed, at five
sites #635 did not touch:

  * scripts/save-session.sh -- COOLDOWN_MARKER's self-heal write (the
    ELAPSED<0 branch of the save cooldown gate -- distinct from the
    guarded write #635 fixed a few lines below it, which is reached via
    a DIFFERENT branch of the same `if`)
  * scripts/save-session.sh -- NDC_MARKER's self-heal write (same shape,
    the ELAPSED<0 branch of the NDC cooldown gate)
  * scripts/save-session.sh -- NOW_DAY_FILE stamped when now.md starts
    fresh
  * scripts/save-session.sh -- NOW_DAY_FILE re-stamped after an NDC
    compression that kept bytes
  * scripts/save-session.sh -- the NDC_TAIL truncate-and-copy `tail`
    write, guarded by an `if ... ; then ... else ...` rather than `||`,
    but the same `>` then `2>/dev/null` ordering

Grepped for the exact shape via `grep -n '> "\\$[A-Za-z_]*" 2>/dev/null'
scripts/save-session.sh` -- returns exactly these five lines (plus the two
already-fixed `{ ...; }`-wrapped sites, which the regex naturally excludes
since a `{` line does not match the shape at all). Each site is extracted
by content (not line number, which shifts under review-cycle edits) and run
in isolation, mirroring the extraction technique
tests/test_ndc_read_gen_fifo_634.py and tests/test_now_day_file_fifo_642.py
already use, with the fix verified end-to-end against a directory standing
in for the target file -- the same technique
tests/test_marker_write_stderr_order_635.py itself uses, since `date +%s >
DIRECTORY` and `printf ... > DIRECTORY` both fail the same way a
permission-denied write does ("... : Is a directory"), reliably and without
depending on this process's uid ever being denied a permission bit.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "save-session.sh"

RAW_SHELL_DIAGNOSTIC = "Is a directory"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _script_text(), re.MULTILINE)
    assert match, f"{label} not found in scripts/save-session.sh -- extraction regex is stale"
    return match.group(0)


def _run_statement(statement: str, env_extra: dict) -> subprocess.CompletedProcess:
    """Run one extracted statement in isolation, with $TARGET set to either a
    directory (forces the `>` to fail) or a real writable file (proves the
    statement still does its job when nothing is wrong)."""
    script = statement + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_file = f.name
    try:
        env = dict(os.environ)
        env.update(env_extra)
        return subprocess.run(
            ["bash", script_file], capture_output=True, text=True, timeout=10,
            env=env, check=False,
        )
    finally:
        os.unlink(script_file)


# Matches both the pre-fix `date +%s > "$X" 2>/dev/null || true` shape and
# the post-fix `{ date +%s > "$X"; } 2>/dev/null || true` shape, so the same
# extraction works red (before the fix) and green (after it).
SELF_HEAL_PATTERN = (
    r'^\s*\{?\s*date \+%%s > "\$%s"\s*;?\s*\}?\s*2>/dev/null \|\| true\s*$'
)
NOW_DAY_PATTERN = (
    r'^\s*\{?\s*printf \'%%s\\n\' %s\s*> "\$NOW_DAY_FILE"\s*;?\s*\}?\s*2>/dev/null \|\| true\s*$'
)
NDC_TAIL_PATTERN = (
    r'^\s*if \{?\s*tail -c \+\$\(\( NDC_SRC_BYTES \+ 1 \)\) "\$MEMORY_FILE"'
    r' > "\$NDC_TAIL"\s*;?\s*\}?\s*2>/dev/null; then$'
)


class TestCooldownMarkerSelfHealWrite:
    """scripts/save-session.sh -- the ELAPSED<0 self-heal write for
    COOLDOWN_MARKER, distinct from the guarded write #635 already fixed a
    few lines further down (that one ends `|| report_error ...`, this one
    ends `|| true`)."""

    def _statement(self) -> str:
        return _extract(
            SELF_HEAL_PATTERN % "COOLDOWN_MARKER",
            "COOLDOWN_MARKER self-heal write",
        )

    def test_a_directory_target_does_not_leak_the_raw_diagnostic(self, tmp_path):
        target = tmp_path / "cooldown-marker"
        target.mkdir()
        proc = _run_statement(self._statement(), {"COOLDOWN_MARKER": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"raw shell diagnostic leaked for COOLDOWN_MARKER's self-heal "
            f"write: stderr={proc.stderr!r}"
        )

    def test_a_writable_target_still_gets_the_new_timestamp(self, tmp_path):
        """Positive control: the fix must not turn the write itself into a
        no-op -- a stub harness that silenced everything would also pass
        the negative assertion above."""
        target = tmp_path / "cooldown-marker"
        proc = _run_statement(self._statement(), {"COOLDOWN_MARKER": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert target.is_file() and target.read_text().strip().isdigit(), (
            f"the write itself must still succeed against a normal target, "
            f"got: exists={target.exists()} content={target.read_text() if target.exists() else None!r}"
        )


class TestNdcMarkerSelfHealWrite:
    """Same shape, NDC_MARKER's own ELAPSED<0 self-heal write."""

    def _statement(self) -> str:
        return _extract(
            SELF_HEAL_PATTERN % "NDC_MARKER",
            "NDC_MARKER self-heal write",
        )

    def test_a_directory_target_does_not_leak_the_raw_diagnostic(self, tmp_path):
        target = tmp_path / "ndc-marker"
        target.mkdir()
        proc = _run_statement(self._statement(), {"NDC_MARKER": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"raw shell diagnostic leaked for NDC_MARKER's self-heal write: "
            f"stderr={proc.stderr!r}"
        )

    def test_a_writable_target_still_gets_the_new_timestamp(self, tmp_path):
        target = tmp_path / "ndc-marker"
        proc = _run_statement(self._statement(), {"NDC_MARKER": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert target.is_file() and target.read_text().strip().isdigit(), (
            f"the write itself must still succeed against a normal target, "
            f"got: exists={target.exists()} content={target.read_text() if target.exists() else None!r}"
        )


class TestNowDayFileFreshStampWrite:
    """scripts/save-session.sh -- NOW_DAY_FILE stamped when now.md is empty
    (a fresh session)."""

    def _statement(self) -> str:
        return _extract(
            NOW_DAY_PATTERN % r'"\$TODAY_DATE"',
            "NOW_DAY_FILE fresh-stamp write",
        )

    def test_a_directory_target_does_not_leak_the_raw_diagnostic(self, tmp_path):
        target = tmp_path / "now-day"
        target.mkdir()
        proc = _run_statement(self._statement(), {"NOW_DAY_FILE": str(target), "TODAY_DATE": "2026-01-01"})
        assert proc.returncode == 0, proc.stderr
        assert RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"raw shell diagnostic leaked for NOW_DAY_FILE's fresh-stamp "
            f"write: stderr={proc.stderr!r}"
        )

    def test_a_writable_target_still_gets_stamped(self, tmp_path):
        target = tmp_path / "now-day"
        proc = _run_statement(self._statement(), {"NOW_DAY_FILE": str(target), "TODAY_DATE": "2026-01-01"})
        assert proc.returncode == 0, proc.stderr
        assert target.is_file() and target.read_text().strip() == "2026-01-01", (
            f"the write itself must still succeed against a normal target, "
            f"got: {target.read_text() if target.exists() else None!r}"
        )


class TestNowDayFileRestampWrite:
    """scripts/save-session.sh -- NOW_DAY_FILE re-stamped after an NDC
    compression that kept bytes."""

    def _statement(self) -> str:
        return _extract(
            NOW_DAY_PATTERN % r'"\$\(_remember_date \+%Y-%m-%d\)"',
            "NOW_DAY_FILE restamp write",
        )

    def _env(self, target: Path) -> dict:
        # _remember_date is a function this repo's lib provides; stub it so
        # the extracted statement can run standalone.
        return {"NOW_DAY_FILE": str(target)}

    def _statement_with_stub(self) -> str:
        return '_remember_date() { printf "%s" "2026-02-02"; }\n' + self._statement()

    def test_a_directory_target_does_not_leak_the_raw_diagnostic(self, tmp_path):
        target = tmp_path / "now-day"
        target.mkdir()
        proc = _run_statement(self._statement_with_stub(), self._env(target))
        assert proc.returncode == 0, proc.stderr
        assert RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"raw shell diagnostic leaked for NOW_DAY_FILE's restamp write: "
            f"stderr={proc.stderr!r}"
        )

    def test_a_writable_target_still_gets_restamped(self, tmp_path):
        target = tmp_path / "now-day"
        proc = _run_statement(self._statement_with_stub(), self._env(target))
        assert proc.returncode == 0, proc.stderr
        assert target.is_file() and target.read_text().strip() == "2026-02-02", (
            f"the write itself must still succeed against a normal target, "
            f"got: {target.read_text() if target.exists() else None!r}"
        )


class TestNdcTailTruncateWrite:
    """scripts/save-session.sh -- the NDC_TAIL truncate-and-copy `tail`
    write inside the NDC compression commit. Guarded by an `if ...; then
    ... else ...` rather than `||`, but the same `>` then `2>/dev/null`
    ordering: a failing `>` leaks before `2>/dev/null` takes effect."""

    def _statement(self) -> str:
        return _extract(NDC_TAIL_PATTERN, "NDC_TAIL truncate write")

    def test_a_directory_target_does_not_leak_the_raw_diagnostic(self, tmp_path):
        memory_file = tmp_path / "now.md"
        memory_file.write_text("some content that is long enough\n")
        target = tmp_path / "ndc-tail"
        target.mkdir()
        script = (
            self._statement()
            + '\n  echo TOOK_THEN; else\n  echo TOOK_ELSE; fi\n'
        )
        proc = _run_statement(
            script,
            {"MEMORY_FILE": str(memory_file), "NDC_TAIL": str(target), "NDC_SRC_BYTES": "0"},
        )
        assert proc.returncode == 0, proc.stderr
        assert "TOOK_ELSE" in proc.stdout, (
            f"a directory target must take the failure branch, got stdout="
            f"{proc.stdout!r}"
        )
        assert RAW_SHELL_DIAGNOSTIC not in proc.stderr, (
            f"raw shell diagnostic leaked for the NDC_TAIL truncate write: "
            f"stderr={proc.stderr!r}"
        )

    def test_a_writable_target_still_gets_the_tail_copy(self, tmp_path):
        memory_file = tmp_path / "now.md"
        memory_file.write_text("0123456789\n")
        target = tmp_path / "ndc-tail"
        script = (
            self._statement()
            + '\n  echo TOOK_THEN; else\n  echo TOOK_ELSE; fi\n'
        )
        proc = _run_statement(
            script,
            {"MEMORY_FILE": str(memory_file), "NDC_TAIL": str(target), "NDC_SRC_BYTES": "0"},
        )
        assert proc.returncode == 0, proc.stderr
        assert "TOOK_THEN" in proc.stdout, (
            f"a normal target must take the success branch, got stdout="
            f"{proc.stdout!r}"
        )
        assert target.is_file() and target.read_text() == "0123456789\n", (
            f"the write itself must still succeed against a normal target, "
            f"got: {target.read_text() if target.exists() else None!r}"
        )
