"""thresholds.session_start_max_bytes: a total SessionStart budget (#842).

Claude Code (observed on 2.1.278) persists hook stdout longer than roughly
10,000 characters to a file and hands the model only a short preview plus the
file's path. An ordinary SessionStart on a healthy store (six small files,
each individually well under thresholds.memory_inject_max_bytes) can still
sum past that line -- the per-file cap cannot help, because it has no notion
of a TOTAL across files. This is request 1 of #842: fill handoff -> now ->
recent -> today -> archive in that priority order, and list by name and size
whatever does not fit, exactly like the source=compact path already does for
its own deferred files.

TDD bar for every "must not fit" assertion here: would it still pass if the
budget were simply never applied? Paired with a positive control proving the
same section IS injected when the budget is not tight.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

FROZEN_TODAY = "2099-05-17"
TODAY_FILE = "today-" + FROZEN_TODAY + ".md"


def _shim_date(bindir, today):
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f"  echo {today}\n"
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _store(tmp_path, bodies, config=None):
    """features.plugin_promos is forced off: the promo banner wraps the
    whole hook body in a JSON envelope (hookSpecificOutput/systemMessage)
    whenever it fires, which is unrelated to #842 and would make a total-
    byte-budget assertion depend on whether a promo happened to be shown."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    for name, body in bodies.items():
        (remember / name).write_text(body, encoding="utf-8")
    merged = {"features": {"plugin_promos": False}}
    if config is not None:
        for key, val in config.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **val}
            else:
                merged[key] = val
    (remember / "config.json").write_text(json.dumps(merged), encoding="utf-8")
    return home, project, remember


def _env(home, project, remember, bindir):
    """Deliberately does NOT set REMEMBER_DIR or _LIB_MEMORY_DIR_LOADED --
    those shortcut the real lib-memory-dir.sh resolution (as the #339
    compact-recap fixture does, for speed) and, as a side effect, skip the
    three-layer config MERGE entirely, which is exactly what this file's
    config-driven tests need to exercise for real. Letting PROJECT_DIR ->
    REMEMBER_DIR resolve normally (default data_dir=".remember") still
    lands on the same path `_store` already wrote the fixture files to."""
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_NO_PRINTF_T": "1",
        # #843 (split off #842): the flattened-config cache is not reliably
        # scoped per REMEMBER_DIR across runs that share one $TMPDIR, which
        # is exactly this test process -- the documented workaround keeps
        # that known, separately-tracked issue from leaking into these
        # assertions rather than papering over it here.
        "REMEMBER_CONFIG_CACHE": "0",
    }
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    return env


def _run(tmp_path, bodies, config=None, source="startup"):
    home, project, remember = _store(tmp_path, bodies, config)
    bindir = tmp_path / "bin"
    _shim_date(bindir, FROZEN_TODAY)
    payload = json.dumps({
        "session_id": "aaaaaaaa-0000-4000-8000-000000000842",
        "transcript_path": "/does/not/matter/842.jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
        "source": source,
    })
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(home, project, remember, bindir),
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, remember


SMALL_BODIES = {
    "identity.md": "IDENTITY-842\n",
    "core-memories.md": "CORE-842\n",
    TODAY_FILE: "TODAY-BODY-842\n",
    "now.md": "NOW-BODY-842\n",
    "recent.md": "RECENT-BODY-842\n",
    "archive.md": "ARCHIVE-BODY-842\n",
}


class TestUnderBudgetEverythingSurvives:
    """Positive control for the whole feature: a healthy small store must
    read exactly as it always has."""

    def test_small_store_injects_every_section_in_full(self, tmp_path):
        out, _ = _run(tmp_path, SMALL_BODIES)
        for body in SMALL_BODIES.values():
            assert body.strip() in out, f"{body!r} missing from an under-budget render"
        assert "over thresholds.session_start_max_bytes" not in out


