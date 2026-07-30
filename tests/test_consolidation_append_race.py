"""Consolidation must not seal away entries appended while it runs.

`cmd_consolidate` reads every `today-*.md`, then calls Haiku with a 180s
budget, and `run-consolidation.sh` afterwards renames each file it read to
`.done.md`. The rename is blind: it moves whatever the file contains NOW, not
the bytes that were actually consolidated.

A save landing in that window is therefore sealed inside the `.done.md`. The
next consolidation's glob explicitly skips `.done.md`, and session start never
injects those files either — so the entry was written to disk successfully and
is then unreachable forever.

This is #142's shape (parent reads → long call → blind write loses newer data),
one layer over: #142's fix kept the bytes appended past the snapshot offset in
`now.md`, but consolidation's staging rename never got the same treatment.
`run-consolidation.sh` is spawned `nohup ... & disown` from session start, so it
runs concurrently with any live `save-session.sh` by design — and NDC appends to
`today-<day>.md`, which can be a past day for a session that crossed midnight.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Stands in for the real pipeline. `consolidate` reads the staging files,
#: records how many bytes it consumed, and then appends to one of them —
#: standing in for a save that lands during the Haiku call.
STUB_SHELL = '''\
import os, sys, tempfile, glob

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "consolidate":
    staging_dir = sys.argv[2]
    paths = sorted(p for p in glob.glob(os.path.join(staging_dir, "today-*.md"))
                   if not p.endswith(".done.md"))
    consumed = {}
    for p in paths:
        with open(p, encoding="utf-8") as f:
            consumed[p] = len(f.read().encode("utf-8"))

    extra = os.environ.get("STUB_APPEND_DURING_CONSOLIDATION")
    if extra and paths:
        with open(paths[0], "a", encoding="utf-8") as f:
            f.write(extra)

    fd_r, recent_out = tempfile.mkstemp(suffix="-recent.md")
    with os.fdopen(fd_r, "w") as f:
        f.write("# Recent\\n\\nconsolidated\\n")
    fd_a, archive_out = tempfile.mkstemp(suffix="-archive.md")
    with os.fdopen(fd_a, "w") as f:
        f.write("# Archive\\n\\nolder\\n")

    fd_s, staging_paths_file = tempfile.mkstemp(suffix="-paths.bin")
    with os.fdopen(fd_s, "wb") as f:
        for p in paths:
            f.write(p.encode("utf-8") + b"\\x00")
            f.write(str(consumed[p]).encode("ascii") + b"\\x00")

    print(f"STAGING_COUNT={len(paths)}")
    print("CONSOLIDATION_STATUS=ok")
    print(f"RECENT_OUT={recent_out}")
    print(f"ARCHIVE_OUT={archive_out}")
    print(f"STAGING_PATHS_FILE={staging_paths_file}")
    print("TK_IN=0"); print("TK_OUT=0"); print("TK_CACHE=0"); print("TK_COST=0")
'''


def _make_env(tmp_path: Path):
    """A project whose store holds one past-day staging file."""
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)

    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "pipeline").mkdir(parents=True)
    (plugin / "pipeline" / "__init__.py").write_text("")
    (plugin / "pipeline" / "haiku.py").write_text("# marker\n")
    (plugin / "pipeline" / "shell.py").write_text(STUB_SHELL)
    for script in ("run-consolidation.sh", "resolve-paths.sh", "detect-tools.sh",
                   "bootstrap-dirs.sh", "log.sh", "lib-memory-dir.sh",
                   "lib-lock.sh", "lib-slug.sh", "lib-clock.sh"):
        (plugin / "scripts" / script).write_text((REPO_ROOT / "scripts" / script).read_text())
    (plugin / "config.json").write_text('{"cooldowns": {}, "thresholds": {}}')

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return env, project, plugin, remember


def _run(plugin: Path, env: dict):
    return subprocess.run(["bash", str(plugin / "scripts" / "run-consolidation.sh")],
                          capture_output=True, text=True, env=env, timeout=90)


APPENDED = "\n## 23:59 | main\n\n- landed during consolidation\n"


def test_an_entry_appended_during_consolidation_is_not_sealed_away(tmp_path):
    """The reported loss: written to disk, then unreachable forever."""
    env, project, plugin, remember = _make_env(tmp_path)
    staging = remember / "today-2026-07-24.md"
    staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
    env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED

    result = _run(plugin, env)
    assert result.returncode == 0, result.stderr

    live = "".join(p.read_text(encoding="utf-8")
                   for p in remember.glob("today-*.md")
                   if not p.name.endswith(".done.md"))
    assert "landed during consolidation" in live, (
        "the entry was sealed into .done.md — nothing globs those again and "
        "session start never injects them, so it is unreachable memory"
    )


def test_the_consolidated_span_is_still_retired(tmp_path):
    """Keeping the tail must not stop the consumed part being retired.

    Otherwise the next run re-consolidates work already in recent.md.
    """
    env, project, plugin, remember = _make_env(tmp_path)
    staging = remember / "today-2026-07-24.md"
    staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")
    env["STUB_APPEND_DURING_CONSOLIDATION"] = APPENDED

    _run(plugin, env)

    done = list(remember.glob("today-*.done.md"))
    assert done, "nothing was retired"
    assert "earlier work" in done[0].read_text(encoding="utf-8")
    live = "".join(p.read_text(encoding="utf-8")
                   for p in remember.glob("today-*.md")
                   if not p.name.endswith(".done.md"))
    assert "earlier work" not in live, "the consolidated span was left to be redone"


def test_a_quiet_consolidation_still_retires_the_whole_file(tmp_path):
    """Nothing appended: the ordinary path must be untouched."""
    env, project, plugin, remember = _make_env(tmp_path)
    staging = remember / "today-2026-07-24.md"
    staging.write_text("# Day\n\n## 10:00 | main\n\n- earlier work\n", encoding="utf-8")

    _run(plugin, env)

    assert not staging.exists(), "the file should have been renamed away entirely"
    done = remember / "today-2026-07-24.done.md"
    assert done.is_file() and "earlier work" in done.read_text(encoding="utf-8")


# ── End-to-end through the REAL pipeline.shell (no STUB_SHELL) ─────────────
#
# The suite above stands in for `pipeline.shell consolidate` entirely, so it
# cannot prove anything about how the consumed-byte count is actually
# computed — that logic lives in the real cmd_consolidate (review of
# 8d2cdab). This runs the genuine scripts/run-consolidation.sh against the
# genuine pipeline package (CLAUDE_PLUGIN_ROOT = the repo itself), with only
# the `claude` binary swapped for a script that returns a fixed response —
# the one boundary every other test in this file also stubs.

FAKE_CLAUDE = '''\
#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()  # the prompt arrives on stdin; content doesn't matter here

# Stand-in for a save landing WHILE this (fake, but still out-of-process)
# Haiku call is in flight — the real #142-shaped race: the file is read
# before this call and renamed after it returns.
append_file = os.environ.get("APPEND_DURING_CONSOLIDATION_FILE")
append_text = os.environ.get("APPEND_DURING_CONSOLIDATION_TEXT")
if append_file and append_text:
    with open(append_file, "a", encoding="utf-8") as f:
        f.write(append_text)

print(json.dumps({
    "result": "===RECENT===\\n# Recent\\n\\n## 2020-01-02\\n- compressed\\n\\n"
              "===ARCHIVE===\\n# Archive\\n\\nolder\\n",
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_read_input_tokens": 0,
}))
'''


def _real_pipeline_env(tmp_path: Path):
    """Real pipeline.shell + real run-consolidation.sh; only `claude` is faked."""
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (remember / "logs").mkdir(parents=True)

    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text(FAKE_CLAUDE)
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_CLAUDE_BIN": str(fake_claude),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return env, project, remember


def _run_real(env: dict):
    return subprocess.run(["bash", str(REPO_ROOT / "scripts" / "run-consolidation.sh")],
                          capture_output=True, text=True, env=env, timeout=90)


class TestRealConsolidateNonUtf8AppendRace:
    """Defect 1 (8d2cdab): an overstated consumed-byte count makes
    run-consolidation.sh take the blind-rename branch, sealing anything
    appended during consolidation inside .done.md.

    A non-UTF-8 staging file used to trigger exactly that: the byte count
    was measured from the decoded-and-re-encoded string (errors="replace"
    turns each stray byte into a 3-byte U+FFFD), so it overstated the file,
    `staging_now -gt staging_consumed` read false, and the append below
    would have been sealed away rather than kept.
    """

    def test_entry_appended_during_real_consolidation_survives_non_utf8_staging(
        self, tmp_path
    ):
        env, project, remember = _real_pipeline_env(tmp_path)
        staging = remember / "today-2020-01-01.md"
        # 30 stray non-UTF-8 bytes. Each becomes one U+FFFD on decode, which
        # re-encodes to 3 bytes — a 60-byte overstatement, comfortably bigger
        # than APPENDED (48 bytes) below. That margin is deliberate: it's what
        # makes the bug's effect (an overstated count) outweigh the race's
        # effect (more bytes on disk than were consumed) and actually flip the
        # shell's `staging_now -gt staging_consumed` comparison to false, so
        # this test would still fail with only a couple of stray bytes.
        original = b"# Day\n\n## 10:00 | main\n\n- entry with " + b"\xff" * 30 + b" bytes\n"
        staging.write_bytes(original)

        # The fake `claude` process appends this to the staging file the
        # moment it's invoked — after cmd_consolidate has read + snapshotted
        # the file, before run-consolidation.sh renames it. Same window #142
        # closed for now.md; this proves it for staging.
        env["APPEND_DURING_CONSOLIDATION_FILE"] = str(staging)
        env["APPEND_DURING_CONSOLIDATION_TEXT"] = APPENDED

        result = _run_real(env)
        assert result.returncode == 0, result.stderr

        remaining = staging.read_text(encoding="utf-8", errors="replace") if staging.exists() else ""
        assert "landed during consolidation" in remaining, (
            "the entry appended while the (real) consumed-byte count was "
            "measured wrong got sealed into .done.md instead of surviving in "
            "the live staging file — an overstated count (from decoding a "
            "non-UTF-8 staging file with errors='replace' before measuring) "
            f"makes staging_now > staging_consumed read false. stderr={result.stderr}"
        )
        done = remember / "today-2020-01-01.done.md"
        assert done.is_file(), f"the consumed span was not retired: {result.stderr}"
        assert "entry with" in done.read_text(encoding="utf-8", errors="replace"), (
            "the original (consumed) span must still be retired into .done.md"
        )
