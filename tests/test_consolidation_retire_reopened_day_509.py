"""A retired day re-opened by NDC must not have its earlier retired span
silently destroyed when it is retired a second time (#509).

`run-consolidation.sh`'s retire loop turns a `today-YYYY-MM-DD.md` staging
file into `today-YYYY-MM-DD.done.md`. `recent.md`/`archive.md` already merged
both spans by the time retirement runs, so this is not memory-injection loss
-- but the `.done.md` layer is the "searchable, not injected" hourly detail
the README promises, and a plain `mv`/`head -c ... >` over an EXISTING
`.done.md` truncates it, with no log line marking the loss.

This can happen for real: a long-running session spanning midnight, or NDC
re-creating a `today-<day>.md` for a day that was already retired. The second
consolidation of that day must not erase the first.
"""

from __future__ import annotations

import os
import sys

import pytest

from .test_consolidation_append_race import _make_env, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout -- not portable to Windows runners (#79)",
)


class TestRetireReopenedDay509:
    def test_retiring_a_reopened_day_appends_instead_of_overwriting(self, tmp_path):
        """The reported bug: retire the same day twice, first span must survive."""
        env, _project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text(
            "# Day\n\n## 10:00 | main\n\n- first span work\n", encoding="utf-8"
        )

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        done = remember / "today-2026-07-24.done.md"
        assert done.is_file(), "first consolidation did not retire the day at all"
        first_span_content = done.read_text(encoding="utf-8")
        assert "first span work" in first_span_content

        # NDC re-opens the day: a new today-2026-07-24.md staging file,
        # written by a session spanning midnight or otherwise resuming work
        # on an already-retired day.
        staging.write_text(
            "# Day\n\n## 23:30 | main\n\n- second span work\n", encoding="utf-8"
        )

        result2 = _run(plugin, env)
        assert result2.returncode == 0, result2.stderr

        final_content = done.read_text(encoding="utf-8")
        assert "first span work" in final_content, (
            "the first retired span was destroyed by the second retirement -- "
            f"got: {final_content!r}"
        )
        assert "second span work" in final_content, (
            "the second span was not retired into the .done.md at all -- "
            f"got: {final_content!r}"
        )

    def test_retiring_a_never_retired_day_still_works_normally(self, tmp_path):
        """Positive control: a day retired for the first time is unaffected."""
        env, _project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-25.md"
        staging.write_text(
            "# Day\n\n## 09:00 | main\n\n- only span\n", encoding="utf-8"
        )

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        assert not staging.exists(), "the file should have been retired away"
        done = remember / "today-2026-07-25.done.md"
        assert done.is_file() and "only span" in done.read_text(encoding="utf-8")

    def test_retiring_a_reopened_day_with_a_concurrent_append_still_preserves_the_first_span(
        self, tmp_path
    ):
        """Same bug, but through the head -c ... > branch: a save lands during
        the SECOND consolidation of a re-opened day (staging_now > consumed),
        so the retire loop takes the partial-prefix-write path rather than the
        plain mv. That path truncated too and needs the same guard."""
        env, _project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text(
            "# Day\n\n## 10:00 | main\n\n- first span work\n", encoding="utf-8"
        )

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr
        done = remember / "today-2026-07-24.done.md"
        assert "first span work" in done.read_text(encoding="utf-8")

        staging.write_text(
            "# Day\n\n## 23:30 | main\n\n- second span work\n", encoding="utf-8"
        )
        env["STUB_APPEND_DURING_CONSOLIDATION"] = (
            "\n## 23:59 | main\n\n- landed during consolidation\n"
        )

        result2 = _run(plugin, env)
        assert result2.returncode == 0, result2.stderr

        final_content = done.read_text(encoding="utf-8")
        assert "first span work" in final_content, (
            "the first retired span was destroyed by the second (partial-write) "
            f"retirement -- got: {final_content!r}"
        )
        assert "second span work" in final_content, (
            f"the second span's consumed prefix was not retired -- got: {final_content!r}"
        )

        live = "".join(
            p.read_text(encoding="utf-8")
            for p in remember.glob("today-*.md")
            if not p.name.endswith(".done.md")
        )
        assert "landed during consolidation" in live, (
            "the entry appended during the second consolidation was sealed away "
            f"instead of kept as the live tail -- got: {live!r}"
        )


# `tail` fails ONLY when invoked with `-c +N` against TAIL_STUB_TARGET, so the
# retire loop's own tail extraction (`tail -c +$(( ... ))`) is the sole call
# intercepted; every other `tail` invocation in the script (or its sourced
# libraries) passes straight through to the real binary.
TAIL_STUB = r"""#!/bin/sh
_target="$TAIL_STUB_TARGET"
_last=""
for _a in "$@"; do _last="$_a"; done
case "$1" in
    -c)
        if [ -n "$_target" ] && [ "$_last" = "$_target" ]; then
            exit 1
        fi
        ;;
esac
command -p tail "$@"
"""


class TestConsolidationRetireTailFailureDoesNotDuplicate509:
    def test_a_tail_extraction_failure_does_not_duplicate_the_consumed_prefix(
        self, tmp_path
    ):
        """Explore's #509 self-review finding: an earlier draft of the fix
        wrote the consumed prefix straight into staging_done via
        retire_prefix_into, BEFORE the `tail` extraction below it was known
        to succeed. When `tail` then failed, the whole-file fallback
        (retire_whole_into on the original staging_path) re-committed the
        SAME prefix on top of the one already durably sitting in
        staging_done -- doubling it, unconditionally, on the very first
        attempt. Reproduced here by forcing the retire loop's own `tail -c`
        call to fail while `head -c` (the prefix extraction) still succeeds."""
        env, _project, plugin, remember = _make_env(tmp_path)
        staging = remember / "today-2026-07-24.md"
        staging.write_text(
            "# Day\n\n## 10:00 | main\n\n- first span work\n", encoding="utf-8"
        )
        env["STUB_APPEND_DURING_CONSOLIDATION"] = (
            "\n## 23:59 | main\n\n- landed during consolidation\n"
        )

        bindir = tmp_path / "tail-stub-bin"
        bindir.mkdir()
        stub = bindir / "tail"
        stub.write_text(TAIL_STUB, encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["TAIL_STUB_TARGET"] = str(staging)

        result = _run(plugin, env)
        assert result.returncode == 0, result.stderr

        done = remember / "today-2026-07-24.done.md"
        assert done.is_file(), (
            "the tail failure must still fall through to the whole-file "
            f"retire fallback, not abandon the file: {result.stderr}"
        )
        content = done.read_text(encoding="utf-8")
        assert content.count("first span work") == 1, (
            "the consumed prefix was committed to staging_done more than once "
            f"-- duplicated by the tail-failure fallback. got: {content!r}"
        )
        assert content.count("landed during consolidation") == 1, (
            f"the whole-file fallback did not retire the full file exactly "
            f"once: {content!r}"
        )