class TestOverBudgetDropsLowestPriorityFirst:

    def _oversized_bodies(self, filler_bytes=4000):
        filler = "X" * filler_bytes
        return {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: "TODAY-" + filler + "\n",
            "now.md": "NOW-" + filler + "\n",
            "recent.md": "RECENT-" + filler + "\n",
            "archive.md": "ARCHIVE-" + filler + "\n",
        }

    def test_archive_is_dropped_before_anything_else(self, tmp_path):
        """Four ~4KB files plus handoff/legend overhead comfortably exceeds
        the default 9000-byte budget, but not by so much that every file
        needs to go -- archive.md (the lowest priority) should be the one
        that gives."""
        bodies = self._oversized_bodies(filler_bytes=2200)
        out, remember = _run(tmp_path, bodies)

        assert "NOW-" in out, f"now.md (highest memory priority) was dropped.\noutput tail: {out[-500:]}"
        assert "RECENT-" in out, f"recent.md was dropped before archive.md.\noutput tail: {out[-500:]}"
        assert "ARCHIVE-" not in out, f"archive.md should be the first to go.\noutput tail: {out[-500:]}"
        assert str(remember / "archive.md") in out, "a dropped file must be listed by path"

    def test_dropped_files_are_listed_with_their_size(self, tmp_path):
        bodies = self._oversized_bodies(filler_bytes=2200)
        out, remember = _run(tmp_path, bodies)
        archive_path = remember / "archive.md"
        size = archive_path.stat().st_size
        assert f"({size} bytes)" in out, (
            f"dropped file listed without its size.\noutput tail: {out[-500:]}"
        )
        assert "session_start_max_bytes" in out, (
            "the listing notice should name the config key responsible"
        )

    def test_a_severely_oversized_store_eventually_drops_every_droppable_file(self, tmp_path):
        """Each file ALONE already exceeds the default budget, so even the
        highest-priority droppable file (now.md) must still give way once
        archive/today/recent are gone and it is still, by itself, too big."""
        bodies = self._oversized_bodies(filler_bytes=9500)
        out, remember = _run(tmp_path, bodies)
        for fname in (TODAY_FILE, "now.md", "recent.md", "archive.md"):
            path = remember / fname
            assert str(path) in out, f"{fname} should be listed when dropped"


class TestTotalStaysUnderBudgetAfterDropping:

    def test_total_emitted_output_is_under_the_configured_budget(self, tmp_path):
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: "TODAY-" + ("Y" * 5000) + "\n",
            "now.md": "NOW-" + ("Y" * 5000) + "\n",
            "recent.md": "RECENT-" + ("Y" * 5000) + "\n",
            "archive.md": "ARCHIVE-" + ("Y" * 5000) + "\n",
        }
        out, _ = _run(tmp_path, bodies, config={"thresholds": {"session_start_max_bytes": 3000}})
        assert len(out.encode("utf-8")) < 3000 + 500, (
            f"total output ({len(out.encode('utf-8'))} bytes) is not meaningfully "
            f"bounded by the configured budget (3000)"
        )

    def test_worst_case_each_file_just_under_its_own_percap_but_total_huge(self, tmp_path):
        """Per-file memory_inject_max_bytes alone cannot catch this: every
        file individually passes it, and the TOTAL is still what #842 reports
        (six healthy-looking files summing past Claude Code's own cap)."""
        per_file_cap = 2500
        filler = "Z" * (per_file_cap - 20)
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: filler + "\n",
            "now.md": filler + "\n",
            "recent.md": filler + "\n",
            "archive.md": filler + "\n",
        }
        config = {"thresholds": {"memory_inject_max_bytes": per_file_cap}}
        out, _ = _run(tmp_path, bodies, config=config)
        assert len(out.encode("utf-8")) < 9000 + 1500, (
            f"total output ({len(out.encode('utf-8'))} bytes) exceeded the default "
            f"session_start_max_bytes (9000) even though every file individually "
            f"passed its own per-file cap"
        )


class TestMultibyteHeavyStoreRespectsByteBudget:

    def test_multibyte_content_still_fits_under_the_byte_budget(self, tmp_path):
        """The budget counts BYTES, not characters -- a store whose content
        is heavily multibyte (each character several bytes in UTF-8) must
        still have its TOTAL emitted output bounded by the same byte
        count, never let through because its CHARACTER count looked small."""
        multibyte_filler = "中文" * 2000  # ~6 bytes/char in UTF-8, ~24000 bytes
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: multibyte_filler + "\n",
            "now.md": "NOW-BODY-842\n",
            "recent.md": "RECENT-BODY-842\n",
            "archive.md": multibyte_filler + "\n",
        }
        out, _remember = _run(tmp_path, bodies)
        assert len(out.encode("utf-8")) < 9000 + 1500, (
            f"multibyte content pushed total output ({len(out.encode('utf-8'))} bytes) "
            f"past the default byte budget (9000)"
        )
        assert "NOW-BODY-842" in out, "higher-priority now.md should survive the drop"


class TestZeroDisablesTheBudget:

    def test_zero_never_drops_anything_regardless_of_total_size(self, tmp_path):
        filler = "W" * 6000
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: "TODAY-" + filler + "\n",
            "now.md": "NOW-" + filler + "\n",
            "recent.md": "RECENT-" + filler + "\n",
            "archive.md": "ARCHIVE-" + filler + "\n",
        }
        out, _ = _run(tmp_path, bodies, config={"thresholds": {"session_start_max_bytes": 0}})
        assert "ARCHIVE-" in out, "session_start_max_bytes=0 must disable the budget entirely"
        assert "over thresholds.session_start_max_bytes" not in out


class TestMalformedValueFallsBackAndIsLogged:

    def test_malformed_value_is_logged_and_default_budget_still_applies(self, tmp_path):
        filler = "V" * 6000
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: "TODAY-" + filler + "\n",
            "now.md": "NOW-" + filler + "\n",
            "recent.md": "RECENT-" + filler + "\n",
            "archive.md": "ARCHIVE-" + filler + "\n",
        }
        config = {"thresholds": {"session_start_max_bytes": "9OOO"}}
        out, remember = _run(tmp_path, bodies, config=config)
        assert "ARCHIVE-" not in out, (
            "a malformed override should fall back to the default (9000), which "
            "still engages the budget on a store this large"
        )
        log_dir = remember / "logs"
        logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in log_dir.glob("*.log")) if log_dir.is_dir() else ""
        assert "session_start_max_bytes" in logs, (
            f"a malformed override must be logged, naming the key: {logs!r}"
        )
        assert "9OOO" in logs, f"the log should name the bad value that was discarded: {logs!r}"

    def test_a_valid_override_is_not_logged_as_malformed(self, tmp_path):
        """Positive control."""
        _out, remember = _run(tmp_path, SMALL_BODIES, config={"thresholds": {"session_start_max_bytes": 20000}})
        log_dir = remember / "logs"
        logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in log_dir.glob("*.log")) if log_dir.is_dir() else ""
        assert "session_start_max_bytes" not in logs, (
            f"a valid configured override was reported as malformed: {logs!r}"
        )


class TestCompactModeIsUnaffectedByTheBudget:
    """Positive control: source=compact already minimizes its own output
    (identity only, everything else named) -- #842's budget must not change
    that existing, separately-tested shape."""

    def test_compact_recap_is_unchanged_by_the_new_budget_code(self, tmp_path):
        filler = "U" * 6000
        bodies = {
            "identity.md": "IDENTITY-842\n",
            "core-memories.md": "CORE-842\n",
            TODAY_FILE: "TODAY-" + filler + "\n",
            "now.md": "NOW-" + filler + "\n",
            "recent.md": "RECENT-" + filler + "\n",
            "archive.md": "ARCHIVE-" + filler + "\n",
        }
        out, remember = _run(tmp_path, bodies, source="compact")
        assert "=== MEMORY ===" in out
        assert "IDENTITY-842" in out
        for fname in ("now.md", "recent.md", "archive.md", TODAY_FILE):
            assert str(remember / fname) in out, f"{fname} should still be named at compact"
        assert "session_start_max_bytes" not in out, (
            "the budget notice should never fire at source=compact, which "
            "already names everything via its own, separate mechanism"
        )
